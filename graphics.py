"""First deliberately narrow graphic decoders for early Capcom arcade boards.

Bit offsets come from MAME's 1942.cpp and commando.cpp gfx_layout tables.
No accurate arcade RGB palettes or assembled animations are claimed.
"""
from __future__ import annotations
from dataclasses import dataclass
import json
from pathlib import Path
from PIL import Image
import catalog

@dataclass(frozen=True)
class Layout:
    name: str
    region: str
    width: int
    height: int
    planes: tuple[int, ...]
    xoffs: tuple[int, ...]
    yoffs: tuple[int, ...]
    stride: int
    divisor: int
    transparency: int

def sequence(count, step=1):
    return tuple(i*step for i in range(count))

def layouts(game):
    # Offsets of planes across 2 or 3 equal-sized sub-regions are substituted
    # once the assembled ROM region's actual size is known.
    chars = Layout("characters", "gfx1", 8, 8, (4,0),
        (0,1,2,3,8,9,10,11),sequence(8,16),128,1,0)
    tiles = Layout("background_tiles", "gfx2", 16,16, (0,1,2),
        tuple(range(8))+tuple(128+x for x in range(8)),
        sequence(16,8),256,3,-1)
    sprites = Layout("sprite_tiles", "gfx3", 16,16, (0,1,2,3),
        (0,1,2,3,8,9,10,11,256,257,258,259,264,265,266,267),
        sequence(16,16),512,2,15)
    if game == "1942":
        return [
            chars,
            tiles,
            sprites
        ]
    if game == "commando":
        return [
            Layout("characters","chars",8,8,chars.planes,chars.xoffs,chars.yoffs,128,1,3),
            Layout("background_tiles","tiles",16,16,tiles.planes,tiles.xoffs,tiles.yoffs,256,3,-1),
            Layout("sprite_tiles","sprites",16,16,sprites.planes,sprites.xoffs,sprites.yoffs,512,2,15)
        ]
    return []

def plane_bits(spec, region_size):
    bits = region_size*8
    if spec.name == "characters":
        return spec.planes
    if spec.name == "background_tiles":
        return (0, bits//3, 2*bits//3)
    if spec.name == "sprite_tiles":
        half = bits//2
        return (half+4, half, 4, 0)
    raise ValueError("unknown layout")

def decode(data, spec, index):
    """Use the same MSB-first bit numbering and plane ordering as MAME drawgfx."""
    shifts = plane_bits(spec,len(data))
    start = spec.stride * index
    out = bytearray(spec.width*spec.height)
    for y in range(spec.height):
        for x in range(spec.width):
            pixel = 0
            for p, bitstart in enumerate(shifts):
                bit = start + bitstart + spec.yoffs[y] + spec.xoffs[x]
                if bit >= len(data)*8:
                    raise ValueError("Tile extends past graphics region")
                if data[bit//8] & (0x80 >> (bit & 7)):
                    pixel |= 1 << (len(shifts)-p-1)
            out[y*spec.width+x] = pixel
    return bytes(out)

def tile_count(spec, region_size):
    return ((region_size * 8) // spec.divisor) // spec.stride

def make_atlas(decoded, spec, output, columns=16):
    n = len(decoded)
    rows = max(1,(n+columns-1)//columns)
    unit_w,unit_h = spec.width+1,spec.height+1
    atlas = Image.new("RGBA",(columns*unit_w,rows*unit_h),(0,0,0,0))
    for n, pixels in enumerate(decoded):
        ncolors = 2**len(spec.planes)
        rgba = bytearray()
        for idx in pixels:
            gray = round(idx * 255 / (ncolors-1))
            alpha = 0 if idx == spec.transparency else 255
            rgba.extend((gray,gray,gray,alpha))
        image = Image.frombytes("RGBA",(spec.width,spec.height),bytes(rgba))
        atlas.paste(image,((n%columns)*unit_w,(n//columns)*unit_h),image)
    atlas.save(output)

def extract_known(db, game, paths, output):
    specs = layouts(game)
    if not specs:
        raise ValueError("No board-specific decoder for this game. Use experimental scan.")
    inspection = catalog.audit(db,game,paths)
    if inspection["status"] == "no_metadata":
        raise ValueError("Import MAME -listxml to verify the ROM first")
    blobs = catalog.regions(db,game,paths,{s.region for s in specs},inspection)
    out = Path(output)
    out.mkdir(parents=True,exist_ok=True)
    assets=[]
    for spec in specs:
        data = blobs[spec.region]
        total = tile_count(spec,len(data))
        pixels = [decode(data,spec,i) for i in range(total)]
        filename = spec.name+".png"
        make_atlas(pixels,spec,out/filename)
        assets.append({"name":spec.name,"region":spec.region,"tiles":total,
                       "width":spec.width,"height":spec.height,
                       "transparency_index":spec.transparency,"file":filename})
    manifest={"game":game,"verification":inspection["status"],
              "mode":"known_bit_layout","palette":"placeholder_grayscale",
              "complete_sprite_frames":False,"submission_ready":False,
              "note":"Raw graphical tiles, not complete animations or accurate RGB palettes.",
              "assets":assets}
    (out/"manifest.json").write_text(json.dumps(manifest,indent=2),encoding="utf8")
    return manifest

def experimental(game,paths,output,limit=256):
    """Guessed contiguous packed 4bpp; works for curiosity, not correctness."""
    import zipfile
    out = Path(output)
    out.mkdir(parents=True,exist_ok=True)
    files=[]
    for path in paths:
        with zipfile.ZipFile(path) as z:
            for member in z.infolist():
                if member.is_dir() or member.file_size < 32 or member.file_size > 8*1024*1024:
                    continue
                raw = z.read(member)
                count = min(limit,len(raw)//32)
                if not count:
                    continue
                spec = Layout("experimental","",8,8,(0,1,2,3),tuple(range(8)),
                              tuple(range(8)),0,1,0)
                decoded=[]
                for i in range(count):
                    segment=raw[i*32:(i+1)*32]
                    pix=bytearray()
                    for b in segment:
                        pix.extend((b>>4,b&15))
                    decoded.append(bytes(pix))
                filename=f"candidate_{len(files):03d}.png"
                make_atlas(decoded,spec,out/filename)
                files.append({"archive":Path(path).name,"member":member.filename,
                              "tiles":count,"file":filename})
                if len(files)>=24:
                    break
        if len(files)>=24:
            break
    manifest={"game":game,"mode":"experimental_guessed_packed4",
              "verification":"separate_audit_required",
              "palette":"placeholder_grayscale",
              "submission_ready":False,
              "warning":"Only speculative 8x8 packed4 candidate tile previews, not validated decodes.",
              "files":files}
    (out/"manifest.json").write_text(json.dumps(manifest,indent=2),encoding="utf8")
    return manifest
