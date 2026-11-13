"""Local MAME -listxml catalog and content-based ROM ZIP verification.

This file never uploads or extracts a ROM. CRC32 is for MAME compatibility;
SHA-1 and size are checked as well when supplied by the MAME catalog.
"""
from __future__ import annotations
import hashlib
from pathlib import Path
import sqlite3
import xml.etree.ElementTree as ET
import zipfile
import zlib

DEFAULT_DB = Path.home() / ".mame_2d_express" / "catalog.sqlite3"
MAX_MEMBER_BYTES = 512 * 1024 * 1024

def connect(path=DEFAULT_DB):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.executescript("""
    PRAGMA foreign_keys=ON;
    CREATE TABLE IF NOT EXISTS games (
        name TEXT PRIMARY KEY, description TEXT, year TEXT,
        manufacturer TEXT, sourcefile TEXT, cloneof TEXT, romof TEXT
    );
    CREATE TABLE IF NOT EXISTS roms (
        game TEXT NOT NULL, name TEXT NOT NULL, region TEXT, offset TEXT,
        size INTEGER NOT NULL, crc TEXT, sha1 TEXT, merge TEXT,
        status TEXT, optional TEXT, loadflag TEXT,
        FOREIGN KEY(game) REFERENCES games(name)
    );
    CREATE INDEX IF NOT EXISTS roms_game ON roms(game);
    CREATE TABLE IF NOT EXISTS support (
        game TEXT PRIMARY KEY, decoder TEXT, review TEXT NOT NULL DEFAULT 'untested',
        notes TEXT NOT NULL DEFAULT '', FOREIGN KEY(game) REFERENCES games(name)
    );
    CREATE TABLE IF NOT EXISTS reports (
        id INTEGER PRIMARY KEY, game TEXT, date TEXT DEFAULT CURRENT_TIMESTAMP,
        stage TEXT, result TEXT, notes TEXT
    );
    """)
    db.commit()
    return db

def import_xml(db, xml_path):
    """Stream machine records from a real MAME -listxml export, retaining reviews."""
    count = 0
    with db:
        for event, elem in ET.iterparse(str(xml_path), events=("end",)):
            if elem.tag != "machine":
                continue
            name = elem.get("name", "")
            if not name or elem.get("isdevice") == "yes":
                elem.clear()
                continue
            def text(tag):
                value = elem.findtext(tag)
                return value.strip() if value else ""
            db.execute("""INSERT INTO games
                (name, description, year, manufacturer, sourcefile, cloneof, romof)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET description=excluded.description,
                year=excluded.year, manufacturer=excluded.manufacturer,
                sourcefile=excluded.sourcefile, cloneof=excluded.cloneof,
                romof=excluded.romof""",
                (name, text("description"), text("year"), text("manufacturer"),
                 elem.get("sourcefile",""), elem.get("cloneof",""), elem.get("romof","")))
            db.execute("DELETE FROM roms WHERE game=?", (name,))
            rows = []
            for rom in elem.findall("rom"):
                try:
                    size = int(rom.get("size", "0"))
                except ValueError:
                    size = 0
                rows.append((name, rom.get("name",""), rom.get("region",""),
                    rom.get("offset",""), size, rom.get("crc","").lower(),
                    rom.get("sha1","").lower(), rom.get("merge",""),
                    rom.get("status",""), rom.get("optional",""),
                    rom.get("loadflag","")))
            db.executemany("""INSERT INTO roms
                (game,name,region,offset,size,crc,sha1,merge,status,optional,loadflag)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)""", rows)
            db.execute("INSERT OR IGNORE INTO support (game) VALUES (?)", (name,))
            count += 1
            elem.clear()
    return count

def games(db, search="", limit=250):
    term = "%" + search.strip() + "%"
    return db.execute("""SELECT g.*, s.decoder, s.review FROM games g
        LEFT JOIN support s ON s.game=g.name
        WHERE g.name LIKE ? OR g.description LIKE ?
        ORDER BY CASE WHEN g.name IN ('1942','commando') THEN 0 ELSE 1 END,
                 g.description LIMIT ?""", (term,term,limit)).fetchall()

def roms_for(db, game):
    return db.execute("SELECT * FROM roms WHERE game=? ORDER BY region,offset,name",
                      (game,)).fetchall()

def _digest_zip(paths):
    """Inspect archive members in memory; never extract untrusted ZIP paths."""
    members = []
    for path in paths:
        path = Path(path)
        with zipfile.ZipFile(path) as z:
            for info in z.infolist():
                if info.is_dir() or info.file_size > MAX_MEMBER_BYTES:
                    continue
                sha = hashlib.sha1()
                crc = 0
                size = 0
                with z.open(info) as src:
                    while True:
                        block = src.read(1024 * 1024)
                        if not block:
                            break
                        size += len(block)
                        if size > MAX_MEMBER_BYTES:
                            raise ValueError("ROM component exceeds 512 MiB safety cap")
                        crc = zlib.crc32(block, crc)
                        sha.update(block)
                members.append({
                    "archive": str(path), "member": info.filename,
                    "name": Path(info.filename).name.lower(),
                    "size": size, "crc": f"{crc & 0xffffffff:08x}",
                    "sha1": sha.hexdigest()
                })
    return members

def _match(rom, members):
    expected_crc = (rom["crc"] or "").lower()
    expected_sha = (rom["sha1"] or "").lower()
    if not (expected_crc or expected_sha):
        return None
    def valid(m):
        return (m["size"] == rom["size"]
                and (not expected_crc or expected_crc == m["crc"])
                and (not expected_sha or expected_sha == m["sha1"]))
    candidates = [m for m in members if valid(m)]
    expected_names = {str(rom["name"]).lower(), str(rom["merge"] or "").lower()}
    candidates.sort(key=lambda m: (m["name"] not in expected_names, m["archive"]))
    return candidates[0] if candidates else None

def audit(db, game, paths):
    entries = roms_for(db, game)
    if not entries:
        return {"status":"no_metadata", "rows":[], "matched":{},
                "message":"No checksum catalog for this game. Import MAME XML first."}
    members = _digest_zip(paths)
    result = []
    matches = {}
    required = 0
    missing = 0
    unverifiable = 0
    for i, rom in enumerate(entries):
        ignored = rom["optional"] == "yes"
        if rom["status"] == "nodump" or not (rom["sha1"] or rom["crc"]):
            state = "unverifiable"
            if not ignored:
                unverifiable += 1
        else:
            member = _match(rom,members)
            if member:
                state = "known_bad_dump" if rom["status"] == "baddump" else "match"
                matches[i] = member
                if state == "known_bad_dump" and not ignored:
                    unverifiable += 1
            else:
                state = "missing_or_mismatch"
                if not ignored:
                    missing += 1
            if not ignored:
                required += 1
        result.append({"name":rom["name"], "region":rom["region"], "state":state,
                      "optional":ignored, "expected_sha1":rom["sha1"],
                      "expected_crc":rom["crc"],
                      "found":matches.get(i,{}).get("member","")})
    if missing:
        status = "incomplete"
    elif unverifiable or not required:
        status = "partial"
    else:
        status = "verified"
    return {"status":status,"rows":result,"matched":matches,
            "message":f"{len(matches)} hash-matched; {missing} required missing/mismatched; "
                      f"{unverifiable} required unverifiable or flagged bad dumps."}

def regions(db, game, paths, wanted, inspection=None):
    """Assemble ONLY contiguous ROM_LOAD regions, never guess interleaving."""
    entries = roms_for(db,game)
    inspection = inspection or audit(db,game,paths)
    blobs = {}
    for region in wanted:
        selected = [(i,r) for i,r in enumerate(entries) if r["region"] == region]
        if not selected:
            raise ValueError(f"No MAME ROM metadata for graphics region {region}")
        if any(i not in inspection["matched"] for i,_ in selected):
            raise ValueError(f"Graphics region {region} has missing/unverified ROM data")
        if any(r["loadflag"] not in ("", None) for _,r in selected):
            raise ValueError(f"Region {region} uses noncontiguous ROM loading; requires MAME bridge")
        length = max(int(r["offset"] or "0",16) + r["size"] for _,r in selected)
        if length > MAX_MEMBER_BYTES:
            raise ValueError("Region exceeds safety cap")
        data = bytearray(length)
        occupied = bytearray(length)
        for i,r in selected:
            m = inspection["matched"][i]
            with zipfile.ZipFile(m["archive"]) as z:
                with z.open(m["member"]) as src:
                    chunk = src.read(MAX_MEMBER_BYTES+1)
            offset = int(r["offset"] or "0",16)
            if len(chunk) != r["size"]:
                raise ValueError("ROM size changed since hash check")
            if any(occupied[offset:offset+len(chunk)]):
                raise ValueError("Overlapping ROM loads require a special loader")
            data[offset:offset+len(chunk)] = chunk
            occupied[offset:offset+len(chunk)] = b"\x01" * len(chunk)
        blobs[region] = bytes(data)
    return blobs
