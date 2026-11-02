#!/usr/bin/env python3
"""Build a self-contained CartShelf application for the current Mac architecture."""
from __future__ import annotations

import argparse
import base64
import hashlib
import inspect
import json
import os
import platform
import plistlib
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

from media_bundle import media_source
from runtime import prepare_runtime, runtime_manifest, sha256

ROOT = Path(__file__).resolve().parents[1]
CORE_FILES = (
    'library_manager.py', 'library_experience.py', 'recipes.py', 'system_files.py', 'media.py',
    'card_saves.py', 'cart_updates.py', 'handheld_metadata.py', 'prepared_images.py',
    'prepare_everdrive64.py', 'rom_verifier.py',
)
THEME_FILES = (
    'themes/CartShelf-64/dist/CartShelf-64.bgr',
    'themes/CartShelf-64/dist/CartShelf-64-background.png',
    'themes/CartShelf-64/source/OFL.txt',
    'themes/CartShelf-64/LICENSE-ASSETS.md',
    'themes/CartShelf-GB/apply_theme.py',
    'themes/CartShelf-GB/dist/CartShelf-GB-X7-v1.06.ips',
    'themes/CartShelf-GB/dist/CartShelf-GB-X7-v1.06-restore.ips',
)
MACHO_MAGICS = {bytes.fromhex(value) for value in (
    'feedface', 'feedfacf', 'cefaedfe', 'cffaedfe', 'cafebabe', 'bebafeca', 'cafebabf', 'bfbafeca')}


def write_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, indent=2) + '\n')


def clean_environment() -> dict:
    return {'PATH': '/usr/bin:/bin:/usr/sbin:/sbin', 'LANG': 'en_US.UTF-8',
            'COPYFILE_DISABLE': '1', 'PYTHONDONTWRITEBYTECODE': '1'}


def make_icon(resources: Path, build: Path):
    from PIL import Image, ImageDraw
    iconset = build / 'CartShelf.iconset'
    iconset.mkdir(parents=True, exist_ok=True)
    image = Image.new('RGBA', (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rectangle((4, 4, 59, 59), fill='#121212')
    draw.rectangle((7, 2, 56, 61), fill='#121212')
    draw.rectangle((2, 7, 61, 56), fill='#121212')
    draw.rectangle((6, 7, 57, 56), outline='#616161', width=2)
    draw.rectangle((13, 10, 50, 51), fill='#ffe000')
    draw.rectangle((17, 14, 46, 36), fill='#242424')
    draw.rectangle((20, 17, 43, 32), fill='#00bcde')
    draw.rectangle((19, 41, 29, 44), fill='#151515')
    draw.rectangle((22, 38, 25, 47), fill='#151515')
    draw.rectangle((38, 39, 42, 43), fill='#f20a21')
    draw.rectangle((32, 45, 36, 49), fill='#f20a21')
    colors = ['#f90b2a', '#ff7a2f', '#ffe000', '#79cf31', '#05bccb', '#486be2']
    for i, color in enumerate(colors):
        draw.rectangle((8 + i * 8, 54, 15 + i * 8, 57), fill=color)
    for size in [16, 32, 128, 256, 512]:
        for scale in [1, 2]:
            name = f'icon_{size}x{size}' + ('@2x' if scale == 2 else '') + '.png'
            image.resize((size * scale, size * scale), Image.Resampling.NEAREST).save(iconset / name)
    subprocess.run(['/usr/bin/iconutil', '-c', 'icns', str(iconset), '-o', str(resources / 'CartShelf.icns')], check=True)


def core_snapshot(resources: Path) -> dict:
    target = resources / 'core'
    target.mkdir()
    files = list(CORE_FILES) + list(THEME_FILES) + ['LICENSE']
    if (ROOT / 'THIRD_PARTY_NOTICES.md').is_file():
        files.append('THIRD_PARTY_NOTICES.md')
    rows = []
    for relative in files:
        source = ROOT / relative
        if source.is_symlink() or not source.is_file():
            raise FileNotFoundError('Required beta source or asset is missing: ' + relative)
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        digest = sha256(destination)
        if digest != sha256(source):
            raise OSError('A bundled source changed during copying: ' + relative)
        rows.append({'path': relative, 'bytes': destination.stat().st_size, 'sha256': digest})
    result = {'schema': 1, 'files': rows}
    write_json(resources / 'core-manifest.json', result)
    return result


def embed_media(core: Path, build: Path) -> tuple[Path, str, dict]:
    source, modules = media_source(core)
    compile(source, 'CartShelfMedia', 'exec')
    encoded = base64.b64encode(source).decode('ascii')
    digest = hashlib.sha256(source).hexdigest()
    destination = build / 'EmbeddedMedia.swift'
    destination.write_text('enum EmbeddedMedia {\n    static let sourceBase64 = "' + encoded +
                           '"\n    static let sha256 = "' + digest + '"\n}\n')
    return destination, digest, modules


def macho_files(folder: Path) -> list[Path]:
    found = []
    for path in folder.rglob('*'):
        if path.is_symlink() or not path.is_file():
            continue
        with path.open('rb') as stream:
            if stream.read(4) in MACHO_MAGICS:
                found.append(path)
    return sorted(found, key=lambda path: (len(path.parts), path.as_posix()), reverse=True)


def sign_code(path: Path, identity: str, *, identifier: str | None = None) -> None:
    args = ['/usr/bin/codesign', '--force', '--sign', identity]
    args += ['--timestamp=none'] if identity == '-' else ['--options', 'runtime', '--timestamp']
    if identifier:
        args += ['--identifier', identifier]
    subprocess.run(args + [str(path)], check=True)


def notarize(app: Path, profile: str) -> None:
    archive = app.with_suffix('.notary.zip')
    subprocess.run(['/usr/bin/ditto', '-c', '-k', '--keepParent', str(app), str(archive)], check=True)
    result = subprocess.run(['xcrun', 'notarytool', 'submit', str(archive), '--keychain-profile',
                             profile, '--wait', '--output-format', 'json'], check=True,
                            capture_output=True, text=True)
    receipt = json.loads(result.stdout)
    write_json(app.with_suffix('.notary.json'), receipt)
    if receipt.get('status') != 'Accepted':
        raise RuntimeError('Apple did not accept this notarization submission. Keep its receipt.')
    subprocess.run(['xcrun', 'stapler', 'staple', str(app)], check=True)
    subprocess.run(['xcrun', 'stapler', 'validate', str(app)], check=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'dist/CartShelf.app')
    parser.add_argument('--bundle-id')
    parser.add_argument('--display-name', default='CartShelf')
    parser.add_argument('--no-preferences', action='store_true')
    parser.add_argument('--library-root', type=Path, help='QA-only initial library. Never use in a public build.')
    parser.add_argument('--identity', default='-', help='Developer ID identity or - for local ad-hoc verification.')
    parser.add_argument('--notary-profile', help='Submit using this existing notarytool profile, then staple.')
    parser.add_argument('--replace', action='store_true', help='Retain an old output under build/previous before replacement.')
    args = parser.parse_args()
    if sys.platform != 'darwin':
        raise SystemExit('The native app build requires macOS and the Xcode command-line toolchain.')
    release = json.loads((ROOT / 'release.json').read_text())
    architecture = platform.machine()
    bundle_id = args.bundle_id or release['bundle_id']
    if args.library_root and bundle_id == release['bundle_id']:
        raise SystemExit('A QA library default requires a distinct QA bundle ID.')
    if args.notary_profile and args.identity == '-':
        raise SystemExit('Notarization requires a Developer ID signature.')
    output = args.output.expanduser().absolute()
    if output.suffix != '.app' or output.is_symlink():
        raise SystemExit('Choose a real .app output path.')
    if output.exists() and not args.replace:
        raise SystemExit('The output exists. Use --replace to retain it and build a replacement.')
    build = ROOT / 'build' / ('package-' + uuid.uuid4().hex)
    build.mkdir(parents=True)
    app = build / 'CartShelf.app'
    contents = app / 'Contents'
    resources, macos = contents / 'Resources', contents / 'MacOS'
    resources.mkdir(parents=True)
    macos.mkdir()
    prepare_runtime(resources / 'python', ROOT / 'build/downloads', architecture)
    python = resources / 'python/bin/python3'
    core = core_snapshot(resources)
    embedded, media_digest, modules = embed_media(resources / 'core', build)
    native_source = build / 'CartShelf.swift'
    shutil.copyfile(ROOT / 'app/CartShelf.swift', native_source)
    native_digest = sha256(native_source)
    executable = macos / 'CartShelf'
    subprocess.run(['xcrun', 'swiftc', '-O', '-swift-version', '5', '-parse-as-library',
                    '-D', 'CARTSHELF_EMBEDDED_MEDIA', '-target',
                    architecture + '-apple-macos' + release['minimum_macos'],
                    '-debug-prefix-map', str(ROOT) + '=/CartShelfSource',
                    str(native_source), str(embedded), '-o', str(executable),
                    '-framework', 'SwiftUI', '-framework', 'AppKit', '-framework', 'CoreText',
                    '-framework', 'ImageIO'], check=True)
    for name in ('Silkscreen-Bold.ttf', 'Silkscreen-Regular.ttf', 'OFL.txt'):
        shutil.copyfile(ROOT / 'app/Resources' / name, resources / name)
    for name in ('LICENSE', 'THIRD_PARTY_NOTICES.md'):
        if (ROOT / name).is_file():
            shutil.copyfile(ROOT / name, resources / name)
    icon_code = 'from pathlib import Path\nimport subprocess\n' + inspect.getsource(make_icon) + '\nimport sys\nmake_icon(Path(sys.argv[1]), Path(sys.argv[2]))'
    subprocess.run([str(python), '-I', '-B', '-c', icon_code, str(resources), str(build)],
                   env=clean_environment(), check=True)
    info = {
        'CFBundleName': args.display_name, 'CFBundleDisplayName': args.display_name,
        'CFBundleIdentifier': bundle_id, 'CFBundleExecutable': 'CartShelf',
        'CFBundlePackageType': 'APPL', 'CFBundleShortVersionString': release['bundle_version'],
        'CFBundleVersion': str(release['build']), 'CFBundleIconFile': 'CartShelf.icns',
        'LSMinimumSystemVersion': release['minimum_macos'], 'NSHighResolutionCapable': True,
        'NSSupportsAutomaticGraphicsSwitching': True, 'NSPrincipalClass': 'NSApplication',
        'LSApplicationCategoryType': 'public.app-category.utilities',
        'NSRemovableVolumesUsageDescription': 'CartShelf uses your selected library and removable game cards for verified packs and backups.',
        'CartShelfCoreRelativePath': 'core', 'CartShelfPythonRelativePath': 'python/bin/python3',
        'CartShelfVersion': release['version'], 'CartShelfReleaseChannel': release['channel'],
        'CartShelfMediaSHA256': media_digest, 'CartShelfUIPreferences': not args.no_preferences,
        'CartShelfQABuild': bool(args.library_root),
    }
    if args.library_root:
        library = args.library_root.expanduser().resolve(strict=True)
        if not library.is_dir():
            raise SystemExit('The QA library must be an existing directory.')
        info['CartShelfInitialLibraryRoot'] = str(library)
    (contents / 'Info.plist').write_bytes(plistlib.dumps(info))
    (contents / 'PkgInfo').write_bytes(b'APPL????')
    for code in macho_files(app):
        relative = code.relative_to(app).as_posix()
        identifier = bundle_id + '.runtime.' + hashlib.sha256(relative.encode()).hexdigest()[:16]
        sign_code(code, args.identity, identifier=identifier)
    runtime = runtime_manifest(resources / 'python', architecture)
    write_json(resources / 'runtime-manifest.json', runtime)
    write_json(resources / 'release-manifest.json', {
        'schema': 1, 'version': release['version'], 'build': release['build'],
        'architecture': architecture, 'minimum_macos': release['minimum_macos'],
        'signing': 'ad-hoc' if args.identity == '-' else 'Developer ID',
        'hardened_runtime': args.identity != '-',
        'notarization_at_build': 'not-submitted', 'media_sha256': media_digest,
        'media_modules': modules, 'native_source_sha256': native_digest,
        'core_file_count': len(core['files']),
    })
    sign_code(app, args.identity)
    subprocess.run(['/usr/bin/codesign', '--verify', '--deep', '--strict', str(app)], check=True)
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        previous = ROOT / 'build/previous' / (uuid.uuid4().hex + '.app')
        previous.parent.mkdir(exist_ok=True)
        output.rename(previous)
    app.rename(output)
    if args.notary_profile:
        notarize(output, args.notary_profile)
    write_json(build / 'build-result.json', {
        'schema': 1, 'app_path': str(output), 'version': release['version'],
        'architecture': architecture, 'media_sha256': media_digest, 'media_modules': modules,
        'native_source_sha256': native_digest,
        'signature_verified': True, 'notarized': bool(args.notary_profile),
        'qa_library_default': bool(args.library_root),
    })
    print(output)


if __name__ == '__main__':
    main()
