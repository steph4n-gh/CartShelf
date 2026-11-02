#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
"""Apply or restore the fixed CartShelf-GB palette to an exact OS 1.06 copy."""

import argparse
import hashlib
import os
from pathlib import Path


SIZE = 81920
OFFSETS = (0x1FA4, 0x1FA6, 0x1FA8, 0x1FAA)
PALETTE = ("#080808", "#FFFFFF", "#A51018", "#FFDE08")
STOCK_BYTES = bytes.fromhex("0000e413ef3dffff")
STOCK_SHA256 = "c61824768584b01f7fabe290eccc5dd9e10d78c49cc948a9addba1fb86204635"
THEMED_SHA256 = "e12e7074817f5753891a52a422cb31fd25237fb20c148ed40deb5a6e25e8052d"
PATCH_SHA256 = "048d45c1a9d6e4cd235272ce9717c44f4d2ac616c59ca48656fa190dba68fc4e"
RESTORE_SHA256 = "34532fd27124f5bcfcd1c554a89b5246410c5b472f0531bb4e26ce685f0abe97"


def digest(data):
    return hashlib.sha256(data).hexdigest()


def palette_bytes():
    words = []
    for color in PALETTE:
        red, green, blue = (int(color[i:i + 2], 16) // 8 for i in (1, 3, 5))
        words.append((red + (green << 5) + (blue << 10)).to_bytes(2, "little"))
    return b"".join(words)


def checked_records(patch, expected_hash, expected_palette):
    if digest(patch) != expected_hash:
        raise ValueError("The fixed IPS SHA-256 does not match.")
    if len(patch) != 36 or patch[:5] != b"PATCH" or patch[-3:] != b"EOF":
        raise ValueError("The IPS must contain exactly four fixed two-byte records.")
    records = []
    for index, offset in enumerate(OFFSETS):
        record = patch[5 + index * 7:12 + index * 7]
        if int.from_bytes(record[:3], "big") != offset:
            raise ValueError("The IPS contains an unexpected palette offset.")
        if record[3:5] != b"\x00\x02":
            raise ValueError("The IPS contains an unexpected record length.")
        if record[5:] != expected_palette[index * 2:index * 2 + 2]:
            raise ValueError("The IPS does not match the fixed palette.")
        records.append((offset, record[5:]))
    return records


def verified_output(original, result, expected_hash):
    if len(result) != SIZE or digest(result) != expected_hash:
        raise ValueError("The output size or SHA-256 does not match.")
    if original[:OFFSETS[0]] != result[:OFFSETS[0]]:
        raise ValueError("The output changes bytes before the palette.")
    if original[OFFSETS[-1] + 2:] != result[OFFSETS[-1] + 2:]:
        raise ValueError("The output changes bytes after the palette.")


def apply(source, output, restore):
    if source.resolve() == output.resolve():
        raise ValueError("Input and output must be different files.")
    if os.path.lexists(output):
        raise ValueError("The output already exists. Choose a new path.")
    if not source.is_file() or source.stat().st_size != SIZE:
        raise ValueError("The input must be an 81,920-byte regular file.")
    original = source.read_bytes()
    source_hash, output_hash = (THEMED_SHA256, STOCK_SHA256) if restore else (STOCK_SHA256, THEMED_SHA256)
    if digest(original) != source_hash:
        raise ValueError("The input SHA-256 does not match the required {} OS 1.06 file.".format(
            "CartShelf-GB" if restore else "stock"))
    name = "CartShelf-GB-X7-v1.06-restore.ips" if restore else "CartShelf-GB-X7-v1.06.ips"
    patch = (Path(__file__).resolve().parent / "dist" / name).read_bytes()
    records = checked_records(patch, RESTORE_SHA256 if restore else PATCH_SHA256,
                              STOCK_BYTES if restore else palette_bytes())
    result = bytearray(original)
    for offset, value in records:
        result[offset:offset + 2] = value
    verified_output(original, result, output_hash)
    with output.open("x+b") as handle:
        handle.write(result)
        handle.flush()
        handle.seek(0)
        verified_output(original, handle.read(), output_hash)
    return output_hash


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--restore", action="store_true", help="Restore this exact themed BIN to stock bytes.")
    parser.add_argument("input", type=Path, help="Exact stock BIN, or exact themed BIN with --restore.")
    parser.add_argument("output", type=Path, help="New output file. Its parent directory must exist.")
    args = parser.parse_args()
    try:
        output_hash = apply(args.input, args.output, args.restore)
    except (OSError, ValueError) as error:
        parser.exit(1, "Error: {}\n".format(error))
    print("Wrote {} ({} bytes). SHA-256 {}".format(args.output, SIZE, output_hash))


if __name__ == "__main__":
    main()
