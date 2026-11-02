from __future__ import annotations

from dataclasses import dataclass
import datetime as dt
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import secrets
import stat

import media


SHA256 = re.compile(r"[a-f0-9]{64}\Z")


@dataclass(frozen=True)
class _PreparedImage:
    item: dict
    receipt: dict
    manifest_bytes: bytes
    states: dict[Path, tuple]


def _managed(root: Path, path: Path) -> Path:
    path = path if path.is_absolute() else root / path
    resolved = path.resolve(strict=True)
    if not resolved.is_relative_to(root):
        raise ValueError("Prepared images must stay inside the library.")
    for component in (path, *path.parents):
        if component == root:
            break
        if component.is_symlink():
            raise ValueError("Prepared image paths cannot contain symbolic links.")
    return resolved


def _file_state(root: Path, path: Path) -> tuple:
    info = _managed(root, path).stat()
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("Prepared images require regular image, receipt, and manifest files.")
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _unchanged(root: Path, states: dict[Path, tuple]) -> None:
    if any(_file_state(root, path) != state for path, state in states.items()):
        raise ValueError("The prepared image or its identity records changed during inspection.")


def _manifest(data: bytes, build: str) -> dict:
    manifest = json.loads(data)
    if not isinstance(manifest, dict):
        raise ValueError("The pack manifest must be an object.")
    profile = manifest.get("profile")
    name = manifest.get("name")
    release_ids = manifest.get("release_ids")
    files = manifest.get("files")
    if (not isinstance(profile, str) or profile not in {"m64", "chromatic"} or not isinstance(name, str)
            or not name.strip() or manifest.get("build") != build
            or not isinstance(release_ids, list) or not release_ids
            or not isinstance(files, list) or not files):
        raise ValueError("The pack manifest has an invalid profile, name, build, or file list.")
    if any(not isinstance(value, str) or not value.startswith(profile + ":")
           or not SHA256.fullmatch(value[len(profile) + 1:]) for value in release_ids):
        raise ValueError("The pack manifest has invalid exact-build identities.")
    if len(set(release_ids)) != len(release_ids):
        raise ValueError("The pack manifest repeats an exact-build identity.")
    paths = set()
    for record in files:
        if not isinstance(record, dict):
            raise ValueError("The pack manifest contains an invalid file record.")
        value = record.get("path")
        if (not isinstance(value, str) or not value or "\\" in value
                or any(ord(character) < 32 for character in value)):
            raise ValueError("The pack manifest contains an invalid file path.")
        path = PurePosixPath(value)
        if (not path.parts or path.is_absolute() or ".." in path.parts or path.as_posix() != value
                or value.casefold() in paths or value.casefold() == "manifest.json"
                or type(record.get("bytes")) is not int or not 0 <= record["bytes"] < 4 * 1024**3
                or not isinstance(record.get("sha256"), str)
                or not SHA256.fullmatch(record["sha256"])):
            raise ValueError("The pack manifest contains an invalid file path, size, or hash.")
        paths.add(value.casefold())
    return manifest


def _prepared(root: Path, receipt_path: Path) -> _PreparedImage:
    exports = _managed(root, root / "exports")
    receipt_path = _managed(root, receipt_path)
    if receipt_path.parent != exports or not receipt_path.name.endswith(".img.json"):
        raise ValueError("Choose a prepared image directly inside exports.")
    states = {receipt_path: _file_state(root, receipt_path)}
    receipt = json.loads(receipt_path.read_bytes())
    if not isinstance(receipt, dict):
        raise ValueError("The prepared image receipt must be an object.")
    image = _managed(root, receipt_path.with_suffix(""))
    pack = _managed(root, image.with_suffix(""))
    if (receipt.get("image_path") != str(image) or receipt.get("pack_path") != str(pack)
            or image.parent != exports or pack.parent != exports or not pack.is_dir()):
        raise ValueError("The prepared image receipt does not match its image and pack paths.")
    size = receipt.get("size_bytes")
    expected_hash = receipt.get("sha256")
    start, sectors = receipt.get("partition_start"), receipt.get("partition_sectors")
    if (type(size) is not int or size <= 0 or size % 512
            or not isinstance(expected_hash, str) or not SHA256.fullmatch(expected_hash)
            or type(start) is not int or start <= 0 or type(sectors) is not int or sectors <= 0
            or (start + sectors) * 512 > size):
        raise ValueError("The prepared image receipt has an invalid size, hash, or partition.")
    created_at = receipt.get("created_at")
    if not isinstance(created_at, str) or dt.datetime.fromisoformat(created_at.replace("Z", "+00:00")).tzinfo is None:
        raise ValueError("The prepared image receipt has an invalid creation time.")
    manifest_path = _managed(root, pack / "MANIFEST.json")
    states.update({image: _file_state(root, image), manifest_path: _file_state(root, manifest_path)})
    if states[image][2] != size:
        raise ValueError("The prepared image size no longer matches its receipt.")
    manifest_bytes = manifest_path.read_bytes()
    manifest = _manifest(manifest_bytes, pack.name)
    if (type(receipt.get("file_count")) is not int or receipt["file_count"] != len(manifest["files"]) + 1
            or type(receipt.get("payload_bytes")) is not int
            or receipt["payload_bytes"] != sum(record["bytes"] for record in manifest["files"]) + len(manifest_bytes)
            or receipt["payload_bytes"] > size):
        raise ValueError("The prepared image receipt no longer matches its pack manifest.")
    _unchanged(root, states)
    return _PreparedImage({
        "path": str(image), "name": manifest["name"], "profile": manifest["profile"],
        "release_ids": manifest["release_ids"], "game_count": len(manifest["release_ids"]),
        "size_bytes": size, "sha256": expected_hash, "created_at": created_at,
        "verification": "at-build",
    }, receipt, manifest_bytes, states)


def list_images(root: Path) -> dict:
    """Read managed receipts and manifests without hashing or mounting image bytes."""
    root = root.resolve(strict=True)
    if not root.is_dir():
        raise ValueError("Choose a library directory.")
    result = {"images": [], "issues": []}
    exports = root / "exports"
    if not exports.exists() and not exports.is_symlink():
        return result
    try:
        exports = _managed(root, exports)
        if not exports.is_dir():
            raise ValueError("The library exports path is not a directory.")
    except (OSError, ValueError) as error:
        result["issues"].append({"path": str(exports), "message": str(error)})
        return result
    for receipt_path in sorted(exports.glob("*.img.json")):
        try:
            result["images"].append(_prepared(root, receipt_path).item)
        except (OSError, ValueError) as error:
            result["issues"].append({"path": str(receipt_path), "message": str(error)})
    result["images"].sort(
        key=lambda item: dt.datetime.fromisoformat(item["created_at"].replace("Z", "+00:00")),
        reverse=True,
    )
    return result


def inspect_image(root: Path, path: Path) -> dict:
    """Verify a listed image's current bytes and embedded exact-build manifest."""
    root = root.resolve(strict=True)
    image = _managed(root, path)
    if image.parent != root / "exports" or image.suffix != ".img":
        raise ValueError("Choose a prepared image directly inside exports.")
    prepared = _prepared(root, image.with_suffix(".img.json"))
    size = prepared.item["size_bytes"]
    media.progress("inspect-image", 0, size * 2, "Verifying prepared image bytes")
    info = media.raw_image_info(image)
    if (info["size_bytes"] != size or not secrets.compare_digest(info["sha256"], prepared.item["sha256"])
            or any(info[key] != prepared.receipt[key] for key in ("partition_start", "partition_sectors"))):
        raise ValueError("The prepared image bytes no longer match their build receipt.")
    _unchanged(root, prepared.states)
    media.progress("inspect-image", size, size * 2, "Checking the embedded collection manifest")
    with media.mounted_image(image, readonly=True) as mount:
        mount = mount.resolve(strict=True)
        manifest_path = _managed(mount, mount / "MANIFEST.json")
        manifest_state = _file_state(mount, manifest_path)
        embedded = manifest_path.read_bytes()
        embedded_hash = hashlib.sha256(embedded).hexdigest()
        expected_hash = hashlib.sha256(prepared.manifest_bytes).hexdigest()
        if embedded != prepared.manifest_bytes or not secrets.compare_digest(embedded_hash, expected_hash):
            raise ValueError("The image's embedded manifest does not match its prepared pack.")
        manifest = _manifest(embedded, image.stem)
        if manifest["profile"] != prepared.item["profile"] or manifest["release_ids"] != prepared.item["release_ids"]:
            raise ValueError("The image contains a different profile or exact-build collection.")
        _unchanged(mount, {manifest_path: manifest_state})
    _unchanged(root, prepared.states)
    media.progress("inspect-image", size, size * 2, "Confirming image bytes after inspection")
    if not secrets.compare_digest(media.sha256(image), info["sha256"]):
        raise ValueError("The prepared image changed during inspection.")
    _unchanged(root, prepared.states)
    media.progress("inspect-image", size * 2, size * 2, "Prepared image verified")
    return {**prepared.item, "verification": "current", "verified": True,
            "verified_at": dt.datetime.now(dt.timezone.utc).isoformat()}
