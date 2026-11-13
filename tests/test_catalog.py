"""Synthetic test fixtures: no copyrighted ROM data required."""
import hashlib
from pathlib import Path
import tempfile
import unittest
import xml.etree.ElementTree as ET
import zipfile
import zlib

import catalog
import graphics

class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.work=tempfile.TemporaryDirectory()
        self.addCleanup(self.work.cleanup)
        folder=Path(self.work.name)
        self.db=catalog.connect(folder/"catalog.sqlite3")
        self.addCleanup(self.db.close)
        self.data=b"\x80"+b"\x00"*31
        self.crc=f"{zlib.crc32(self.data)&0xffffffff:08x}"
        self.sha=hashlib.sha1(self.data).hexdigest()
        self.xml=folder/"mame.xml"
        self.xml.write_text(
            '<mame><machine name="testgame" sourcefile="source.cpp">'
            '<description>Test Game</description><manufacturer>Example</manufacturer>'
            '<rom name="tile.rom" region="gfx" offset="0000" '
            f'size="32" crc="{self.crc}" sha1="{self.sha}"/>'
            '</machine></mame>',encoding="utf8")
        self.assertEqual(catalog.import_xml(self.db,self.xml),1)
        self.rom=folder/"testgame.zip"
        with zipfile.ZipFile(self.rom,"w") as z:
            z.writestr("tile.rom",self.data)

    def test_import_search(self):
        rows=catalog.games(self.db,"test")
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]["name"],"testgame")
        self.assertEqual(rows[0]["sourcefile"],"source.cpp")

    def test_verified_rom_and_regions(self):
        check=catalog.audit(self.db,"testgame",[self.rom])
        self.assertEqual(check["status"],"verified")
        self.assertEqual(catalog.regions(self.db,"testgame",[self.rom],["gfx"],check)["gfx"],
                         self.data)

    def test_missing_or_corrupt_rom_rejected(self):
        wrong=Path(self.work.name)/"wrong.zip"
        with zipfile.ZipFile(wrong,"w") as z:
            z.writestr("tile.rom",b"\x00"*32)
        self.assertEqual(catalog.audit(self.db,"testgame",[wrong])["status"],"incomplete")

    def test_renamed_matching_hash_is_valid(self):
        renamed=Path(self.work.name)/"renamed.zip"
        with zipfile.ZipFile(renamed,"w") as z:
            z.writestr("renamed.bin",self.data)
        self.assertEqual(catalog.audit(self.db,"testgame",[renamed])["status"],"verified")

    def test_missing_parent_can_be_provided(self):
        blank=Path(self.work.name)/"clone.zip"
        with zipfile.ZipFile(blank,"w") as z:
            z.writestr("unrelated.rom",b"\x05")
        self.assertEqual(catalog.audit(self.db,"testgame",[blank])["status"],"incomplete")
        self.assertEqual(catalog.audit(self.db,"testgame",[blank,self.rom])["status"],"verified")

    def test_not_known_without_metadata(self):
        self.assertEqual(catalog.audit(self.db,"unknown",[self.rom])["status"],"no_metadata")

    def test_layout_reading_is_msb_first(self):
        spec=graphics.layouts("1942")[0]
        data=b"\x80"+b"\x00"*15
        pix=graphics.decode(data,spec,0)
        self.assertEqual(len(pix),64)
        self.assertEqual(pix[0],1)
        self.assertEqual(sum(pix),1)
        self.assertEqual(graphics.layouts("other"),[])

if __name__=="__main__":
    unittest.main()
