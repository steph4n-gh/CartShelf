#!/usr/bin/env python3
"""Verify a relocated packaged runtime without a personal library or SD card."""
from __future__ import annotations

import argparse
import hashlib
import json
import plistlib
import shutil
import subprocess
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP = (
    "import runpy,sys;core=sys.argv.pop(1);name=sys.argv.pop(1);"
    "sys.path.insert(0,core);sys.argv[0]=core+'/'+name;"
    "runpy.run_path(sys.argv[0],run_name='__main__')"
)


def digest(path: Path) -> str:
    with path.open('rb') as source:
        return hashlib.file_digest(source, 'sha256').hexdigest()


def verify_inventory(folder: Path, manifest: dict) -> None:
    expected = {row['path'] for row in manifest['files']}
    links = {row['path']: row['target'] for row in manifest.get('symlinks', [])}
    actual = {path.relative_to(folder).as_posix() for path in folder.rglob('*') if path.is_file() or path.is_symlink()}
    assert actual == expected | set(links), 'Package inventory differs from its manifest.'
    for row in manifest['files']:
        path = folder / row['path']
        assert not path.is_symlink() and path.stat().st_size == row['bytes']
        assert digest(path) == row['sha256'], row['path']
    for relative, target in links.items():
        path = folder / relative
        assert path.is_symlink() and path.readlink().as_posix() == target
        assert path.resolve().is_relative_to(folder.resolve())
    assert not list(folder.rglob('*.pyc')), 'Bytecode caches must not be shipped.'


def verify(app: Path, *, tls: bool, work_dir: Path | None = None) -> dict:
    app = app.resolve(strict=True)
    if work_dir:
        work_dir.mkdir(parents=True, exist_ok=True)
        if work_dir.resolve().is_relative_to(ROOT):
            raise ValueError('Put the disposable verification library outside the source checkout.')
    with tempfile.TemporaryDirectory(dir=work_dir, prefix='cartshelf-relocated-') as temporary:
        temporary = Path(temporary)
        moved = temporary / 'Moved App.app'
        shutil.copytree(app, moved, symlinks=True)
        resources = moved / 'Contents/Resources'
        info = plistlib.loads((moved / 'Contents/Info.plist').read_bytes())
        assert 'CartShelfRoot' not in info and 'CartShelfPython' not in info
        assert info['CartShelfCoreRelativePath'] == 'core'
        assert info['CartShelfPythonRelativePath'] == 'python/bin/python3'
        runtime, core = resources / 'python', resources / 'core'
        for folder, filename in [(runtime, 'runtime-manifest.json'), (core, 'core-manifest.json')]:
            verify_inventory(folder, json.loads((resources / filename).read_text()))
        subprocess.run(['/usr/bin/codesign', '--verify', '--deep', '--strict', str(moved)], check=True, capture_output=True)
        data = temporary / 'Library Data'
        data.mkdir()
        (data / 'yaml.py').write_text("raise RuntimeError('The library must not supply dependencies.')\n")
        (data / 'library_manager.py').write_text("raise RuntimeError('The library must not supply executable code.')\n")
        environment = {'PATH': '/usr/bin:/bin:/usr/sbin:/sbin', 'LANG': 'en_US.UTF-8',
                       'PYTHONPATH': str(data), 'PYTHONHOME': str(data), 'VIRTUAL_ENV': str(data)}
        python = runtime / 'bin/python3'
        source = (
            "import json,sys,ssl,PIL,yaml;from PIL import Image;Image.new('RGB',(2,2)).tobytes();"
            "print(json.dumps({'version':sys.version.split()[0],'prefix':sys.prefix,'executable':sys.executable,"
            "'pillow':PIL.__version__,'yaml':yaml.__version__,'module_files':[PIL.__file__,yaml.__file__],"
            "'ca_file':ssl.get_default_verify_paths().cafile,'paths':sys.path}))"
        )
        loaded = subprocess.run([str(python), '-I', '-B', '-c', source], env=environment,
                                cwd=data, capture_output=True, text=True, check=True)
        loaded = json.loads(loaded.stdout)
        assert Path(loaded['prefix']).resolve() == runtime.resolve()
        for path in loaded['paths'] + loaded['module_files']:
            assert Path(path).resolve().is_relative_to(runtime.resolve()), path
        for command in [('init',), ('scan',)]:
            result = subprocess.run([str(python), '-I', '-B', '-c', BOOTSTRAP, str(core),
                                     'library_manager.py', '--root', str(data), *command], env=environment,
                                    cwd=data, capture_output=True, text=True, check=True)
            json.loads(result.stdout)
        assert (data / 'library/catalog.json').is_file()
        assert not (core / 'library').exists()
        tls_status = None
        if tls:
            source = (
                "import urllib.request;r=urllib.request.urlopen("
                "'https://krikzz.com/pub/support/everdrive-gb/x-series/OS/',timeout=30);"
                "assert r.status==200;assert r.read(256);print(r.status)"
            )
            result = subprocess.run([str(python), '-I', '-B', '-c', source], env=environment,
                                    cwd=data, capture_output=True, text=True, check=True)
            tls_status = int(result.stdout.strip())
        release = json.loads((resources / 'release-manifest.json').read_text())
        return {
            'schema': 1, 'passed': True, 'version': release['version'], 'architecture': release['architecture'],
            'runtime': {key: loaded[key] for key in ('version', 'pillow', 'yaml')},
            'runtime_inventory_verified': True, 'core_inventory_verified': True,
            'relocated_signature_verified': True, 'no_homebrew_path': True,
            'library_cwd_injection_rejected': True, 'empty_library_initialized': True,
            'tls_verified_https_status': tls_status, 'tls_verification_disabled': False,
            'physical_cards_accessed': False, 'personal_library_accessed': False,
            'external_clean_mac_test': 'Not performed. This is an isolated local relocation check.',
        }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--app', required=True, type=Path)
    parser.add_argument('--output', type=Path, default=ROOT / 'build/runtime-verification.json')
    parser.add_argument('--work-dir', type=Path, help='Optional temporary parent outside the source checkout.')
    parser.add_argument('--tls', action='store_true', help='Check normal HTTPS verification against official Krikzz.')
    args = parser.parse_args()
    result = verify(args.app, tls=args.tls, work_dir=args.work_dir)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
