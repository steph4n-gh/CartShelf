#!/usr/bin/env python3
"""Build verified card images and replace explicitly reviewed removable cards."""
from __future__ import annotations

import argparse
import array
import contextlib
import ctypes
import datetime as dt
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import plistlib
import re
import secrets
import shutil
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import time
import uuid
from typing import BinaryIO


CHUNK = 4 * 1024 * 1024
MIB = 1024 * 1024
DISK = re.compile(r"disk[0-9]+\Z")
DISKUTIL = "/usr/sbin/diskutil"
HDIUTIL = "/usr/bin/hdiutil"
AUTHOPEN = "/usr/libexec/authopen"
PROGRESS_BYTES = 64 * MIB
GB_X7_OS_BYTES = 81920
GB_X7_OS_SHA256 = frozenset({
    'c61824768584b01f7fabe290eccc5dd9e10d78c49cc948a9addba1fb86204635',
    'e12e7074817f5753891a52a422cb31fd25237fb20c148ed40deb5a6e25e8052d',
})
CARD_FILE_SCOPE = "card-files-v1"
MACOS_SYSTEM_DIRECTORIES = frozenset({".fseventsd", ".Spotlight-V100", ".Trashes"})
MACOS_SYSTEM_DIRECTORIES_FOLDED = frozenset(name.casefold() for name in MACOS_SYSTEM_DIRECTORIES)
_WORKSPACE_IDENTITIES: dict[str, tuple[int, int]] = {}


class ImageDetachError(RuntimeError):
    pass


class UnsupportedCardFilesystem(ValueError):
    pass


def progress(phase: str, completed: int, total: int, message: str) -> None:
    print(json.dumps({"event": "progress", "phase": phase, "completed": completed,
                      "total": total, "message": message}, separators=(",", ":")),
          file=sys.stderr, flush=True)


def stream_sha256(source: BinaryIO, size: int, phase: str = "verify") -> str:
    source.seek(0)
    digest = hashlib.sha256()
    completed = 0
    progress(phase, 0, size, "Verifying bytes")
    while completed < size:
        block = source.read(min(CHUNK, size - completed))
        if not block:
            raise OSError("The device ended before the expected byte count.")
        digest.update(block)
        completed += len(block)
        if completed % PROGRESS_BYTES == 0 or completed == size:
            progress(phase, completed, size, "Verifying bytes")
    return digest.hexdigest()


def workspace(root: Path) -> Path:
    locator = str(Path(root).absolute())
    root = Path(root).resolve(strict=True)
    if not root.is_dir() or not os.access(root, os.R_OK | os.W_OK | os.X_OK):
        raise ValueError("Choose an available writable library folder.")
    for folder in (root, *root.parents):
        git = folder / '.git'
        source_archive = ((folder / 'release.json').is_file()
                          and (folder / 'library_manager.py').is_file()
                          and (folder / 'app/CartShelf.swift').is_file())
        if git.exists() or git.is_symlink() or source_archive:
            raise ValueError("Choose a library folder outside Git repositories and CartShelf source folders.")
    current = root.stat()
    identity = (current.st_dev, current.st_ino)
    keys = {locator, str(root)}
    if any(key in _WORKSPACE_IDENTITIES and _WORKSPACE_IDENTITIES[key] != identity for key in keys):
        raise ValueError("The chosen library filesystem or folder changed during this operation. Reconnect the library.")
    for key in keys:
        _WORKSPACE_IDENTITIES[key] = identity
    return root


def library_identity(root: Path) -> dict:
    root = workspace(root)
    info = root.stat()
    return {'path': str(root), 'device': info.st_dev, 'inode': info.st_ino}


def managed(root: Path, path: Path) -> Path:
    root = workspace(root)
    resolved = path.resolve()
    if not resolved.is_relative_to(root):
        raise ValueError("This operation must stay inside the selected library.")
    return resolved


def sha256(path: Path, length: int | None = None) -> str:
    digest = hashlib.sha256()
    with path.open("rb", buffering=0) as source:
        remaining = length
        while remaining is None or remaining:
            block = source.read(CHUNK if remaining is None else min(CHUNK, remaining))
            if not block:
                if remaining:
                    raise OSError("The device ended before the expected byte count.")
                break
            digest.update(block)
            if remaining is not None:
                remaining -= len(block)
    return digest.hexdigest()


def command(args: list[str], *, plist: bool = False, temp_dir: Path | None = None) -> dict | bytes:
    environment = os.environ.copy()
    environment["COPYFILE_DISABLE"] = "1"
    if temp_dir is not None:
        environment["TMPDIR"] = str(temp_dir)
    result = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            check=False, env=environment)
    if result.returncode:
        error = result.stderr.decode("utf-8", "replace").strip()
        if not error and plist:
            with contextlib.suppress(ValueError, plistlib.InvalidFileException):
                error = plistlib.loads(result.stdout).get("ErrorMessage", "")
        raise RuntimeError(error or "The disk operation failed.")
    return plistlib.loads(result.stdout) if plist else result.stdout


def disk_info(target: str) -> dict:
    return command([DISKUTIL, "info", "-plist", target], plist=True)


def disk_description(identifier: str) -> dict:
    if not DISK.fullmatch(identifier):
        raise ValueError("Choose a whole physical disk.")
    cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
    da = ctypes.CDLL("/System/Library/Frameworks/DiskArbitration.framework/DiskArbitration")
    pointer = ctypes.c_void_p
    pointers = ctypes.POINTER(pointer)
    for library, name, result, arguments in [
        (da, "DASessionCreate", pointer, [pointer]),
        (da, "DADiskCreateFromBSDName", pointer, [pointer, pointer, ctypes.c_char_p]),
        (da, "DADiskCopyDescription", pointer, [pointer]),
        (cf, "CFDictionaryGetValue", pointer, [pointer, pointer]),
        (cf, "CFDictionaryCreate", pointer, [pointer, pointers, pointers, ctypes.c_long, pointer, pointer]),
        (cf, "CFPropertyListCreateData", pointer, [pointer, pointer, ctypes.c_long, ctypes.c_ulong, pointers]),
        (cf, "CFDataGetLength", ctypes.c_long, [pointer]),
        (cf, "CFDataGetBytePtr", pointer, [pointer]),
        (cf, "CFRelease", None, [pointer]),
    ]:
        function = getattr(library, name)
        function.restype = result
        function.argtypes = arguments
    owned = []

    def own(reference):
        if not reference:
            raise RuntimeError("Unable to read the disk's public description.")
        owned.append(reference)
        return reference

    try:
        session = own(da.DASessionCreate(None))
        disk = own(da.DADiskCreateFromBSDName(None, session, identifier.encode("ascii")))
        description = own(da.DADiskCopyDescription(disk))
        keys, values = [], []
        for field in (
            "DeviceInternal", "DeviceProtocol", "MediaRemovable", "MediaWhole", "MediaWritable",
            "MediaBSDName", "MediaSize", "MediaBlockSize", "DeviceModel", "MediaName", "BusPath",
        ):
            key = pointer.in_dll(da, "kDADiskDescription" + field + "Key").value
            if not key:
                raise RuntimeError("The disk description key is unavailable.")
            value = cf.CFDictionaryGetValue(description, key)
            if value:
                keys.append(key)
                values.append(value)
        # The description owns these borrowed values until serialization finishes.
        projected = own(cf.CFDictionaryCreate(None, (pointer * len(keys))(*keys),
                        (pointer * len(values))(*values), len(keys), None, None))
        error = pointer()
        data = cf.CFPropertyListCreateData(None, projected, 200, 0, ctypes.byref(error))
        if error.value:
            owned.append(error.value)
        data = own(data)
        if error.value:
            raise RuntimeError("Unable to serialize the disk's public description.")
        length, contents = cf.CFDataGetLength(data), cf.CFDataGetBytePtr(data)
        if length <= 0 or not contents:
            raise RuntimeError("Unable to serialize the disk's public description.")
        result = plistlib.loads(ctypes.string_at(contents, length))
        if not isinstance(result, dict):
            raise RuntimeError("The disk's public description is malformed.")
        copy_media = getattr(da, "DADiskCopyIOMedia", None)
        if copy_media is not None:
            io = ctypes.CDLL("/System/Library/Frameworks/IOKit.framework/IOKit")
            copy_media.restype, copy_media.argtypes = ctypes.c_uint, [pointer]
            io.IORegistryEntryGetRegistryEntryID.restype = ctypes.c_int
            io.IORegistryEntryGetRegistryEntryID.argtypes = [ctypes.c_uint, ctypes.POINTER(ctypes.c_uint64)]
            io.IOObjectRelease.restype, io.IOObjectRelease.argtypes = ctypes.c_int, [ctypes.c_uint]
            media = copy_media(disk)
            if not media:
                raise RuntimeError("The card's media generation is unavailable.")
            try:
                generation = ctypes.c_uint64()
                if io.IORegistryEntryGetRegistryEntryID(media, ctypes.byref(generation)) or not generation.value:
                    raise RuntimeError("The card's media generation is unavailable.")
                result["IORegistryEntryID"] = generation.value
            finally:
                io.IOObjectRelease(media)
        return result
    finally:
        for reference in reversed(owned):
            cf.CFRelease(reference)


def card_disk_info(listing: dict) -> dict:
    identifier = listing.get("DeviceIdentifier", "")
    description = disk_description(identifier)
    size = description.get("DAMediaSize")
    if (description.get("DAMediaBSDName") != identifier or type(size) is not int
            or type(listing.get("Size")) is not int or size <= 0 or size != listing["Size"]
            or description.get("DAMediaWhole") is not True):
        raise ValueError("The card's identity or size changed during discovery. Scan again.")
    return {
        "DeviceIdentifier": identifier, "WholeDisk": True, "VirtualOrPhysical": "Physical",
        "OSInternalMedia": listing.get("OSInternal"),
        "Internal": description.get("DADeviceInternal"),
        "BusProtocol": description.get("DADeviceProtocol"),
        "RemovableMedia": description.get("DAMediaRemovable"),
        "WritableMedia": description.get("DAMediaWritable"),
        "TotalSize": size, "DeviceBlockSize": description.get("DAMediaBlockSize"),
        "MediaName": description.get("DADeviceModel") or description.get("DAMediaName"),
        "IORegistryEntryName": description.get("DAMediaName"),
        "DeviceTreePath": description.get("DABusPath"),
        "DiskUUID": listing.get("DiskUUID"), "MediaUUID": listing.get("MediaUUID"),
        "IORegistryEntryID": description.get("IORegistryEntryID"),
    }


def physical_disks(info: dict, lookup=None, seen: frozenset[str] = frozenset(),
                   physical_parents: dict[str, str] | None = None) -> set[str]:
    lookup = lookup or disk_info
    identifier = info.get("DeviceIdentifier", "")
    if identifier in seen:
        raise ValueError("Unable to resolve the library's physical disk.")
    visited = seen | {identifier}
    if physical_parents and identifier in physical_parents:
        return {physical_parents[identifier]}

    def resolve(target):
        if physical_parents and target in physical_parents:
            return {physical_parents[target]}
        return physical_disks(lookup(target), lookup, visited, physical_parents)

    stores = info.get("APFSPhysicalStores", [])
    if stores:
        result: set[str] = set()
        for store in stores:
            result |= resolve(store["APFSPhysicalStore"])
        return result
    parent = info.get("ParentWholeDisk")
    if parent and parent != identifier:
        return resolve(parent)
    physical = info.get("VirtualOrPhysical") == "Physical" or info.get("Internal") is True
    if info.get("WholeDisk") is True and physical and DISK.fullmatch(identifier):
        return {identifier}
    raise ValueError("Unable to establish which physical disk must be protected.")


def card_record(info: dict, listing: dict, protected: set[str]) -> dict | None:
    identifier = info.get("DeviceIdentifier", "")
    removable = info.get("RemovableMedia") is True or info.get("Removable") is True
    sd_bus = str(info.get("BusProtocol", "")).casefold() in {"secure digital", "sd"}
    if not (
        DISK.fullmatch(identifier)
        and identifier not in protected
        and info.get("WholeDisk") is True
        and (info.get("Internal") is False or (
            info.get("Internal") is True and sd_bus and removable
            and info.get("OSInternalMedia") is False
        ))
        and info.get("OSInternalMedia") is not True
        and info.get("VirtualOrPhysical") == "Physical"
        and info.get("WritableMedia") is True
        and (removable or sd_bus)
        and int(info.get("TotalSize", 0)) > 0
    ):
        return None
    partitions = listing.get("Partitions", [])
    fingerprint = {
        key: info.get(key) for key in (
            "DeviceIdentifier", "DeviceTreePath", "MediaName", "IORegistryEntryName",
            "BusProtocol", "TotalSize", "DeviceBlockSize", "DiskUUID", "MediaUUID", "IORegistryEntryID",
        )
    }
    fingerprint["partitions"] = [
        {key: part.get(key) for key in ("DiskUUID", "VolumeUUID", "Size", "Content")}
        for part in partitions
    ]
    physical = {key: fingerprint[key] for key in fingerprint if key not in {"DiskUUID", "MediaUUID", "partitions"}}
    return {
        "id": identifier,
        "name": info.get("MediaName") or info.get("IORegistryEntryName") or identifier,
        "size_bytes": int(info["TotalSize"]),
        "block_size": int(info.get("DeviceBlockSize") or 512),
        "mount_points": [part["MountPoint"] for part in partitions if part.get("MountPoint")],
        "identity": hashlib.sha256(json.dumps(fingerprint, sort_keys=True).encode()).hexdigest(),
        "physical_identity": hashlib.sha256(json.dumps(physical, sort_keys=True).encode()).hexdigest(),
        "volume_uuid": partitions[0].get("VolumeUUID") if len(partitions) == 1 else None,
    }


def execution_mounts() -> set[Path]:
    paths = [Path(sys.executable).resolve(strict=True)]
    module_file = globals().get('__file__')
    if module_file and Path(module_file).is_file():
        paths.append(Path(module_file).resolve(strict=True))
    return {next(parent for parent in (path, *path.parents) if parent.is_mount()) for path in paths}


def cards(root: Path) -> dict:
    root = workspace(root)
    mount = next(path for path in (root, *root.parents) if path.is_mount())
    listing = command([DISKUTIL, "list", "-plist", "physical"], plist=True)
    physical_parents = {
        child["DeviceIdentifier"]: disk["DeviceIdentifier"]
        for disk in listing.get("AllDisksAndPartitions", [])
        if DISK.fullmatch(disk["DeviceIdentifier"])
        for child in [disk, *disk.get("Partitions", [])]
    }
    protected_mounts = list(dict.fromkeys([mount, Path('/'), *sorted(execution_mounts())]))
    protected = set()
    for protected_mount in protected_mounts:
        protected |= physical_disks(disk_info(str(protected_mount)), physical_parents=physical_parents)
    found = []
    for disk in listing.get("AllDisksAndPartitions", []):
        if disk["DeviceIdentifier"] in protected:
            continue
        info = card_disk_info(disk)
        parts = disk.get('Partitions', [])
        if (len(parts) == 1 and not parts[0].get('VolumeUUID')
                and parts[0].get('Content') in {'DOS_FAT_32', 'Windows_FAT_32', 'Microsoft Basic Data'}
                and re.fullmatch(re.escape(disk['DeviceIdentifier']) + r's[0-9]+', parts[0].get('DeviceIdentifier', ''))):
            volume = disk_info(parts[0]['DeviceIdentifier'])
            if volume.get('FilesystemType') == 'msdos' and volume.get('ParentWholeDisk') == disk['DeviceIdentifier']:
                disk = {**disk, 'Partitions': [{**parts[0], 'VolumeUUID': volume.get('VolumeUUID')}]}
        record = card_record(info, disk, protected)
        if record:
            found.append(record)
    return {
        "cards": found,
        "protected_disks": sorted(protected),
        "message": "Choose a removable SD card." if found else "No removable SD card found. The library drive is protected.",
    }


def current_card(root: Path, identifier: str) -> dict:
    if not DISK.fullmatch(identifier):
        raise ValueError("Choose a whole removable SD card.")
    for card in cards(root)["cards"]:
        if card["id"] == identifier:
            return card
    raise ValueError("That device is not an eligible removable SD card.")


def raw_image_info(path: Path) -> dict:
    if path.is_symlink() or not path.is_file():
        raise ValueError("Choose a regular raw card image.")
    size = path.stat().st_size
    with path.open("rb") as stream:
        mbr = stream.read(512)
        if len(mbr) != 512 or mbr[510:] != b"\x55\xaa" or size % 512:
            raise ValueError("The image is not a raw MBR disk image.")
        entries = [mbr[446 + 16 * i:462 + 16 * i] for i in range(4)]
        partitions = [entry for entry in entries if entry[4] != 0]
        if len(partitions) != 1 or partitions[0][4] not in {0x0B, 0x0C}:
            raise ValueError("The image must contain one FAT32 MBR partition.")
        entry = partitions[0]
        start, sectors = struct.unpack("<II", entry[8:16])
        if not start or not sectors or (start + sectors) * 512 > size:
            raise ValueError("The image partition extends beyond the image file.")
        stream.seek(start * 512)
        boot = stream.read(512)
        if boot[510:512] != b"\x55\xaa" or boot[82:90] != b"FAT32   ":
            raise ValueError("The partition does not contain a FAT32 boot sector.")
        if struct.unpack("<H", boot[11:13])[0] != 512:
            raise ValueError("This image requires an unsupported sector size.")
    return {"size_bytes": size, "sha256": sha256(path), "partition_start": start, "partition_sectors": sectors}


def image_info(root: Path, path: Path) -> dict:
    path = managed(root, path)
    if path.is_dir():
        receipt = restore_file_backup_info(root, path)
        return {"size_bytes": receipt["size_bytes"], "sha256": receipt["sha256"], "kind": "file-backup"}
    if path.parent != root / "backups":
        return {**raw_image_info(path), "kind": "compilation"}
    receipt_path = managed(root, path.with_suffix(".img.json"))
    if not path.is_file() or not receipt_path.is_file():
        raise ValueError("A card backup requires its verified CartShelf receipt.")
    receipt = json.loads(receipt_path.read_text())
    if not isinstance(receipt, dict):
        raise ValueError("The card backup receipt is malformed.")
    size = path.stat().st_size
    expected = receipt.get("sha256", "")
    if (receipt.get("verified") is not True or receipt.get("backup_path") != str(path)
            or type(receipt.get("size_bytes")) is not int or receipt["size_bytes"] != size
            or size <= 0 or size % 512 or not isinstance(expected, str)
            or not re.fullmatch(r"[a-f0-9]{64}", expected)):
        raise ValueError("The card backup receipt does not match this image.")
    actual = sha256(path)
    if not secrets.compare_digest(actual, expected):
        raise ValueError("The verified card backup changed. It cannot be restored.")
    return {"size_bytes": size, "sha256": actual, "kind": "backup"}


def file_relative(name: str) -> Path:
    if not isinstance(name, str):
        raise ValueError("A file-backup path is malformed.")
    relative = Path(name)
    if (not name or name in {".", ".."} or relative.is_absolute() or relative.as_posix() != name
            or any(part in {".", ".."} or re.search(r'[<>:"/\\|?*\x00-\x1f]', part)
                   or len(part.encode("utf-16-le")) > 510
                   or (part.endswith((" ", ".")) and not (name == "._." and part == "._."))
                   for part in relative.parts)):
        raise ValueError("A file-backup path is not safe for FAT32: " + name)
    return relative


def file_inventory(folder: Path, *, exclude_root_system: bool = False) -> dict:
    if folder.is_symlink() or not folder.is_dir():
        raise ValueError("File backup requires a real directory.")
    files, directories, folded, excluded = [], [], set(), []

    def visit(directory):
        with os.scandir(directory) as entries:
            children = sorted(entries, key=lambda entry: entry.name)
        for entry in children:
            path = Path(entry.path)
            name = path.relative_to(folder).as_posix()
            file_relative(name)
            key = name.casefold()
            if key in folded:
                raise ValueError("File names collide on FAT32: " + name)
            folded.add(key)
            info = entry.stat(follow_symlinks=False)
            row = {"path": name, "mtime_ns": info.st_mtime_ns, "inode": info.st_ino}
            system_root = directory == folder and key in MACOS_SYSTEM_DIRECTORIES_FOLDED
            if exclude_root_system and system_root and stat.S_ISREG(info.st_mode):
                raise ValueError("A regular card file uses a reserved macOS root folder name: " + name
                                 + ". Choose a whole card image to preserve this card. Nothing was written.")
            if stat.S_ISDIR(info.st_mode):
                if exclude_root_system and system_root:
                    excluded.append(name)
                    continue
                directories.append(row)
                visit(path)
            elif stat.S_ISREG(info.st_mode) and info.st_size < 4 * 1024**3:
                files.append({**row, "bytes": info.st_size})
            else:
                raise ValueError("File backup cannot copy a link, special file or oversized file: " + name)

    visit(folder)
    return {"files": sorted(files, key=lambda row: row["path"]),
            "directories": sorted(directories, key=lambda row: row["path"]),
            **({"excluded_system_paths": sorted(excluded, key=str.casefold)} if exclude_root_system else {})}


def verify_file_records(folder: Path, files: list[dict], directories: list[dict]) -> None:
    observed = file_inventory(folder)
    folded = set()
    for key, rows, fields in (("files", files, ("path", "bytes", "mtime_ns")),
                              ("directories", directories, ("path", "mtime_ns"))):
        if not isinstance(rows, list):
            raise ValueError("An all-files backup inventory is malformed.")
        for row in rows:
            if not isinstance(row, dict) or any(field not in row for field in fields):
                raise ValueError("An all-files backup record is malformed.")
            file_relative(row["path"])
            if (row["path"].casefold() in folded or type(row["mtime_ns"]) is not int
                    or not -(2**63) <= row["mtime_ns"] < 2**63):
                raise ValueError("An all-files backup timestamp or file name is invalid.")
            folded.add(row["path"].casefold())
        if [{field: row[field] for field in fields} for row in observed[key]] != [{field: row[field] for field in fields} for row in rows]:
            raise ValueError("The all-files backup inventory or timestamps changed.")
    for row in files:
        if (type(row["bytes"]) is not int or not 0 <= row["bytes"] < 4 * 1024**3
                or not re.fullmatch(r"[a-f0-9]{64}", str(row.get("sha256", "")))):
            raise ValueError("An all-files backup record is invalid.")
        if sha256(folder / row["path"]) != row["sha256"]:
            raise ValueError("An all-files backup file changed: " + row["path"])


@contextlib.contextmanager
def uncached_file(path: Path):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    with os.fdopen(descriptor, "rb", buffering=0) as source:
        fcntl.fcntl(source.fileno(), 48, 1)
        fcntl.fcntl(source.fileno(), 45, 0)
        yield source


def card_file_volume(root: Path, card: dict, partition: str | None = None,
                     volume_uuid: str | None = None, *, readonly: bool = False) -> tuple[dict, Path | None]:
    current = same_physical_card(root, card)
    if current["identity"] != card["identity"]:
        raise ValueError("The source card partition changed during file backup.")
    listing = command([DISKUTIL, "list", "-plist", "/dev/" + card["id"]], plist=True)
    disks = listing.get("AllDisksAndPartitions", [])
    parts = disks[0].get("Partitions", []) if len(disks) == 1 and disks[0].get("DeviceIdentifier") == card["id"] else []
    if len(parts) != 1 or not re.fullmatch(re.escape(card["id"]) + r"s[0-9]+", parts[0].get("DeviceIdentifier", "")):
        raise UnsupportedCardFilesystem("Card Files backup requires one readable FAT volume. Choose Back Up Only with Whole Card Image for other layouts.")
    identifier = parts[0]["DeviceIdentifier"]
    info = disk_info(identifier)
    if (info.get("ParentWholeDisk") != card["id"]
            or (partition is not None and identifier != partition)
            or (partition is not None and info.get("VolumeUUID") != volume_uuid)):
        raise ValueError("The source volume changed. Review the card again.")
    if info.get("FilesystemType") != "msdos":
        raise UnsupportedCardFilesystem("Card Files backup requires a readable FAT volume. Choose Back Up Only with Whole Card Image for other filesystems.")
    mount = Path(info["MountPoint"]) if info.get("MountPoint") else None
    if readonly and (info.get("WritableVolume") is not False or mount is None
                     or mount.parent != Path("/Volumes") or mount.is_symlink() or not mount.is_dir()):
        raise ValueError("The source card did not mount read-only at the expected volume.")
    return info, mount


@contextlib.contextmanager
def readonly_card_contents(root: Path, card: dict):
    before, _ = card_file_volume(root, card)
    partition, volume_uuid = before["DeviceIdentifier"], before.get("VolumeUUID")
    unmount_card(root, card)
    card_file_volume(root, card, partition, volume_uuid)
    command([DISKUTIL, "mount", "readOnly", partition])
    try:
        _, mount = card_file_volume(root, card, partition, volume_uuid, readonly=True)
        yield mount, lambda: card_file_volume(root, card, partition, volume_uuid, readonly=True)
    finally:
        same_physical_card(root, card)
        unmount_card(root, card)


def backup_all_files(root: Path, card: dict, destination: Path) -> dict:
    destination = managed(root, destination)
    work = destination.with_name(destination.name + ".partial")
    if destination.parent != root / "backups" or destination.suffix != ".files" or destination.exists() or work.exists():
        raise ValueError("Choose an unused all-files backup destination.")
    with readonly_card_contents(root, card) as (mount, guard):
        inventory = file_inventory(mount, exclude_root_system=True)
        total = sum(row["bytes"] for row in inventory["files"])
        check_capacity(root, card, backup_bytes=total + 4096 * (len(inventory["files"]) + len(inventory["directories"])))
        data = media_directory(root, work / "files")
        for row in inventory["directories"]:
            media_directory(root, data / row["path"])
        records = []
        for index, row in enumerate(inventory["files"], 1):
            guard()
            source, target = mount / row["path"], data / row["path"]
            with uncached_file(source) as src, target.open("xb", buffering=0) as dst:
                inherit_owner(dst.fileno(), target.parent)
                digest = copy_stream(src, dst, row["bytes"], "backup-files")
            if target.stat().st_size != row["bytes"] or sha256(target) != digest:
                raise OSError("An all-files backup copy did not verify: " + row["path"])
            os.utime(target, ns=(row["mtime_ns"], row["mtime_ns"]), follow_symlinks=False)
            with target.open("rb") as saved:
                os.fsync(saved.fileno())
            records.append({key: row[key] for key in ("path", "bytes", "mtime_ns")} | {"sha256": digest})
            progress("backup-files", index, len(inventory["files"]), "Preserving " + row["path"])
        for row in records:
            guard()
            with uncached_file(mount / row["path"]) as source:
                if stream_sha256(source, row["bytes"], "verify-source-files") != row["sha256"]:
                    raise OSError("A source file changed during backup: " + row["path"])
            if sha256(data / row["path"]) != row["sha256"]:
                raise OSError("A backed-up file changed during verification.")
        if file_inventory(mount, exclude_root_system=True) != inventory:
            raise OSError("The source card inventory changed during backup.")
        guard()
        directories = [{key: row[key] for key in ("path", "mtime_ns")} for row in inventory["directories"]]
        for row in sorted(directories, key=lambda row: len(Path(row["path"]).parts), reverse=True):
            os.utime(data / row["path"], ns=(row["mtime_ns"], row["mtime_ns"]), follow_symlinks=False)
        verify_file_records(data, records, directories)
    receipt = {"schema": 1, "kind": "files", "backup_kind": "files", "backup_path": str(destination),
               "scope": CARD_FILE_SCOPE, "excluded_system_paths": inventory["excluded_system_paths"],
               "device": card, "size_bytes": total, "files": records, "directories": directories,
               "created_at": dt.datetime.now(dt.timezone.utc).isoformat(), "verified": True,
               "verification": "Two uncached source reads, destination rehash and unchanged source inventory."}
    write_json(work / "MANIFEST.json", receipt)
    (work / "MANIFEST.sha256").write_text(sha256(work / "MANIFEST.json") + "\n")
    with (work / "MANIFEST.sha256").open("rb") as stream:
        os.fsync(stream.fileno())
    for directory in sorted([work, data, *(data / row["path"] for row in directories)], key=lambda path: len(path.parts), reverse=True):
        descriptor = os.open(directory, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    work.rename(destination)
    descriptor = os.open(destination.parent, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return file_backup_info(root, destination)


def file_backup_info(root: Path, path: Path) -> dict:
    path = managed(root, path)
    if path.parent != root / "backups" or path.suffix != ".files" or path.is_symlink() or not path.is_dir():
        raise ValueError("Choose a published CartShelf .files backup, not a partial snapshot.")
    manifest, checksum = path / "MANIFEST.json", path / "MANIFEST.sha256"
    if manifest.is_symlink() or checksum.is_symlink() or not manifest.is_file() or not checksum.is_file():
        raise ValueError("The all-files backup is incomplete.")
    digest = sha256(manifest)
    if checksum.read_text().strip() != digest:
        raise ValueError("The all-files backup manifest changed.")
    receipt = json.loads(manifest.read_text())
    if (not isinstance(receipt, dict) or receipt.get("schema") != 1 or receipt.get("kind") != "files"
            or receipt.get("verified") is not True or receipt.get("backup_path") != str(path)
            or not isinstance(receipt.get("files"), list) or not isinstance(receipt.get("directories"), list)):
        raise ValueError("The all-files backup receipt is malformed.")
    if "scope" in receipt:
        excluded = receipt.get("excluded_system_paths")
        if (receipt["scope"] != CARD_FILE_SCOPE or not isinstance(excluded, list)
                or any(not isinstance(name, str) or name.casefold() not in MACOS_SYSTEM_DIRECTORIES_FOLDED for name in excluded)
                or len({name.casefold() for name in excluded}) != len(excluded)
                or excluded != sorted(excluded, key=str.casefold)):
            raise ValueError("The card-files backup scope or exclusions are invalid.")
    elif "excluded_system_paths" in receipt:
        raise ValueError("The file backup exclusions require a recognized scope.")
    verify_file_records(path / "files", receipt["files"], receipt["directories"])
    if "scope" in receipt and any(Path(row["path"]).parts[0].casefold() in MACOS_SYSTEM_DIRECTORIES_FOLDED
                                   for key in ("files", "directories") for row in receipt[key]):
        raise ValueError("A scoped card-files backup cannot record protected macOS root paths.")
    total = sum(row["bytes"] for row in receipt["files"])
    if type(receipt.get("size_bytes")) is not int or receipt["size_bytes"] != total:
        raise ValueError("The all-files backup byte count changed.")
    return {**receipt, "sha256": digest,
            **({"backup_scope": receipt["scope"]} if "scope" in receipt else {})}


def restore_file_backup_info(root: Path, path: Path) -> dict:
    receipt = file_backup_info(root, path)
    protected = sorted({Path(row["path"]).parts[0] for key in ("files", "directories") for row in receipt[key]
                        if Path(row["path"]).parts[0].casefold() in MACOS_SYSTEM_DIRECTORIES_FOLDED}, key=str.casefold)
    if protected:
        raise ValueError("This file backup records protected macOS root folders or file names that macOS cannot recreate: "
                         + ", ".join(protected) + ". Use a whole card image to restore protected system metadata. Nothing was written.")
    return receipt


@contextlib.contextmanager
def backup_contents(root: Path, backup: Path):
    if backup.is_dir():
        file_backup_info(root, backup)
        yield backup / "files"
    else:
        with mounted_image(backup, readonly=True) as mount:
            yield mount


def verify_backup(root: Path, backup: Path, receipt: dict) -> None:
    if receipt.get("backup_kind") == "files":
        actual = file_backup_info(root, backup)
        valid = actual["sha256"] == receipt["sha256"]
    else:
        valid = receipt.get("verified") is True and sha256(backup, receipt["size_bytes"]) == receipt["sha256"]
    if not valid:
        raise OSError("The completed backup changed. Nothing was written.")


def pack_files(pack: Path) -> list[dict]:
    files = []
    folded = set()
    for path in sorted(pack.rglob("*")):
        if path.is_symlink():
            raise ValueError("Pack images cannot contain symbolic links.")
        if not path.is_file():
            continue
        relative = path.relative_to(pack)
        if any(part.startswith(".") for part in relative.parts):
            continue
        if any(re.search(r'[<>:"/\\|?*\x00-\x1f]', part) or part.endswith((" ", ".")) for part in relative.parts):
            raise ValueError("A pack filename is not supported by FAT32: " + str(relative))
        key = str(relative).casefold()
        if key in folded:
            raise ValueError("Two pack filenames collide on FAT32: " + str(relative))
        folded.add(key)
        size = path.stat().st_size
        if size >= 4 * 1024**3:
            raise ValueError("FAT32 cannot hold a file of 4 GiB or larger: " + str(relative))
        if path.suffix.casefold() in {".sav", ".srm", ".ram", ".rtc", ".eep", ".fla", ".mpk"}:
            raise ValueError("Game images must not contain historical save files.")
        files.append({"path": relative.as_posix(), "bytes": size, "sha256": sha256(path)})
    if not files:
        raise ValueError("The selected pack is empty.")
    manifest = pack / "MANIFEST.json"
    if manifest.is_file():
        expected = json.loads(manifest.read_text())["files"]
        actual = {record["path"]: record for record in files if record["path"] != "MANIFEST.json"}
        if len(expected) != len(actual) or {record["path"] for record in expected} != set(actual):
            raise ValueError("The pack manifest does not match its files.")
        for record in expected:
            if record != actual[record["path"]]:
                raise ValueError("A pack file no longer matches its manifest: " + record["path"])
    return files


def verify_files(folder: Path, files: list[dict]) -> None:
    for record in files:
        path = folder / record["path"]
        if path.stat().st_size != record["bytes"] or sha256(path) != record["sha256"]:
            raise OSError("Image verification failed: " + record["path"])


@contextlib.contextmanager
def mounted_image(image: Path, *, readonly: bool = False):
    mount = Path("/Volumes") / ("CartShelf-" + uuid.uuid4().hex)
    args = [HDIUTIL, "attach", "-nobrowse", "-noautoopen", "-owners", "off",
            "-mountpoint", str(mount), "-plist"]
    if readonly:
        args.append("-readonly")
    result = command(args + [str(image)], plist=True, temp_dir=image.parent)
    entities = result.get("system-entities", [])
    device = next((item["dev-entry"] for item in entities if re.fullmatch(r"/dev/disk[0-9]+", item.get("dev-entry", ""))), None)
    if not device:
        partition = next((re.match(r"(/dev/disk[0-9]+)s[0-9]+\Z", item.get("dev-entry", ""))
                          for item in entities if re.match(r"/dev/disk[0-9]+s[0-9]+\Z", item.get("dev-entry", ""))), None)
        device = partition.group(1) if partition else None
    if not device:
        raise RuntimeError("The image did not attach as a whole virtual disk.")
    try:
        mounted = [item for item in entities if item.get("mount-point") == str(mount)]
        if len(mounted) != 1:
            raise RuntimeError("The image did not mount at the expected location.")
        yield mount
    finally:
        try:
            command([HDIUTIL, "detach", device], temp_dir=image.parent)
        except RuntimeError:
            try:
                command([HDIUTIL, "detach", "-force", device], temp_dir=image.parent)
            except RuntimeError as error:
                raise ImageDetachError("The temporary image could not detach. Its working file is retained at " + str(image)) from error


def publish(source: Path, destination: Path) -> None:
    os.link(source, destination)
    source.unlink()


def write_json(path: Path, data: dict, *, replace: bool = False) -> None:
    descriptor, name = tempfile.mkstemp(prefix="." + path.name + ".", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w") as stream:
            inherit_owner(stream.fileno(), path.parent)
            json.dump(data, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        if replace:
            temporary.replace(path)
        else:
            publish(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def inherit_owner(descriptor: int, parent: Path) -> None:
    if os.geteuid() == 0:
        owner = parent.stat()
        os.fchown(descriptor, owner.st_uid, owner.st_gid)


def media_directory(root: Path, path: Path) -> Path:
    path = managed(root, path)
    missing = []
    ancestor = path
    while ancestor != root and not ancestor.exists():
        missing.append(ancestor)
        ancestor = ancestor.parent
    for directory in reversed(missing):
        directory.mkdir()
    if os.geteuid() == 0:
        owner = root.stat()
        for directory in set(missing) | {path, root / "library"}:
            if directory.exists():
                os.chown(directory, owner.st_uid, owner.st_gid, follow_symlinks=False)
    return path


def plan_token(plan: dict) -> str:
    facts = {key: value for key, value in plan.items() if key != "token"}
    return hashlib.sha256(json.dumps(facts, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


@contextlib.contextmanager
def card_lock(root: Path):
    directory = media_directory(root, root / "library/card-plans")
    with (directory / "operation.lock").open("a") as stream:
        inherit_owner(stream.fileno(), directory)
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RuntimeError("Another card backup or replacement is already in progress.") from error
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def create_image(root: Path, pack_folder: Path) -> dict:
    root = workspace(root)
    pack = managed(root, pack_folder)
    if not pack.is_dir():
        raise ValueError("Choose a prepared pack folder.")
    _, files = compilation_files(pack)
    exports = managed(root, root / "exports")
    exports.mkdir(exist_ok=True)
    destination = exports / (pack.name + ".img")
    receipt_path = destination.with_suffix(".img.json")
    checksum_path = destination.with_suffix(".img.sha256")
    if any(path.exists() for path in (destination, receipt_path, checksum_path)):
        raise FileExistsError("This image already exists. Prepare a pack with a new name.")
    payload = sum(record["bytes"] for record in files)
    size_mib = max(128, math.ceil((payload + max(64 * MIB, payload // 8)) / (64 * MIB)) * 64)
    if shutil.disk_usage(root).free < size_mib * MIB + 64 * MIB:
        raise OSError("The library drive does not have enough free space for this image.")
    temporary = managed(root, root / "tmp")
    temporary.mkdir(exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="card-image-", dir=temporary))
    image = work / "disk.cdr"
    published = []
    try:
        progress("format-image", 0, size_mib * MIB, "Creating FAT32 image")
        command([HDIUTIL, "create", "-size", f"{size_mib}m", "-fs", "MS-DOS FAT32",
                 "-layout", "MBRSPUD", "-volname", "CARTSHELF", "-type", "UDTO",
                 str(image)], temp_dir=work)
        with mounted_image(image) as mount:
            for index, record in enumerate(files, 1):
                target = mount / record["path"]
                target.parent.mkdir(parents=True, exist_ok=True)
                with (pack / record["path"]).open("rb") as source, target.open("xb") as output:
                    shutil.copyfileobj(source, output, CHUNK)
                    output.flush()
                    os.fsync(output.fileno())
                progress("fill-image", index, len(files), "Copying " + record["path"])
            verify_files(mount, files)
            remove_system_files(mount)
        with mounted_image(image, readonly=True) as mount:
            verify_files(mount, files)
            if any(path.name.startswith("._") or path.name in {".DS_Store", ".Spotlight-V100"}
                   for path in mount.rglob("*")):
                raise OSError("macOS metadata files remain in the image.")
        progress("verify-image", 0, image.stat().st_size, "Verifying raw image")
        info = raw_image_info(image)
        receipt = {
            "image_path": str(destination), **info, "pack_path": str(pack),
            "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "file_count": len(files), "payload_bytes": payload,
            "message": "Built and read-back verified a portable FAT32 card image.",
        }
        write_json(work / "receipt.json", receipt)
        (work / "checksum.txt").write_text(info["sha256"] + "  " + destination.name + "\n")
        for source, target in ((work / "receipt.json", receipt_path),
                               (work / "checksum.txt", checksum_path), (image, destination)):
            publish(source, target)
            published.append(target)
        progress("complete", info["size_bytes"], info["size_bytes"], "SD image verified")
        return receipt
    except BaseException:
        for path in published:
            path.unlink(missing_ok=True)
        raise
    finally:
        if not isinstance(sys.exc_info()[1], ImageDetachError):
            shutil.rmtree(work)


def remove_system_files(folder: Path) -> None:
    for path in sorted(folder.rglob("*"), key=lambda path: len(path.parts), reverse=True):
        if path.name.startswith("._") or path.name in {".DS_Store", ".Spotlight-V100", ".fseventsd", ".Trashes"}:
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
            else:
                path.unlink(missing_ok=True)


def check_capacity(root: Path, card: dict, image_size: int = 0, *, backup_bytes: int | None = None) -> None:
    if card.get("block_size", 512) not in {512, 1024, 2048, 4096}:
        raise ValueError("The selected card has unsupported sector geometry.")
    if image_size > card["size_bytes"]:
        raise ValueError("The selected image is larger than this card.")
    required = card["size_bytes"] if backup_bytes is None else backup_bytes
    if shutil.disk_usage(root).free < required + 64 * MIB:
        raise OSError("The library drive needs enough free space for the selected backup before replacement.")


def save_choices_json(value: str) -> list[dict]:
    import card_saves

    if len(value.encode('utf-8')) > 1024 * 1024:
        raise ValueError('Save choices exceed the supported bound.')
    try:
        return card_saves.validate_choices(json.loads(value))
    except (TypeError, json.JSONDecodeError) as error:
        raise ValueError('Save choices must be a bounded JSON array of exact bundles.') from error


def _preview_games(folder: Path, profile: str, excluded=()) -> list[dict]:
    import card_saves

    rows = {}
    paths = card_saves._files(folder, excluded)
    games = [path for path in paths if path.suffix.casefold() in card_saves.ROM_EXTENSIONS[profile]]
    for index, path in enumerate(games, 1):
        progress('review-game-files', index - 1, len(games), 'Reviewing ' + path.name)
        warning = ''
        try:
            digest, _ = card_saves._rom(path, profile)
        except ValueError as error:
            digest = sha256(path)
            warning = str(error)
        stem = path.stem
        match = re.search(r'\((?:v|V)([0-9][^)]*)\)|\(Rev[- ]?([^)]*)\)', stem)
        row = {'identity_sha256': digest, 'title': re.split(r'\s*[\[(]', stem, maxsplit=1)[0] or stem,
               'version': (match.group(1) or 'Rev ' + match.group(2)) if match else 'Unknown',
               'path': path.relative_to(folder).as_posix()}
        if warning:
            row['warning'] = warning
        rows.setdefault(digest, row)
    progress('review-game-files', len(games), len(games), 'Reviewed exact game identities')
    return sorted(rows.values(), key=lambda row: (row['title'].casefold(), row['identity_sha256']))


def _preview_saves(current: Path, restore: dict) -> dict:
    saves = {kind: [] for kind in ('current', 'history', 'selected_earlier')}
    for row in restore['files']:
        saves[row['source_kind']].append({key: row[key] for key in ('path', 'identity_sha256', 'source_kind', 'bytes', 'sha256')})
    restored_sources = {(row['source_save_path'], row['sha256']) for row in restore['files']}
    saves['kept_in_backup'] = []
    for row in restore['skipped']:
        source = current / file_relative(row['path'])
        if (row['path'], sha256(source)) not in restored_sources:
            saves['kept_in_backup'].append({**row, 'source_kind': 'kept_in_backup',
                                           'bytes': source.stat().st_size})
    saves['warnings'] = restore['warnings']
    return saves


def replacement_preview(root: Path, current: Path, pack: Path, profile: str, choices=()) -> dict:
    import card_saves

    excluded = MACOS_SYSTEM_DIRECTORIES
    source_inventory = file_inventory(current, exclude_root_system=True)
    old = _preview_games(current, profile, excluded)
    incoming = _preview_games(pack, profile)
    old_ids = {row['identity_sha256'] for row in old}
    new_ids = {row['identity_sha256'] for row in incoming}
    history = root / 'backups/save-history'
    managed(root, history)
    restore = card_saves.build_restore_plan(current, pack, profile, history,
                                           archive=False, choices=choices, excluded_root_names=excluded)
    saves = _preview_saves(current, restore)
    save_facts = []
    for path in card_saves._files(current, excluded):
        relative = path.relative_to(current).as_posix()
        if save_path(relative) or path.suffix.casefold() in card_saves.SAVE_EXTENSIONS:
            save_facts.append({'path': relative, 'bytes': path.stat().st_size, 'sha256': sha256(path)})
    facts = {'source_files': [{key: row[key] for key in ('path', 'bytes', 'mtime_ns')}
                             for row in source_inventory['files']],
             'games': old, 'saves': save_facts, 'restore': saves}
    firmware = []
    _, files = compilation_files(pack)
    for row in files:
        path = row['path']
        if path.casefold().endswith('.efu') or path.casefold() == 'gbcsys/gbcos.bin':
            version = Path(path).stem if path.casefold().endswith('.efu') else 'GB X7 OS, exact file below'
            firmware.append({**row, 'version': version, 'version_source': 'filename' if path.casefold().endswith('.efu') else 'file hash'})
    return {'profile': profile, 'games': {
                'added': [row for row in incoming if row['identity_sha256'] not in old_ids],
                'retained': [row for row in incoming if row['identity_sha256'] in old_ids],
                'removed': [row for row in old if row['identity_sha256'] not in new_ids]},
            'saves': saves,
            'source_fingerprint': hashlib.sha256(json.dumps(facts, sort_keys=True, separators=(',', ':')).encode()).hexdigest(),
            'firmware': {'files_in_pack': firmware, 'installed_cartridge_version': None, 'installed_status': 'unknown'}}


def plan_preview(root: Path, card: dict, image: Path, info: dict, choices=(), *, backup_kind="files") -> dict:
    if info['kind'] != 'compilation':
        if choices:
            raise ValueError('Explicit save choices apply only to game packs.')
        return {'kind': info['kind'], 'profile': None,
                'message': 'Restores the recorded card snapshot. Saves are not remapped.',
                'games': {'added': [], 'retained': [], 'removed': []},
                'saves': {'current': [], 'history': [], 'selected_earlier': [], 'kept_in_backup': [], 'warnings': []},
                'firmware': {'files_in_pack': [], 'installed_cartridge_version': None, 'installed_status': 'unknown'}}
    with contextlib.ExitStack() as contexts:
        try:
            prior, guard = contexts.enter_context(readonly_card_contents(root, card))
        except UnsupportedCardFilesystem as error:
            mode = ' Whole Card Image is selected, but sector backup does not make this layout readable for preview.' if backup_kind == 'raw' else ''
            raise ValueError('Fresh game-pack review in this beta requires one readable FAT volume.' + mode
                             + ' Use Back Up Only with Whole Card Image to preserve the card bytes before preparing a FAT32 volume, then review the pack again.') from error
        pack = contexts.enter_context(mounted_image(image, readonly=True))
        profile, _ = compilation_files(pack)
        guard()
        preview = replacement_preview(root, prior, pack, profile, choices)
        guard()
        return preview


def plan_install(root: Path, image: Path, identifier: str, backup_kind: str = "files", *, save_choices=()) -> dict:
    import card_saves

    root = workspace(root)
    save_choices = card_saves.validate_choices(save_choices)
    image = managed(root, image)
    info = image_info(root, image)
    card = current_card(root, identifier)
    if backup_kind not in {"files", "raw"}:
        raise ValueError("Choose card-files backup or whole card image.")
    if info["kind"] == "backup":
        backup_kind = "raw"
    check_capacity(root, card, info["size_bytes"], backup_bytes=0 if backup_kind == "files" else None)
    preview = plan_preview(root, card, image, info, save_choices, backup_kind=backup_kind)
    plans = media_directory(root, root / "library/card-plans")
    backups = media_directory(root, root / "backups")
    operation = str(uuid.uuid4())
    path = plans / (operation + ".json")
    plan = {
        "plan_path": str(path), "nonce": secrets.token_urlsafe(32), "device": card,
        "library_identity": library_identity(root),
        "backup_path": str(backups / (f"{identifier}-{operation}." + ("files" if backup_kind == "files" else "img"))),
        "backup_kind": backup_kind, "save_choices": save_choices, "preview": preview,
        "image_path": str(image), "image_sha256": info["sha256"], "image_size_bytes": info["size_bytes"],
        "image_kind": info["kind"],
        "install_mode": "raw-backup" if info["kind"] == "backup" else "full-capacity-files",
        "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "message": ("This restores a complete verified card backup" if info["kind"] == "backup" else "This formats the full card as FAT32, installs the pack and restores saves for exact matching ROMs")
                   + (" after a verified card-files backup, excluding root macOS Spotlight, event-log and Trash folders." if backup_kind == "files" else " after a complete verified raw card backup.")
                   + " Review the exact device, source and backup before continuing.",
    }
    if backup_kind == "files":
        plan["backup_scope"] = CARD_FILE_SCOPE
    plan["token"] = plan_token(plan)
    write_json(path, plan)
    return plan


def validate_plan(root: Path, plan: dict, token: str) -> tuple[dict, Path, Path]:
    import card_saves

    if plan.get('image_kind') == 'compilation':
        if not isinstance(plan.get('preview'), dict) or 'save_choices' not in plan:
            raise ValueError('This review predates the game and save preview. Review the card again.')
        card_saves.validate_choices(plan['save_choices'])
    expected = plan_token(plan)
    if not secrets.compare_digest(str(plan.get("token", "")), token) or not secrets.compare_digest(expected, token):
        raise ValueError("The card replacement confirmation does not match this plan.")
    if plan.get('library_identity') != library_identity(root):
        raise ValueError('The library folder or filesystem changed after review. Reconnect the original library and review the card again.')
    created = dt.datetime.fromisoformat(plan["created_at"])
    if abs((dt.datetime.now(dt.timezone.utc) - created).total_seconds()) > 600:
        raise ValueError("The card replacement review expired. Review the card again.")
    card = current_card(root, plan["device"]["id"])
    if card["identity"] != plan["device"]["identity"] or card["size_bytes"] != plan["device"]["size_bytes"]:
        raise ValueError("The selected card changed. Review a new replacement plan.")
    image = managed(root, Path(plan["image_path"]))
    info = image_info(root, image)
    if (info["sha256"] != plan["image_sha256"] or info["size_bytes"] != plan["image_size_bytes"]
            or info["kind"] != plan.get("image_kind")):
        raise ValueError("The image changed after review. Prepare a new replacement plan.")
    mode = "raw-backup" if info["kind"] == "backup" else "full-capacity-files"
    if plan.get("install_mode") != mode:
        raise ValueError("The installation method changed. Review a new card replacement plan.")
    backup = managed(root, Path(plan["backup_path"]))
    backup_kind = plan.get("backup_kind")
    if backup_kind not in {"files", "raw"} or (info["kind"] == "backup" and backup_kind != "raw"):
        raise ValueError("The backup method changed. Review a new replacement plan.")
    if backup_kind == "files" and plan.get("backup_scope") != CARD_FILE_SCOPE:
        raise ValueError("The backup scope changed. Review a new replacement plan.")
    if backup.suffix != (".files" if backup_kind == "files" else ".img"):
        raise ValueError("The backup destination does not match the reviewed method.")
    if (backup.parent != root / "backups" or backup.exists() or backup.with_suffix(".img.json").exists()
            or backup.with_name(backup.name + ".partial").exists()):
        raise ValueError("The backup destination is no longer available. Review a new plan.")
    check_capacity(root, card, info["size_bytes"], backup_bytes=0 if backup_kind == "files" else None)
    return card, image, backup


def copy_stream(source, output, size: int, phase: str = "copy") -> str:
    digest = hashlib.sha256()
    remaining = size
    progress(phase, 0, size, "Copying bytes")
    while remaining:
        block = source.read(min(CHUNK, remaining))
        if not block:
            raise OSError("The source ended before the full backup or image was copied.")
        digest.update(block)
        pending = memoryview(block)
        while pending:
            written = output.write(pending)
            if not written:
                raise OSError("The destination stopped accepting data.")
            pending = pending[written:]
        remaining -= len(block)
        if (size - remaining) % PROGRESS_BYTES == 0 or not remaining:
            progress(phase, size - remaining, size, "Copying bytes")
    output.flush()
    os.fsync(output.fileno())
    return digest.hexdigest()


def backup_to_file(device: BinaryIO, destination: Path, size: int) -> dict:
    partial = destination.with_suffix(".img.partial")
    device.seek(0)
    with partial.open("xb", buffering=0) as output:
        inherit_owner(output.fileno(), destination.parent)
        copied_hash = copy_stream(device, output, size, "backup-card")
    if sha256(partial, size) != copied_hash or stream_sha256(device, size, "verify-backup") != copied_hash:
        raise OSError("The card backup did not verify. The card has not been replaced.")
    if partial.stat().st_size != size:
        raise OSError("The backup does not contain the whole card.")
    publish(partial, destination)
    result = {"backup_path": str(destination), "sha256": copied_hash, "size_bytes": size, "verified": True, "backup_kind": "raw"}
    write_json(destination.with_suffix(".img.json"), result)
    return result


def raw_device(identifier: str) -> Path:
    if not DISK.fullmatch(identifier):
        raise ValueError("Invalid card device identifier.")
    path = Path("/dev/r" + identifier)
    mode = path.stat().st_mode
    if not stat.S_ISCHR(mode) and not stat.S_ISBLK(mode):
        raise ValueError("The selected device is no longer a disk.")
    return path


def authorized_device_descriptor(path: Path, flags: int) -> int:
    if flags & (os.O_CREAT | os.O_TRUNC | os.O_APPEND):
        raise ValueError("Raw card authorization cannot create, truncate or append a file.")
    descriptors = []
    process = None
    parent, child = socket.socketpair()
    try:
        parent.settimeout(120)
        # authopen adds O_NOFOLLOW_ANY; combining it with O_NOFOLLOW is invalid on macOS.
        authorization_flags = (flags & ~os.O_NOFOLLOW) | 0x20000000
        process = subprocess.Popen([AUTHOPEN, "-stdoutpipe", "-o", str(authorization_flags), str(path)],
                                   stdin=subprocess.DEVNULL, stdout=child, stderr=subprocess.PIPE)
        child.close()
        message, ancillary, received_flags, _ = parent.recvmsg(1, socket.CMSG_SPACE(4 * array.array("i").itemsize))
        valid_ancillary = True
        for level, kind, data in ancillary:
            if level != socket.SOL_SOCKET or kind != socket.SCM_RIGHTS:
                valid_ancillary = False
                continue
            values = array.array("i")
            if len(data) % values.itemsize:
                valid_ancillary = False
            values.frombytes(data[:len(data) - len(data) % values.itemsize])
            descriptors.extend(values)
        _, diagnostic = process.communicate(timeout=10)
        if process.returncode:
            details = diagnostic.decode("utf-8", errors="replace").strip()
            raise PermissionError("macOS did not authorize opening the SD card" + (": " + details[-1600:] if details else "."))
        if not message or not valid_ancillary or received_flags & (socket.MSG_CTRUNC | socket.MSG_TRUNC) or len(descriptors) != 1:
            raise RuntimeError("The macOS card authorization helper returned an invalid file descriptor.")
        descriptor = descriptors[0]
        os.set_inheritable(descriptor, False)
        if fcntl.fcntl(descriptor, fcntl.F_GETFL) & os.O_ACCMODE != flags & os.O_ACCMODE:
            raise RuntimeError("The card authorization helper returned the wrong access mode.")
        return descriptors.pop()
    except (socket.timeout, subprocess.TimeoutExpired) as error:
        raise TimeoutError("The macOS card authorization did not finish. The card has not been written.") from error
    finally:
        parent.close()
        child.close()
        for descriptor in descriptors:
            os.close(descriptor)
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.communicate(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate()


@contextlib.contextmanager
def opened_device(identifier: str, *, writable: bool):
    path = raw_device(identifier)
    expected = path.stat()
    flags = (os.O_RDWR if writable else os.O_RDONLY) | os.O_CLOEXEC | os.O_NOFOLLOW
    flags |= getattr(os, "O_EXLOCK", 0) | getattr(os, "O_NONBLOCK", 0)
    descriptor = os.open(path, flags) if os.geteuid() == 0 else authorized_device_descriptor(path, flags)
    try:
        actual = os.fstat(descriptor)
        if (actual.st_dev, actual.st_ino, actual.st_rdev, stat.S_IFMT(actual.st_mode)) != (
                expected.st_dev, expected.st_ino, expected.st_rdev, stat.S_IFMT(expected.st_mode)):
            raise ValueError("The card device changed while it was being opened.")
        with os.fdopen(descriptor, "r+b" if writable else "rb", buffering=0) as stream:
            descriptor = None
            yield stream
    finally:
        if descriptor is not None:
            os.close(descriptor)


def same_card(root: Path, expected: dict) -> dict:
    current = current_card(root, expected["id"])
    if current["identity"] != expected["identity"] or current["size_bytes"] != expected["size_bytes"]:
        raise ValueError("The card changed. The verified backup is retained and nothing more was written.")
    if current["mount_points"]:
        raise ValueError("The card mounted again. The backup is retained; review the card before retrying.")
    return current


def extract_save_vault(root: Path, backup: Path) -> dict:
    destination = managed(root, backup.with_suffix(".saves"))
    work = destination.with_name(destination.name + ".partial")
    records = []
    try:
        with backup_contents(root, backup) as mount:
            selected = []
            for path in mount.rglob("*"):
                if path.is_symlink() or not path.is_file():
                    continue
                relative = path.relative_to(mount)
                if any(part.startswith(".") for part in relative.parts):
                    continue
                key = relative.as_posix().casefold()
                if (key.startswith(("ed64/gamedata/", "gbcsys/save/", "gbcsys/snap/"))
                        or path.suffix.casefold() in {".sav", ".srm", ".ram", ".rtc", ".eep", ".fla", ".mpk"}):
                    selected.append((path, relative))
            if not selected:
                return {"save_vault_path": "", "save_files": 0,
                        "save_vault_warning": "No recognized save files were found in the mounted backup."}
            total = sum(path.stat().st_size for path, _ in selected)
            if len(selected) > 10000 or total > 2 * 1024**3 or shutil.disk_usage(root).free < total + 64 * MIB:
                raise OSError("The save vault exceeds its available space or bounded copy size.")
            media_directory(root, work)
            for index, (path, relative) in enumerate(selected, 1):
                target = managed(root, work / relative)
                media_directory(root, target.parent)
                size = path.stat().st_size
                with path.open("rb", buffering=0) as source, target.open("xb", buffering=0) as output:
                    inherit_owner(output.fileno(), target.parent)
                    digest = copy_stream(source, output, size, "save-file")
                if sha256(target) != digest:
                    raise OSError("A save-vault copy did not verify.")
                records.append({"path": relative.as_posix(), "bytes": size, "sha256": digest})
                progress("save-vault", index, len(selected), "Preserving readable save files")
        write_json(work / "MANIFEST.json", {"backup_path": str(backup), "files": records})
        work.rename(destination)
        return {"save_vault_path": str(destination), "save_files": len(records), "save_vault_warning": ""}
    except (OSError, ValueError, RuntimeError, KeyError) as error:
        shutil.rmtree(work, ignore_errors=True)
        return {"save_vault_path": "", "save_files": 0,
                "save_vault_warning": "The backup is verified; readable saves were not extracted: " + str(error)}


def backup_card(root: Path, identifier: str, backup_kind: str = "files") -> dict:
    root = workspace(root)
    if backup_kind not in {"files", "raw"}:
        raise ValueError("Choose card-files backup or whole card image.")
    with card_lock(root):
        card = current_card(root, identifier)
        check_capacity(root, card, backup_bytes=0 if backup_kind == "files" else None)
        backups = media_directory(root, root / "backups")
        destination = backups / (f"{identifier}-{uuid.uuid4()}." + ("files" if backup_kind == "files" else "img"))
        if backup_kind == "files":
            result = backup_all_files(root, card, destination)
        else:
            command([DISKUTIL, "unmountDisk", "/dev/" + identifier])
            with opened_device(identifier, writable=False) as device:
                same_card(root, card)
                result = backup_to_file(device, destination, card["size_bytes"])
                same_card(root, card)
        vault = extract_save_vault(root, destination)
        receipt = {**result, **vault, "device": card}
        receipt_path = destination.with_suffix(".completed.json") if backup_kind == "files" else destination.with_suffix(".img.json")
        return complete_ejection(identifier, receipt_path, receipt,
                                 "The card-files backup verified." if backup_kind == "files" else "The complete card backup verified.")


def clear_old_partition_tail(output: BinaryIO, image_size: int, card_size: int, block_size: int) -> int:
    if image_size > card_size or card_size < 0 or block_size not in {512, 1024, 2048, 4096}:
        raise ValueError("The selected card has unsupported sector geometry.")
    start = max(image_size, card_size - 34 * block_size)
    length = card_size - start
    if not length:
        return 0
    progress("clear-old-map", 0, length, "Clearing the previous card's trailing partition map")
    output.seek(start)
    pending = memoryview(bytes(length))
    while pending:
        written = output.write(pending)
        if not written:
            raise OSError("The card stopped accepting partition-map cleanup.")
        pending = pending[written:]
    output.flush()
    os.fsync(output.fileno())
    output.seek(start)
    remaining = length
    while remaining:
        block = output.read(remaining)
        if not block or any(block):
            raise OSError("The trailing partition-map cleanup did not verify. Keep the verified backup.")
        remaining -= len(block)
    progress("clear-old-map", length, length, "Previous trailing partition map cleared")
    return length


def write_and_verify(source: BinaryIO, output: BinaryIO, image_size: int, expected_sha256: str,
                     *, card_size: int | None = None, block_size: int = 512) -> int:
    if card_size is not None and (image_size > card_size or card_size < 0 or block_size not in {512, 1024, 2048, 4096}):
        raise ValueError("The selected card has unsupported sector geometry.")
    source.seek(0)
    output.seek(0)
    copied_hash = copy_stream(source, output, image_size, "write-card")
    if copied_hash != expected_sha256:
        raise OSError("The image changed while it was being written. Keep the verified backup for recovery.")
    cleared = clear_old_partition_tail(output, image_size, card_size, block_size) if card_size is not None else 0
    if stream_sha256(output, image_size, "verify-card") != expected_sha256:
        raise OSError("Card read-back verification failed. Keep the verified backup for recovery.")
    return cleared


def complete_ejection(identifier: str, receipt_path: Path, receipt: dict, message: str) -> dict:
    receipt = {**receipt, "ejected": False, "manual_eject_required": True,
               "message": message + " Keep the card connected until it is safely ejected."}
    write_json(receipt_path, receipt, replace=True)
    try:
        command([DISKUTIL, "eject", "/dev/" + identifier])
    except (OSError, RuntimeError) as error:
        receipt["eject_warning"] = str(error)
        receipt["message"] = message + " Automatic ejection failed. Keep the card connected and eject it safely in Finder."
    else:
        receipt.update(ejected=True, manual_eject_required=False, eject_warning="",
                       ejected_at_utc=dt.datetime.now(dt.timezone.utc).isoformat(),
                       message=message + " The card was safely ejected.")
    write_json(receipt_path, receipt, replace=True)
    return receipt


def compilation_files(mount: Path) -> tuple[str, list[dict]]:
    manifest = mount / "MANIFEST.json"
    if manifest.is_symlink() or not manifest.is_file():
        raise ValueError("A full-capacity install requires a verified CartShelf pack manifest.")
    data = json.loads(manifest.read_text())
    if not isinstance(data, dict) or data.get("profile") not in {"m64", "chromatic"}:
        raise ValueError("The CartShelf pack has an unsupported device profile.")
    files = pack_files(mount)
    verify_files(mount, files)
    if data['profile'] == 'chromatic':
        os_file = next((row for row in files if row['path'].casefold() == 'gbcsys/gbcos.bin'), None)
        if os_file is None or os_file['bytes'] != GB_X7_OS_BYTES or os_file['sha256'] not in GB_X7_OS_SHA256:
            raise ValueError('A fresh GB X7 card requires the verified stock or CartShelf-themed OS 1.06 at GBCSYS/GBCOS.BIN. Add the supported system file before building or writing this pack.')
    return data["profile"], files


def same_physical_card(root: Path, expected: dict) -> dict:
    current = current_card(root, expected["id"])
    physical = expected.get("physical_identity")
    if not physical or current.get("physical_identity") != physical or current["size_bytes"] != expected["size_bytes"]:
        raise ValueError("The physical card changed. Keep the verified backup and review the card again.")
    return current


def unmount_card(root: Path, card: dict) -> None:
    for attempt in range(4):
        same_physical_card(root, card)
        try:
            command([DISKUTIL, "unmountDisk", "/dev/" + card["id"]])
            return
        except RuntimeError:
            if attempt == 3:
                raise
            progress("unmount-card", attempt + 1, 4, "Waiting for macOS to release the SD card")
            time.sleep(0.25 * (attempt + 1))


def formatted_card(root: Path, expected: dict, volume_uuid: str | None = None,
                   *, writable: bool) -> tuple[dict, Path]:
    card = same_physical_card(root, expected)
    listing = command([DISKUTIL, "list", "-plist", "/dev/" + card["id"]], plist=True)
    disks = listing.get("AllDisksAndPartitions", [])
    if len(disks) != 1 or disks[0].get("DeviceIdentifier") != card["id"] or disks[0].get("Content") != "FDisk_partition_scheme":
        raise ValueError("The new card does not have the expected MBR partition map.")
    partitions = disks[0].get("Partitions", [])
    if len(partitions) != 1 or not re.fullmatch(re.escape(card["id"]) + r"s[0-9]+", partitions[0].get("DeviceIdentifier", "")):
        raise ValueError("The new card does not have one expected data partition.")
    info = disk_info(partitions[0]["DeviceIdentifier"])
    mount = Path(info.get("MountPoint", ""))
    cluster = info.get("VolumeAllocationBlockSize", 0)
    if (info.get("ParentWholeDisk") != card["id"] or info.get("FilesystemType") != "msdos"
            or info.get("WritableVolume") is not writable or not info.get("VolumeUUID")
            or (volume_uuid is not None and info["VolumeUUID"] != volume_uuid)
            or not mount.is_absolute() or mount.parent != Path("/Volumes") or not mount.is_dir()
            or info.get("IOKitSize", 0) < card["size_bytes"] * 0.99
            or info.get("TotalSize", 0) < card["size_bytes"] * 0.95
            or type(cluster) is not int or cluster <= 0 or cluster > 32768 or cluster & (cluster - 1)):
        raise ValueError("The new FAT32 volume identity, capacity or cluster size did not verify.")
    return info, mount


def card_payload_inventory(mount: Path, expected: set[str], directories: set[str] | None = None) -> list[Path]:
    entries = set(expected)
    entries.update(directories or ())
    for name in expected:
        entries.update(parent.as_posix() for parent in Path(name).parents if parent != Path("."))
    companions = []
    observed = set()
    for path in mount.rglob("*"):
        if path.is_symlink():
            raise ValueError("An unexpected symbolic link appeared on the card.")
        relative = path.relative_to(mount).as_posix()
        if relative in entries:
            if relative in expected:
                if not path.is_file():
                    raise ValueError("A planned card file is not a regular file: " + relative)
                observed.add(relative)
            elif not path.is_dir():
                raise ValueError("A planned card directory is not a directory: " + relative)
            continue
        root_name = Path(relative).parts[0]
        if ((root_name.casefold() in MACOS_SYSTEM_DIRECTORIES_FOLDED and (mount / root_name).is_dir())
                or (path.name == ".DS_Store" and path.is_file())):
            continue
        companion_name = path.name[2:] if path.name.startswith("._") else ""
        system_companion = (relative == "._." or companion_name == ".DS_Store"
                            or (path.parent == mount and companion_name.casefold() in MACOS_SYSTEM_DIRECTORIES_FOLDED))
        if not path.is_file() or not companion_name:
            raise ValueError("An unexpected file appeared on the card: " + relative)
        if not system_companion:
            if companion_name in {".", ".."}:
                raise ValueError("An unexpected file appeared on the card: " + relative)
            companion = path.with_name(companion_name)
            if not companion.exists() or companion.relative_to(mount).as_posix() not in entries:
                raise ValueError("An unexpected file appeared on the card: " + relative)
        data = path.read_bytes()
        if len(data) < 26 or data[:8] != bytes.fromhex("0005160700020000"):
            raise ValueError("An unrecognized macOS companion appeared on the card: " + relative)
        count = int.from_bytes(data[24:26], "big")
        if 26 + count * 12 > len(data):
            raise ValueError("A macOS companion has a truncated entry table.")
        for index in range(count):
            _, offset, size = struct.unpack_from(">III", data, 26 + index * 12)
            if offset + size > len(data):
                raise ValueError("A macOS companion has an invalid entry boundary.")
        if not system_companion:
            companions.append(path)
    if observed != expected:
        raise ValueError("The card's files do not match the planned payload.")
    return companions


def save_path(relative: str) -> bool:
    path = Path(relative)
    key = path.as_posix().casefold()
    return (key.startswith(("ed64/gamedata/", "gbcsys/save/", "gbcsys/snap/"))
            or path.suffix.casefold() in {".sav", ".srm", ".ram", ".rtc", ".eep", ".fla", ".mpk"})


def archive_card_metadata(root: Path, backup: Path, source: Path, relative: Path) -> str:
    archive = media_directory(root, backup.with_suffix(".mac-metadata"))
    destination = archive / relative
    media_directory(root, destination.parent)
    with source.open("rb", buffering=0) as src, destination.open("xb", buffering=0) as dst:
        digest = copy_stream(src, dst, source.stat().st_size, "archive-mac-metadata")
    if sha256(destination) != digest:
        raise OSError("A macOS companion archive failed verification.")
    return digest


def install_file_tree(root: Path, card: dict, backup: Path, plan: dict, receipt: dict,
                      rows: list[dict], directories: list[dict]) -> dict:
    receipt_path = Path(plan["plan_path"]).with_suffix(".completed.json")
    expected = [{key: row[key] for key in ("path", "bytes", "sha256")} for row in rows]
    directory_names = {row["path"] for row in directories}
    restoring = plan["image_kind"] == "file-backup"
    format_started = False

    def directory_times(mount):
        for row in sorted(directories, key=lambda row: len(Path(row["path"]).parts), reverse=True):
            target = mount / row["path"]
            if target.is_symlink() or not target.is_dir():
                raise ValueError("An original directory did not restore: " + row["path"])
            os.utime(target, ns=(row["mtime_ns"], row["mtime_ns"]), follow_symlinks=False)

    try:
        if sum(row["bytes"] for row in rows) + 32768 * len(directories) > card["size_bytes"] * 0.95:
            raise ValueError("The source files and matching saves do not fit safely on this card. Nothing was written.")
        same_card(root, card)
        same_physical_card(root, card)
        label = "ED64" if receipt.get("profile") == "m64" else "GBX7" if receipt.get("profile") == "chromatic" else "CARTSHELF"
        receipt.update(files=expected, directories=directories, operation_phase="format-card")
        write_json(receipt_path, receipt, replace=True)
        format_started = True
        command([DISKUTIL, "eraseDisk", "MS-DOS FAT32", label, "MBRFormat", "/dev/" + card["id"]])
        info, mount = formatted_card(root, card, writable=True)
        volume_uuid = info["VolumeUUID"]
        receipt.update(volume_uuid=volume_uuid, filesystem_bytes=info["TotalSize"],
                       cluster_bytes=info["VolumeAllocationBlockSize"], operation_phase="copy-pack-and-saves")
        write_json(receipt_path, receipt, replace=True)
        for row in directories:
            target = mount / row["path"]
            if target.is_symlink() or (target.exists() and not target.is_dir()):
                raise ValueError("An unexpected file blocks an original directory.")
            target.mkdir(parents=True, exist_ok=True)
        ordered = sorted(rows, key=lambda row: (Path(row["path"]).name.startswith("._"), row["path"])) if restoring else rows
        for index, row in enumerate(ordered, 1):
            formatted_card(root, card, volume_uuid, writable=True)
            target = mount / row["path"]
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                name = Path(row["path"])
                generated = (name.name.startswith("._") or name.name == ".DS_Store"
                             or name.parts[0] in {".fseventsd", ".Spotlight-V100"})
                if not restoring or not generated or target.is_symlink() or not target.is_file():
                    raise ValueError("An unexpected file blocks restoration: " + row["path"])
                digest = archive_card_metadata(root, backup, target, Path("replaced") / name)
                if sha256(target) != digest:
                    raise OSError("Generated metadata changed before original-file restoration.")
                target.unlink()
            with Path(row["source_path"]).open("rb", buffering=0) as src, target.open("xb", buffering=0) as dst:
                if copy_stream(src, dst, row["bytes"], "write-card-files") != row["sha256"]:
                    raise OSError("A source changed during copying: " + row["path"])
            if "mtime_ns" in row:
                os.utime(target, ns=(row["mtime_ns"], row["mtime_ns"]), follow_symlinks=False)
            if row.get("save"):
                receipt["restored_save_files"] += 1
            if restoring:
                receipt["restored_files"] += 1
            progress("write-card-files", index, len(rows), "Installing " + row["path"])
        directory_times(mount)
        unmount_card(root, card)
        same_physical_card(root, card)
        command([DISKUTIL, "mount", "readOnly", info["DeviceIdentifier"]])
        _, mount = formatted_card(root, card, volume_uuid, writable=False)
        companions = card_payload_inventory(mount, {row["path"] for row in expected}, directory_names)
        if companions:
            archived = []
            for path in companions:
                relative = path.relative_to(mount)
                digest = archive_card_metadata(root, backup, path, Path("extras") / relative)
                archived.append({"path": relative.as_posix(), "sha256": digest})
            receipt["archived_mac_companions"] = archived
            unmount_card(root, card)
            same_physical_card(root, card)
            command([DISKUTIL, "mount", info["DeviceIdentifier"]])
            _, mount = formatted_card(root, card, volume_uuid, writable=True)
            for row in archived:
                path = mount / row["path"]
                if sha256(path) != row["sha256"]:
                    raise OSError("A macOS companion changed before cleanup.")
                path.unlink()
            directory_times(mount)
            unmount_card(root, card)
            same_physical_card(root, card)
            command([DISKUTIL, "mount", "readOnly", info["DeviceIdentifier"]])
            _, mount = formatted_card(root, card, volume_uuid, writable=False)
        if card_payload_inventory(mount, {row["path"] for row in expected}, directory_names):
            raise OSError("macOS companions remain after cleanup.")
        receipt["operation_phase"] = "verify-card-files"
        for repeat in range(2):
            for row in rows:
                target = mount / row["path"]
                observed = target.stat()
                if observed.st_size != row["bytes"] or ("mtime_ns" in row and observed.st_mtime_ns != row["mtime_ns"]):
                    raise OSError("Card read-back size or timestamp differs: " + row["path"])
                with uncached_file(target) as stream:
                    if stream_sha256(stream, row["bytes"], "verify-card-files") != row["sha256"]:
                        raise OSError("Card read-back verification failed: " + row["path"])
            for row in directories:
                target = mount / row["path"]
                if not target.is_dir() or target.stat().st_mtime_ns != row["mtime_ns"]:
                    raise OSError("An original directory or timestamp did not verify: " + row["path"])
            progress("verify-card-files", repeat + 1, 2, "Verified the card's files and restored saves")
        formatted_card(root, card, volume_uuid, writable=False)
        receipt.update(verified=True, operation_phase="complete", written_bytes=sum(row["bytes"] for row in expected),
                       full_capacity=True, read_back_passes=2, tail_cleared_bytes=0)
        return receipt
    except Exception as error:
        if format_started:
            receipt.update(verified=False, error=str(error), message="Installation stopped after formatting began. Keep the verified backup and safely eject the card before recovery.")
            write_json(receipt_path, receipt, replace=True)
        raise


def install_compilation(root: Path, card: dict, image: Path, backup: Path,
                        plan: dict, backup_receipt: dict, vault: dict) -> dict:
    import card_saves

    with mounted_image(image, readonly=True) as pack, backup_contents(root, backup) as prior:
        profile, files = compilation_files(pack)
        history = root / "backups/save-history"
        managed(root, history)
        if 'preview' not in plan or 'save_choices' not in plan:
            raise ValueError('This pack review predates the game and save preview. Review the card again.')
        preview = replacement_preview(root, prior, pack, profile, plan['save_choices'])
        if preview != plan['preview']:
            raise ValueError('The card contents or save selection changed after review. The verified backup is retained. Review the card again before replacement.')
        history = media_directory(root, history)
        restore = card_saves.build_restore_plan(prior, pack, profile, history=history,
                                               choices=plan['save_choices'], excluded_root_names=MACOS_SYSTEM_DIRECTORIES)
        if _preview_saves(prior, restore) != plan['preview']['saves']:
            raise ValueError('The final save restoration changed after review. The verified backup is retained. Review the card again before replacement.')
        saves = restore["files"]
        folded = {row["path"].casefold() for row in files}
        for row in saves:
            relative = Path(row["path"])
            source = Path(row["source_path"])
            file_relative(row["path"])
            if (source.is_symlink() or not source.is_file()
                    or not (source.resolve().is_relative_to(prior.resolve()) or source.resolve().is_relative_to(history.resolve()))
                    or row["path"].casefold() in folded or source.stat().st_size != row["bytes"] or sha256(source) != row["sha256"]):
                raise ValueError("A save migration path, source or destination did not verify.")
            folded.add(row["path"].casefold())
        rows = [{**row, "source_path": str(pack / row["path"])} for row in files]
        rows += [{**row, "save": True} for row in saves]
        if sha256(image) != plan["image_sha256"]:
            raise OSError("The verified source image changed. Nothing was written.")
        verify_backup(root, backup, backup_receipt)
        receipt = {"device": card, "backup_path": str(backup), "image_path": str(image), **vault,
                   **{key: backup_receipt[key] for key in ("backup_scope", "excluded_system_paths") if key in backup_receipt},
                   "backup_kind": plan["backup_kind"], "image_sha256": plan["image_sha256"], "image_kind": "compilation",
                   "install_mode": "full-capacity-files", "verified": False, "ejected": False,
                   "manual_eject_required": True, "restored_save_files": 0,
                   "planned_restore_save_files": len(saves), "skipped_save_files": len(restore["skipped"]),
                   "save_restore": restore, "save_restore_skipped": restore["skipped"],
                   "save_restore_warnings": restore.get("warnings", []), "save_history_path": str(history), "profile": profile,
                   "preview": preview, "save_choices": plan["save_choices"]}
        return install_file_tree(root, card, backup, plan, receipt, rows, [])


def install_file_backup(root: Path, card: dict, source: Path, backup: Path,
                        plan: dict, backup_receipt: dict, vault: dict) -> dict:
    original = restore_file_backup_info(root, source)
    if original["sha256"] != plan["image_sha256"]:
        raise ValueError("The selected file backup changed after review. Nothing was written.")
    verify_backup(root, backup, backup_receipt)
    rows = [{**row, "source_path": str(source / "files" / row["path"]), "save": save_path(row["path"])} for row in original["files"]]
    top_levels = {Path(row["path"]).parts[0].casefold() for row in rows}
    profile = "chromatic" if "gbcsys" in top_levels else "m64" if "ed64" in top_levels else None
    receipt = {"device": card, "backup_path": str(backup), "image_path": str(source), **vault,
               **{key: backup_receipt[key] for key in ("backup_scope", "excluded_system_paths") if key in backup_receipt},
               "backup_kind": plan["backup_kind"], "image_sha256": original["sha256"], "image_kind": "file-backup",
               "install_mode": "file-backup-restore", "verified": False, "ejected": False,
               "manual_eject_required": True, "restored_save_files": 0, "restored_files": 0,
               "save_restore_skipped": [], "save_restore_warnings": [], "profile": profile}
    return install_file_tree(root, card, backup, plan, receipt, rows, original["directories"])


def install(root: Path, plan_path: Path, token: str) -> dict:
    root = workspace(root)
    plan_path = managed(root, plan_path)
    if plan_path.parent != root / "library/card-plans" or plan_path.is_symlink():
        raise ValueError("Choose a card replacement plan from this library.")
    plan = json.loads(plan_path.read_text())
    if plan.get("plan_path") != str(plan_path):
        raise ValueError("The replacement plan moved after review. Review the card again.")
    with card_lock(root):
        card, image, backup = validate_plan(root, plan, token)
        if plan["image_kind"] == "backup":
            command([DISKUTIL, "unmountDisk", "/dev/" + card["id"]])
            with opened_device(card["id"], writable=True) as device, image.open("rb", buffering=0) as image_stream:
                same_card(root, card)
                backup_receipt = backup_to_file(device, backup, card["size_bytes"])
                if (os.fstat(image_stream.fileno()).st_size != plan["image_size_bytes"]
                        or stream_sha256(image_stream, plan["image_size_bytes"], "verify-source-image") != plan["image_sha256"]):
                    raise ValueError("The image changed after review. Nothing was written.")
                verify_backup(root, backup, backup_receipt)
                same_card(root, card)
                cleared = write_and_verify(image_stream, device, plan["image_size_bytes"], plan["image_sha256"],
                                           card_size=card["size_bytes"], block_size=card.get("block_size", 512))
            vault = extract_save_vault(root, backup)
            receipt = {"device": card, "backup_path": str(backup), "image_path": str(image), **vault,
                       "backup_kind": "raw", "image_sha256": plan["image_sha256"],
                       "written_bytes": plan["image_size_bytes"], "image_kind": "backup",
                       "tail_cleared_bytes": cleared, "verified": True, "install_mode": "raw-backup"}
            return complete_ejection(card["id"], plan_path.with_suffix(".completed.json"), receipt,
                                     "The backup and written image both verified.")
        if plan["backup_kind"] == "files":
            backup_receipt = backup_all_files(root, card, backup)
        else:
            command([DISKUTIL, "unmountDisk", "/dev/" + card["id"]])
            with opened_device(card["id"], writable=False) as device:
                same_card(root, card)
                backup_receipt = backup_to_file(device, backup, card["size_bytes"])
                same_card(root, card)
        verify_backup(root, backup, backup_receipt)
        same_card(root, card)
        info = image_info(root, image)
        if (info["sha256"] != plan["image_sha256"] or info["size_bytes"] != plan["image_size_bytes"]
                or info["kind"] != plan["image_kind"]):
            raise ValueError("The reviewed source changed. Nothing was written.")
        vault = extract_save_vault(root, backup)
        if plan["image_kind"] == "compilation":
            receipt = install_compilation(root, card, image, backup, plan, backup_receipt, vault)
            message = "The full-capacity card and restored matching saves verified."
        else:
            receipt = install_file_backup(root, card, image, backup, plan, backup_receipt, vault)
            message = "The full-capacity card and original backed-up files verified."
        return complete_ejection(card["id"], plan_path.with_suffix(".completed.json"), receipt, message)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    sub = parser.add_subparsers(dest="operation", required=True)
    sub.add_parser("cards")
    build = sub.add_parser("build-image")
    build.add_argument("--pack", type=Path, required=True)
    plan = sub.add_parser("plan-install")
    plan.add_argument("--image", type=Path, required=True)
    plan.add_argument("--device", required=True)
    plan.add_argument("--backup-kind", choices=("files", "raw"), default="files")
    plan.add_argument("--save-choices", default="[]")
    replace = sub.add_parser("install")
    replace.add_argument("--plan", type=Path, required=True)
    replace.add_argument("--confirm", required=True)
    backup = sub.add_parser("backup")
    backup.add_argument("--device", required=True)
    backup.add_argument("--backup-kind", choices=("files", "raw"), default="files")
    args = parser.parse_args()
    try:
        if args.operation == "cards":
            result = cards(args.root)
        elif args.operation == "build-image":
            result = create_image(args.root, args.pack)
        elif args.operation == "plan-install":
            result = plan_install(args.root, args.image, args.device, args.backup_kind, save_choices=save_choices_json(args.save_choices))
        elif args.operation == "install":
            result = install(args.root, args.plan, args.confirm)
        else:
            result = backup_card(args.root, args.device, args.backup_kind)
        print(json.dumps(result, separators=(",", ":")))
        return 0
    except (OSError, ValueError, RuntimeError, KeyError) as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
