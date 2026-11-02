#!/usr/bin/env python3
"""Manage a chosen CartShelf library and create verified, independent card packs."""
from __future__ import annotations

import argparse
import configparser
import contextlib
import datetime as dt
import hashlib
import importlib.util
import json
import os
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
import unicodedata
import uuid
import zipfile
from collections import Counter
from pathlib import Path, PurePosixPath

CODE_ROOT = Path(__file__).resolve().parent
PROFILES = {
    'm64': {'console': 'M64', 'cartridge': 'EverDrive-64 PRO'},
    'chromatic': {'console': 'Chromatic', 'cartridge': 'EverDrive-GB X7'},
}
N64_SUFFIXES = {'.z64', '.v64', '.n64', '.rom', '.ndd'}
GB_SUFFIXES = {'.gb', '.gbc'}
MAX_ROM_BYTES = 256 * 1024 * 1024
MAX_IMPORT_BYTES = 4 * 1024 * 1024 * 1024
MAX_ARCHIVE_ENTRIES = 10000
GB_LOGO = bytes.fromhex('CEED6666CC0D000B03730083000C000D0008111F8889000EDCCC6EE6DDDDD999BBBB67636E0EECCCDDDC999FBBB9333E')


def progress(phase: str, message: str, completed: int = 0, total: int = 0) -> None:
    print(json.dumps({'event': 'progress', 'phase': phase, 'completed': completed,
                      'total': total, 'message': message}), file=sys.stderr, flush=True)


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def title_from_name(name: str) -> str:
    return re.split(r'\s*[\[(]', Path(name).stem, maxsplit=1)[0].strip() or Path(name).stem


def version_from_name(name: str) -> str:
    match = re.search(r'\((?:v|V)([0-9][^)]*)\)', name)
    if match:
        return match.group(1)
    match = re.search(r'\(Rev[- ]?([^)]*)\)', name, re.IGNORECASE)
    return 'Rev ' + match.group(1) if match else 'Unknown'


def handheld_path(filename: str) -> str:
    system = 'GBC' if Path(filename).suffix.lower() == '.gbc' else 'GB'
    initial = unicodedata.normalize('NFKD', filename).upper()[:1]
    if initial.isdigit():
        group = '0-9'
    elif 'A' <= initial <= 'F':
        group = 'A-F'
    elif 'G' <= initial <= 'M':
        group = 'G-M'
    elif 'N' <= initial <= 'S':
        group = 'N-S'
    elif 'T' <= initial <= 'Z':
        group = 'T-Z'
    else:
        group = 'Other'
    return f'{system}/{group}/{filename}'


def _resolve_save_namespaces(releases: list[dict]) -> None:
    sources = []
    for release in releases:
        for source in release['sources']:
            original = PurePosixPath(handheld_path(source['filename'])) if release['profile'] == 'chromatic' else PurePosixPath(source['export_path']).with_name(source['filename'])
            source['export_path'] = original.as_posix()
            sources.append((release, source, original))

    def collisions():
        groups = {}
        for release, source, original in sources:
            path = PurePosixPath(source['export_path'])
            name = path.stem if release['profile'] == 'chromatic' else path.name
            groups.setdefault((release['profile'], name.casefold()), []).append((release, source, original))
        return [group for group in groups.values() if len({r['id'] for r, _, _ in group}) > 1]

    for include_identity in (False, True):
        for group in collisions():
            for release, source, original in group:
                platform = original.suffix[1:].upper() if release['profile'] == 'chromatic' else 'N64'
                label = platform + ('-' + release['identity_sha256'][:12] if include_identity else '')
                suffix = ' [' + label + ']'
                stem = original.stem[:255 - len(original.suffix) - len(suffix)]
                source['export_path'] = original.with_name(stem + suffix + original.suffix).as_posix()
    if collisions():
        raise ValueError('Distinct ROMs still share a cartridge save filename. Rename the conflicting source files before preparing a pack.')


def _load_n64():
    spec = importlib.util.spec_from_file_location('cartshelf_n64', Path(__file__).with_name('prepare_everdrive64.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


N64 = _load_n64()


class Library:
    def __init__(self, root: Path):
        self.root = Path(root).resolve(strict=True)
        if self.root.is_relative_to(CODE_ROOT):
            raise ValueError('Choose a library folder outside the application source or bundle.')
        for folder in (self.root, *self.root.parents):
            git = folder / '.git'
            source_archive = ((folder / 'release.json').is_file()
                              and (folder / 'library_manager.py').is_file()
                              and (folder / 'app/CartShelf.swift').is_file())
            if git.exists() or git.is_symlink() or source_archive:
                raise ValueError('Choose a library folder outside Git repositories and CartShelf source folders.')
        if not self.root.is_dir() or not os.access(self.root, os.W_OK | os.X_OK):
            raise ValueError('Choose an existing writable library folder.')
        identity = self.root.stat()
        self.storage_device = identity.st_dev
        self.root_identity = (identity.st_dev, identity.st_ino)
        self.catalog = self.root / 'library/catalog.json'
        self.collections_dir = self.root / 'library/collections'
        self._managed(self.catalog)
        self._managed(self.collections_dir)

    def _managed(self, path: Path) -> Path:
        try:
            current = self.root.stat()
        except OSError as error:
            raise ValueError('The selected library is unavailable. Reconnect its drive or folder.') from error
        if not stat.S_ISDIR(current.st_mode) or (current.st_dev, current.st_ino) != self.root_identity:
            raise ValueError('The selected library changed. Reconnect its original folder.')
        path = Path(path)
        if not path.is_absolute():
            path = self.root / path
        resolved = path.resolve()
        if not resolved.is_relative_to(self.root):
            raise ValueError(f'Path escapes the selected library: {path}')
        return path

    def initialize(self) -> dict:
        self._managed(self.catalog)
        if os.path.lexists(self.catalog) or (self.collections_dir.exists() and any(self.collections_dir.iterdir())):
            raise ValueError('This folder already contains a CartShelf library. Connect it instead of creating a new library.')
        folders = [self._managed(self.root / name) for name in (
            'library/N64/Imports', 'library/GB-GBC/Imports', 'library/collections',
            'exports', 'backups', 'cache', 'reference', 'quarantine', 'tmp',
        )]
        for folder in folders:
            if folder.exists() and not folder.is_dir():
                raise ValueError('A file blocks a required library folder: ' + folder.name)
        for folder in folders:
            self._managed(folder).mkdir(parents=True, exist_ok=True)
        return self.scan()

    def _write_json(self, path: Path, value) -> None:
        path = self._managed(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, prefix='.' + path.name + '.', delete=False, encoding='utf-8') as stream:
            temporary = Path(stream.name)
            try:
                json.dump(value, stream, ensure_ascii=False, separators=(',', ':'))
                stream.write('\n')
                stream.flush()
                os.fsync(stream.fileno())
            except BaseException:
                temporary.unlink(missing_ok=True)
                raise
        try:
            self._managed(path)
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)

    def _read_json(self, path: Path, default):
        path = self._managed(path)
        return json.loads(path.read_text()) if path.is_file() else default

    def _source_roots(self):
        return [
            ('m64', self.root / 'N64ROMsPACK'),
            ('m64', self.root / 'library/N64/Supplemental'),
            ('m64', self.root / 'library/N64/Imports'),
            ('chromatic', self.root / 'library/GB-GBC/game-boy-and-game-boy-color-complete-collection'),
            ('chromatic', self.root / 'library/GB-GBC/Chromatic'),
            ('chromatic', self.root / 'library/GB-GBC/Imports'),
        ]

    def _source_paths(self):
        seen = set()
        for profile, root in self._source_roots():
            self._managed(root)
            if not root.is_dir():
                continue
            suffixes = N64_SUFFIXES if profile == 'm64' else GB_SUFFIXES
            for path in sorted(root.rglob('*'), key=lambda p: str(p).casefold()):
                if path.suffix.lower() not in suffixes or any(part.startswith('.') for part in path.relative_to(root).parts):
                    continue
                self._managed(path)
                if path.is_file() and str(path.resolve()) not in seen:
                    seen.add(str(path.resolve()))
                    yield profile, path
        homebrew = self._managed(self.root / 'homebrew')
        for path in sorted(homebrew.glob('*/dist/*.z64')):
            self._managed(path)
            if path.is_file() and str(path.resolve()) not in seen:
                yield 'm64', path

    def _analyze(self, profile: str, path: Path) -> dict:
        size = path.stat().st_size
        if not 0 < size <= MAX_ROM_BYTES:
            raise ValueError(f'ROM is empty or exceeds the supported import size: {path.name}')
        warnings = []
        if profile == 'm64':
            row = N64.scan_rom(path)
            if row['byte_order'] == 'unknown':
                warnings.append('Unrecognized N64 header.')
            elif not row['extension_matches_byte_order']:
                warnings.append(f"Content byte order is {row['byte_order']}; the filename extension differs.")
            return {
                'identity_sha256': row['normalized_sha256'], 'raw_sha256': sha256(path),
                'size_bytes': size, 'game_code': row['game_code'], 'region': row['region'] or 'Unknown',
                'normalized_sha1': row['normalized_sha1'], 'byte_order': row['byte_order'],
                'warnings': warnings,
            }
        data = path.read_bytes()
        if len(data) < 0x150:
            raise ValueError(f'Game Boy ROM header is incomplete: {path.name}')
        header_checksum = 0
        for byte in data[0x134:0x14d]:
            header_checksum = (header_checksum - byte - 1) & 255
        size_code = data[0x148]
        declared = (32768 << size_code) if size_code <= 8 else {0x52: 72 * 16384, 0x53: 80 * 16384, 0x54: 96 * 16384}.get(size_code)
        checks = {
            'Nintendo logo mismatch.': data[0x104:0x134] == GB_LOGO,
            'Header checksum mismatch.': header_checksum == data[0x14d],
            'Global checksum mismatch.': ((sum(data) - data[0x14e] - data[0x14f]) & 65535) == int.from_bytes(data[0x14e:0x150], 'big'),
            'Declared ROM size differs from the file size.': declared == len(data),
        }
        warnings.extend(message for message, passed in checks.items() if not passed)
        digest = hashlib.sha256(data).hexdigest()
        region_match = re.search(r'\((USA(?:, Europe)?|Europe|World|Japan|J|U|E)(?:\)|,)', path.name)
        return {
            'identity_sha256': digest, 'raw_sha256': digest, 'size_bytes': size,
            'game_code': '', 'region': region_match.group(1) if region_match else 'Unknown',
            'warnings': warnings, 'verified': all(checks.values()),
        }

    def _metadata(self, code: str) -> tuple[str, str]:
        if not re.fullmatch(r'[A-Za-z0-9]{4}', code):
            return '', ''
        title = artwork = ''
        for base in [self.root / 'cache/n64-flashcart-menu-metadata/metadata', self.root / 'custom-metadata']:
            directory = self._managed(base.joinpath(*code))
            ini = self._managed(directory / 'metadata.ini')
            if ini.is_file():
                match = re.search(r'^\s*name\s*=\s*(.+)$', ini.read_text(encoding='utf-8-sig', errors='replace'), re.MULTILINE)
                if match:
                    title = match.group(1).strip()
            image = self._managed(directory / 'boxart_front.png')
            if image.is_file():
                artwork = str(image)
        return title, artwork

    def _display_metadata(self, release: dict) -> dict:
        fields = ('description', 'genre', 'developer', 'publisher', 'author', 'release_year',
                  'canonical_title', 'source_url', 'confidence')
        result = {field: '' for field in fields}
        result['identity_verified'] = False
        if release['profile'] == 'chromatic':
            import handheld_metadata
            result.update(handheld_metadata.enrich(self.root, release))
        else:
            code = release['game_code']
            supported = re.fullmatch(r'[A-Za-z0-9]{4}', code) is not None
            result['ed64_metadata_supported'] = supported
            if supported:
                available_sources = []
                for base, source_name in [(self.root / 'cache/n64-flashcart-menu-metadata/metadata', 'community'),
                                          (self.root / 'custom-metadata', 'local custom')]:
                    directory = self._managed(base.joinpath(*code))
                    if any(self._managed(directory / name).is_file() for name in
                           ('metadata.ini', 'description.txt', 'boxart_front.png', 'boxart_back.png', 'gamepak_front.png')):
                        available_sources.append(source_name)
                    ini = directory / 'metadata.ini'
                    if ini.is_file():
                        parser = configparser.ConfigParser(interpolation=None, strict=False)
                        try:
                            parser.read_string(ini.read_text(encoding='utf-8-sig', errors='replace'))
                        except configparser.Error:
                            continue
                        meta = parser['meta'] if parser.has_section('meta') else {}
                        result['canonical_title'] = meta.get('name', result['canonical_title'])
                        result['author'] = meta.get('author', result['author'])
                        result['genre'] = meta.get('genre', result['genre'])
                        date = meta.get('release-date', '')
                        if re.match(r'^\d{4}', date):
                            result['release_year'] = date[:4]
                        if not result['description']:
                            result['description'] = meta.get('short-desc', '')
                    description = self._managed(directory / 'description.txt')
                    if description.is_file():
                        result['description'] = description.read_text(encoding='utf-8', errors='replace').strip()[:12000]
                if 'community' in available_sources:
                    result['source_url'] = 'https://github.com/n64-tools/n64-flashcart-menu-metadata/tree/main/metadata/' + '/'.join(code)
                if available_sources:
                    result['confidence'] = ' and '.join(available_sources).capitalize() + ' metadata matched by cartridge game code; local revision identity is separate.'
                else:
                    result['confidence'] = 'Metadata unavailable for this cartridge game code.'
                result['identity_verified'] = release['verified']
            else:
                result['ed64_metadata_limit'] = 'This ROM header cannot select stock EverDrive menu metadata. CartShelf can still show its artwork and details.'
        override = self._managed(self.root / 'library/metadata-overrides' / (release['id'].replace(':', '-') + '.json'))
        if override.is_file():
            value = self._read_json(override, {})
            if value.get('id') != release['id'] or value.get('identity_sha256') != release['identity_sha256']:
                raise ValueError('The metadata override does not match this exact game build.')
            result.update(value)
        artwork = result.get('artwork_path', '')
        if artwork:
            path = self._managed(Path(artwork))
            result['artwork_path'] = str(path) if path.is_file() else ''
        for field in fields:
            result[field] = str(result.get(field) or '')
        return result

    def _collections(self) -> list[dict]:
        self._managed(self.collections_dir)
        result = []
        for path in sorted(self.collections_dir.glob('*.json')):
            value = self._read_json(path, None)
            if not isinstance(value, dict) or value.get('profile') not in PROFILES or not isinstance(value.get('release_ids'), list):
                raise ValueError(f'Invalid collection file: {path.name}')
            result.append(value)
        return result

    def _updates(self):
        feed = self._read_json(self.root / 'library/modretro-updates.json', {})
        if not feed:
            return {}, None
        try:
            import cart_updates
        except ImportError:
            return feed, None
        return feed, cart_updates

    def scan(self) -> dict:
        previous = self._read_json(self.catalog, {})
        cached_files = previous.get('files', {}) if previous.get('schema') == 1 else {}
        dat = self._managed(self.root / 'reference/Nintendo - Nintendo 64.dat')
        dat_version, entries = N64.parse_dat(dat) if dat.is_file() else ('No cached No-Intro DAT', [])
        by_sha1 = {entry['sha1']: entry for entry in entries}
        feed, updates = self._updates()
        verification_state = self._read_json(self.root / 'library/verification-catalog.json', {})
        files, releases, issues = {}, {}, []
        sources = list(self._source_paths())
        for file_number, (profile, path) in enumerate(sources, 1):
            if file_number == 1 or file_number % 100 == 0 or file_number == len(sources):
                progress('scan', 'Indexing library files', file_number, len(sources))
            before = path.stat()
            key = str(path.resolve())
            signature = [before.st_size, before.st_mtime_ns]
            cached = cached_files.get(key)
            try:
                if cached and cached.get('signature') == signature and cached.get('profile') == profile:
                    analysis = cached['analysis']
                else:
                    analysis = self._analyze(profile, path)
                    after = path.stat()
                    if [after.st_size, after.st_mtime_ns] != signature:
                        raise ValueError('Source changed while being scanned. Scan again.')
                if profile == 'm64' and (before.st_size < 0x40 or analysis['byte_order'] == 'unknown'):
                    raise ValueError('The N64 header is incomplete or unrecognized.')
            except (OSError, ValueError) as error:
                issues.append({'path': key, 'filename': path.name, 'profile': profile, 'reason': str(error)})
                continue
            files[key] = {'signature': signature, 'profile': profile, 'analysis': analysis}
            reference = by_sha1.get(analysis.get('normalized_sha1', '')) if profile == 'm64' else None
            title = title_from_name(reference['name'] if reference else path.name)
            _, artwork = self._metadata(analysis['game_code']) if profile == 'm64' else ('', '')
            release_id = profile + ':' + analysis['identity_sha256']
            if release_id not in releases:
                release = {
                    'id': release_id, 'profile': profile, 'title': title,
                    'version': version_from_name(path.name), 'identity_sha256': analysis['identity_sha256'],
                    'game_code': analysis['game_code'], 'artwork_path': artwork,
                    'region': analysis['region'], 'verified': bool(reference) if profile == 'm64' else analysis['verified'],
                    'sources': [], 'warnings': list(analysis['warnings']),
                }
                releases[release_id] = release
            publisher_source = path.is_relative_to(self.root / 'library/GB-GBC/Chromatic') or re.search(r'\(ModRetro Chromatic\)', path.name, re.IGNORECASE)
            if updates and profile == 'chromatic' and publisher_source and 'update' not in releases[release_id]:
                update = updates.status_for(title, releases[release_id]['version'], feed)
                if update:
                    releases[release_id]['update'] = update
            if profile == 'm64':
                is_homebrew = path.is_relative_to(self.root / 'homebrew')
                export_path = ('N64 Games/Homebrew/' if is_homebrew else 'N64 Games/') + path.name
            else:
                export_path = handheld_path(path.name)
            releases[release_id]['sources'].append({
                'path': key, 'filename': path.name, 'raw_sha256': analysis['raw_sha256'],
                'size_bytes': analysis['size_bytes'], 'export_path': export_path,
            })
        ordered = sorted(releases.values(), key=lambda r: (r['profile'], r['title'].casefold(), r['version'], r['id']))
        _resolve_save_namespaces(ordered)
        for release in ordered:
            release['sources'].sort(key=lambda s: (s['filename'].casefold(), s['path']))
            metadata = self._display_metadata(release)
            release['metadata'] = metadata
            if metadata.get('artwork_path'):
                release['artwork_path'] = metadata['artwork_path']
            if metadata.get('id') == release['id'] and metadata.get('title'):
                release['title'] = metadata['title']
            verified = verification_state.get('records', {}).get(release['id'])
            if verified:
                observed = {(s['path'], s.get('raw_sha256', '')) for s in verified.get('sources', [])}
                if all((s['path'], s['raw_sha256']) in observed for s in release['sources']):
                    release['verification'] = {key: verified[key] for key in ('status', 'summary', 'checked_at', 'reference_name', 'reference_source', 'warnings')}
        profiles = []
        for profile_id, definition in PROFILES.items():
            relevant = [r for r in ordered if r['profile'] == profile_id]
            profiles.append({
                'id': profile_id, **definition, 'file_count': sum(len(r['sources']) for r in relevant),
                'release_count': len(relevant), 'title_count': len({r['title'].casefold() for r in relevant}),
            })
        import prepared_images
        image_inventory = prepared_images.list_images(self.root)
        snapshot = {
            'project_root': str(self.root), 'profiles': profiles, 'releases': ordered,
            'collections': self._collections(), 'metadata_version': dat_version,
            'issues': issues,
            'verification_summary': verification_state.get('summary'),
            'prepared_images': image_inventory['images'],
            'prepared_image_issues': image_inventory['issues'],
            'management': {
                'duplicate_files': sum(max(0, len(r['sources']) - 1) for r in ordered),
                'duplicate_bytes': sum(s['size_bytes'] for r in ordered for s in self._duplicate_sources(r)[1]),
                'warning_releases': sum(bool(r['warnings']) for r in ordered),
                'quarantine_batches': self._quarantine_batches(),
            },
        }
        self._write_json(self.catalog, {'schema': 1, 'files': files, 'snapshot': snapshot})
        return snapshot

    def _duplicate_sources(self, release: dict) -> tuple[dict, list[dict]]:
        if len(release['sources']) == 1:
            return release['sources'][0], []
        def preference(source):
            path = Path(source['path'])
            correct_extension = True
            native_order = True
            if release['profile'] == 'm64':
                with path.open('rb') as stream:
                    order = N64.MAGIC_TO_ORDER.get(stream.read(4), '')
                correct_extension = path.suffix.lower() == '.' + order
                native_order = order == 'z64'
            version = version_from_name(source['filename'])
            numbers = tuple(-int(n) for n in re.findall(r'\d+', version)) or (0,)
            return (not correct_extension, not native_order, 'Imports' in path.parts, numbers, source['filename'].casefold(), source['path'])
        ordered = sorted(release['sources'], key=preference)
        return ordered[0], ordered[1:]

    def _quarantine_batches(self) -> list[dict]:
        directory = self._managed(self.root / 'quarantine')
        batches = []
        for path in sorted(directory.glob('*/RECEIPT.json')):
            record = self._read_json(path, {})
            if isinstance(record, dict) and re.fullmatch(r'[a-f0-9]{32}', str(record.get('id', ''))):
                batches.append({
                    'id': record['id'], 'count': sum(1 for p in path.parent.glob('files/**/*') if p.is_file()),
                    'created_at': record.get('created_at', ''), 'restored': bool(record.get('restored', False)),
                })
        return batches

    def cleanup_plan(self, profile: str | None = None) -> dict:
        if profile is not None and profile not in PROFILES:
            raise ValueError('Unknown device profile.')
        snapshot = self.scan()
        releases = [r for r in snapshot['releases'] if profile is None or r['profile'] == profile]
        kept, moves, warnings = [], [], []
        for release in releases:
            if release['warnings']:
                warnings.append({k: release[k] for k in ['id', 'title', 'warnings', 'sources']} | {'release_id': release['id']})
            canonical, duplicates = self._duplicate_sources(release)
            if not duplicates:
                continue
            kept.append({**canonical, 'identity_sha256': release['identity_sha256'], 'profile': release['profile']})
            for source in duplicates:
                moves.append({
                    'source': source['path'], 'filename': source['filename'],
                    'raw_sha256': source['raw_sha256'], 'size_bytes': source['size_bytes'],
                    'reason': 'Identical game contents after N64 byte-order normalization.' if release['profile'] == 'm64' else 'Identical SHA-256 contents.',
                    'kept_path': canonical['path'], 'profile': release['profile'],
                    'identity_sha256': release['identity_sha256'],
                })
        plan_id = uuid.uuid4().hex
        path = self._managed(self.root / 'library/cleanup-plans' / (plan_id + '.json'))
        plan = {
            'schema': 1, 'id': plan_id, 'token': secrets.token_hex(24),
            'created_at': dt.datetime.now(dt.timezone.utc).isoformat(), 'profile': profile,
            'plan_path': str(path), 'duplicate_files': len(moves),
            'duplicate_bytes': sum(m['size_bytes'] for m in moves), 'kept': kept, 'moves': moves,
            'warnings': warnings, 'issues': snapshot['issues'],
            'message': f'{len(moves)} confirmed duplicate files can be moved to recoverable quarantine. Checksum warnings remain for review.',
        }
        self._write_json(path, plan)
        return plan

    def _cleanup_record(self, path: Path, directory: Path) -> dict:
        path = self._managed(path)
        if path.parent.resolve() != self._managed(directory).resolve() or not re.fullmatch(r'[a-f0-9]{32}\.json', path.name):
            raise ValueError('Invalid cleanup plan path.')
        record = self._read_json(path, {})
        if not isinstance(record, dict) or record.get('schema') != 1 or record.get('id') != path.stem:
            raise ValueError('Invalid cleanup plan.')
        return record

    def cleanup(self, plan_path: Path, confirmation: str) -> dict:
        plan = self._cleanup_record(plan_path, self.root / 'library/cleanup-plans')
        if not isinstance(plan.get('token'), str) or not secrets.compare_digest(plan['token'], confirmation):
            raise ValueError('The cleanup confirmation does not match this plan.')
        if not isinstance(plan.get('moves'), list) or not isinstance(plan.get('kept'), list):
            raise ValueError('Invalid cleanup plan records.')
        for record in plan['kept'] + plan['moves']:
            if not isinstance(record, dict) or record.get('profile') not in PROFILES or not isinstance(record.get('size_bytes'), int):
                raise ValueError('Invalid cleanup source record.')
            for field in ['raw_sha256', 'identity_sha256']:
                if not re.fullmatch(r'[a-f0-9]{64}', str(record.get(field, ''))):
                    raise ValueError('Invalid cleanup source checksum.')
        if any(not isinstance(r.get('path'), str) for r in plan['kept']) or any(not isinstance(r.get('source'), str) or not isinstance(r.get('kept_path'), str) for r in plan['moves']):
            raise ValueError('Invalid cleanup source path.')
        active = {str(p.resolve()): profile for profile, p in self._source_paths()}
        kept = {r.get('path'): r for r in plan['kept']}
        batch = self._managed(self.root / 'quarantine' / plan['id'])
        if batch.exists():
            raise ValueError('This cleanup plan has already been used.')
        checked, seen = [], set()
        for move in plan['moves']:
            source = self._managed(Path(move['source'])).resolve()
            canonical = self._managed(Path(move['kept_path'])).resolve()
            source_key, kept_key = str(source), str(canonical)
            record = kept.get(kept_key)
            profile = active.get(source_key)
            if source_key in seen or source_key == kept_key or not record or profile != move.get('profile') or active.get(kept_key) != profile:
                raise ValueError('The cleanup plan contains a source outside the active library or an invalid duplicate pair.')
            if not source.is_file() or source.stat().st_size != move['size_bytes'] or sha256(source) != move['raw_sha256']:
                raise ValueError(f'Source changed since the cleanup plan: {source.name}. Make a new plan.')
            if not canonical.is_file() or canonical.stat().st_size != record['size_bytes'] or sha256(canonical) != record['raw_sha256']:
                raise ValueError(f'The kept source changed since the cleanup plan: {canonical.name}. Make a new plan.')
            identity = self._analyze(profile, source)['identity_sha256']
            if identity != move['identity_sha256'] or identity != record['identity_sha256'] or self._analyze(profile, canonical)['identity_sha256'] != identity:
                raise ValueError('The cleanup sources no longer have identical game contents.')
            target = self._managed(batch / 'files' / source.relative_to(self.root))
            if target.exists() or source.stat().st_dev != self.storage_device:
                raise ValueError('Cleanup requires an unused quarantine path on the project volume.')
            checked.append({**move, 'source': str(source), 'kept_path': str(canonical), 'quarantine_path': str(target)})
            seen.add(source_key)
        if seen.intersection(kept):
            raise ValueError('A kept source cannot also be quarantined.')
        if not checked:
            snapshot = self.scan()
            snapshot['message'] = 'No duplicate files needed quarantine.'
            return snapshot
        receipt = {'schema': 1, 'id': plan['id'], 'created_at': plan['created_at'], 'restored': False, 'status': 'pending', 'moves': checked}
        completed = []
        try:
            self._write_json(batch / 'RECEIPT.json', receipt)
            for move in checked:
                source, target = Path(move['source']), Path(move['quarantine_path'])
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.exists():
                    raise ValueError('A quarantine destination appeared during cleanup.')
                source.rename(target)
                completed.append(move)
            receipt['status'] = 'quarantined'
            self._write_json(batch / 'RECEIPT.json', receipt)
        except BaseException:
            for move in reversed(completed):
                Path(move['quarantine_path']).rename(move['source'])
            if batch.exists():
                shutil.rmtree(batch)
            raise
        snapshot = self.scan()
        snapshot['cleanup_batch'] = plan['id']
        snapshot['message'] = f'Moved {len(checked)} confirmed duplicate files to recoverable quarantine. No ROM bytes were deleted.'
        return snapshot

    def restore_cleanup(self, batch_id: str) -> dict:
        if not re.fullmatch(r'[a-f0-9]{32}', batch_id):
            raise ValueError('Invalid quarantine batch ID.')
        batch = self._managed(self.root / 'quarantine' / batch_id)
        receipt = self._read_json(batch / 'RECEIPT.json', {})
        if receipt.get('schema') != 1 or receipt.get('id') != batch_id or not isinstance(receipt.get('moves'), list):
            raise ValueError('Quarantine batch not found.')
        if receipt.get('restored'):
            raise ValueError('This quarantine batch has already been restored.')
        checked = []
        for move in receipt['moves']:
            if not isinstance(move, dict) or not isinstance(move.get('source'), str) or not isinstance(move.get('quarantine_path'), str) or not isinstance(move.get('size_bytes'), int) or not re.fullmatch(r'[a-f0-9]{64}', str(move.get('raw_sha256', ''))):
                raise ValueError('Invalid quarantine source record.')
            original = self._managed(Path(move['source'])).resolve()
            saved = self._managed(Path(move['quarantine_path'])).resolve()
            roots = [base.resolve() for profile, base in self._source_roots() if profile == move.get('profile')]
            homebrew = self.root / 'homebrew'
            homebrew_file = move.get('profile') == 'm64' and original.is_relative_to(homebrew) and len(original.relative_to(homebrew).parts) == 3 and original.parent.name == 'dist' and original.suffix.lower() == '.z64'
            if not any(original.is_relative_to(base) for base in roots) and not homebrew_file:
                raise ValueError('The restore path is outside the active library.')
            expected = self._managed(batch / 'files' / original.relative_to(self.root))
            if saved.resolve() != expected.resolve() or original.is_symlink():
                raise ValueError('A restore destination is occupied or the receipt path is invalid. Nothing restored.')
            if receipt.get('status') == 'pending' and original.is_file() and not saved.exists() and original.stat().st_size == move['size_bytes'] and sha256(original) == move['raw_sha256']:
                continue
            if original.exists():
                raise ValueError('A restore destination is occupied. Nothing restored.')
            if not saved.is_file() or saved.stat().st_size != move['size_bytes'] or sha256(saved) != move['raw_sha256']:
                raise ValueError('A quarantined file changed. Nothing restored.')
            checked.append((saved, original))
        completed = []
        try:
            for saved, original in checked:
                original.parent.mkdir(parents=True, exist_ok=True)
                if original.exists() or original.is_symlink():
                    raise ValueError('A restore destination appeared during restore.')
                saved.rename(original)
                completed.append((saved, original))
            receipt['restored'] = True
            receipt['status'] = 'restored'
            receipt['restored_at'] = dt.datetime.now(dt.timezone.utc).isoformat()
            self._write_json(batch / 'RECEIPT.json', receipt)
        except BaseException:
            for saved, original in reversed(completed):
                original.rename(saved)
            raise
        snapshot = self.scan()
        snapshot['message'] = f'Restored {len(completed)} quarantined duplicate files.'
        return snapshot

    def _archive_member(self, name: str) -> PurePosixPath:
        path = PurePosixPath(name)
        if '\\' in name or path.is_absolute() or '..' in path.parts or re.match(r'^[A-Za-z]:', name) or '\x00' in name:
            raise ValueError(f'Unsafe archive member: {name!r}')
        return path

    def _expand_import(self, source: Path, stage: Path, budget: dict, depth: int = 0):
        suffix = source.suffix.lower()
        if suffix in N64_SUFFIXES | GB_SUFFIXES:
            size = source.stat().st_size
            if not 0 < size <= MAX_ROM_BYTES:
                raise ValueError(f'ROM exceeds the import size limit: {source.name}')
            budget['bytes'] += size
            if budget['bytes'] > MAX_IMPORT_BYTES:
                raise ValueError('Import exceeds the total expanded size limit.')
            yield source.name, source
            return
        if depth >= 2 or suffix not in {'.zip', '.7z'}:
            return
        if suffix == '.zip':
            with zipfile.ZipFile(source) as archive:
                members = archive.infolist()
                budget['entries'] += len(members)
                if budget['entries'] > MAX_ARCHIVE_ENTRIES:
                    raise ValueError('Archive contains too many entries.')
                for member in members:
                    member_path = self._archive_member(member.filename)
                    mode = member.external_attr >> 16
                    if stat.S_ISLNK(mode):
                        raise ValueError(f'Archive links are not supported: {member.filename}')
                    if member.is_dir():
                        continue
                    if member_path.suffix.lower() not in N64_SUFFIXES | GB_SUFFIXES | {'.7z'}:
                        continue
                    if member.file_size > MAX_ROM_BYTES:
                        raise ValueError(f'Archive member exceeds the size limit: {member.filename}')
                    target = stage / uuid.uuid4().hex / member_path.name
                    target.parent.mkdir()
                    with archive.open(member) as src, target.open('xb') as dst:
                        shutil.copyfileobj(src, dst)
                    if target.stat().st_size != member.file_size:
                        raise ValueError('Archive member size mismatch.')
                    if member_path.suffix.lower() in N64_SUFFIXES | GB_SUFFIXES:
                        budget['bytes'] += member.file_size
                        if budget['bytes'] > MAX_IMPORT_BYTES:
                            raise ValueError('Import exceeds the total expanded size limit.')
                        yield member_path.name, target
                    else:
                        yield from self._expand_import(target, stage, budget, depth + 1)
            return
        executable = shutil.which('7zz') or shutil.which('7z')
        if not executable:
            raise ValueError('7-Zip is needed for this archive. Install 7zz or import extracted ROM files.')
        listing = subprocess.run([executable, 'l', '-slt', '-ba', '--', str(source)], capture_output=True, text=True, check=True).stdout
        blocks = []
        for block in listing.strip().split('\n\n'):
            fields = dict(line.split(' = ', 1) for line in block.splitlines() if ' = ' in line)
            if fields:
                blocks.append(fields)
        budget['entries'] += len(blocks)
        if budget['entries'] > MAX_ARCHIVE_ENTRIES:
            raise ValueError('Archive contains too many entries.')
        for member in blocks:
            name = member.get('Path', '')
            member_path = self._archive_member(name)
            if 'Symbolic Link' in member or 'Hard Link' in member or 'L' in member.get('Attributes', ''):
                raise ValueError(f'Archive links are not supported: {name}')
            if member.get('Folder') == '+' or member_path.suffix.lower() not in N64_SUFFIXES | GB_SUFFIXES:
                continue
            size = int(member.get('Size', '0'))
            if not 0 < size <= MAX_ROM_BYTES:
                raise ValueError(f'Archive member exceeds the size limit: {name}')
            budget['bytes'] += size
            if budget['bytes'] > MAX_IMPORT_BYTES:
                raise ValueError('Import exceeds the total expanded size limit.')
            process = subprocess.Popen([executable, 'x', '-so', '-spd', '--', str(source), name], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            data = process.stdout.read(MAX_ROM_BYTES + 1)
            if len(data) > MAX_ROM_BYTES:
                process.kill()
            error = process.stderr.read()
            process.stdout.close()
            process.stderr.close()
            code = process.wait()
            if code or len(data) != size:
                raise ValueError(f'Archive extraction failed for {name}: {error.decode(errors="replace").strip()}')
            target = stage / uuid.uuid4().hex / member_path.name
            target.parent.mkdir()
            target.write_bytes(data)
            yield member_path.name, target

    def import_paths(self, inputs: list[Path]) -> dict:
        if not inputs:
            raise ValueError('Choose at least one ROM or archive to import.')
        temporary_root = self._managed(self.root / 'tmp')
        temporary_root.mkdir(parents=True, exist_ok=True)
        existing = self.scan()
        existing_exports = {}
        for release in existing['releases']:
            for source in release['sources']:
                key = (release['profile'], source['export_path'])
                existing_exports.setdefault(key, set()).add(source['raw_sha256'])
        created = []
        imported = skipped = 0
        with tempfile.TemporaryDirectory(prefix='cartshelf-import-', dir=temporary_root) as temporary:
            stage = Path(temporary)
            plan = {}
            budget = {'bytes': 0, 'entries': 0}
            for source in inputs:
                source = Path(source).resolve(strict=True)
                if not source.is_file():
                    raise ValueError(f'Import input is not a regular file: {source}')
                for name, payload in self._expand_import(source, stage, budget):
                    if not name or Path(name).name != name or name.startswith('.'):
                        raise ValueError(f'Invalid ROM filename: {name!r}')
                    profile = 'm64' if Path(name).suffix.lower() in N64_SUFFIXES else 'chromatic'
                    analysis = self._analyze(profile, payload)
                    if profile == 'm64' and analysis['byte_order'] == 'unknown':
                        raise ValueError(f'Unrecognized N64 ROM: {name}')
                    if profile == 'chromatic' and any(w in analysis['warnings'] for w in ['Nintendo logo mismatch.', 'Header checksum mismatch.', 'Declared ROM size differs from the file size.']):
                        raise ValueError(f'Invalid Game Boy ROM header: {name}')
                    export_path = 'N64 Games/' + name if profile == 'm64' else handheld_path(name)
                    known_hashes = existing_exports.get((profile, export_path), set())
                    if known_hashes:
                        if known_hashes != {analysis['raw_sha256']}:
                            raise ValueError(f'A different library file already uses this export filename: {name}. Nothing overwritten.')
                        skipped += 1
                        continue
                    folder = 'library/N64/Imports' if profile == 'm64' else 'library/GB-GBC/Imports'
                    target = self._managed(self.root / folder / name)
                    if target.exists():
                        if not target.is_file() or sha256(target) != analysis['raw_sha256']:
                            raise ValueError(f'A different file already exists: {name}. Nothing overwritten.')
                        skipped += 1
                        continue
                    if target in plan:
                        if plan[target][1] != analysis['raw_sha256']:
                            raise ValueError(f'Import contains different ROMs with the same filename: {name}')
                        skipped += 1
                        continue
                    plan[target] = (payload, analysis['raw_sha256'])
            if not plan and not skipped:
                raise ValueError('No supported ROMs were found in the selected inputs.')
            try:
                for target, (payload, expected) in plan.items():
                    self._managed(target)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with payload.open('rb') as src, target.open('xb') as dst:
                        created.append(target)
                        shutil.copyfileobj(src, dst)
                    if sha256(target) != expected:
                        raise ValueError(f'Imported copy failed verification: {target.name}')
                    imported += 1
                snapshot = self.scan()
            except BaseException:
                for path in created:
                    path.unlink(missing_ok=True)
                raise
        snapshot['message'] = f'Imported and verified {imported} files. {skipped} existing or repeated filenames were unchanged.'
        return snapshot

    def save_collection(self, name: str, profile: str, release_ids: list[str], collection_id: str | None = None, description: str | None = None) -> dict:
        name = name.strip()
        if not name or len(name) > 100:
            raise ValueError('Collection name must contain 1 to 100 characters.')
        if profile not in PROFILES:
            raise ValueError('Unknown device profile.')
        if description is not None:
            if not isinstance(description, str) or len(description.strip()) > 1000:
                raise ValueError('Collection description must contain at most 1000 characters.')
            description = description.strip()
        snapshot = self.scan()
        available = {r['id']: r for r in snapshot['releases']}
        ids = list(dict.fromkeys(release_ids))
        if not ids:
            raise ValueError('Choose at least one build for the collection.')
        for release_id in ids:
            if release_id not in available or available[release_id]['profile'] != profile:
                raise ValueError('Collections must contain known builds from one device profile.')
        if collection_id is not None and not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', collection_id):
            raise ValueError('Invalid collection ID.')
        collection_id = collection_id or uuid.uuid4().hex
        path = self._managed(self.collections_dir / (collection_id + '.json'))
        previous = {}
        if path.exists():
            previous = self._read_json(path, {})
            if previous.get('profile') != profile:
                raise ValueError('An existing collection cannot change device profile.')
        value = {'id': collection_id, 'name': name, 'profile': profile, 'release_ids': ids}
        description = previous.get('description', '') if description is None else description
        if description:
            value['description'] = description
        self._write_json(path, value)
        snapshot['collections'] = self._collections()
        snapshot['message'] = f'Saved {name} with {len(ids)} builds.'
        return snapshot

    def delete_collection(self, collection_id: str) -> dict:
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', collection_id):
            raise ValueError('Invalid collection ID.')
        path = self._managed(self.collections_dir / (collection_id + '.json'))
        if path.is_symlink() or not path.is_file():
            raise ValueError('Collection not found.')
        archive = self._managed(self.root / 'backups/sets')
        archive.mkdir(parents=True, exist_ok=True)
        with path.open('rb') as source:
            with tempfile.NamedTemporaryFile(dir=archive, prefix=collection_id + '--', suffix='.json', delete=False) as output:
                archived = Path(output.name)
                try:
                    shutil.copyfileobj(source, output)
                    output.flush()
                    os.fsync(output.fileno())
                except BaseException:
                    archived.unlink(missing_ok=True)
                    raise
        path.unlink()
        snapshot = self.scan()
        snapshot['message'] = 'Set archived in Backups & Saves. ROM files remain in the library.'
        return snapshot

    def refresh_metadata(self, profile: str = 'm64') -> dict:
        if profile not in PROFILES:
            raise ValueError('Choose one of the two device libraries.')
        progress('metadata', 'Refreshing ' + PROFILES[profile]['console'] + ' metadata')
        if profile == 'm64':
            stored = self._read_json(self.catalog, {})
            releases = (stored.get('snapshot') or self.scan())['releases']
            codes = {r['game_code'] for r in releases if r['profile'] == 'm64' and re.fullmatch(r'[A-Za-z0-9]{4}', r['game_code'])}
            with contextlib.redirect_stdout(sys.stderr):
                N64.sync_inputs(
                    self._managed(self.root / 'cache/n64-flashcart-menu-metadata'),
                    self._managed(self.root / 'reference/Nintendo - Nintendo 64.dat'),
                    self._managed(self.root / 'cache/firmware'),
                    game_codes=codes,
                )
        else:
            import handheld_metadata
            stored = self._read_json(self.catalog, {})
            releases = (stored.get('snapshot') or self.scan())['releases']
            handheld_metadata.refresh_metadata(self.root, releases)
        snapshot = self.scan()
        snapshot['message'] = PROFILES[profile]['console'] + ' metadata refreshed. ROM files were unchanged.'
        return snapshot

    def check_updates(self) -> dict:
        import cart_updates
        progress('updates', 'Reading the public ModRetro release notes')
        cart_updates.refresh_updates(self.root)
        snapshot = self.scan()
        snapshot['message'] = 'Checked the publisher update feed. No ROM files were uploaded or changed.'
        return snapshot

    def verify(self, profile: str | None = None) -> dict:
        import rom_verifier
        stored = self._read_json(self.catalog, {})
        snapshot = stored.get('snapshot') or self.scan()
        releases = list(snapshot['releases'])
        represented = {s['path'] for r in releases for s in r['sources']}
        for issue in snapshot.get('issues', []):
            if issue['path'] not in represented:
                releases.append({'id': issue['profile'] + ':unrecognized:' + hashlib.sha256(issue['path'].encode()).hexdigest(),
                                 'profile': issue['profile'], 'title': title_from_name(issue['filename']),
                                 'sources': [{'path': issue['path'], 'filename': issue['filename'], 'raw_sha256': ''}]})
        result = rom_verifier.verify_library(self.root, releases, profile)
        result['summary']['scope'] = profile or 'both'
        state = self._read_json(self.root / 'library/verification-catalog.json', {})
        records = state.get('records', {})
        records.update(result['records'])
        self._write_json(self.root / 'library/verification-catalog.json', {'summary': result['summary'], 'records': records})
        snapshot = self.scan()
        snapshot['message'] = f'Verified {result["summary"]["files_checked"]} ROM files. Review findings and unlisted builds are separate from invalid files.'
        return snapshot

    def _copy_verified(self, source: Path, destination: Path, expected: str) -> None:
        self._managed(source)
        self._managed(destination)
        if not source.is_file() or sha256(source) != expected:
            raise ValueError(f'Source changed since the last scan: {source.name}. Rescan before preparing a pack.')
        destination.parent.mkdir(parents=True, exist_ok=True)
        with source.open('rb') as src, destination.open('xb') as dst:
            shutil.copyfileobj(src, dst)
        if sha256(destination) != expected:
            raise ValueError(f'Copy verification failed: {source.name}')

    def _handheld_os(self) -> tuple[bytes | None, str]:
        official = self._managed(self.root / 'cache/firmware/gb-x7')
        cached_os = self._managed(official / 'GBCSYS/GBCOS.BIN')
        receipt_path = self._managed(official / 'source.json')
        if cached_os.exists() or receipt_path.exists():
            if not cached_os.is_file() or not receipt_path.is_file():
                raise ValueError('The official GB X7 OS cache is incomplete.')
            receipt = json.loads(receipt_path.read_text())
            data = cached_os.read_bytes()
            expected = receipt.get('sha256', '')
            if not re.fullmatch(r'[a-fA-F0-9]{64}', expected) or hashlib.sha256(data).hexdigest() != expected.lower():
                raise ValueError('The official GB X7 OS cache failed SHA-256 verification.')
            return data, expected.lower()
        legacy = self._managed(self.root / 'legacy/everdrive-gb-x7-20261012')
        backup = self._managed(legacy / 'everdrive-backup-20261012-1812')
        manifest_path = self._managed(backup / 'BACKUP_MANIFEST.json')
        archive_path = self._managed(legacy / 'EverDrive_Full_Backup_20261012.zip')
        if manifest_path.is_file():
            records = json.loads(manifest_path.read_text())
        elif archive_path.is_file():
            with zipfile.ZipFile(archive_path) as archive:
                records = json.loads(archive.read('BACKUP_MANIFEST.json'))
        else:
            return None, ''
        record = next((r for r in records if r['path'] == 'GBCSYS/GBCOS.BIN'), None)
        if not record:
            return None, ''
        source = self._managed(backup / record['path'])
        if source.is_file():
            data = source.read_bytes()
        elif archive_path.is_file():
            with zipfile.ZipFile(archive_path) as archive:
                data = archive.read(record['path'])
        else:
            return None, ''
        if len(data) != record['bytes'] or hashlib.sha256(data).hexdigest() != record['sha256']:
            raise ValueError('The preserved GB X7 OS does not match its backup manifest.')
        return data, record['sha256']

    def _n64_assets(self, codes: set[str], stage: Path, scratch: Path) -> None:
        for index, base in enumerate([self.root / 'cache/n64-flashcart-menu-metadata/metadata', self.root / 'custom-metadata']):
            narrow = scratch / f'metadata-{index}'
            for code in sorted(codes):
                if not re.fullmatch(r'[A-Za-z0-9]{4}', code):
                    continue
                source = self._managed(base.joinpath(*code))
                for name in N64.SUPPORTED_METADATA_FILES:
                    file = self._managed(source / name)
                    if file.is_file():
                        target = narrow.joinpath(*code) / name
                        target.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(file, target)
            with contextlib.redirect_stdout(sys.stderr):
                N64.convert_metadata_tree(narrow, stage / 'ed64/metadata')
        firmware_dir = self._managed(self.root / 'cache/firmware')
        firmware = N64.find_cached_firmware(firmware_dir)
        if firmware:
            self._managed(firmware)
            self._copy_verified(firmware, stage / 'Firmware' / firmware.name, sha256(firmware))
        theme_dir = CODE_ROOT / 'themes/CartShelf-64/dist'
        for name in N64.CARTSHELF_THEME_FILES:
            if not (theme_dir / name).resolve().is_relative_to(CODE_ROOT):
                raise ValueError('The bundled CartShelf theme leaves the application assets.')
        N64.stage_cartshelf_theme(theme_dir, stage)

    def _handheld_theme(self, data: bytes, target: Path, scratch: Path) -> None:
        source = scratch / 'gb-x7-stock.bin'
        source.write_bytes(data)
        applier = CODE_ROOT / 'themes/CartShelf-GB/apply_theme.py'
        if not applier.resolve().is_relative_to(CODE_ROOT) or not applier.is_file():
            raise ValueError('The CartShelf-GB theme applier is missing.')
        patch = applier.parent / 'dist/CartShelf-GB-X7-v1.06.ips'
        if not patch.resolve().is_relative_to(CODE_ROOT):
            raise ValueError('The CartShelf-GB palette patch leaves the application assets.')
        target.parent.mkdir(parents=True)
        try:
            subprocess.run([sys.executable, '-I', '-B', str(applier), str(source), str(target)],
                           check=True, capture_output=True, text=True)
        except subprocess.CalledProcessError as error:
            raise ValueError('The CartShelf-GB theme requires its verified stock OS 1.06 and fixed patch: ' + error.stderr.strip()) from error

    def prepare(self, profile: str, name: str, release_ids: list[str], format: str = 'folder') -> dict:
        if profile not in PROFILES or format not in {'folder', 'zip', 'image'}:
            raise ValueError('Unknown device profile or pack format.')
        name = name.strip()
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9 _.-]{0,79}', name) or name in {'.', '..'}:
            raise ValueError('Use a pack name of 1 to 80 letters, numbers, spaces, dots, hyphens, or underscores.')
        exports = self._managed(self.root / 'exports')
        build_name = name
        while any((exports / (build_name + suffix)).exists() for suffix in ('', '.zip', '.img')):
            build_name = name + '--' + dt.datetime.now().astimezone().strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:6]
        destination = self._managed(exports / build_name)
        zip_destination = self._managed(exports / (build_name + '.zip'))
        stored = self._read_json(self.catalog, {})
        snapshot = stored.get('snapshot') or self.scan()
        _resolve_save_namespaces(snapshot['releases'])
        all_releases = {r['id']: r for r in snapshot['releases']}
        selected = []
        if release_ids:
            for release_id in dict.fromkeys(release_ids):
                release = all_releases.get(release_id)
                if not release or release['profile'] != profile:
                    raise ValueError('The pack must contain known builds for the selected device.')
                selected.append(release)
        else:
            selected = [r for r in snapshot['releases'] if r['profile'] == profile]
        if not selected:
            raise ValueError('No games are selected for this device.')
        os_data, _ = (None, '') if profile == 'm64' else self._handheld_os()
        if profile == 'chromatic' and format == 'image' and os_data is None:
            raise ValueError('A Chromatic SD image requires the verified GB X7 operating system. Refresh its official OS cache before building an image.')
        progress('preflight', 'Checking the selected source files', 0, len(selected))
        plan = {}
        for release_number, release in enumerate(selected, 1):
            sources = release['sources'][:1] if release_ids else release['sources']
            for source in sources:
                relative = PurePosixPath(source['export_path'])
                if relative.is_absolute() or '..' in relative.parts:
                    raise ValueError('Invalid catalog export path. Rescan the library.')
                path = self._managed(Path(source['path']))
                if not path.is_file() or sha256(path) != source['raw_sha256']:
                    raise ValueError(f'Source changed since the last scan: {source["filename"]}. Rescan before preparing a pack.')
                if source['export_path'] in plan:
                    if plan[source['export_path']]['raw_sha256'] != source['raw_sha256']:
                        raise ValueError(f'Different selected files share an export filename: {source["filename"]}')
                else:
                    plan[source['export_path']] = source
            if release_number % 16 == 0 or release_number == len(selected):
                progress('preflight', 'Checking the selected source files', release_number, len(selected))
        exports.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='.prepare-', dir=exports) as temporary:
            scratch = Path(temporary)
            stage = scratch / build_name
            stage.mkdir()
            for file_number, (relative, source) in enumerate(sorted(plan.items()), 1):
                self._copy_verified(Path(source['path']), stage / relative, source['raw_sha256'])
                if file_number == 1 or file_number % 8 == 0 or file_number == len(plan):
                    progress('copy', 'Copying and verifying game files', file_number, len(plan))
            has_os = True
            if profile == 'm64':
                self._n64_assets({r['game_code'] for r in selected}, stage, scratch)
            else:
                data = os_data
                has_os = data is not None
                if data is not None:
                    target = stage / 'GBCSYS/GBCOS.BIN'
                    self._handheld_theme(data, target, scratch)
            records = []
            for path in sorted(stage.rglob('*')):
                if path.is_file():
                    records.append({'path': str(path.relative_to(stage)), 'bytes': path.stat().st_size, 'sha256': sha256(path)})
            manifest = {
                'profile': profile, 'name': name, 'release_ids': [r['id'] for r in selected],
                'build': build_name,
                'metadata_version': snapshot['metadata_version'], 'files': records,
            }
            manifest_path = stage / 'MANIFEST.json'
            manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + '\n')
            manifest_hash = sha256(manifest_path)
            total_bytes = sum(r['bytes'] for r in records) + manifest_path.stat().st_size
            if format == 'zip':
                progress('zip', 'Creating the verified ZIP pack')
                archive_path = scratch / (build_name + '.zip')
                with zipfile.ZipFile(archive_path, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
                    for path in sorted(stage.rglob('*')):
                        if path.is_file():
                            archive.write(path, str(path.relative_to(stage)))
                with zipfile.ZipFile(archive_path) as archive:
                    for record in records:
                        data = archive.read(record['path'])
                        if len(data) != record['bytes'] or hashlib.sha256(data).hexdigest() != record['sha256']:
                            raise ValueError('ZIP pack verification failed.')
                    if hashlib.sha256(archive.read('MANIFEST.json')).hexdigest() != manifest_hash:
                        raise ValueError('ZIP manifest verification failed.')
                self._managed(zip_destination)
                archive_path.rename(zip_destination)
            self._managed(destination)
            stage.rename(destination)
        image_path = ''
        if format == 'image':
            import media
            result = media.create_image(self.root, destination)
            image_path = str(result.get('image_path') or result.get('path')) if isinstance(result, dict) else str(result)
        result_path = image_path if format == 'image' else str(zip_destination) if format == 'zip' else str(destination)
        message = f'Prepared and verified {len(plan)} ROM file{"" if len(plan) == 1 else "s"} for {PROFILES[profile]["cartridge"]}.'
        if not has_os:
            message += ' This games-only pack needs the card\'s existing GBCSYS operating system.'
        return {
            'path': result_path, 'folder': str(destination),
            'zip_path': str(zip_destination) if format == 'zip' else '', 'image_path': image_path,
            'game_count': len(plan), 'file_count': len(records) + 1,
            'total_bytes': total_bytes, 'manifest_sha256': manifest_hash, 'message': message,
        }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True, help='Existing library data folder, separate from application code.')
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('scan')
    commands.add_parser('init')
    commands.add_parser('prepared-images')
    inspect_image = commands.add_parser('inspect-image')
    inspect_image.add_argument('--path', type=Path, required=True)
    importer = commands.add_parser('import')
    importer.add_argument('--path', type=Path, action='append', required=True)
    collection = commands.add_parser('collection-save')
    collection.add_argument('--name', required=True)
    collection.add_argument('--profile', choices=PROFILES, required=True)
    collection.add_argument('--release', action='append', default=[])
    collection.add_argument('--id')
    collection.add_argument('--description')
    delete = commands.add_parser('collection-delete')
    delete.add_argument('--id', required=True)
    refresh = commands.add_parser('refresh-metadata')
    refresh.add_argument('--profile', choices=PROFILES, default='m64')
    commands.add_parser('check-updates')
    verify = commands.add_parser('verify')
    verify.add_argument('--profile', choices=PROFILES)
    cleanup_plan = commands.add_parser('cleanup-plan')
    cleanup_plan.add_argument('--profile', choices=PROFILES)
    cleanup = commands.add_parser('cleanup')
    cleanup.add_argument('--plan', type=Path, required=True)
    cleanup.add_argument('--confirm', required=True)
    restore = commands.add_parser('restore-cleanup')
    restore.add_argument('--id', required=True)
    prepare = commands.add_parser('prepare')
    prepare.add_argument('--profile', choices=PROFILES, required=True)
    prepare.add_argument('--name', required=True)
    prepare.add_argument('--release', action='append', default=[])
    prepare.add_argument('--format', choices=['folder', 'zip', 'image'], default='folder')
    args = parser.parse_args(argv)
    try:
        library = Library(args.root)
        if args.command == 'init':
            result = library.initialize()
        elif args.command == 'scan':
            result = library.scan()
        elif args.command == 'prepared-images':
            import prepared_images
            result = prepared_images.list_images(library.root)
        elif args.command == 'inspect-image':
            import prepared_images
            result = prepared_images.inspect_image(library.root, args.path)
        elif args.command == 'import':
            result = library.import_paths(args.path)
        elif args.command == 'collection-save':
            result = library.save_collection(args.name, args.profile, args.release, args.id, args.description)
        elif args.command == 'collection-delete':
            result = library.delete_collection(args.id)
        elif args.command == 'refresh-metadata':
            result = library.refresh_metadata(args.profile)
        elif args.command == 'check-updates':
            result = library.check_updates()
        elif args.command == 'verify':
            result = library.verify(args.profile)
        elif args.command == 'cleanup-plan':
            result = library.cleanup_plan(args.profile)
        elif args.command == 'cleanup':
            result = library.cleanup(args.plan, args.confirm)
        elif args.command == 'restore-cleanup':
            result = library.restore_cleanup(args.id)
        else:
            result = library.prepare(args.profile, args.name, args.release, args.format)
        print(json.dumps(result, ensure_ascii=False, separators=(',', ':')))
        return 0
    except (OSError, ValueError, RuntimeError, zipfile.BadZipFile, subprocess.CalledProcessError) as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
