# SPDX-License-Identifier: GPL-3.0-only
"""Import supported cartridge system files into a chosen library without flashing."""
from __future__ import annotations

from dataclasses import dataclass
import ctypes
import datetime as dt
import errno
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import sys
import tempfile
import zipfile


@dataclass(frozen=True)
class SystemFile:
    profile: str
    version: str
    filename: str
    size: int
    sha256: str
    official_url: str
    binary: str
    provenance: str


SPECS = {
    "chromatic": SystemFile(
        "chromatic", "1.06", "GBCOS.BIN", 81920,
        "c61824768584b01f7fabe290eccc5dd9e10d78c49cc948a9addba1fb86204635",
        "https://krikzz.com/pub/support/everdrive-gb/x-series/OS/",
        "cache/firmware/gb-x7/GBCSYS/GBCOS.BIN", "cache/firmware/gb-x7/source.json"),
    "m64": SystemFile(
        "m64", "26.1014", "ed64-pro-fw-v26.1014.efu", 1631184,
        "576ffd43f80de93c2e1f8d28bbfecdabca2b52b5305ebda5a08cc78bc669b1ad",
        "https://krikzz.com/pub/support/everdrive-64/pro-series/",
        "cache/firmware/ed64-pro-fw-v26.1014.efu",
        "cache/firmware/ed64-pro-fw-v26.1014.source.json"),
}
MAX_INPUT_BYTES = 16 * 1024 * 1024
MAX_ZIP_ENTRIES = 512
MAX_RECEIPT_BYTES = 16 * 1024


def _spec(profile: str) -> SystemFile:
    if not isinstance(profile, str) or profile not in SPECS:
        raise ValueError("Choose M64 or Chromatic system files.")
    return SPECS[profile]


def _managed(library, path: Path) -> Path:
    path = library._managed(path)
    current = library.root
    for part in path.relative_to(library.root).parts:
        current /= part
        if current.is_symlink():
            raise ValueError("System-file cache paths cannot contain symbolic links.")
    return path


def _read_regular(path: Path, limit: int) -> bytes:
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode) or before.st_size > limit:
        raise ValueError("Choose a bounded regular system file or ZIP, not a symbolic link.")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as source:
        opened = os.fstat(source.fileno())
        if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise ValueError("The selected system file changed before reading.")
        data = source.read(limit + 1)
        after = os.fstat(source.fileno())
        if (len(data) > limit or len(data) != after.st_size
                or (opened.st_ino, opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns)
                != (after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)):
            raise ValueError("The selected system file changed while reading or exceeds the size limit.")
    return data


def _verified(data: bytes, spec: SystemFile) -> None:
    if len(data) != spec.size or hashlib.sha256(data).hexdigest() != spec.sha256:
        raise ValueError("The selected file does not match the supported " + spec.filename
                         + " version " + spec.version + " size and SHA-256. Existing cache files were kept.")


def _payload(path: Path, spec: SystemFile) -> bytes:
    encoded = _read_regular(path, MAX_INPUT_BYTES)
    if path.suffix.casefold() != ".zip":
        _verified(encoded, spec)
        return encoded
    try:
        with zipfile.ZipFile(io.BytesIO(encoded)) as archive:
            members = archive.infolist()
            if len(members) > MAX_ZIP_ENTRIES:
                raise ValueError("The system-files ZIP has too many entries.")
            selected = []
            for member in members:
                name = member.filename
                relative = PurePosixPath(name)
                mode = member.external_attr >> 16
                if (not name or "\\" in name or any(ord(char) < 32 for char in name)
                        or relative.is_absolute() or ".." in relative.parts
                        or any(":" in part for part in relative.parts)
                        or relative.as_posix() != name.rstrip("/")
                        or stat.S_IFMT(mode) not in {0, stat.S_IFREG, stat.S_IFDIR}
                        or member.flag_bits & 1):
                    raise ValueError("The system-files ZIP contains an unsupported path or entry.")
                if not member.is_dir() and relative.name.casefold() == spec.filename.casefold():
                    selected.append(member)
            if len(selected) != 1 or selected[0].file_size != spec.size:
                raise ValueError("Choose a ZIP containing exactly one supported " + spec.filename + ".")
            with archive.open(selected[0]) as source:
                data = source.read(spec.size + 1)
    except (zipfile.BadZipFile, RuntimeError, NotImplementedError) as error:
        raise ValueError("The selected system-files ZIP cannot be read.") from error
    _verified(data, spec)
    return data


def _receipt(library, path: Path, spec: SystemFile) -> bool:
    if not os.path.lexists(_managed(library, path)):
        return False
    try:
        value = json.loads(_read_regular(path, MAX_RECEIPT_BYTES))
    except (json.JSONDecodeError, RecursionError) as error:
        raise ValueError("The system-file provenance is unreadable. Keep the existing cache before repairing it.") from error
    expected = value.get("sha256") if isinstance(value, dict) else None
    if not isinstance(expected, str) or expected.lower() != spec.sha256:
        raise ValueError("The system-file provenance changed. Keep the existing cache before repairing it.")
    return True


def status(library, profile: str) -> dict:
    spec = _spec(profile)
    result = {"profile": profile, "status": "missing", "version": spec.version,
              "sha256": spec.sha256, "official_url": spec.official_url,
              "message": "Import supported " + spec.filename + " version " + spec.version
                         + " from a file or official ZIP. No cartridge update runs automatically."}
    try:
        binary = _managed(library, library.root / spec.binary)
        receipt = _managed(library, library.root / spec.provenance)
        has_receipt = _receipt(library, receipt, spec)
        if not os.path.lexists(binary):
            if has_receipt:
                result.update(status="changed", message="The cached system file is missing. Reimport the supported file to complete the cache.")
            return result
        _verified(_read_regular(binary, spec.size), spec)
        if profile == "chromatic" and not has_receipt:
            result.update(status="changed", message="The verified OS needs its cache provenance. Reimport the same supported file to complete the cache.")
            return result
        result.update(status="ready", path=str(binary),
                      message="Verified " + spec.filename + " version " + spec.version
                              + " is ready for packs. Installed cartridge firmware remains unknown.")
    except (OSError, ValueError) as error:
        result.update(status="changed", message=str(error))
    return result


def _write(path: Path, data: bytes) -> None:
    with path.open("xb") as target:
        target.write(data)
        target.flush()
        os.fsync(target.fileno())
    if _read_regular(path, len(data)) != data:
        raise OSError("The staged system file did not verify.")


def _sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _publish(source: Path, destination: Path) -> None:
    try:
        os.link(source, destination)
        return
    except OSError as error:
        if sys.platform != "darwin" or error.errno not in {errno.EPERM, errno.ENOTSUP}:
            raise
    library = ctypes.CDLL(None, use_errno=True)
    rename = library.renamex_np
    rename.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
    rename.restype = ctypes.c_int
    if rename(os.fsencode(source), os.fsencode(destination), 0x00000004):
        error = ctypes.get_errno()
        if error == errno.ENOTSUP:
            raise OSError(error, "This library filesystem cannot publish system files atomically without replacing existing data.")
        raise OSError(error, os.strerror(error), str(destination))


def import_file(library, profile: str, path: Path) -> dict:
    spec = _spec(profile)
    data = _payload(Path(path), spec)
    binary = _managed(library, library.root / spec.binary)
    receipt = _managed(library, library.root / spec.provenance)
    if os.path.lexists(binary):
        _verified(_read_regular(binary, spec.size), spec)
    has_receipt = _receipt(library, receipt, spec)
    provenance = {"schema": 1, "kind": "cartshelf-system-file", "profile": profile,
                  "version": spec.version, "sha256": spec.sha256, "bytes": spec.size,
                  "official_url": spec.official_url, "import_method": "user-selected offline file",
                  "imported_at": dt.datetime.now(dt.timezone.utc).isoformat()}
    parent = _managed(library, library.root / "cache/firmware")
    parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=".system-file-", dir=_managed(library, parent)))
    identity = temporary.stat()
    try:
        staged_binary, staged_receipt = temporary / "binary", temporary / "source.json"
        _write(_managed(library, staged_binary), data)
        encoded = (json.dumps(provenance, sort_keys=True, indent=2) + "\n").encode("utf-8")
        _write(_managed(library, staged_receipt), encoded)
        _sync_directory(_managed(library, temporary))
        for target in (receipt.parent, binary.parent):
            _managed(library, target).mkdir(parents=True, exist_ok=True)
        if not has_receipt:
            try:
                _publish(staged_receipt, _managed(library, receipt))
            except FileExistsError:
                _receipt(library, receipt, spec)
        try:
            _publish(staged_binary, _managed(library, binary))
        except FileExistsError:
            _verified(_read_regular(_managed(library, binary), spec.size), spec)
        _sync_directory(_managed(library, receipt.parent))
        _sync_directory(_managed(library, binary.parent))
        result = status(library, profile)
        if result["status"] != "ready":
            raise ValueError(result["message"])
        return result
    finally:
        try:
            _managed(library, temporary)
            current = temporary.lstat()
            if (current.st_dev, current.st_ino) == (identity.st_dev, identity.st_ino) and stat.S_ISDIR(current.st_mode):
                shutil.rmtree(temporary)
        except (OSError, ValueError):
            pass
