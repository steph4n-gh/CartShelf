#!/usr/bin/env python3
"""Prepare and audit an EverDrive-64 PRO SD-card staging folder on macOS.

The metadata conversion intentionally mirrors Krikzz edmeta 1.0.0.1:
only metadata.ini, description.txt, boxart_front.png, boxart_back.png,
and gamepak_front.png are copied; text is reduced to ASCII and artwork is
resized to the menu's horizontally doubled 240x120 coordinate space.
"""

from __future__ import annotations

import argparse
import csv
from concurrent.futures import ThreadPoolExecutor
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import unicodedata
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
import zlib
from collections import Counter, defaultdict
from pathlib import Path


ROOT = Path(__file__).resolve().parent
DEFAULT_THEME_DIR = ROOT / "themes" / "CartShelf-64" / "dist"
CARTSHELF_THEME_FILES = {
    "CartShelf-64.bgr": "0cd207bfb4d9e73d2a52306cc4ed7b4f15ca2eab7e5e3c2114d7995544a50ff9",
    "CartShelf-64-background.png": "e6263811cc0b3d2e17ce149ddc239c664e99f9ea5a063c6bf5b8e62607495c58",
}

METADATA_REPO_URL = "https://github.com/n64-tools/n64-flashcart-menu-metadata.git"
METADATA_API_URL = "https://api.github.com/repos/n64-tools/n64-flashcart-menu-metadata"
METADATA_RAW_URL = "https://raw.githubusercontent.com/n64-tools/n64-flashcart-menu-metadata/"
METADATA_SOURCE_FILE = ".cartshelf-source.json"
DAT_URL = (
    "https://raw.githubusercontent.com/libretro/libretro-database/master/"
    "metadat/no-intro/Nintendo%20-%20Nintendo%2064.dat"
)
FIRMWARE_INDEX_URL = (
    "https://krikzz.com/pub/support/everdrive-64/pro-series/firmware/"
)

ROM_SUFFIXES = {".z64", ".v64", ".n64", ".rom", ".ndd"}
MAGIC_TO_ORDER = {
    bytes.fromhex("80371240"): "z64",
    bytes.fromhex("37804012"): "v64",
    bytes.fromhex("40123780"): "n64",
}
ORDER_LABELS = {
    "z64": "big-endian",
    "v64": "byte-swapped",
    "n64": "little-endian",
    "unknown": "unknown",
}
REGION_NAMES = {
    "7": "Beta",
    "A": "Japan/USA",
    "B": "Brazil",
    "C": "China",
    "D": "Germany",
    "E": "North America",
    "F": "France",
    "G": "Gateway 64 (NTSC)",
    "H": "Netherlands",
    "I": "Italy",
    "J": "Japan",
    "K": "Korea",
    "L": "Gateway 64 (PAL)",
    "N": "Canada",
    "P": "Europe",
    "S": "Spain",
    "U": "Australia",
    "W": "Scandinavia",
    "X": "Europe",
    "Y": "Europe",
}
SUPPORTED_METADATA_FILES = (
    "metadata.ini",
    "description.txt",
    "boxart_front.png",
    "boxart_back.png",
    "gamepak_front.png",
)
TEXT_METADATA_FILES = {"metadata.ini", "description.txt"}
IMAGE_METADATA_FILES = {
    "boxart_front.png",
    "boxart_back.png",
    "gamepak_front.png",
}


def log(message: str) -> None:
    print(message, flush=True)


def run(command: list[str], cwd: Path | None = None) -> str:
    result = subprocess.run(
        command,
        cwd=cwd,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    return result.stdout.strip()


def download(url: str, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(url, headers={"User-Agent": "CartShelf"})
    with urllib.request.urlopen(request, timeout=60) as response:
        with tempfile.NamedTemporaryFile(
            dir=destination.parent, prefix=destination.name + ".", delete=False
        ) as temporary:
            shutil.copyfileobj(response, temporary)
            temporary_path = Path(temporary.name)
    temporary_path.replace(destination)


def _metadata_response(url: str, limit: int) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "CartShelf"})
    with urllib.request.urlopen(request, timeout=60) as response:
        payload = response.read(limit + 1)
    if len(payload) > limit:
        raise ValueError("The N64 metadata response exceeds its size limit.")
    return payload


def sync_metadata_files(metadata_repo: Path, game_codes: set[str] | None) -> None:
    """Fetch supported files at one upstream commit without requiring host Git."""
    if game_codes is not None and any(not re.fullmatch(r"[A-Za-z0-9]{4}", code) for code in game_codes):
        raise ValueError("Invalid N64 metadata game code.")
    commit = json.loads(_metadata_response(METADATA_API_URL + "/commits/main", 1024 * 1024))
    revision = commit["sha"]
    tree_sha = commit["commit"]["tree"]["sha"]
    if not all(isinstance(value, str) and re.fullmatch(r"[0-9a-f]{40}", value) for value in (revision, tree_sha)):
        raise ValueError("The N64 metadata source did not provide a valid commit.")
    date, message = commit["commit"]["committer"]["date"], commit["commit"]["message"]
    if not isinstance(date, str) or not isinstance(message, str):
        raise ValueError("The N64 metadata source did not provide valid commit details.")
    tree = json.loads(_metadata_response(METADATA_API_URL + "/git/trees/" + tree_sha + "?recursive=1", 16 * 1024 * 1024))
    if tree.get("truncated") is not False or not isinstance(tree.get("tree"), list):
        raise ValueError("The N64 metadata file list is incomplete. Existing files were kept.")
    entries = []
    for entry in tree["tree"]:
        parts = str(entry.get("path", "")).split("/")
        if len(parts) != 6 or parts[0] != "metadata" or parts[-1] not in SUPPORTED_METADATA_FILES:
            continue
        code = "".join(parts[1:5])
        if any(len(part) != 1 for part in parts[1:5]) or not re.fullmatch(r"[A-Za-z0-9]{4}", code):
            continue
        if game_codes is not None and code not in game_codes:
            continue
        if (entry.get("type") != "blob" or entry.get("mode") != "100644"
                or not isinstance(entry.get("sha"), str) or not re.fullmatch(r"[0-9a-f]{40}", entry["sha"])
                or type(entry.get("size")) is not int or not 0 <= entry["size"] <= 16 * 1024 * 1024):
            raise ValueError("The N64 metadata file list contains an unsupported file.")
        entries.append(entry)
    if len({entry["path"] for entry in entries}) != len(entries):
        raise ValueError("The N64 metadata file list contains duplicate paths.")
    codes = game_codes if game_codes is not None else {"".join(entry["path"].split("/")[1:5]) for entry in entries}
    targets = [metadata_repo.joinpath("metadata", *code, name) for code in sorted(codes) for name in SUPPORTED_METADATA_FILES]
    if any(not path.resolve().is_relative_to(metadata_repo.resolve()) or path.is_symlink()
           or (path.exists() and not path.is_file()) for path in targets):
        raise ValueError("An N64 metadata cache path leaves the selected library.")
    metadata_repo.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="n64-metadata-", dir=metadata_repo.parent) as temporary:
        stage = Path(temporary)

        def fetch(entry: dict) -> dict:
            cached = metadata_repo / entry["path"]
            payload = cached.read_bytes() if cached.is_file() and cached.stat().st_size == entry["size"] else None
            if payload is None or hashlib.sha1(f"blob {len(payload)}\0".encode() + payload).hexdigest() != entry["sha"]:
                payload = _metadata_response(METADATA_RAW_URL + revision + "/" + entry["path"], entry["size"])
            blob = hashlib.sha1(f"blob {len(payload)}\0".encode() + payload).hexdigest()
            if len(payload) != entry["size"] or blob != entry["sha"]:
                raise ValueError("An N64 metadata download failed its source checksum. Existing files were kept.")
            target = stage / entry["path"]
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(payload)
            return {"path": entry["path"], "bytes": len(payload), "sha256": hashlib.sha256(payload).hexdigest(), "git_blob": blob}

        log(f"Downloading N64 details and artwork for {len(game_codes) if game_codes is not None else 'all'} game codes ({len(entries)} files)...")
        with ThreadPoolExecutor(max_workers=4) as executor:
            files = list(executor.map(fetch, entries))
        # Complete every download and checksum before touching the previous cache.
        present = {entry["path"] for entry in entries}
        for target in targets:
            relative = target.relative_to(metadata_repo).as_posix()
            if relative in present:
                target.parent.mkdir(parents=True, exist_ok=True)
                (stage / relative).replace(target)
            else:
                target.unlink(missing_ok=True)
        receipt = {
            "commit": revision, "date": date, "subject": (message.splitlines() or [""])[0],
            "source_url": METADATA_REPO_URL, "scope": "selected game codes" if game_codes is not None else "all supported files",
            "game_codes": sorted(codes), "files": sorted(files, key=lambda item: item["path"]),
        }
        source = stage / METADATA_SOURCE_FILE
        source.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
        metadata_repo.mkdir(parents=True, exist_ok=True)
        source.replace(metadata_repo / METADATA_SOURCE_FILE)


def sync_inputs(metadata_repo: Path, dat_path: Path, firmware_cache: Path, game_codes: set[str] | None = None) -> Path:
    """Update the three time-sensitive upstream inputs and return latest firmware."""
    metadata_repo.parent.mkdir(parents=True, exist_ok=True)
    if game_codes is not None or (metadata_repo / METADATA_SOURCE_FILE).is_file():
        sync_metadata_files(metadata_repo, game_codes)
    elif (metadata_repo / ".git").is_dir():
        log("Updating the current metadata source...")
        run(["git", "pull", "--ff-only"], cwd=metadata_repo)
    elif metadata_repo.exists():
        raise RuntimeError(f"Metadata path exists but is not a Git checkout: {metadata_repo}")
    else:
        log("Downloading the current metadata source (the first sync is about 3.3 GB)...")
        run(["git", "clone", "--depth", "1", METADATA_REPO_URL, str(metadata_repo)])

    log("Updating the No-Intro checksum reference...")
    download(DAT_URL, dat_path)

    log("Checking the official Krikzz firmware directory...")
    request = urllib.request.Request(
        FIRMWARE_INDEX_URL, headers={"User-Agent": "CartShelf"}
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        index = response.read().decode("utf-8", "replace")
    names = sorted(
        set(re.findall(r"ed64-pro-fw-v(\d{2}\.\d{4})\.efu", index)),
        key=lambda value: tuple(int(part) for part in value.split(".")),
    )
    if not names:
        raise RuntimeError("No EverDrive-64 PRO firmware was found in the official index")
    firmware_name = f"ed64-pro-fw-v{names[-1]}.efu"
    firmware_path = firmware_cache / firmware_name
    if not firmware_path.exists():
        download(FIRMWARE_INDEX_URL + firmware_name, firmware_path)
    return firmware_path


def find_cached_firmware(firmware_cache: Path) -> Path | None:
    candidates = sorted(
        firmware_cache.glob("ed64-pro-fw-v*.efu"),
        key=lambda path: tuple(
            int(part)
            for part in re.search(r"v(\d{2}\.\d{4})", path.name).group(1).split(".")
        ),
    )
    return candidates[-1] if candidates else None


def parse_dat(path: Path) -> tuple[str, list[dict[str, str]]]:
    text = path.read_text(encoding="utf-8")
    version_match = re.search(r'^\s*version "([^"]+)"', text, re.MULTILINE)
    version = version_match.group(1) if version_match else "unknown"
    entries: list[dict[str, str]] = []
    for match in re.finditer(r"^game \(\s*\n(.*?)^\)\s*$", text, re.MULTILINE | re.DOTALL):
        body = match.group(1)
        name_match = re.search(r'^\s*name "([^"]+)"', body, re.MULTILINE)
        rom_match = re.search(
            r'^\s*rom \( name "([^"]+)" size (\d+) crc ([0-9A-Fa-f]+) '
            r'md5 ([0-9A-Fa-f]+) sha1 ([0-9A-Fa-f]+)(?: serial "([^"]+)")?',
            body,
            re.MULTILINE,
        )
        if not name_match or not rom_match:
            continue
        region_match = re.search(r'^\s*region "([^"]+)"', body, re.MULTILINE)
        serial_match = re.search(r'^\s*serial "([^"]+)"', body, re.MULTILINE)
        entries.append(
            {
                "name": name_match.group(1),
                "region": region_match.group(1) if region_match else "",
                "serial": (
                    serial_match.group(1)
                    if serial_match
                    else (rom_match.group(6) or "")
                ),
                "rom_name": rom_match.group(1),
                "size": rom_match.group(2),
                "crc32": rom_match.group(3).lower(),
                "md5": rom_match.group(4).lower(),
                "sha1": rom_match.group(5).lower(),
            }
        )
    return version, entries


def north_american_retail_set(entries: list[dict[str, str]]) -> dict[str, dict]:
    """Build the conventional 296-title North American retail set.

    No-Intro includes prototypes, LodgeNet, Virtual Console, later aftermarket
    releases, and GameShark. Those are excluded. 1080 Snowboarding is added
    from its combined Japan/USA entry.
    """
    exclusions = (
        "(Beta",
        "(Proto",
        "(LodgeNet",
        "(Limited Run Games)",
        "(Piko Interactive)",
        "(Wii Virtual Console)",
        "(GameCube)",
        "(Zelda Collection)",
    )
    games: dict[str, dict] = {}
    for entry in entries:
        name = entry["name"]
        is_1080 = name.startswith("1080 Snowboarding (Japan, USA)")
        is_usa = "(USA)" in name and not any(tag in name for tag in exclusions)
        if not (is_1080 or is_usa) or name.startswith("GameShark Pro"):
            continue
        title = "1080 Snowboarding" if is_1080 else name.split(" (USA)", 1)[0]
        game = games.setdefault(title, {"title": title, "codes": set(), "variants": []})
        if entry["serial"]:
            game["codes"].add(entry["serial"])
        game["variants"].append(entry)
    if len(games) != 296:
        raise RuntimeError(
            f"North American retail filter produced {len(games)} titles instead of 296"
        )
    return games


def parse_archive_evidence(rom_dir: Path) -> tuple[dict[str, str], dict[str, dict]]:
    base_name = rom_dir.name
    metadata_path = rom_dir / f"{base_name}_meta.xml"
    files_path = rom_dir / f"{base_name}_files.xml"
    collection: dict[str, str] = {}
    archived_files: dict[str, dict] = {}
    if metadata_path.exists():
        root = ET.parse(metadata_path).getroot()
        for key in ("identifier", "title", "publicdate", "scanner", "mediatype"):
            node = root.find(key)
            if node is not None and node.text:
                collection[key] = node.text.strip()
    if files_path.exists():
        root = ET.parse(files_path).getroot()
        for node in root.findall("file"):
            name = node.attrib.get("name", "")
            archived_files[name] = {
                "source": node.attrib.get("source", ""),
                "size": node.findtext("size", ""),
                "md5": node.findtext("md5", "").lower(),
                "sha1": node.findtext("sha1", "").lower(),
                "mtime": node.findtext("mtime", ""),
            }
    return collection, archived_files


def normalize_chunk(data: bytes, order: str) -> bytes:
    if order == "z64":
        return data
    if order == "v64":
        if len(data) % 2:
            raise ValueError("byte-swapped ROM length is not divisible by two")
        swapped = bytearray(len(data))
        swapped[0::2] = data[1::2]
        swapped[1::2] = data[0::2]
        return bytes(swapped)
    if order == "n64":
        if len(data) % 4:
            raise ValueError("little-endian ROM length is not divisible by four")
        swapped = bytearray(len(data))
        swapped[0::4] = data[3::4]
        swapped[1::4] = data[2::4]
        swapped[2::4] = data[1::4]
        swapped[3::4] = data[0::4]
        return bytes(swapped)
    return data


def scan_rom(path: Path) -> dict:
    raw_md5 = hashlib.md5()
    raw_sha1 = hashlib.sha1()
    normalized_md5 = hashlib.md5()
    normalized_sha1 = hashlib.sha1()
    normalized_sha256 = hashlib.sha256()
    normalized_crc32 = 0
    header = bytearray()
    with path.open("rb") as stream:
        magic = stream.read(4)
        stream.seek(0)
        order = MAGIC_TO_ORDER.get(magic, "unknown")
        while True:
            raw = stream.read(1024 * 1024)
            if not raw:
                break
            raw_md5.update(raw)
            raw_sha1.update(raw)
            normalized = normalize_chunk(raw, order)
            if len(header) < 0x40:
                header.extend(normalized[: 0x40 - len(header)])
            normalized_md5.update(normalized)
            normalized_sha1.update(normalized)
            normalized_sha256.update(normalized)
            normalized_crc32 = zlib.crc32(normalized, normalized_crc32)

    valid_header = order != "unknown" and len(header) >= 0x40
    title = (
        bytes(header[0x20:0x34]).decode("ascii", "replace").rstrip(" \0")
        if valid_header
        else ""
    )
    game_code = (
        bytes(header[0x3B:0x3F]).decode("ascii", "replace") if valid_header else ""
    )
    revision = header[0x3F] if valid_header else None
    crc1, crc2 = struct.unpack(">II", header[0x10:0x18]) if valid_header else (0, 0)
    extension = path.suffix.lower().lstrip(".")
    filename_tags = re.findall(r"\([^)]*\)|\[[^]]*\]", path.name)
    return {
        "filename": path.name,
        "path": str(path.resolve()),
        "size_bytes": path.stat().st_size,
        "extension": extension,
        "byte_order": order,
        "byte_order_label": ORDER_LABELS[order],
        "extension_matches_byte_order": extension == order,
        "header_title": title,
        "game_code": game_code,
        "region_code": game_code[-1:] if game_code else "",
        "region": REGION_NAMES.get(game_code[-1:], "Unknown") if game_code else "",
        "revision_byte": revision,
        "display_version": f"1.{revision}" if revision is not None else "",
        "header_crc1": f"{crc1:08X}" if valid_header else "",
        "header_crc2": f"{crc2:08X}" if valid_header else "",
        "raw_md5": raw_md5.hexdigest(),
        "raw_sha1": raw_sha1.hexdigest(),
        "normalized_crc32": f"{normalized_crc32 & 0xFFFFFFFF:08x}",
        "normalized_md5": normalized_md5.hexdigest(),
        "normalized_sha1": normalized_sha1.hexdigest(),
        "normalized_sha256": normalized_sha256.hexdigest(),
        "filename_tags": " ".join(filename_tags),
    }


def attach_reference_data(
    rows: list[dict],
    dat_entries: list[dict[str, str]],
    archive_files: dict[str, dict],
    metadata_root: Path,
) -> None:
    by_sha1 = {entry["sha1"]: entry for entry in dat_entries}
    for row in rows:
        match = by_sha1.get(row["normalized_sha1"])
        row["no_intro_match"] = bool(match)
        row["no_intro_name"] = match["name"] if match else ""
        row["no_intro_expected_filename"] = match["rom_name"] if match else ""
        if match:
            suggested_suffix = "." + row["byte_order"]
            row["suggested_filename"] = str(Path(match["rom_name"]).with_suffix(suggested_suffix))
        else:
            row["suggested_filename"] = ""

        archive = archive_files.get(row["filename"], {})
        row["archive_source"] = archive.get("source", "")
        row["archive_recorded_md5"] = archive.get("md5", "")
        row["archive_recorded_sha1"] = archive.get("sha1", "")
        row["archive_hash_match"] = bool(archive) and (
            archive.get("md5") == row["raw_md5"]
            and archive.get("sha1") == row["raw_sha1"]
        )

        code = row["game_code"]
        entry_dir = metadata_root.joinpath(*code) if len(code) == 4 else Path("/")
        row["metadata_relative_dir"] = "/".join(code) if len(code) == 4 else ""
        for name in SUPPORTED_METADATA_FILES:
            key = "metadata_" + name.replace(".", "_")
            row[key] = len(code) == 4 and (entry_dir / name).exists()

    duplicate_number = 0
    by_content: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        by_content[row["normalized_sha1"]].append(row)
    for group in sorted(by_content.values(), key=lambda items: items[0]["filename"].casefold()):
        if len(group) < 2:
            continue
        duplicate_number += 1
        group_id = f"D{duplicate_number:03d}"
        for row in group:
            row["duplicate_group"] = group_id
    for row in rows:
        row.setdefault("duplicate_group", "")


def write_csv(path: Path, rows: list[dict], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def inventory_fields() -> list[str]:
    return [
        "source_collection",
        "path",
        "selected_for_stage",
        "fill_title",
        "filename",
        "size_bytes",
        "extension",
        "byte_order",
        "extension_matches_byte_order",
        "header_title",
        "game_code",
        "region_code",
        "region",
        "revision_byte",
        "display_version",
        "header_crc1",
        "header_crc2",
        "raw_md5",
        "raw_sha1",
        "normalized_crc32",
        "normalized_md5",
        "normalized_sha1",
        "normalized_sha256",
        "no_intro_match",
        "no_intro_name",
        "no_intro_expected_filename",
        "suggested_filename",
        "filename_tags",
        "duplicate_group",
        "archive_source",
        "archive_recorded_md5",
        "archive_recorded_sha1",
        "archive_hash_match",
        "metadata_relative_dir",
        "metadata_metadata_ini",
        "metadata_description_txt",
        "metadata_boxart_front_png",
        "metadata_boxart_back_png",
        "metadata_gamepak_front_png",
    ]


def build_completeness(
    retail_games: dict[str, dict], rows: list[dict]
) -> tuple[list[dict], list[dict]]:
    by_code: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        by_code[row["game_code"]].append(row)
    completeness: list[dict] = []
    for title in sorted(retail_games, key=str.casefold):
        game = retail_games[title]
        matching_rows = [row for code in game["codes"] for row in by_code.get(code, [])]
        completeness.append(
            {
                "title": title,
                "game_codes": ", ".join(sorted(game["codes"])),
                "status": "present" if matching_rows else "missing",
                "verification": (
                    "No-Intro hash verified"
                    if any(row["no_intro_match"] for row in matching_rows)
                    else ("header-code match only" if matching_rows else "")
                ),
                "local_files": " | ".join(sorted(row["filename"] for row in matching_rows)),
            }
        )
    missing = [row for row in completeness if row["status"] == "missing"]
    return completeness, missing


def select_verified_fills(
    retail_games: dict[str, dict],
    missing: list[dict],
    supplemental_rows: list[dict],
) -> list[dict]:
    """Choose the newest No-Intro-verified retail revision for each missing title."""
    selected: list[dict] = []
    for missing_item in missing:
        game = retail_games[missing_item["title"]]
        accepted_hashes = {
            variant["sha1"] for variant in game["variants"] if variant.get("sha1")
        }
        candidates = [
            row
            for row in supplemental_rows
            if row["no_intro_match"]
            and row["normalized_sha1"] in accepted_hashes
            and row["game_code"] in game["codes"]
        ]
        if not candidates:
            continue
        candidates.sort(
            key=lambda row: (
                row["revision_byte"] if row["revision_byte"] is not None else -1,
                row["filename"].casefold(),
            ),
            reverse=True,
        )
        chosen = candidates[0]
        chosen["selected_for_stage"] = True
        chosen["fill_title"] = missing_item["title"]
        selected.append(chosen)
    return selected


def metadata_source_info(metadata_repo: Path) -> dict[str, str]:
    receipt = metadata_repo / METADATA_SOURCE_FILE
    if receipt.is_file():
        values = json.loads(receipt.read_text(encoding="utf-8"))
        return {key: values[key] for key in ("commit", "date", "subject", "scope")}
    try:
        values = run(
            [
                "git",
                "log",
                "-1",
                "--date=iso-strict",
                "--format=%H%n%ad%n%s",
            ],
            cwd=metadata_repo,
        ).splitlines()
        return {
            "commit": values[0],
            "date": values[1],
            "subject": values[2] if len(values) > 2 else "",
        }
    except Exception:
        return {"commit": "unknown", "date": "unknown", "subject": ""}


def write_report(
    path: Path,
    rows: list[dict],
    completeness: list[dict],
    missing: list[dict],
    collection: dict[str, str],
    dat_version: str,
    dat_path: Path,
    metadata_repo: Path,
    firmware_path: Path | None,
    supplemental_rows: list[dict],
    selected_fills: list[dict],
    remaining_missing: list[dict],
    supplemental_dir: Path | None,
) -> None:
    unique_contents = {row["normalized_sha1"] for row in rows}
    unique_codes = {row["game_code"] for row in rows if row["game_code"]}
    duplicate_groups: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        if row["duplicate_group"]:
            duplicate_groups[row["duplicate_group"]].append(row)
    metadata_counts = {
        name: len(
            {
                row["game_code"]
                for row in rows
                if row["game_code"] and row["metadata_" + name.replace(".", "_")]
            }
        )
        for name in SUPPORTED_METADATA_FILES
    }
    metadata_gaps = [
        row
        for code, row in {
            row["game_code"]: row for row in rows if row["game_code"]
        }.items()
        if not row["metadata_metadata_ini"]
    ]
    no_art = [
        row
        for code, row in {
            row["game_code"]: row for row in rows if row["game_code"]
        }.items()
        if not row["metadata_boxart_front_png"]
    ]
    retail_codes = {
        code
        for item in completeness
        for code in (part.strip() for part in item["game_codes"].split(","))
        if code
    }
    extras_by_code: dict[str, list[str]] = defaultdict(list)
    for row in rows:
        if row["game_code"] and row["game_code"] not in retail_codes:
            extras_by_code[row["game_code"]].append(row["filename"])
    unmatched = [row for row in rows if not row["no_intro_match"]]
    mode_counts = Counter(row["byte_order"] for row in rows)
    verified_unique = len(
        {row["normalized_sha1"] for row in rows if row["no_intro_match"]}
    )
    archive_matches = sum(row["archive_hash_match"] for row in rows)
    metadata_info = metadata_source_info(metadata_repo)
    generated = dt.datetime.now().astimezone().isoformat(timespec="seconds")
    dat_sha256 = hashlib.sha256(dat_path.read_bytes()).hexdigest()
    fills_by_title = {row["fill_title"]: row for row in selected_fills}
    staged_rows = rows + selected_fills
    staged_metadata_titles = 0
    staged_front_art_titles = 0
    for item in completeness:
        codes = {part.strip() for part in item["game_codes"].split(",") if part.strip()}
        matching = [row for row in staged_rows if row["game_code"] in codes]
        staged_metadata_titles += any(row["metadata_metadata_ini"] for row in matching)
        staged_front_art_titles += any(row["metadata_boxart_front_png"] for row in matching)

    lines = [
        "# Nintendo 64 collection report",
        "",
        f"Generated: {generated}",
        "",
        "## Outcome",
        "",
        f"- Primary ROM files: **{len(rows)}**",
        f"- Unique ROM contents after byte-order normalization: **{len(unique_contents)}**",
        f"- Distinct four-character header game codes: **{len(unique_codes)}**",
        f"- North American retail titles present in the primary folder: **{len(completeness) - len(missing)} / {len(completeness)}**",
        f"- Missing from the primary folder: **{len(missing)}**",
        f"- Verified fills selected from the supplemental folder: **{len(selected_fills)}**",
        f"- Retail titles still missing after verified fills: **{len(remaining_missing)}**",
        f"- Staged retail titles with `metadata.ini`: **{staged_metadata_titles} / {len(completeness)}**",
        f"- Staged retail titles with front box art: **{staged_front_art_titles} / {len(completeness)}**",
        f"- No-Intro-verified files: **{sum(row['no_intro_match'] for row in rows)} / {len(rows)}** ({verified_unique} unique contents)",
        f"- Exact duplicate-content groups: **{len(duplicate_groups)}**",
        f"- Files whose hashes match the local archive manifest: **{archive_matches} / {len(rows)}**",
        "",
        "The 296-title comparison excludes prototypes, betas, LodgeNet builds, Virtual Console/GameCube builds, GameShark, and later aftermarket releases. It includes the combined Japan/USA release of 1080 Snowboarding.",
        "",
        "## Missing North American retail titles",
        "",
    ]
    for item in missing:
        fill = fills_by_title.get(item["title"])
        suffix = f" - filled by `{fill['filename']}` (No-Intro verified)" if fill else " - no verified fill found"
        lines.append(f"- {item['title']} ({item['game_codes']}){suffix}")
    lines.extend(["", "After using the selected supplemental fills:", ""])
    if remaining_missing:
        lines.extend(
            f"- {item['title']} ({item['game_codes']})" for item in remaining_missing
        )
    else:
        lines.append("- **296 / 296 North American retail titles are represented in staging.**")
    lines.extend(
        [
            "",
            "## Additional imports or non-retail images",
            "",
        ]
    )
    if extras_by_code:
        for code in sorted(extras_by_code):
            lines.append(f"- {code}: " + "; ".join(sorted(extras_by_code[code])))
    else:
        lines.append("- None")

    lines.extend(
        [
            "",
            "## Byte order and filenames",
            "",
            f"- Big-endian (`z64` content): {mode_counts['z64']}",
            f"- Byte-swapped (`v64` content): {mode_counts['v64']}",
            f"- Little-endian (`n64` content): {mode_counts['n64']}",
            f"- Filename extensions that do not match the actual byte order: {sum(not row['extension_matches_byte_order'] for row in rows)}",
            "",
            "The EverDrive-64 PRO supports all three byte orders. Metadata is selected from the internal four-character game code, not the filename. A misleading extension therefore does not prevent metadata matching, but it is recorded in `rom_inventory.csv`.",
            "",
            "Common filename tags in this folder:",
            "",
            "- `(U)`, `(E)`, `(J)`, `(JU)`: old GoodTools-style region labels (USA, Europe, Japan, or a combined Japan/USA build).",
            "- `(M2)`, `(M3)`: multilingual ROM with two or three languages.",
            "- `(V1.1)`, `(V1.2)`: later cartridge revision; the header revision byte is reported separately.",
            "- `[!]`: GoodTools claimed it was a verified good dump. This is not the same as a current No-Intro hash match.",
            "- `(!)`: a non-standard/malformed variation seen in this pack; it is not proof of integrity.",
            "- `_s`, `_98`, `_99`: apostrophes were replaced with underscores when these filenames were sanitized.",
            "",
            "## Duplicate contents",
            "",
        ]
    )
    if duplicate_groups:
        for group_id in sorted(duplicate_groups):
            names = "; ".join(row["filename"] for row in duplicate_groups[group_id])
            lines.append(f"- {group_id}: {names}")
    else:
        lines.append("- None")

    lines.extend(
        [
            "",
            "## No-Intro-unmatched files",
            "",
            "These files are not automatically bad, but their normalized SHA-1 does not occur in the referenced current DAT. Old `[!]` tags do not override this result.",
            "",
        ]
    )
    for row in unmatched:
        lines.append(f"- `{row['filename']}` - {row['game_code']} - {row['normalized_sha1']}")

    lines.extend(
        [
            "",
            "## EverDrive metadata coverage",
            "",
            f"Coverage across {len(unique_codes)} distinct local header codes:",
            "",
            f"- `metadata.ini`: {metadata_counts['metadata.ini']}",
            f"- `description.txt`: {metadata_counts['description.txt']}",
            f"- `boxart_front.png`: {metadata_counts['boxart_front.png']}",
            f"- `boxart_back.png`: {metadata_counts['boxart_back.png']}",
            f"- `gamepak_front.png`: {metadata_counts['gamepak_front.png']}",
            "",
            "The current Krikzz converter only consumes those five files. Other source artwork (spines and bottom/side images) is not used by the August 2026 stock EverDrive-64 PRO converter.",
            "",
            "Codes without `metadata.ini`:",
            "",
        ]
    )
    lines.extend(
        f"- {row['game_code']}: {row['filename']}" for row in sorted(metadata_gaps, key=lambda x: x["game_code"])
    )
    lines.extend(["", "Codes without front box art:", ""])
    lines.extend(
        f"- {row['game_code']}: {row['filename']}" for row in sorted(no_art, key=lambda x: x["game_code"])
    )

    lines.extend(
        [
            "",
            "## Provenance and reference versions",
            "",
            f"- Local collection identifier: `{collection.get('identifier', 'not found')}`",
            f"- Local collection title: {collection.get('title', 'not found')}",
            f"- Local manifest public date: {collection.get('publicdate', 'not found')}",
            f"- Local uploader software: {collection.get('scanner', 'not found')}",
            f"- No-Intro/libretro DAT version: `{dat_version}`",
            f"- No-Intro/libretro DAT SHA-256: `{dat_sha256}`",
            f"- Metadata repository commit: `{metadata_info['commit']}`",
            f"- Metadata repository commit date: `{metadata_info['date']}`",
            f"- Metadata source scope: {metadata_info.get('scope', 'Git checkout')}",
            f"- Firmware staged: `{firmware_path.name if firmware_path else 'not available'}`",
            f"- Supplemental comparison folder: `{supplemental_dir if supplemental_dir else 'not used'}`",
            f"- Supplemental files inspected: {len(supplemental_rows)}",
            f"- Supplemental files selected: {len(selected_fills)}",
            "",
            "The local `_meta.xml`, `_files.xml`, `_meta.sqlite`, torrent, and reviews files identify this folder as an Internet Archive item snapshot. Matching the local files to `_files.xml` proves they match that item manifest; it does not prove how this copy was acquired, that each dump came from an owned cartridge, or that `[!]` claims are current.",
            "",
            "The supplemental Lightroom-backup folder has no equivalent acquisition manifest in scope. Its filenames resemble No-Intro naming, but only selected files whose normalized hashes match the current DAT are used to fill staging gaps.",
            "",
            "For future additions, retain a personal dump log (cartridge title, region, label/revision, dump date, dumper/tool, and raw hashes) and verify normalized hashes against a current preservation DAT. Do not use filename tags alone as provenance.",
        ]
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def audit(
    rom_dir: Path,
    supplemental_rom_dir: Path | None,
    dat_path: Path,
    metadata_repo: Path,
    report_dir: Path,
    firmware_path: Path | None,
) -> tuple[list[dict], list[dict], list[dict], list[dict]]:
    metadata_root = metadata_repo / "metadata"
    if not metadata_root.is_dir():
        raise RuntimeError(f"Metadata source folder not found: {metadata_root}")
    if not dat_path.is_file():
        raise RuntimeError(f"No-Intro DAT not found: {dat_path}")
    rom_paths = sorted(
        (
            path
            for path in rom_dir.iterdir()
            if path.is_file() and path.suffix.lower() in ROM_SUFFIXES
        ),
        key=lambda path: path.name.casefold(),
    )
    log(f"Hashing and inspecting {len(rom_paths)} primary ROM files...")
    rows = []
    for index, path in enumerate(rom_paths, start=1):
        row = scan_rom(path)
        row["source_collection"] = "primary"
        row["selected_for_stage"] = True
        row["fill_title"] = ""
        rows.append(row)
        if index % 50 == 0 or index == len(rom_paths):
            log(f"  inspected {index}/{len(rom_paths)}")

    dat_version, dat_entries = parse_dat(dat_path)
    collection, archive_files = parse_archive_evidence(rom_dir)
    attach_reference_data(rows, dat_entries, archive_files, metadata_root)
    retail_games = north_american_retail_set(dat_entries)
    completeness, missing = build_completeness(retail_games, rows)

    supplemental_rows: list[dict] = []
    if supplemental_rom_dir and supplemental_rom_dir.is_dir():
        supplemental_paths = sorted(
            (
                path
                for path in supplemental_rom_dir.rglob("*")
                if path.is_file() and path.suffix.lower() in ROM_SUFFIXES
            ),
            key=lambda path: str(path).casefold(),
        )
        log(f"Hashing and inspecting {len(supplemental_paths)} supplemental ROM files...")
        for index, path in enumerate(supplemental_paths, start=1):
            row = scan_rom(path)
            row["source_collection"] = "supplemental Lightroom backup"
            row["selected_for_stage"] = False
            row["fill_title"] = ""
            supplemental_rows.append(row)
            if index % 50 == 0 or index == len(supplemental_paths):
                log(f"  inspected {index}/{len(supplemental_paths)}")
        attach_reference_data(
            supplemental_rows, dat_entries, {}, metadata_root
        )

    selected_fills = select_verified_fills(retail_games, missing, supplemental_rows)
    staged_completeness, remaining_missing = build_completeness(
        retail_games, rows + selected_fills
    )

    report_dir.mkdir(parents=True, exist_ok=True)
    write_csv(report_dir / "rom_inventory.csv", rows, inventory_fields())
    write_csv(
        report_dir / "supplemental_rom_inventory.csv",
        supplemental_rows,
        inventory_fields(),
    )
    write_csv(
        report_dir / "selected_supplemental_fills.csv",
        selected_fills,
        inventory_fields(),
    )
    write_csv(
        report_dir / "north_american_retail_checklist.csv",
        completeness,
        ["title", "game_codes", "status", "verification", "local_files"],
    )
    write_csv(
        report_dir / "missing_north_american_retail.csv",
        missing,
        ["title", "game_codes", "status", "verification", "local_files"],
    )
    write_csv(
        report_dir / "staged_north_american_retail_checklist.csv",
        staged_completeness,
        ["title", "game_codes", "status", "verification", "local_files"],
    )
    write_csv(
        report_dir / "unfilled_north_american_retail.csv",
        remaining_missing,
        ["title", "game_codes", "status", "verification", "local_files"],
    )
    duplicate_rows = [row for row in rows if row["duplicate_group"]]
    write_csv(
        report_dir / "duplicate_contents.csv",
        duplicate_rows,
        [
            "duplicate_group",
            "filename",
            "game_code",
            "no_intro_name",
            "normalized_sha1",
        ],
    )
    write_report(
        report_dir / "COLLECTION_REPORT.md",
        rows,
        completeness,
        missing,
        collection,
        dat_version,
        dat_path,
        metadata_repo,
        firmware_path,
        supplemental_rows,
        selected_fills,
        remaining_missing,
        supplemental_rom_dir,
    )
    return rows, missing, selected_fills, remaining_missing


def clean_ascii(text: str) -> str:
    decomposed = unicodedata.normalize("NFD", text)
    return "".join(
        character
        for character in decomposed
        if unicodedata.category(character) != "Mn" and ord(character) <= 0x7F
    )


def image_dimensions(path: Path) -> tuple[int, int]:
    try:
        from PIL import Image

        with Image.open(path) as image:
            return image.size
    except ImportError:
        output = run(["sips", "-g", "pixelWidth", "-g", "pixelHeight", str(path)])
        width = int(re.search(r"pixelWidth: (\d+)", output).group(1))
        height = int(re.search(r"pixelHeight: (\d+)", output).group(1))
        return width, height


def resize_metadata_image(source: Path, destination: Path) -> None:
    width, height = image_dimensions(source)
    if width > height:
        target_width = 240
        target_height = int(height * (120.0 / width))
    else:
        target_width = int(width * (120.0 / height)) * 2
        target_height = 120
    target_width = max(1, target_width)
    target_height = max(1, target_height)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if (width, height) == (target_width, target_height):
        shutil.copy2(source, destination)
        return
    try:
        from PIL import Image

        with Image.open(source) as image:
            image.convert("RGBA").resize(
                (target_width, target_height), Image.Resampling.BICUBIC
            ).save(destination, format="PNG", optimize=True)
    except ImportError:
        run(
            [
                "sips",
                "--resampleHeightWidth",
                str(target_height),
                str(target_width),
                str(source),
                "--out",
                str(destination),
            ]
        )


def convert_metadata_tree(source_root: Path, destination_root: Path) -> Counter:
    counts: Counter = Counter()
    if not source_root.is_dir():
        return counts
    entry_dirs = sorted(
        path
        for path in source_root.glob("*/*/*/*")
        if path.is_dir() and all(len(part) == 1 for part in path.relative_to(source_root).parts)
    )
    for index, source_dir in enumerate(entry_dirs, start=1):
        relative = source_dir.relative_to(source_root)
        destination_dir = destination_root / relative
        for name in SUPPORTED_METADATA_FILES:
            source = source_dir / name
            if not source.is_file():
                continue
            destination = destination_dir / name
            if name in TEXT_METADATA_FILES:
                text = source.read_text(encoding="utf-8-sig", errors="replace")
                text = clean_ascii(text)
                if name.endswith(".txt"):
                    text = text.strip()
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_text(text, encoding="utf-8", newline="")
            else:
                resize_metadata_image(source, destination)
            counts[name] += 1
        if index % 100 == 0 or index == len(entry_dirs):
            log(f"  converted {index}/{len(entry_dirs)} metadata entries")
    return counts


def stage_cartshelf_theme(theme_dir: Path, output: Path) -> list[str]:
    files = {}
    for name, expected in CARTSHELF_THEME_FILES.items():
        source = theme_dir / name
        if source.is_symlink() or not source.is_file():
            raise RuntimeError(f"The CartShelf-64 theme is missing: {source}")
        data = source.read_bytes()
        if hashlib.sha256(data).hexdigest() != expected:
            raise RuntimeError(f"The CartShelf-64 theme failed SHA-256 verification: {name}")
        files["Themes/" + name] = data
    files["ed64/sysdata/theme.bgr"] = files["Themes/CartShelf-64.bgr"]
    for relative, data in files.items():
        target = output / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("xb") as stream:
            stream.write(data)
        if target.read_bytes() != data:
            raise RuntimeError(f"The CartShelf-64 theme copy failed verification: {relative}")
    return list(CARTSHELF_THEME_FILES)


def build_stage(
    rom_dir: Path,
    supplemental_fills: list[dict],
    homebrew_dir: Path,
    metadata_repo: Path,
    custom_metadata: Path,
    theme_dir: Path,
    firmware_path: Path | None,
    output: Path,
    copy_roms: bool,
) -> None:
    if output.exists():
        raise RuntimeError(
            f"Output already exists: {output}\nChoose a new --output path or move the old build first."
        )
    output.mkdir(parents=True)
    games_dir = output / "N64 Games"
    games_dir.mkdir()

    rom_paths = sorted(
        (
            path
            for path in rom_dir.iterdir()
            if path.is_file() and path.suffix.lower() in ROM_SUFFIXES
        ),
        key=lambda path: path.name.casefold(),
    )

    def stage_file(source: Path, destination: Path) -> None:
        if copy_roms:
            shutil.copy2(source, destination)
        else:
            try:
                os.link(source, destination)
            except OSError:
                shutil.copy2(source, destination)

    log(f"Staging {len(rom_paths)} primary ROM files...")
    for source in rom_paths:
        stage_file(source, games_dir / source.name)

    log(f"Adding {len(supplemental_fills)} verified supplemental fills...")
    for row in supplemental_fills:
        source = Path(row["path"])
        destination = games_dir / source.name
        if destination.exists():
            destination = destination.with_name(
                destination.stem + " [supplemental]" + destination.suffix
            )
        stage_file(source, destination)

    homebrew_roms = sorted(
        (
            path
            for path in homebrew_dir.glob("*/dist/*.z64")
            if path.is_file()
        ),
        key=lambda path: path.name.casefold(),
    ) if homebrew_dir.is_dir() else []
    if homebrew_roms:
        homebrew_destination = games_dir / "Homebrew"
        homebrew_destination.mkdir()
        log(f"Adding {len(homebrew_roms)} locally built homebrew ROM files...")
        for source in homebrew_roms:
            destination = homebrew_destination / source.name
            if destination.exists():
                raise RuntimeError(f"Duplicate homebrew ROM filename: {source.name}")
            stage_file(source, destination)

    metadata_destination = output / "ed64" / "metadata"
    log("Converting the official metadata database...")
    counts = convert_metadata_tree(metadata_repo / "metadata", metadata_destination)
    if custom_metadata.is_dir():
        custom_counts = convert_metadata_tree(custom_metadata, metadata_destination)
        counts.update(custom_counts)
    if firmware_path and firmware_path.is_file():
        firmware_dir = output / "Firmware"
        firmware_dir.mkdir()
        shutil.copy2(firmware_path, firmware_dir / firmware_path.name)

    theme_files: list[str] = []
    if theme_dir.resolve() == DEFAULT_THEME_DIR.resolve():
        theme_files = stage_cartshelf_theme(theme_dir, output)
    elif theme_dir.is_dir():
        theme_sources = sorted(
            (
                path
                for path in theme_dir.iterdir()
                if path.is_file() and path.suffix.lower() in {".bgr", ".png"}
            ),
            key=lambda path: path.name.casefold(),
        )
        if theme_sources:
            themes_destination = output / "Themes"
            themes_destination.mkdir()
            for source in theme_sources:
                shutil.copy2(source, themes_destination / source.name)
                theme_files.append(source.name)

    build_info = {
        "generated": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "source_rom_directory": str(rom_dir.resolve()),
        "primary_rom_files": len(rom_paths),
        "supplemental_fills": len(supplemental_fills),
        "homebrew_rom_files": len(homebrew_roms),
        "staged_rom_files": len(rom_paths) + len(supplemental_fills) + len(homebrew_roms),
        "metadata_files": dict(counts),
        "firmware": firmware_path.name if firmware_path else None,
        "theme_files": theme_files,
        "rom_staging_mode": "copies" if copy_roms else "hard links when possible",
    }
    (output / ".cartshelf-build.json").write_text(
        json.dumps(build_info, indent=2) + "\n", encoding="utf-8"
    )
    for path in output.rglob("*"):
        if path.is_file() and (path.name == ".DS_Store" or path.name.startswith("._")):
            path.unlink()
    log(f"SD-card staging folder created at: {output}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Audit N64 ROMs and prepare an EverDrive-64 PRO SD-card staging folder."
    )
    parser.add_argument("--root", type=Path, required=True, help="Chosen data folder, separate from the source repository.")
    parser.add_argument("--rom-dir", type=Path)
    parser.add_argument(
        "--supplemental-rom-dir",
        type=Path,
        default=None,
        help="Read-only source used to find verified fills for missing retail titles.",
    )
    parser.add_argument("--metadata-repo", type=Path)
    parser.add_argument("--dat", type=Path)
    parser.add_argument("--reports", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--custom-metadata", type=Path)
    parser.add_argument("--homebrew-dir", type=Path)
    parser.add_argument("--firmware-cache", type=Path)
    parser.add_argument("--theme-dir", type=Path, default=DEFAULT_THEME_DIR)
    parser.add_argument(
        "--offline",
        action="store_true",
        help="Use cached metadata, DAT, and firmware without checking for updates.",
    )
    parser.add_argument(
        "--audit-only", action="store_true", help="Write reports without building the staging folder."
    )
    parser.add_argument(
        "--copy-roms",
        action="store_true",
        help="Use full ROM copies in local staging instead of space-saving hard links.",
    )
    args = parser.parse_args()
    try:
        root = args.root.resolve(strict=True)
    except OSError:
        parser.error("Choose an existing data directory.")
    if not root.is_dir() or root == ROOT:
        parser.error("Choose an existing data directory separate from this source repository.")
    defaults = {
        'rom_dir': root / 'library/N64',
        'metadata_repo': root / 'cache/n64-flashcart-menu-metadata',
        'dat': root / 'reference/Nintendo - Nintendo 64.dat',
        'reports': root / 'reports',
        'output': root / 'exports/EverDrive64-SD',
        'custom_metadata': root / 'custom-metadata',
        'homebrew_dir': root / 'homebrew',
        'firmware_cache': root / 'cache/firmware',
    }
    for name, default in defaults.items():
        if getattr(args, name) is None:
            setattr(args, name, default)
    return args


def main() -> int:
    args = parse_args()
    try:
        if not args.rom_dir.is_dir():
            raise RuntimeError(f"ROM directory not found: {args.rom_dir}")
        if not args.audit_only and args.output.exists():
            raise RuntimeError(
                f"Output already exists: {args.output}\nChoose a new --output path or move the old build first."
            )
        if args.offline:
            firmware_path = find_cached_firmware(args.firmware_cache)
        else:
            firmware_path = sync_inputs(args.metadata_repo, args.dat, args.firmware_cache)
        _, missing, selected_fills, remaining_missing = audit(
            args.rom_dir,
            args.supplemental_rom_dir,
            args.dat,
            args.metadata_repo,
            args.reports,
            firmware_path,
        )
        if not args.audit_only:
            build_stage(
                args.rom_dir,
                selected_fills,
                args.homebrew_dir,
                args.metadata_repo,
                args.custom_metadata,
                args.theme_dir,
                firmware_path,
                args.output,
                args.copy_roms,
            )
        log(f"Reports written to: {args.reports}")
        log(f"Missing from the primary folder: {len(missing)}")
        log(f"Still missing after verified supplemental fills: {len(remaining_missing)}")
        return 0
    except (OSError, RuntimeError, subprocess.CalledProcessError, urllib.error.URLError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
