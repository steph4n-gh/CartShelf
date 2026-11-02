"""Prepare the pinned, relocatable Python runtime used by the Mac application."""
from __future__ import annotations

import argparse
import hashlib
import json
import platform
import shutil
import tarfile
import urllib.request
import uuid
import zipfile
from pathlib import Path, PurePosixPath


ROOT = Path(__file__).resolve().parents[1]
LOCK = Path(__file__).with_name('runtime-lock.json')


def sha256(path: Path) -> str:
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def load_lock() -> dict:
    value = json.loads(LOCK.read_text())
    if value.get('schema') != 1:
        raise ValueError('Unsupported runtime lock schema.')
    return value


def artifact_file(record: dict, cache: Path) -> Path:
    cache.mkdir(parents=True, exist_ok=True)
    path = cache / record['filename']
    if path.is_file() and path.stat().st_size == record['bytes'] and sha256(path) == record['sha256']:
        return path
    temporary = cache / ('.' + uuid.uuid4().hex + '.download')
    try:
        request = urllib.request.Request(record['url'], headers={'User-Agent': 'CartShelf-build'})
        with urllib.request.urlopen(request, timeout=90) as source, temporary.open('xb') as target:
            shutil.copyfileobj(source, target)
        if temporary.stat().st_size != record['bytes'] or sha256(temporary) != record['sha256']:
            raise ValueError('Downloaded runtime artifact does not match its pinned size and SHA-256.')
        temporary.replace(path)
        return path
    finally:
        temporary.unlink(missing_ok=True)


def wheel_contents(archive: Path, destination: Path) -> None:
    with zipfile.ZipFile(archive) as wheel:
        for member in wheel.infolist():
            relative = PurePosixPath(member.filename)
            if relative.is_absolute() or '..' in relative.parts or '\\' in member.filename:
                raise ValueError('A wheel contains an unsafe path.')
            if member.external_attr >> 16 & 0o170000 == 0o120000:
                raise ValueError('A wheel contains an unsupported symbolic link.')
            wheel.extract(member, destination)


def tree_manifest(root: Path) -> tuple[list[dict], list[dict]]:
    files, links = [], []
    for path in sorted(root.rglob('*')):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            if not path.resolve().is_relative_to(root.resolve()) or not path.exists():
                raise ValueError('The runtime has a symbolic link outside its installation.')
            links.append({'path': relative, 'target': path.readlink().as_posix()})
        elif path.is_file():
            files.append({'path': relative, 'bytes': path.stat().st_size, 'sha256': sha256(path)})
        elif not path.is_dir():
            raise ValueError('The runtime contains an unsupported file type.')
    return files, links


def runtime_manifest(destination: Path, architecture: str) -> dict:
    lock = load_lock()
    files, links = tree_manifest(destination)
    return {
        'schema': 1, 'python_version': lock['python_version'], 'architecture': architecture,
        'source': lock['python'][architecture],
        'dependencies': [
            {'name': package['name'], 'version': package['version'], **package['architectures'][architecture]}
            for package in lock['dependencies']
        ],
        'files': files, 'symlinks': links,
    }


def copy_runtime_notices(destination: Path) -> None:
    source = Path(__file__).with_name('runtime-licenses')
    provenance = json.loads((source / 'PROVENANCE.json').read_text())
    records = provenance['files']
    expected = {row['path'] for row in records} | {'PROVENANCE.json'}
    if {path.name for path in source.iterdir()} != expected:
        raise ValueError('The runtime license inventory differs from its provenance record.')
    for row in records:
        path = source / row['path']
        if path.is_symlink() or not path.is_file() or path.stat().st_size != row['bytes'] or sha256(path) != row['sha256']:
            raise ValueError('An upstream runtime license text changed.')
    shutil.copytree(source, destination / 'share/licenses/python-build-standalone')


def prepare_runtime(destination: Path, cache: Path, architecture: str | None = None) -> dict:
    architecture = architecture or platform.machine()
    lock = load_lock()
    if architecture not in lock['python']:
        raise ValueError('Build on a supported arm64 or x86_64 Mac.')
    if destination.exists() or destination.is_symlink():
        raise FileExistsError('Choose a new runtime preparation directory.')
    destination.parent.mkdir(parents=True, exist_ok=True)
    scratch = destination.parent / ('.runtime-' + uuid.uuid4().hex)
    scratch.mkdir()
    try:
        archive = artifact_file(lock['python'][architecture], cache)
        with tarfile.open(archive, 'r:gz') as source:
            source.extractall(scratch, filter='data')
        prepared = scratch / 'python'
        if not (prepared / 'bin/python3').is_file():
            raise ValueError('The pinned runtime archive has an unexpected layout.')
        site = prepared / ('lib/python' + '.'.join(lock['python_version'].split('.')[:2]) + '/site-packages')
        site.mkdir(parents=True, exist_ok=True)
        for package in lock['dependencies']:
            wheel_contents(artifact_file(package['architectures'][architecture], cache), site)
        copy_runtime_notices(prepared)
        for path in sorted(prepared.rglob('__pycache__'), reverse=True):
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
        for path in prepared.rglob('*.pyc'):
            path.unlink()
        tree_manifest(prepared)
        prepared.rename(destination)
        return runtime_manifest(destination, architecture)
    finally:
        shutil.rmtree(scratch)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--destination', required=True, type=Path)
    parser.add_argument('--cache', type=Path, default=ROOT / 'build/downloads')
    parser.add_argument('--architecture', choices=['arm64', 'x86_64'], default=platform.machine())
    parser.add_argument('--manifest', type=Path)
    args = parser.parse_args()
    result = prepare_runtime(args.destination, args.cache, args.architecture)
    if args.manifest:
        args.manifest.parent.mkdir(parents=True, exist_ok=True)
        args.manifest.write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps({'python_version': result['python_version'], 'architecture': result['architecture'],
                      'files': len(result['files']), 'symlinks': len(result['symlinks'])}))


if __name__ == '__main__':
    main()
