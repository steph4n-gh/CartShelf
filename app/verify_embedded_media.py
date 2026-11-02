#!/usr/bin/env python3
"""Exercise the packaged frozen worker using an owned disposable image only."""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import plistlib
import subprocess
import tempfile
from pathlib import Path
from media_bundle import media_source

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--app', required=True, type=Path)
    parser.add_argument('--output', type=Path, default=ROOT / 'build/embedded-media-verification.json')
    parser.add_argument('--work-dir', type=Path, help='Optional temporary parent outside the source checkout.')
    args = parser.parse_args()
    app = args.app.resolve(strict=True)
    resources = app / 'Contents/Resources'
    info = plistlib.loads((app / 'Contents/Info.plist').read_bytes())
    source, modules = media_source(resources / 'core')
    encoded = base64.b64encode(source)
    assert encoded in (app / 'Contents/MacOS/CartShelf').read_bytes(), 'Compiled worker differs from packaged sources.'
    digest = hashlib.sha256(source).hexdigest()
    assert digest == info['CartShelfMediaSHA256']
    loader = "import base64;exec(compile(base64.b64decode('" + encoded.decode() + "'),'CartShelfMedia','exec'),{'__name__':'__main__'})"
    python = resources / 'python/bin/python3'
    command = [str(python), '-I', '-B', '-c', loader]
    environment = {'PATH': '/usr/bin:/bin:/usr/sbin:/sbin', 'LANG': 'en_US.UTF-8', 'COPYFILE_DISABLE': '1'}
    subprocess.run(command + ['--help'], env=environment, capture_output=True, check=True)
    if args.work_dir:
        args.work_dir.mkdir(parents=True, exist_ok=True)
        if args.work_dir.resolve().is_relative_to(ROOT):
            raise ValueError('Put disposable image fixtures outside the source checkout.')
    with tempfile.TemporaryDirectory(dir=args.work_dir, prefix='cartshelf-media-fixture-') as directory:
        root = Path(directory)
        pack = root / 'exports/Runtime-Fixture'
        pack.mkdir(parents=True)
        game = pack / 'N64 Games/Generated Runtime Fixture.z64'
        game.parent.mkdir()
        authored = bytearray(0x1000)
        authored[:4] = bytes.fromhex('80371240')
        authored[0x20:0x34] = b'RUNTIME FIXTURE'.ljust(20, b' ')
        game.write_bytes(authored)
        record = {'path': game.relative_to(pack).as_posix(), 'bytes': len(authored),
                  'sha256': hashlib.sha256(authored).hexdigest()}
        (pack / 'MANIFEST.json').write_text(json.dumps({'profile': 'm64', 'files': [record]}))
        process = subprocess.run(command + ['--root', str(root), 'build-image', '--pack', str(pack)],
                                 env=environment, cwd=root, capture_output=True, text=True,
                                 timeout=120, check=True)
        receipt = json.loads(process.stdout)
        image = Path(receipt['image_path'])
        with image.open('rb') as stream:
            assert hashlib.file_digest(stream, 'sha256').hexdigest() == receipt['sha256']
            stream.seek(510)
            assert stream.read(2) == b'\x55\xaa'
        report = {'schema': 1, 'passed': True, 'source_sha256': digest,
                  'compiled_payload_matches_packaged_sources': True, 'modules': modules,
                  'bundled_interpreter_used': True, 'isolated': True,
                  'disposable_image_bytes': image.stat().st_size, 'image_sha256_verified': True,
                  'fixture': 'Authored non-playable N64 header and zero payload. No user game bytes.',
                  'physical_cards_accessed': False, 'personal_library_accessed': False}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
