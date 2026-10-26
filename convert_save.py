#!/usr/bin/env python3
"""
Convert Pokemon Mystery Dungeon: Red Rescue Team save files between the
US and European (PAL) releases.

Supported input:
  * Raw GBA save files (0x20000 bytes, or truncated)
  * SharkPort / XPort style containers (header is detected and stripped)

Output is always a raw 0x20000-byte GBA save.  The primary save at 0x0000 and
the backup save at 0x6000 are both written.  Any quick-save / suspend-save
data after 0xC000 is erased (filled with 0xFF) because it is a raw memory
snapshot and is not portable between regions.

Usage:
    python3 convert_save.py USA.sav --to eu
    python3 convert_save.py EU.sav --to us -o converted_us.sav
    python3 convert_save.py save.sav --to eu --check

The save format was derived from the pret/pmd-red decompilation and the
falker47/PokemonMysteryDungeon-SavEditor project.
"""

from __future__ import annotations

import argparse
import struct
import sys
from dataclasses import dataclass
from pathlib import Path

# ---------------------------------------------------------------------------
# Format constants
# ---------------------------------------------------------------------------

RAW_SAVE_SIZE = 0x20000
BLOCK_SIZE = 0x6000          # primary save block, and backup block spacing
PRIMARY_OFFSET = 0x0000
BACKUP_OFFSET = 0x6000
QUICKSAVE_OFFSET = 0xC000    # everything from here on is not part of the main save

GAME_NAME_OFFSET = 0x404
GAME_NAME_LEN = 0x10
MAGIC_OFFSET = 0x414
DATA_OFFSET_US = 0x448
DATA_OFFSET_EU = 0x44C

# The first script variable ("VERSION") is stored as a little-endian u32 at
# offset 0x004.  The game refuses to load a save whose VERSION does not match
# its own build.  US uses 29 (0x1D), EU uses 30 (0x1E).
SCRIPT_VERSION_OFFSET = 0x004
SCRIPT_VERSION_US = 29
SCRIPT_VERSION_EU = 30

# The struct is 0x57D4 on US and 0x57D8 on EU.  The EU save has one extra
# 32-bit field at 0x424; everything from 0x428 onwards is shifted by +4.
US_SAVE_SIZE = 0x57D4
EU_SAVE_SIZE = 0x57D8
US_MAGIC = 0x05071412
EU_MAGIC = 0x06061320

US_GAME_NAME = b"POKE_DUNGEON__05"
EU_GAME_NAME = b"POKE_DUNGEON_E01"

# Header fields (offsets are relative to the start of a save block).
# US values are given; EU values are +4 from 0x424 onwards.
HEADER_FIELDS_US = {
    "recruited_pokemon": 0x424,
    "poke2s": 0x428,
    "team_inventory": 0x430,
    "rescue_team_info": 0x434,
    "friend_areas": 0x438,
    "adventure_data": 0x43C,
    "sub_8095624": 0x440,
    "mail_info": 0x444,
}
HEADER_FIELDS_EU = {
    "recruited_pokemon": 0x428,
    "poke2s": 0x42C,
    "team_inventory": 0x434,
    "rescue_team_info": 0x438,
    "friend_areas": 0x43C,
    "adventure_data": 0x440,
    "sub_8095624": 0x444,
    "mail_info": 0x448,
}
# Fields that are read from the primary/backup block with the same offset.
GENERAL_FIELDS = {
    "US": {
        "team_name": 0x4EC8,
        "held_money": 0x4E6C,
        "stored_money": 0x4E6F,
        "rescue_points": 0x4ED3,
    },
    "EU": {
        "team_name": 0x4ECC,
        "held_money": 0x4E70,
        "stored_money": 0x4E73,
        "rescue_points": 0x4ED7,
    },
}


@dataclass
class SaveInfo:
    region: str
    container_offset: int
    block_offset: int
    container_header: bytes
    block: bytes
    save_size: int
    checksum: int
    computed_checksum: int
    script_version: int
    team_name: str
    held_money: int
    stored_money: int
    rescue_points: int
    counts: dict[str, int]

    def summary(self) -> str:
        lines = [
            f"Region:              {self.region}",
            f"Container offset:    0x{self.container_offset:X}"
            + (" (SharkPort/XPort header stripped)" if self.container_offset else " (raw save)"),
            f"Save block used:     0x{self.block_offset:X}"
            + (" (backup; primary was invalid)" if self.block_offset else " (primary)"),
            f"Save struct size:    0x{self.save_size:X}",
            f"Checksum:            0x{self.checksum:08X} "
            f"({'valid' if self.checksum == self.computed_checksum else 'INVALID, expected 0x%08X' % self.computed_checksum})",
            f"Script VERSION:      {self.script_version} "
            f"({'expected for ' + self.region if self.script_version == (SCRIPT_VERSION_US if self.region == 'US' else SCRIPT_VERSION_EU) else 'UNEXPECTED'})",
            f"Team name:           {self.team_name!r}",
            f"Held money:          {self.held_money}",
            f"Stored money:        {self.stored_money}",
            f"Rescue points:       {self.rescue_points}",
        ]
        for k, v in self.counts.items():
            lines.append(f"{k.replace('_', ' ').title() + ':':20} {v}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Container handling
# ---------------------------------------------------------------------------

def find_raw_save_start(data: bytes) -> int:
    """Return the offset of the raw save inside a SharkPort/XPort container.

    SharkPort files have a short, variable ASCII header.  Rather than relying
    on a fixed header length, locate the game-internal name and work backwards
    from its known offset inside the save.
    """
    if data.startswith(b"SharkPortSave", 4) or b"SharkPortSave" in data[:0x40]:
        for marker in (US_GAME_NAME, EU_GAME_NAME):
            pos = data.find(marker)
            if pos >= GAME_NAME_OFFSET:
                candidate = pos - GAME_NAME_OFFSET
                if candidate + US_SAVE_SIZE <= len(data):
                    return candidate
        # Fall back to the known SharkPort header length for this game.
        if len(data) > 0x61 and data[0x61:0x65] in (
            struct.pack("<I", US_MAGIC),
            struct.pack("<I", EU_MAGIC),
        ):
            return 0x61
        # Last resort: search for the magic directly.
        for magic in (US_MAGIC, EU_MAGIC):
            pos = data.find(struct.pack("<I", magic))
            if pos >= MAGIC_OFFSET:
                candidate = pos - MAGIC_OFFSET
                if candidate >= 0 and candidate + US_SAVE_SIZE <= len(data):
                    return candidate
    return 0


# ---------------------------------------------------------------------------
# Checksum / region detection
# ---------------------------------------------------------------------------

def checksum_for_size(block: bytes, size: int) -> int:
    """The game's CalculateChecksum(): sum LE32 words from 4 to size-4."""
    if len(block) < size:
        raise ValueError(f"save block is too short: {len(block)} < {size}")
    count = (size - 4) // 4
    return sum(struct.unpack_from(f"<{count}I", block, 4)) & 0xFFFFFFFF


def detect_region(block: bytes) -> tuple[str, int, int, int]:
    """Return (region, save_size, stored_checksum, computed_checksum)."""
    if len(block) < EU_SAVE_SIZE:
        raise ValueError(
            f"save block is too short for a full save: {len(block)} bytes"
        )

    name_us = block[GAME_NAME_OFFSET:GAME_NAME_OFFSET + GAME_NAME_LEN]
    machine = block[MAGIC_OFFSET:MAGIC_OFFSET + 4]
    stored = struct.unpack_from("<I", block, 0)[0]

    candidates = []
    if name_us == US_GAME_NAME or machine == struct.pack("<I", US_MAGIC):
        candidates.append(("US", US_SAVE_SIZE, US_MAGIC))
    if name_us == EU_GAME_NAME or machine == struct.pack("<I", EU_MAGIC):
        candidates.append(("EU", EU_SAVE_SIZE, EU_MAGIC))

    if not candidates:
        raise ValueError(
            "unrecognized save: game name at 0x404 is "
            f"{name_us!r}, magic at 0x414 is {machine.hex()}"
        )

    # If both somehow match, prefer the one whose checksum validates.
    for region, size, magic in candidates:
        calc = checksum_for_size(block, size)
        if calc == stored:
            return region, size, stored, calc

    # No checksum match: use the first region hint, but report the mismatch.
    region, size, _magic = candidates[0]
    return region, size, stored, checksum_for_size(block, size)


def parse_info(data: bytes, container_offset: int = 0) -> SaveInfo:
    header = data[:container_offset] if container_offset else b""
    raw = data[container_offset:]

    # Prefer the primary block, but fall back to the backup if the primary
    # checksum is bad and the backup is good.
    candidates = [(PRIMARY_OFFSET, raw[PRIMARY_OFFSET:PRIMARY_OFFSET + BLOCK_SIZE])]
    if len(raw) >= BACKUP_OFFSET + BLOCK_SIZE:
        candidates.append((BACKUP_OFFSET, raw[BACKUP_OFFSET:BACKUP_OFFSET + BLOCK_SIZE]))

    selected = None
    for block_offset, candidate in candidates:
        try:
            region, save_size, stored, calc = detect_region(candidate)
        except ValueError:
            continue
        if selected is None:
            selected = (block_offset, candidate, region, save_size, stored, calc)
        if stored == calc:
            selected = (block_offset, candidate, region, save_size, stored, calc)
            break

    if selected is None:
        # Let detect_region produce the useful error for the primary block.
        region, save_size, stored, calc = detect_region(candidates[0][1])
        selected = (PRIMARY_OFFSET, candidates[0][1], region, save_size, stored, calc)

    block_offset, block, region, save_size, stored, calc = selected

    fields = GENERAL_FIELDS[region]
    name = block[fields["team_name"]:fields["team_name"] + 10]
    name = name.split(b"\x00", 1)[0].split(b"\xff", 1)[0]
    team_name = name.decode("ascii", errors="replace")

    held_money = int.from_bytes(
        block[fields["held_money"]:fields["held_money"] + 3], "little"
    )
    stored_money = int.from_bytes(
        block[fields["stored_money"]:fields["stored_money"] + 3], "little"
    )
    rescue_points = struct.unpack_from("<I", block, fields["rescue_points"])[0]

    header_offsets = HEADER_FIELDS_US if region == "US" else HEADER_FIELDS_EU
    counts = {k: struct.unpack_from("<I", block, v)[0] for k, v in header_offsets.items()}

    return SaveInfo(
        region=region,
        container_offset=container_offset,
        block_offset=block_offset,
        container_header=header,
        block=block,
        save_size=save_size,
        checksum=stored,
        computed_checksum=calc,
        script_version=struct.unpack_from("<I", block, SCRIPT_VERSION_OFFSET)[0],
        team_name=team_name,
        held_money=held_money,
        stored_money=stored_money,
        rescue_points=rescue_points,
        counts=counts,
    )


# ---------------------------------------------------------------------------
# Conversion
# ---------------------------------------------------------------------------

def convert_block(block: bytes, src_region: str, dst_region: str, eu_extra: int) -> bytes:
    """Convert one save block (primary or backup) to the other region layout."""
    if src_region == dst_region:
        return block

    if src_region == "US" and dst_region == "EU":
        # Insert one 32-bit EU-only field at 0x424, shifting the rest +4.
        # The field is not used for save validation; 0 is what the EU sample
        # save contains.  It can be overridden with --eu-extra.
        body = (
            block[:0x424]
            + struct.pack("<I", eu_extra & 0xFFFFFFFF)
            + block[0x424:US_SAVE_SIZE]
        )
        body += b"\xff" * (BLOCK_SIZE - len(body))
        body = (
            body[:GAME_NAME_OFFSET]
            + EU_GAME_NAME
            + body[GAME_NAME_OFFSET + GAME_NAME_LEN:]
        )
        body = (
            body[:MAGIC_OFFSET]
            + struct.pack("<I", EU_MAGIC)
            + body[MAGIC_OFFSET + 4:]
        )
        # The EU build refuses to load a save whose script VERSION is 29.
        body = (
            body[:SCRIPT_VERSION_OFFSET]
            + struct.pack("<I", SCRIPT_VERSION_EU)
            + body[SCRIPT_VERSION_OFFSET + 4:]
        )
        # Clear old checksum first so it cannot accidentally be summed.
        body = b"\x00\x00\x00\x00" + body[4:]
        checksum = checksum_for_size(body, EU_SAVE_SIZE)
        body = struct.pack("<I", checksum) + body[4:]
        return body[:BLOCK_SIZE]

    if src_region == "EU" and dst_region == "US":
        # Remove the EU-only field at 0x424, shifting everything after it -4.
        body = block[:0x424] + block[0x428:EU_SAVE_SIZE]
        body += b"\xff" * (BLOCK_SIZE - len(body))
        body = (
            body[:GAME_NAME_OFFSET]
            + US_GAME_NAME
            + body[GAME_NAME_OFFSET + GAME_NAME_LEN:]
        )
        body = (
            body[:MAGIC_OFFSET]
            + struct.pack("<I", US_MAGIC)
            + body[MAGIC_OFFSET + 4:]
        )
        # The US build refuses to load a save whose script VERSION is 30.
        body = (
            body[:SCRIPT_VERSION_OFFSET]
            + struct.pack("<I", SCRIPT_VERSION_US)
            + body[SCRIPT_VERSION_OFFSET + 4:]
        )
        body = b"\x00\x00\x00\x00" + body[4:]
        checksum = checksum_for_size(body, US_SAVE_SIZE)
        body = struct.pack("<I", checksum) + body[4:]
        return body[:BLOCK_SIZE]

    raise ValueError(f"unsupported conversion: {src_region} -> {dst_region}")


def build_output(converted_block: bytes, dst_region: str, keep_quicksave: bytes | None) -> bytes:
    """Build a raw 0x20000-byte save with primary + backup blocks."""
    out = bytearray(b"\xff" * RAW_SAVE_SIZE)
    out[PRIMARY_OFFSET:PRIMARY_OFFSET + BLOCK_SIZE] = converted_block
    # Backup is a copy of the primary block, including its own valid checksum.
    out[BACKUP_OFFSET:BACKUP_OFFSET + BLOCK_SIZE] = converted_block

    if keep_quicksave is not None:
        # The caller opted to preserve the raw bytes after 0xC000.  This is
        # usually not useful across regions, but it is available for testing.
        n = min(len(keep_quicksave), RAW_SAVE_SIZE - QUICKSAVE_OFFSET)
        out[QUICKSAVE_OFFSET:QUICKSAVE_OFFSET + n] = keep_quicksave[:n]

    return bytes(out)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Convert Pokemon Mystery Dungeon: Red Rescue Team saves between US and EU.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument("input", type=Path, help="input .sav file (raw or SharkPort/XPort)")
    ap.add_argument(
        "-t", "--to",
        choices=("us", "eu"),
        help="target region (default: the opposite of the detected source region)",
    )
    ap.add_argument("-o", "--output", type=Path, help="output .sav path")
    ap.add_argument(
        "--check",
        action="store_true",
        help="only validate and describe the input, do not write an output file",
    )
    ap.add_argument(
        "--eu-extra",
        type=lambda x: int(x, 0),
        default=0,
        metavar="N",
        help="value for the EU-only 0x424 field when converting US->EU (default: 0)",
    )
    ap.add_argument(
        "--keep-quicksave",
        action="store_true",
        help="copy the source bytes after 0xC000 verbatim (normally cleared to 0xFF)",
    )
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    try:
        data = args.input.read_bytes()
    except OSError as exc:
        print(f"error: cannot read {args.input}: {exc}", file=sys.stderr)
        return 1

    raw_start = find_raw_save_start(data)
    if raw_start:
        if args.verbose:
            print(f"Detected container header: {raw_start} bytes")
    else:
        # Reject files that are clearly not saves.
        if len(data) < MAGIC_OFFSET + 4:
            print("error: file is too small to be a save", file=sys.stderr)
            return 1

    try:
        info = parse_info(data, raw_start)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(info.summary())
    print()

    if info.checksum != info.computed_checksum:
        print(
            "warning: the source checksum is invalid.  The game may reject this save.\n"
            "         Conversion will still recalculate the checksum for the output.",
            file=sys.stderr,
        )

    if args.check:
        return 0

    dst = args.to.upper() if args.to else ("EU" if info.region == "US" else "US")
    if dst == info.region:
        print(f"error: input is already {info.region}; choose the other region with --to", file=sys.stderr)
        return 1

    converted = convert_block(info.block, info.region, dst, args.eu_extra)
    keep = data[raw_start + QUICKSAVE_OFFSET:] if args.keep_quicksave else None
    output = build_output(converted, dst, keep)

    # Validate the converted primary block by re-parsing it.
    try:
        new_info = parse_info(output)
    except ValueError as exc:
        print(f"internal error: converted save did not validate: {exc}", file=sys.stderr)
        return 1

    if new_info.region != dst:
        print(f"internal error: converted region is {new_info.region}, expected {dst}", file=sys.stderr)
        return 1
    if new_info.checksum != new_info.computed_checksum:
        print("internal error: converted checksum is invalid", file=sys.stderr)
        return 1
    if (
        new_info.team_name != info.team_name
        or new_info.held_money != info.held_money
        or new_info.stored_money != info.stored_money
        or new_info.rescue_points != info.rescue_points
    ):
        print("internal error: converted data does not round-trip", file=sys.stderr)
        return 1

    out_path = args.output
    if out_path is None:
        stem = args.input.stem
        suffix = args.input.suffix or ".sav"
        out_path = args.input.with_name(f"{stem}_to_{dst}{suffix}")

    if out_path.resolve() == args.input.resolve():
        print("error: output path is the same as the input path", file=sys.stderr)
        return 1

    out_path.write_bytes(output)
    print(f"Wrote {out_path} ({len(output)} bytes, {dst} layout)")
    print()
    print("Converted save:")
    print(new_info.summary())
    if not args.keep_quicksave:
        print("\nQuick-save/suspend data after 0xC000 was cleared to 0xFF.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
