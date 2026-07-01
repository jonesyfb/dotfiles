#!/usr/bin/env python3
"""
Patch NotoColorEmoji.ttf — replace eagle (U+1F985) with the Huginn raven.

Strategy:
  - Manipulate raw CBDT bytes to swap the PNG
  - Update CBLC offset arrays so subsequent glyphs still point correctly
  - Install patched font to ~/.local/share/fonts/ with fontconfig priority
"""
import struct
import subprocess
import sys
from pathlib import Path

from fontTools.ttLib import TTFont

FONT_SRC  = Path("/usr/share/fonts/noto/NotoColorEmoji.ttf")
FONT_DST  = Path.home() / ".local/share/fonts/HuginnEmoji.ttf"
RAVEN_SVG = Path.home() / "dotfiles/quickshell/assets/Huginn_High_Res_v2.svg"
CODEPOINT = 0x1F985


def rasterize_svg(svg_path: Path, size: int) -> bytes:
    return subprocess.run(
        ["rsvg-convert", "-w", str(size), "-h", str(size), "-f", "png", str(svg_path)],
        capture_output=True, check=True,
    ).stdout


def main() -> None:
    if not RAVEN_SVG.exists():
        sys.exit(f"Raven SVG not found: {RAVEN_SVG}")

    print("Loading font …")
    font = TTFont(FONT_SRC)

    glyph_name = font.getBestCmap().get(CODEPOINT)
    if not glyph_name:
        sys.exit(f"U+{CODEPOINT:04X} not found in cmap.")
    print(f"Eagle glyph: {glyph_name}")

    # Read raw CBDT bytes BEFORE fonttools decompiles anything (reader holds orig data)
    cbdt_raw  = bytearray(font.reader["CBDT"])
    cblc      = font["CBLC"]
    strike    = cblc.strikes[0]
    ppem      = strike.bitmapSizeTable.ppemX

    # Find which subtable holds the eagle and its location entry
    target_sub = None
    target_loc_idx = None
    for sub in strike.indexSubTables:
        if glyph_name in sub.names:
            target_sub = sub
            target_loc_idx = sub.names.index(glyph_name)
            break
    if target_sub is None:
        sys.exit("Eagle glyph not found in any CBLC subtable.")

    loc = target_sub.locations[target_loc_idx]
    # fonttools stores locations as ABSOLUTE CBDT offsets (imageDataOffset already added)
    abs_start = loc[0]
    old_block_len = loc[1] - loc[0]

    # Format 17 layout: [0:5] SmallGlyphMetrics | [5:9] dataLen uint32 | [9:] PNG
    metrics_bytes = bytes(cbdt_raw[abs_start : abs_start + 5])
    old_data_len  = struct.unpack_from(">I", cbdt_raw, abs_start + 5)[0]
    assert old_data_len == old_block_len - 9, \
        f"dataLen mismatch: {old_data_len} vs {old_block_len - 9}"

    print(f"Strike ppem={ppem}, offset={abs_start:#x}, old PNG={old_data_len}B")

    print(f"Rasterizing SVG at {ppem}px …")
    png_bytes    = rasterize_svg(RAVEN_SVG, ppem)
    new_data_len = len(png_bytes)
    new_block    = metrics_bytes + struct.pack(">I", new_data_len) + png_bytes
    delta        = len(new_block) - old_block_len
    print(f"New PNG={new_data_len}B, delta={delta:+d}B")

    # ── Patch CBDT bytes ──────────────────────────────────────────────────────
    cbdt_patched = (
        cbdt_raw[:abs_start]
        + new_block
        + cbdt_raw[abs_start + old_block_len:]
    )

    # ── Patch CBLC locations (absolute offsets — shift everything after eagle) ─
    for i in range(target_loc_idx, len(target_sub.locations)):
        s, e = target_sub.locations[i]
        if i == target_loc_idx:
            target_sub.locations[i] = (s, e + delta)
        else:
            target_sub.locations[i] = (s + delta, e + delta)

    found = False
    for sub in strike.indexSubTables:
        if sub is target_sub:
            found = True
            continue
        if found:
            # All subsequent subtable locations are absolute — shift imageDataOffset
            # so the relative offsets stored in the font remain correct
            sub.imageDataOffset += delta

    # ── Inject raw CBDT, let fonttools recompile CBLC normally ───────────────
    from fontTools.ttLib.tables.otBase import DefaultTable
    raw_cbdt          = DefaultTable("CBDT")
    raw_cbdt.data     = bytes(cbdt_patched)
    font.tables["CBDT"] = raw_cbdt

    FONT_DST.parent.mkdir(parents=True, exist_ok=True)
    print(f"Saving → {FONT_DST}")
    font.save(str(FONT_DST))

    # ── Fontconfig rule ───────────────────────────────────────────────────────
    conf_dir  = Path.home() / ".config/fontconfig/conf.d"
    conf_file = conf_dir / "99-huginn-emoji.conf"
    conf_dir.mkdir(parents=True, exist_ok=True)
    conf_file.write_text("""\
<?xml version="1.0"?>
<!DOCTYPE fontconfig SYSTEM "fonts.dtd">
<fontconfig>
  <alias>
    <family>emoji</family>
    <prefer><family>HuginnEmoji</family></prefer>
  </alias>
  <alias>
    <family>Noto Color Emoji</family>
    <prefer><family>HuginnEmoji</family></prefer>
  </alias>
</fontconfig>
""")
    print(f"Fontconfig → {conf_file}")

    subprocess.run(["fc-cache", "-f", str(FONT_DST.parent)], check=True)
    print("\nDone — restart apps or log out/in to see 🦅 → raven.")


if __name__ == "__main__":
    main()
