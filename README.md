# Pokemon Mystery Dungeon: Red Rescue Team — US/EU save converter

`convert_save.py` converts a save between the US (`B24E`) and European
(`B24P`) releases of Pokemon Mystery Dungeon: Red Rescue Team.

It accepts:

- raw GBA save files (0x20000 bytes, or a truncated raw save),
- SharkPort/XPort containers (the header is detected and stripped).

It writes a **raw** 0x20000-byte `.sav` with both the primary save at `0x0000`
and the backup save at `0x6000`. The input file is never modified.

## Usage

```bash
# US -> EU (auto-detects the source region)
python3 convert_save.py USA.sav --to eu

# EU -> US, explicit output path
python3 convert_save.py EU.sav --to us -o EU_to_USA.sav

# Only validate and describe a save
python3 convert_save.py USA.sav --check

# US -> EU with a specific value for the EU-only 0x424 field
python3 convert_save.py USA.sav --to eu --eu-extra 0
```

If `--to` is omitted the script converts to the opposite region.

## What is different between the US and EU saves?

Both releases use the same 0x6000-byte save-block spacing (primary at `0x0000`,
backup at `0x6000`) and the same checksum algorithm: sum the little-endian
32-bit words from offset `0x004` through `size - 4`, and store the sum at
offset `0x000`.

The save struct is `0x57D4` bytes on US and `0x57D8` on EU. The EU version
inserts one extra 32-bit field at `0x424`; everything from `0x428` onward is
shifted by +4 bytes. The EU checksum therefore includes the extra word at
`0x57D4` (which is `0xFFFFFFFF` padding in practice), while the US checksum
stops at `0x57D0`.

There are also three region-specific header fields:

| Field | US | EU |
|---|---|---|
| Game internal name at `0x404` | `POKE_DUNGEON__05` | `POKE_DUNGEON_E01` |
| Validation magic at `0x414` | `0x05071412` | `0x06061320` |
| Script variable `VERSION` at `0x004` | `29` (`0x1D`) | `30` (`0x1E`) |

The `VERSION` field is the first entry in the game's script-variable table.
The game refuses to load a save whose `VERSION` does not match the build, so
this field **must** be patched during conversion. This was confirmed by
running both ROMs in mGBA: after patching `VERSION`, both games accept the
converted save through `ReadSaveFromPak` with status 0.

## Quick-save / suspend data

Red Rescue Team can also store a quick-save (suspend save) in the flash save
file: data at sector 16 (`0x10000`) plus a 0x800-byte header at sector 31
(`0x1F000`). That data is a raw in-game snapshot, not the normal save struct,
and is not portable between regions.

By default the converter clears everything after `0xC000` to `0xFF`, which
invalidates the quick-save and forces the game to use the normal primary save.
Use `--keep-quicksave` only for experiments; the preserved data will still be
from the source region.

## Notes

- The output is a raw `.sav`, not a SharkPort container. Emulators and most
  flashcarts expect the raw format. If you need a SharkPort file, use your
  SharkPort software to repackage the raw output.
- The converter preserves the normal save data bit-for-bit apart from the
  documented header changes, the 4-byte EU field, the checksum, and the
  script `VERSION`.
- Back up your original saves before using converted saves on real hardware.
