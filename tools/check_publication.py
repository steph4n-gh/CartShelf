#!/usr/bin/env python3
"""Check allowlisted source, Git history, archives, and app resources before release."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import plistlib
import re
import stat
import subprocess
import tarfile
import tempfile
import zipfile


MANIFEST = 'source-manifest.json'
MAX_FILE_BYTES = 16 * 1024 * 1024
IGNORED_DIRECTORIES = {'.git', '.venv', '.audit', '__pycache__', 'build', 'dist', 'tmp'}
PRIVATE_DIRECTORIES = {'library', 'backups', 'exports', 'cache', 'reference', 'reports',
                       'quarantine', 'legacy', 'homebrew', 'n64romspack', 'custom-metadata'}
PRIVATE_SUFFIXES = {'.gb', '.gbc', '.gba', '.nes', '.nds', '.z64', '.v64', '.n64', '.rom',
                    '.srm', '.sav', '.eep', '.mpk', '.rtc', '.efu', '.bin', '.img', '.dmg',
                    '.zip', '.7z', '.tar', '.gz', '.p12', '.pem', '.key', '.pyc'}
PRIVATE_TEXT = (
    re.compile(rb'/(?:Users|home)/[^/\s"\']+'),
    re.compile(b'/Volumes/' + b'Storage(?:/|["\'])'),
    re.compile(b'rom' + b'Project'),
    re.compile(rb'-----BEGIN (?:[A-Z]+ )?PRIVATE KEY-----'),
    re.compile(rb'gh[pousr]_[A-Za-z0-9]{20,}'),
)
PROHIBITED_ASSETS = {
    'c525c5fba96357c41873e4e5265ceeb01562d2406bcef6c9c867517d25b8cb51',
    '296c11ae9484683dcfe42afad28fd0a6023d06df720a6354a7f746a1798d1e68',
}


class PublicationError(ValueError):
    pass


def relative_name(name: str) -> str:
    path = PurePosixPath(name)
    if (not name or path.is_absolute() or '\\' in name or '\x00' in name
            or path.as_posix() != name or any(part in {'', '.', '..'} for part in name.split('/'))):
        raise PublicationError('An entry has an unsafe relative path.')
    if any(part.casefold() in PRIVATE_DIRECTORIES for part in path.parts):
        raise PublicationError('Private data directory is present: ' + name)
    if path.suffix.casefold() in PRIVATE_SUFFIXES or path.name.startswith('._') or path.name == '.DS_Store':
        raise PublicationError('Private or generated payload is present: ' + name)
    return name


def load_manifest(root: Path) -> dict:
    path = root / MANIFEST
    if path.is_symlink() or not path.is_file():
        raise PublicationError('The source allowlist manifest is missing or linked.')
    try:
        value = json.loads(path.read_text())
    except (ValueError, UnicodeError) as error:
        raise PublicationError('The source allowlist manifest is unreadable.') from error
    if (not isinstance(value, dict) or set(value) != {'schema', 'files', 'required_files'}
            or type(value['schema']) is not int or value['schema'] != 1 or not isinstance(value['files'], dict)):
        raise PublicationError('The source allowlist schema is unsupported.')
    for name, record in value['files'].items():
        relative_name(name)
        if not isinstance(record, dict) or record.get('kind') not in {'text', 'asset'}:
            raise PublicationError('Every allowlisted file needs a source role: ' + name)
        if record['kind'] == 'asset':
            if (set(record) != {'kind', 'sha256', 'notice'} or not isinstance(record['sha256'], str)
                    or not re.fullmatch('[0-9a-f]{64}', record['sha256'])):
                raise PublicationError('Every binary asset needs an exact hash and notice: ' + name)
            if not isinstance(record['notice'], str) or record['notice'] not in value['files']:
                raise PublicationError('The asset notice is not allowlisted: ' + name)
        elif set(record) != {'kind'}:
            raise PublicationError('Text allowlist entries use only a source role: ' + name)
    if (not isinstance(value['required_files'], list)
            or any(not isinstance(name, str) or name not in value['files'] for name in value['required_files'])):
        raise PublicationError('Required source files must be allowlisted.')
    return value


def check_record(name: str, data: bytes, manifest: dict, *, mode: int = stat.S_IFREG) -> None:
    relative_name(name)
    if stat.S_IFMT(mode) not in {0, stat.S_IFREG}:
        raise PublicationError('Linked or special file is present: ' + name)
    record = manifest['files'].get(name)
    if record is None:
        raise PublicationError('File has no publication allowlist entry: ' + name)
    if len(data) > MAX_FILE_BYTES:
        raise PublicationError('Source file exceeds the reviewed size bound: ' + name)
    digest = hashlib.sha256(data).hexdigest()
    if digest in PROHIBITED_ASSETS:
        raise PublicationError('An unlicensed historical theme asset is present: ' + name)
    if record['kind'] == 'asset':
        if digest != record['sha256']:
            raise PublicationError('Reviewed asset bytes changed: ' + name)
    else:
        try:
            data.decode('utf-8')
        except UnicodeError as error:
            raise PublicationError('An unreviewed binary is disguised as source: ' + name) from error
        if b'\x00' in data:
            raise PublicationError('Binary data is disguised as source: ' + name)
        if any(pattern.search(data) for pattern in PRIVATE_TEXT):
            raise PublicationError('Private paths or credentials are present: ' + name)


def source_files(root: Path, ignored_directories=IGNORED_DIRECTORIES):
    for current, directories, names in os.walk(root, followlinks=False):
        base = Path(current)
        for name in list(directories):
            path = base / name
            if path.is_symlink():
                raise PublicationError('Linked directory is present: ' + str(path.relative_to(root)))
        excluded = ignored_directories if base == root else ({'__pycache__'} if ignored_directories else set())
        directories[:] = [name for name in directories if name not in excluded]
        for name in names:
            path = base / name
            mode = path.lstat().st_mode
            if not stat.S_ISREG(mode):
                raise PublicationError('Linked or special file is present: ' + str(path.relative_to(root)))
            if base == root and name == '.git':
                continue
            yield path.relative_to(root).as_posix(), path, mode


def check_worktree(root: Path, manifest: dict) -> int:
    found = set()
    for name, path, mode in source_files(root):
        relative_name(name)
        if name not in manifest['files']:
            raise PublicationError('File has no publication allowlist entry: ' + name)
        if path.stat().st_size > MAX_FILE_BYTES:
            raise PublicationError('Source file exceeds the reviewed size bound: ' + name)
        check_record(name, path.read_bytes(), manifest, mode=mode)
        found.add(name)
    missing = set(manifest['required_files']) - found
    if missing:
        raise PublicationError('Required source files are missing: ' + ', '.join(sorted(missing)))
    return len(found)


def git(root: Path, *args: str, allow_failure: bool = False) -> bytes:
    result = subprocess.run(['git', '-C', str(root), *args], capture_output=True, check=False)
    if result.returncode and not allow_failure:
        raise PublicationError('A Git publication inspection failed.')
    return result.stdout if not result.returncode else b''


def git_entries(data: bytes):
    for row in data.split(b'\x00'):
        if row:
            header, path = row.split(b'\t', 1)
            fields = header.decode('ascii').split()
            mode, kind, oid = fields if len(fields) == 3 and fields[1] in {'blob', 'commit', 'tree'} else (fields[0], 'blob', fields[1])
            if kind != 'blob':
                raise PublicationError('A Git submodule or non-file entry is present.')
            yield path.decode('utf-8'), int(mode, 8), oid


def check_git(root: Path, manifest: dict, *, require_clean: bool = False) -> dict:
    if not git(root, 'rev-parse', '--is-inside-work-tree', allow_failure=True):
        return {'index_files': 0, 'history_blobs': 0, 'commits': 0, 'git_present': False}
    if require_clean and git(root, 'status', '--porcelain', '--untracked-files=all'):
        raise PublicationError('The release source tree has uncommitted or untracked changes.')
    index = list(git_entries(git(root, 'ls-files', '--stage', '-z')))
    checked = set()

    def verify_blob(name, mode, oid):
        relative_name(name)
        if name not in manifest['files']:
            raise PublicationError('File has no publication allowlist entry: ' + name)
        if int(git(root, 'cat-file', '-s', oid)) > MAX_FILE_BYTES:
            raise PublicationError('Git blob exceeds the reviewed source size bound: ' + name)
        check_record(name, git(root, 'cat-file', 'blob', oid), manifest, mode=mode)

    for name, mode, oid in index:
        verify_blob(name, mode, oid)
        checked.add((name, mode, oid))
    commits = git(root, 'rev-list', '--all', '--reflog').decode().splitlines()
    historical = set()
    for commit in commits:
        for name, mode, oid in git_entries(git(root, 'ls-tree', '-rz', '--full-tree', commit)):
            entry = (name, mode, oid)
            if entry not in checked:
                verify_blob(name, mode, oid)
                checked.add(entry)
            historical.add(entry)
    return {'index_files': len(index), 'history_blobs': len(historical), 'commits': len(commits), 'git_present': True}


def check_source_archive(archive: Path, manifest: dict) -> int:
    entries = []
    archive_bytes = 0
    if zipfile.is_zipfile(archive):
        with zipfile.ZipFile(archive) as source:
            for item in source.infolist():
                if not item.is_dir():
                    relative_name(item.filename)
                    archive_bytes += item.file_size
                    if archive_bytes > 128 * 1024 * 1024:
                        raise PublicationError('Source archive exceeds the reviewed size bound.')
                    if (item.file_size > MAX_FILE_BYTES or len(entries) >= 4096):
                        raise PublicationError('Archive member exceeds the source size bound.')
                    entries.append((item.filename, source.read(item), item.external_attr >> 16))
    else:
        with tarfile.open(archive) as source:
            for item in source:
                if item.isdir():
                    continue
                relative_name(item.name)
                archive_bytes += item.size
                if archive_bytes > 128 * 1024 * 1024:
                    raise PublicationError('Source archive exceeds the reviewed size bound.')
                if not item.isfile() or item.size > MAX_FILE_BYTES or len(entries) >= 4096:
                    raise PublicationError('Source archive contains a linked, special, or oversized entry.')
                entries.append((item.name, source.extractfile(item).read(), item.mode | stat.S_IFREG))
    manifests = [name for name, _, _ in entries if PurePosixPath(name).name == MANIFEST]
    if len(manifests) != 1:
        raise PublicationError('Source archive needs exactly one allowlist manifest.')
    archived_manifest = next(data for name, data, _ in entries if name == manifests[0])
    if json.loads(archived_manifest) != manifest:
        raise PublicationError('Archive allowlist differs from the reviewed source manifest.')
    parent = PurePosixPath(manifests[0]).parent
    prefix = '' if parent == PurePosixPath('.') else parent.as_posix() + '/'
    found = set()
    for raw, data, mode in entries:
        if not raw.startswith(prefix):
            raise PublicationError('Source archive contains entries outside its source root.')
        name = raw[len(prefix):]
        if name in found:
            raise PublicationError('Source archive contains duplicate entries.')
        check_record(name, data, manifest, mode=mode)
        found.add(name)
    if set(manifest['required_files']) - found:
        raise PublicationError('Source archive omits required source files.')
    return len(found)


def check_app(app: Path, root: Path, manifest: dict) -> dict:
    resources = app / 'Contents/Resources'
    contents = app / 'Contents'
    for path in contents.iterdir():
        if path.name not in {'MacOS', 'Resources', '_CodeSignature', 'Info.plist', 'PkgInfo'}:
            raise PublicationError('An unreviewed app content entry is present: ' + path.name)
    macos = contents / 'MacOS'
    if macos.exists():
        for path in macos.iterdir():
            if path.name != 'CartShelf' or path.is_symlink() or not path.is_file():
                raise PublicationError('An unreviewed app executable entry is present.')
    signature = contents / '_CodeSignature'
    if signature.exists():
        for path in signature.iterdir():
            if path.name != 'CodeResources' or path.is_symlink() or not path.is_file():
                raise PublicationError('An unexpected code-signature entry is present.')
    for path in (app, app / 'Contents', resources, resources / 'core', resources / 'python'):
        if path.is_symlink() or not path.is_dir():
            raise PublicationError('App directories are missing or linked.')
    info_path = app / 'Contents/Info.plist'
    if info_path.is_symlink():
        raise PublicationError('The app property list is linked.')
    info = plistlib.loads(info_path.read_bytes())
    if ('CartShelfRoot' in info or 'CartShelfPython' in info or 'CartShelfInitialLibraryRoot' in info
            or info.get('CartShelfQABuild') is True):
        raise PublicationError('The public app is bound to a local library or interpreter.')
    if info.get('CartShelfCoreRelativePath') != 'core' or info.get('CartShelfPythonRelativePath') != 'python/bin/python3':
        raise PublicationError('The app does not use the reviewed relative runtime layout.')
    release_path = root / 'release.json'
    packaged_release_path = resources / 'release-manifest.json'
    native_path = root / 'app/CartShelf.swift'
    for path in (release_path, packaged_release_path, native_path):
        if path.is_symlink() or not path.is_file():
            raise PublicationError('The reviewed release or native source metadata is missing or linked.')
    try:
        release = json.loads(release_path.read_text())
        packaged_release = json.loads(packaged_release_path.read_text())
    except (ValueError, UnicodeError) as error:
        raise PublicationError('The release metadata is unreadable.') from error
    release_strings = ('version', 'bundle_version', 'channel', 'minimum_macos', 'bundle_id')
    if (not isinstance(release, dict) or release.get('schema') != 1
            or type(release.get('build')) is not int
            or any(not isinstance(release.get(key), str) or not release[key] for key in release_strings)
            or not isinstance(packaged_release, dict) or packaged_release.get('schema') != 1):
        raise PublicationError('The release metadata is unsupported.')
    if any(packaged_release.get(key) != release[key] for key in ('version', 'build', 'minimum_macos')):
        raise PublicationError('The packaged release metadata differs from the reviewed release.')
    expected_info = {
        'CFBundleIdentifier': release['bundle_id'], 'CFBundleShortVersionString': release['bundle_version'],
        'CFBundleVersion': str(release['build']), 'CartShelfVersion': release['version'],
        'CartShelfReleaseChannel': release['channel'], 'LSMinimumSystemVersion': release['minimum_macos'],
    }
    if any(info.get(key) != value for key, value in expected_info.items()):
        raise PublicationError('The app property-list release metadata differs from the reviewed release.')
    if packaged_release.get('native_source_sha256') != hashlib.sha256(native_path.read_bytes()).hexdigest():
        raise PublicationError('The packaged native source differs from the reviewed checkout.')
    core = resources / 'core'
    count = 0
    core_manifest = json.loads((resources / 'core-manifest.json').read_text())
    core_records = {row['path']: row for row in core_manifest['files']}
    core_found = set()
    for name, path, mode in source_files(core, set()):
        check_record(name, path.read_bytes(), manifest, mode=mode)
        if (root / name).read_bytes() != path.read_bytes():
            raise PublicationError('Bundled source differs from the reviewed checkout: ' + name)
        if core_records.get(name) != {'path': name, 'bytes': path.stat().st_size,
                                       'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}:
            raise PublicationError('Bundled source differs from its content manifest: ' + name)
        core_found.add(name)
        count += 1
    if core_found != set(core_records) or not {'library_manager.py', 'media.py', 'card_saves.py', 'library_experience.py'} <= core_found:
        raise PublicationError('The bundled core inventory is incomplete or unexpected.')
    runtime_manifest = json.loads((resources / 'runtime-manifest.json').read_text())
    if runtime_manifest.get('schema') != 1 or not isinstance(runtime_manifest.get('files'), list):
        raise PublicationError('The runtime content manifest is unsupported.')
    expected = {}
    symlinks = {}
    for row in runtime_manifest.get('symlinks', []):
        name = row['path']
        path = PurePosixPath(name)
        if path.is_absolute() or path.as_posix() != name or '..' in path.parts or name in symlinks:
            raise PublicationError('The runtime content manifest has unsafe or duplicate links.')
        symlinks[name] = row['target']
    for row in runtime_manifest['files']:
        name = row['path']
        path = PurePosixPath(name)
        if path.is_absolute() or path.as_posix() != name or '..' in path.parts or name in expected or name in symlinks:
            raise PublicationError('The runtime content manifest has unsafe or duplicate paths.')
        expected[name] = row
    found = set()
    runtime = resources / 'python'
    for current, directories, names in os.walk(runtime, followlinks=False):
        for name in directories + names:
            path = Path(current) / name
            relative = path.relative_to(runtime).as_posix()
            if path.is_symlink():
                target = path.resolve()
                if not target.is_relative_to(runtime.resolve()):
                    raise PublicationError('Runtime link escapes the app bundle.')
                if relative not in symlinks or symlinks[relative] != os.readlink(path):
                    raise PublicationError('Runtime link is absent from its content manifest.')
                found.add(relative)
            elif path.is_file():
                row = expected.get(relative)
                if row is None or row.get('bytes') != path.stat().st_size or row.get('sha256') != hashlib.sha256(path.read_bytes()).hexdigest():
                    raise PublicationError('Runtime bytes differ from their manifest: ' + relative)
                if path.suffix == '.pyc':
                    raise PublicationError('Runtime bytecode is not permitted in the beta package.')
                runtime_patterns = PRIVATE_TEXT[1:] + (re.compile(re.escape(str(Path.home()).encode())),)
                if path.suffix in {'.py', '.json', '.pth', '.cfg'} and any(pattern.search(path.read_bytes()) for pattern in runtime_patterns):
                    raise PublicationError('Private paths are present in runtime text.')
                found.add(relative)
            elif not path.is_dir():
                raise PublicationError('A special runtime entry is present.')
    if found != set(expected) | set(symlinks):
        raise PublicationError('Runtime inventory differs from its content manifest.')
    resource_sources = {
        'Silkscreen-Regular.ttf': 'app/Resources/Silkscreen-Regular.ttf',
        'Silkscreen-Bold.ttf': 'app/Resources/Silkscreen-Bold.ttf',
        'OFL.txt': 'app/Resources/OFL.txt',
        'LICENSE': 'LICENSE',
        'THIRD_PARTY_NOTICES.md': 'THIRD_PARTY_NOTICES.md',
        'LICENSE-ASSETS.md': 'themes/CartShelf-64/LICENSE-ASSETS.md',
    }
    outer = {'core-manifest.json', 'runtime-manifest.json', 'release-manifest.json', 'CartShelf.icns'} | set(resource_sources)
    for path in resources.iterdir():
        if path.name in {'core', 'python'}:
            if path.is_symlink() or not path.is_dir():
                raise PublicationError('Bundled code or runtime is linked or missing.')
        elif path.is_symlink() or not path.is_file() or path.name not in outer:
            raise PublicationError('An unreviewed outer app resource is present: ' + path.name)
        elif path.name in resource_sources:
            source = root / resource_sources[path.name]
            if source.is_symlink() or not source.is_file():
                raise PublicationError('Bundled font or license has no regular reviewed source: ' + path.name)
            if path.read_bytes() != source.read_bytes():
                raise PublicationError('Bundled font or license differs from reviewed source.')
        elif path.suffix == '.json' and any(pattern.search(path.read_bytes()) for pattern in PRIVATE_TEXT):
            raise PublicationError('Private paths are present in a package manifest.')
    return {'core_files': count, 'runtime_entries': len(found)}


def check_app_archive(archive: Path, root: Path, manifest: dict) -> dict:
    if not zipfile.is_zipfile(archive):
        raise PublicationError('The app release archive must be a ZIP.')
    with zipfile.ZipFile(archive) as source, tempfile.TemporaryDirectory(prefix='cartshelf-archive-check-') as temporary:
        destination = Path(temporary).resolve()
        items = source.infolist()
        if len(items) > 30000 or sum(item.file_size for item in items) > 512 * 1024 * 1024:
            raise PublicationError('The app archive exceeds the reviewed size bound.')
        names = set()
        app_names = set()
        links = []
        for item in items:
            raw = item.filename.rstrip('/') if item.is_dir() else item.filename
            name = PurePosixPath(raw)
            if (name.is_absolute() or name.as_posix() != raw or '\\' in raw
                    or any(part in {'', '.', '..'} for part in raw.split('/')) or raw in names):
                raise PublicationError('The app archive contains an unsafe or duplicate path.')
            names.add(raw)
            app_names.add(name.parts[0])
            target = destination.joinpath(*name.parts)
            mode = item.external_attr >> 16
            if stat.S_ISLNK(mode):
                link = source.read(item).decode('utf-8')
                if not link or '\x00' in link or Path(link).is_absolute():
                    raise PublicationError('The app archive contains an unsafe link.')
                links.append((target, link))
            elif item.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            elif stat.S_IFMT(mode) in {0, stat.S_IFREG}:
                target.parent.mkdir(parents=True, exist_ok=True)
                with target.open('xb') as stream:
                    stream.write(source.read(item))
            else:
                raise PublicationError('The app archive contains a special file.')
        if len(app_names) != 1 or not next(iter(app_names)).endswith('.app'):
            raise PublicationError('The app archive must contain one app and no extra payloads.')
        for target, link in links:
            target.parent.mkdir(parents=True, exist_ok=True)
            if not (target.parent / link).resolve().is_relative_to(destination):
                raise PublicationError('The app archive contains an escaping link.')
            target.symlink_to(link)
        return check_app(destination / next(iter(app_names)), root, manifest)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--require-clean', action='store_true')
    parser.add_argument('--source-archive', type=Path)
    parser.add_argument('--app', type=Path)
    parser.add_argument('--app-archive', type=Path)
    args = parser.parse_args(argv)
    try:
        root = args.root.resolve(strict=True)
        manifest = load_manifest(root)
        result = {'passed': True, 'source_files': check_worktree(root, manifest),
                  **check_git(root, manifest, require_clean=args.require_clean)}
        if args.source_archive:
            result['archive_files'] = check_source_archive(args.source_archive, manifest)
        if args.app:
            result['app'] = check_app(args.app, root, manifest)
        if args.app_archive:
            result['app_archive'] = check_app_archive(args.app_archive, root, manifest)
        print(json.dumps(result, indent=2))
        return 0
    except (PublicationError, OSError, ValueError, KeyError, RuntimeError, tarfile.TarError, zipfile.BadZipFile) as error:
        print(json.dumps({'passed': False, 'error': str(error)}, indent=2))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
