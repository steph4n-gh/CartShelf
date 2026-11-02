#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
"""Inspect and share collections, save snapshots and advisory card names."""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import stat
import sys
import uuid

import card_saves
from library_manager import Library
import recipes
import system_files


MAX_METADATA_BYTES = 8 * 1024 * 1024
SNAPSHOT_ID = re.compile(r"[a-f0-9]{32}\Z")


def _profile(value: str) -> str:
    if not isinstance(value, str) or value not in recipes.PROFILES:
        raise ValueError("Choose a supported device profile.")
    return value


def _identity(value: str) -> str:
    if not isinstance(value, str) or not recipes.SHA256.fullmatch(value):
        raise ValueError("Choose an exact SHA-256 ROM identity.")
    return value


def _read_regular(path: Path, limit: int) -> bytes:
    original = path.lstat()
    if not stat.S_ISREG(original.st_mode) or original.st_size > limit:
        raise ValueError("The selected file is not a bounded regular document.")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size > limit or (original.st_dev, original.st_ino) != (before.st_dev, before.st_ino):
            raise ValueError("The selected file is not a bounded regular document.")
        encoded = stream.read(limit + 1)
        after = os.fstat(stream.fileno())
        if len(encoded) > limit or (before.st_ino, before.st_size, before.st_mtime_ns) != (
                after.st_ino, after.st_size, after.st_mtime_ns):
            raise ValueError("The selected document changed while reading.")
    return encoded


def _managed(library: Library, path: Path) -> Path:
    path = library._managed(path)
    relative = path.relative_to(library.root)
    current = library.root
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            raise ValueError("Library experience data cannot contain symbolic links.")
    return path


def _json(library: Library, path: Path, default=None):
    path = _managed(library, path)
    if not path.exists():
        return default
    try:
        return json.loads(_read_regular(path, MAX_METADATA_BYTES))
    except RecursionError as error:
        raise ValueError("The library metadata is nested too deeply.") from error


def _snapshot(library: Library) -> dict:
    return library.scan()


def export_recipe(library: Library, profile: str, name: str, release_ids: list[str],
                  description: str = "") -> dict:
    return recipes.recipe_for_selection(name, _profile(profile), release_ids,
                                        _snapshot(library)["releases"], description)


def inspect_recipe(library: Library, path: Path) -> dict:
    encoded = _read_regular(path, recipes.MAX_RECIPE_BYTES)
    recipe = recipes.parse_recipe(encoded)
    return {"sha256": recipes.document_digest(encoded), "recipe": recipe,
            **recipes.resolve_recipe(recipe, _snapshot(library)["releases"])}


def import_recipe(library: Library, path: Path, confirmation: str, name: str) -> dict:
    _identity(confirmation)
    encoded = _read_regular(path, recipes.MAX_RECIPE_BYTES)
    if not secrets.compare_digest(recipes.document_digest(encoded), confirmation):
        raise ValueError("The recipe changed after inspection. Review it again.")
    recipe = recipes.parse_recipe(encoded)
    name = recipes.text(name.strip(), "collection name", 100)
    resolved = recipes.resolve_recipe(recipe, _snapshot(library)["releases"])
    if not resolved["matched_release_ids"]:
        raise ValueError("No exact recipe builds are available. Import the missing builds first.")
    collection_id = uuid.uuid4().hex
    destination = _managed(library, library.collections_dir / (collection_id + ".json"))
    if os.path.lexists(destination):
        raise ValueError("The new collection destination already exists. Import the recipe again.")
    library.save_collection(name, recipe["profile"], resolved["matched_release_ids"],
                            collection_id, recipe["description"])
    return {"collection_id": collection_id, "profile": recipe["profile"], **resolved}


def save_history(library: Library, profile: str, identity: str | None = None) -> dict:
    profile = _profile(profile)
    if identity is not None:
        _identity(identity)
    history = _managed(library, library.root / "backups/save-history")
    return {"entries": card_saves.history_entries(history, profile, identity)}


def _save_choice(library: Library, profile: str, identity: str, snapshot: str) -> tuple[dict, dict]:
    _profile(profile)
    _identity(identity)
    if not isinstance(snapshot, str) or not SNAPSHOT_ID.fullmatch(snapshot):
        raise ValueError("Choose a recorded save snapshot.")
    entries = save_history(library, profile, identity)["entries"]
    entry = next((row for row in entries if row["snapshot_id"] == snapshot), None)
    if entry is None:
        raise ValueError("The exact save snapshot was not found.")
    choice = {key: entry[key] for key in ("profile", "identity_sha256", "snapshot_id", "bundle_sha256")}
    history = _managed(library, library.root / "backups/save-history")
    return choice, card_saves.verified_history_bundle(history, choice)


def _public_save_files(bundle: dict) -> list[dict]:
    return [{key: row[key] for key in ("key", "path", "bytes", "sha256")} for row in bundle["files"]]


def inspect_save(library: Library, profile: str, identity: str, snapshot: str) -> dict:
    choice, bundle = _save_choice(library, profile, identity, snapshot)
    return {"choice": choice, "files": _public_save_files(bundle), "created_at": bundle["created_at"],
            "verified": True}


def export_save(library: Library, profile: str, identity: str, snapshot: str,
                confirmation: str | None = None) -> dict:
    choice, bundle = _save_choice(library, profile, identity, snapshot)
    if confirmation is not None and not secrets.compare_digest(choice["bundle_sha256"], _identity(confirmation)):
        raise ValueError("The save bundle changed after inspection. Inspect it again.")
    parent = _managed(library, library.root / "exports/saves")
    parent.mkdir(parents=True, exist_ok=True)
    temporary = _managed(library, parent / ("." + uuid.uuid4().hex + ".partial"))
    destination = _managed(library, parent / (profile + "-" + identity[:12] + "-" + uuid.uuid4().hex))
    temporary.mkdir()
    records = []
    try:
        for row in bundle["files"]:
            source = Path(row["source_path"])
            relative = Path("files") / row["key"]
            if relative.is_absolute() or len(relative.parts) != 2 or ".." in relative.parts:
                raise ValueError("The save bundle contains an unsupported member name.")
            target = _managed(library, temporary / relative)
            target.parent.mkdir(exist_ok=True)
            digest = hashlib.sha256()
            descriptor = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
            with os.fdopen(descriptor, "rb") as src, target.open("xb") as output:
                before = os.fstat(src.fileno())
                if not stat.S_ISREG(before.st_mode) or before.st_size != row["bytes"]:
                    raise ValueError("The archived save changed before export.")
                remaining = row["bytes"]
                while remaining:
                    block = src.read(min(1024 * 1024, remaining))
                    if not block:
                        raise OSError("The archived save ended during export.")
                    output.write(block)
                    digest.update(block)
                    remaining -= len(block)
                after = os.fstat(src.fileno())
                if src.read(1) or (before.st_ino, before.st_size, before.st_mtime_ns) != (
                        after.st_ino, after.st_size, after.st_mtime_ns):
                    raise ValueError("The archived save changed during export.")
                output.flush()
                os.fsync(output.fileno())
            if target.stat().st_size != row["bytes"] or digest.hexdigest() != row["sha256"]:
                raise OSError("The save export changed during copying.")
            with target.open("rb") as exported:
                checked = hashlib.sha256(exported.read()).hexdigest()
            if checked != row["sha256"]:
                raise OSError("The save export did not verify.")
            records.append({"key": row["key"], "path": relative.as_posix(),
                            "bytes": row["bytes"], "sha256": row["sha256"]})
        manifest = {"schema": 1, "kind": "cartshelf-save-export", "profile": profile,
                    "identity_sha256": identity, "snapshot_id": snapshot,
                    "bundle_sha256": choice["bundle_sha256"], "archived_at": bundle["created_at"],
                    "files": records}
        encoded = (json.dumps(manifest, indent=2) + "\n").encode()
        for name, data in (("MANIFEST.json", encoded), ("MANIFEST.sha256", (hashlib.sha256(encoded).hexdigest() + "\n").encode())):
            with (temporary / name).open("xb") as output:
                output.write(data)
                output.flush()
                os.fsync(output.fileno())
        if (_read_regular(temporary / "MANIFEST.json", MAX_METADATA_BYTES) != encoded
                or _read_regular(temporary / "MANIFEST.sha256", 65).decode().strip() != hashlib.sha256(encoded).hexdigest()):
            raise OSError("The save export manifest did not verify.")
        _managed(library, destination)
        temporary.rename(destination)
    except BaseException:
        shutil.rmtree(temporary)
        raise
    return {"path": str(destination), "file_count": len(records),
            "message": "Exported and verified the complete exact-build save bundle."}


def _volume(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("Choose a valid volume UUID.")
    try:
        return str(uuid.UUID(value)).upper()
    except ValueError as error:
        raise ValueError("Choose a valid volume UUID.") from error


def _labels(library: Library) -> dict[str, str]:
    document = _json(library, library.root / "library/card-labels.json", {"schema": 1, "labels": {}})
    if not isinstance(document, dict) or set(document) != {"schema", "labels"} or type(document["schema"]) is not int or document["schema"] != 1:
        raise ValueError("Unsupported card label document.")
    labels = document["labels"]
    if not isinstance(labels, dict) or len(labels) > 4096:
        raise ValueError("The card label document exceeds its supported bound.")
    for key, value in labels.items():
        if _volume(key) != key or not isinstance(value, str) or len(value) > 64 or any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError("A card label is invalid.")
    return labels


def label_card(library: Library, volume: str, name: str) -> dict:
    volume = _volume(volume)
    if not isinstance(name, str) or len(name.strip()) > 64 or any(ord(char) < 32 or ord(char) == 127 for char in name):
        raise ValueError("Card names must contain at most 64 printable characters.")
    name = name.strip()
    labels = dict(_labels(library))
    labels[volume] = name
    if len(labels) > 4096:
        raise ValueError("The card label document exceeds its supported bound.")
    library._write_json(_managed(library, library.root / "library/card-labels.json"), {"schema": 1, "labels": labels})
    return {"volume_uuid": volume, "friendly_name": name}


def _record_time(value) -> dt.datetime:
    if not isinstance(value, str):
        raise ValueError("The operation has no recorded verification time.")
    parsed = dt.datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("The operation verification time needs a timezone.")
    return parsed


def card_history(library: Library) -> dict:
    explicit = _labels(library)
    names = dict(explicit)
    records, warnings = [], []
    directory = _managed(library, library.root / "library/card-plans")
    backups = _managed(library, library.root / "backups")
    paths = [*directory.glob("*.completed.json"), *backups.glob("*.completed.json"), *backups.glob("*.img.json")]
    for path in sorted(paths):
        try:
            receipt = _json(library, path)
            if not isinstance(receipt, dict) or receipt.get("verified") is not True:
                continue
            device = receipt.get("device", {})
            if not isinstance(device, dict) or not isinstance(receipt.get("image_path", ""), str) or not isinstance(receipt.get("backup_path", ""), str):
                raise ValueError("The card operation has invalid advisory fields.")
            volume = _volume(receipt.get("volume_uuid") or device.get("volume_uuid"))
            when = _record_time(receipt.get("ejected_at_utc") or receipt.get("completed_at_utc"))
            old = device.get("volume_uuid")
            old = _volume(old) if old else ""
            records.append((when, path.relative_to(library.root).as_posix(), volume, old, receipt))
        except (KeyError, OSError, ValueError, TypeError) as error:
            warnings.append({"path": path.relative_to(library.root).as_posix(), "reason": str(error)})
    rows = {}
    for when, _, volume, old, receipt in sorted(records):
        if volume not in explicit and old in names:
            names[volume] = names[old]
        image = receipt.get("image_path", "")
        backup = receipt.get("backup_path", "")
        if not isinstance(image, str) or not isinstance(backup, str):
            continue
        safe_backup = ""
        if backup:
            try:
                candidate = _managed(library, Path(backup) if Path(backup).is_absolute() else library.root / backup)
                if not candidate.is_relative_to(library.root / "backups"):
                    raise ValueError("The backup location is outside the library backup folder.")
                if candidate.exists():
                    safe_backup = str(candidate)
            except (ValueError, OSError) as error:
                warnings.append({"path": "library/card-plans", "reason": str(error)})
        previous = rows.get(volume, {})
        last_pack = previous.get("last_pack", "")
        if image or receipt.get("image_kind") or receipt.get("install_mode"):
            compilation = (bool(image) and receipt.get("image_kind") == "compilation"
                           and receipt.get("install_mode") == "full-capacity-files")
            last_pack = Path(image).stem if compilation else ""
        rows[volume] = {"volume_uuid": volume, "friendly_name": names.get(volume, ""),
                        "last_pack": last_pack,
                        "last_verified_at": when.isoformat(),
                        "last_backup_path": safe_backup or previous.get("last_backup_path", ""),
                        "last_backup_at": when.isoformat() if safe_backup else previous.get("last_backup_at", "")}
    for volume, name in explicit.items():
        if volume not in rows:
            rows[volume] = {"volume_uuid": volume, "friendly_name": name, "last_pack": "", "last_verified_at": "",
                            "last_backup_path": "", "last_backup_at": ""}
    return {"cards": [rows[key] for key in sorted(rows)], "warnings": warnings}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    export = commands.add_parser("recipe-export")
    export.add_argument("--profile", choices=sorted(recipes.PROFILES), required=True)
    export.add_argument("--name", required=True)
    export.add_argument("--description", default="")
    export.add_argument("--release", action="append", default=[])
    for name in ("recipe-inspect", "recipe-import"):
        command = commands.add_parser(name)
        command.add_argument("--path", type=Path, required=True)
        if name == "recipe-import":
            command.add_argument("--confirm", required=True)
            command.add_argument("--name", required=True)
    history = commands.add_parser("save-history")
    history.add_argument("--profile", choices=sorted(recipes.PROFILES), required=True)
    history.add_argument("--identity")
    for name in ("save-inspect", "save-export"):
        command = commands.add_parser(name)
        command.add_argument("--profile", choices=sorted(recipes.PROFILES), required=True)
        command.add_argument("--identity", required=True)
        command.add_argument("--snapshot", required=True)
        if name == "save-export":
            command.add_argument("--confirm")
    label = commands.add_parser("card-label")
    label.add_argument("--volume", required=True)
    label.add_argument("--name", required=True)
    commands.add_parser("card-history")
    for name in ("system-files-status", "system-files-import"):
        command = commands.add_parser(name)
        command.add_argument("--profile", choices=sorted(recipes.PROFILES), required=True)
        if name == "system-files-import":
            command.add_argument("--path", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        library = Library(args.root)
        if args.command == "recipe-export":
            result = export_recipe(library, args.profile, args.name, args.release, args.description)
        elif args.command == "recipe-inspect":
            result = inspect_recipe(library, args.path)
        elif args.command == "recipe-import":
            result = import_recipe(library, args.path, args.confirm, args.name)
        elif args.command == "save-history":
            result = save_history(library, args.profile, args.identity)
        elif args.command == "save-inspect":
            result = inspect_save(library, args.profile, args.identity, args.snapshot)
        elif args.command == "save-export":
            result = export_save(library, args.profile, args.identity, args.snapshot, args.confirm)
        elif args.command == "card-label":
            result = label_card(library, args.volume, args.name)
        elif args.command == "system-files-status":
            result = system_files.status(library, args.profile)
        elif args.command == "system-files-import":
            result = system_files.import_file(library, args.profile, args.path)
        else:
            result = card_history(library)
        print(json.dumps(result, ensure_ascii=False))
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as error:
        print(json.dumps({"error": str(error)}), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
