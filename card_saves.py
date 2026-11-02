"""Match cartridge saves by ROM content and retain immutable save history."""
from __future__ import annotations

from collections import defaultdict
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import uuid

MAGIC_TO_ORDER = {bytes.fromhex('80371240'): 'z64', bytes.fromhex('37804012'): 'v64', bytes.fromhex('40123780'): 'n64'}
GB_LOGO = bytes.fromhex('CEED6666CC0D000B03730083000C000D0008111F8889000EDCCC6EE6DDDDD999BBBB67636E0EECCCDDDC999FBBB9333E')
ROM_EXTENSIONS = {'chromatic': {'.gb', '.gbc'}, 'm64': {'.z64', '.v64', '.n64', '.rom'}}
SAVE_EXTENSIONS = {'.srm', '.rtc', '.ram', '.sav', '.eep', '.fla', '.mpk'}
ED64_SAVE_NAMES = {'bram.eep', 'bram.srm'}
MAX_SAVE_BYTES = 1024 * 1024
SYSTEM_ROOT_NAMES = frozenset({'.fseventsd', '.spotlight-v100', '.trashes'})


def _ed64_save(name: str) -> bool:
    return Path(name).name == name and '\\' not in name and (name in ED64_SAVE_NAMES or (name.endswith('.mpk') and len(name) > 4))


def _normalized(data: bytes, order: str) -> bytes:
    if order == 'z64':
        return data
    width = 2 if order == 'v64' else 4
    result = bytearray(len(data))
    for offset in range(width):
        result[offset::width] = data[width - offset - 1::width]
    return bytes(result)


def _files(root: Path, excluded_root_names=()) -> list[Path]:
    if root.is_symlink() or not root.is_dir():
        raise ValueError('Save scanning requires a real mounted directory.')
    excluded = {name.casefold() for name in excluded_root_names}
    if not excluded.issubset(SYSTEM_ROOT_NAMES):
        raise ValueError('Only the known root macOS directories can be excluded.')
    result = []

    def visit(directory):
        for path in sorted(directory.iterdir()):
            mode = path.lstat().st_mode
            if stat.S_ISLNK(mode):
                raise ValueError('Save scanning does not follow symbolic links: ' + str(path))
            if directory == root and path.name.casefold() in excluded:
                if not stat.S_ISDIR(mode):
                    raise ValueError('A reserved macOS root name is not a directory. Choose a whole-card image.')
                continue
            if stat.S_ISDIR(mode):
                visit(path)
            elif stat.S_ISREG(mode):
                if not any(part.startswith('.') for part in path.relative_to(root).parts):
                    result.append(path)
            else:
                raise ValueError('Save scanning found an unsupported file: ' + str(path))

    visit(root)
    return result


def _digest(path: Path) -> str:
    result = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            result.update(block)
    return result.hexdigest()


def _rom(path: Path, profile: str) -> tuple[str, bool]:
    before = path.stat()
    maximum = 8 * 1024**2 if profile == 'chromatic' else 64 * 1024**2
    minimum = 0x150 if profile == 'chromatic' else 0x1000
    if not minimum <= before.st_size <= maximum:
        raise ValueError('ROM size is outside the supported cartridge range.')
    digest = hashlib.sha256()
    rtc = False
    with path.open('rb') as stream:
        header = stream.read(0x150)
        stream.seek(0)
        if profile == 'chromatic':
            checksum = 0
            for byte in header[0x134:0x14d]:
                checksum = (checksum - byte - 1) & 255
            if header[0x104:0x134] != GB_LOGO or checksum != header[0x14d]:
                raise ValueError('Game Boy cartridge header is malformed.')
            rtc = header[0x147] in {0x0f, 0x10}
            order = None
        else:
            order = MAGIC_TO_ORDER.get(header[:4])
            if order is None or before.st_size % 4:
                raise ValueError('Nintendo 64 cartridge header or byte order is malformed.')
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(_normalized(block, order) if order else block)
    after = path.stat()
    if (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino):
        raise OSError('A ROM changed while matching its saves.')
    return digest.hexdigest(), rtc


def _save_record(path: Path, root: Path, key: str) -> dict:
    before = path.stat()
    if not 0 < before.st_size <= MAX_SAVE_BYTES:
        raise ValueError('Save size is empty or exceeds the supported bound.')
    if key == 'rtc' and before.st_size != 512:
        raise ValueError('The flat X7 clock file is not the observed 512-byte format.')
    digest = _digest(path)
    after = path.stat()
    if (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino):
        raise OSError('A save changed while it was being matched.')
    return {'key': key, 'path': path.relative_to(root).as_posix(), 'source_path': str(path),
            'bytes': before.st_size, 'sha256': digest}


def _signature(bundle: dict) -> tuple:
    return tuple(sorted((f['key'], f['bytes'], f['sha256']) for f in bundle['files']))


def _archive(history: Path, bundles: dict[str, dict], profile: str) -> None:
    if not bundles:
        return
    if history.is_symlink():
        raise ValueError('Save history cannot be a symbolic link.')
    history.mkdir(parents=True, exist_ok=True)
    owner = history.stat()
    snapshot_id = uuid.uuid4().hex
    temporary = history / ('.' + snapshot_id + '.partial')
    destination = history / snapshot_id
    temporary.mkdir()
    records = []
    for index, bundle in enumerate(bundles.values()):
        saved = []
        for file in bundle['files']:
            stored = f'files/{index:04d}/{file["key"]}'
            target = temporary / stored
            target.parent.mkdir(parents=True, exist_ok=True)
            with Path(file['source_path']).open('rb') as source, target.open('xb') as output:
                digest = hashlib.sha256()
                for block in iter(lambda: source.read(1024 * 1024), b''):
                    output.write(block)
                    digest.update(block)
                output.flush()
                os.fsync(output.fileno())
            if target.stat().st_size != file['bytes'] or digest.hexdigest() != file['sha256'] or _digest(target) != file['sha256']:
                raise OSError('A save-history copy did not verify.')
            saved.append({k: file[k] for k in ('key', 'path', 'bytes', 'sha256')} | {'stored_path': stored})
        records.append({'identity_sha256': bundle['identity_sha256'], 'source_roms': bundle['source_roms'],
                        'files': saved, 'rtc_required': bundle.get('rtc_required', False)})
    manifest = {'schema': 1, 'snapshot_id': snapshot_id, 'profile': profile,
                'created_at': dt.datetime.now(dt.timezone.utc).isoformat(), 'bundles': records}
    encoded = (json.dumps(manifest, indent=2) + '\n').encode()
    for name, data in [('MANIFEST.json', encoded), ('MANIFEST.sha256', (hashlib.sha256(encoded).hexdigest() + '\n').encode())]:
        with (temporary / name).open('xb') as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
    if os.geteuid() == 0:
        for path in [*temporary.rglob('*'), temporary]:
            os.chown(path, owner.st_uid, owner.st_gid)
    temporary.rename(destination)
    for bundle, record in zip(bundles.values(), records):
        bundle['history_snapshot'] = str(destination)
        bundle['files'] = [{**file, 'source_path': str(destination / file['stored_path'])} for file in record['files']]


def _relative(value: str) -> Path:
    if not isinstance(value, str) or not value or '\\' in value or '\x00' in value:
        raise ValueError('Invalid relative save-history path.')
    path = Path(value)
    if path.is_absolute() or any(part in {'', '.', '..'} for part in value.split('/')):
        raise ValueError('Save-history file escapes its snapshot.')
    return path


def _bundle_fingerprint(profile: str, bundle: dict) -> str:
    facts = {'profile': profile, 'identity_sha256': bundle['identity_sha256'],
             'source_roms': bundle['source_roms'], 'rtc_required': bundle.get('rtc_required', False),
             'files': sorted(({key: row[key] for key in ('key', 'path', 'stored_path', 'bytes', 'sha256')}
                              for row in bundle['files']), key=lambda row: row['key'])}
    return hashlib.sha256(json.dumps(facts, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def _history_entries(history: Path | None, profile: str, identity: str | None = None) -> list[dict]:
    if profile not in ROM_EXTENSIONS or (identity is not None and not re.fullmatch(r'[a-f0-9]{64}', identity)):
        raise ValueError('Choose a supported save profile and exact ROM identity.')
    if history is None or not history.exists():
        return []
    history = Path(history)
    if history.is_symlink() or not history.is_dir():
        raise ValueError('Save history must be a real directory.')
    entries = []
    for directory in sorted(history.iterdir()):
        if directory.name.startswith('.'):
            continue
        if directory.is_symlink() or not directory.is_dir() or not re.fullmatch(r'[a-f0-9]{32}', directory.name):
            raise ValueError('Save history contains an unexpected snapshot entry.')
        for child in directory.rglob('*'):
            mode = child.lstat().st_mode
            if stat.S_ISLNK(mode):
                raise ValueError('Save history contains a symbolic link.')
            if not stat.S_ISDIR(mode) and not stat.S_ISREG(mode):
                raise ValueError('Save history contains an unsupported file.')
        manifest_path = directory / 'MANIFEST.json'
        if manifest_path.stat().st_size > 4 * 1024 * 1024:
            raise ValueError('Save-history manifest exceeds the supported bound.')
        encoded = manifest_path.read_bytes()
        if hashlib.sha256(encoded).hexdigest() != (directory / 'MANIFEST.sha256').read_text().strip():
            raise ValueError('A save-history manifest changed after it was archived.')
        manifest = json.loads(encoded)
        if manifest.get('schema') != 1 or manifest.get('snapshot_id') != directory.name:
            raise ValueError('Unsupported save-history snapshot.')
        created = dt.datetime.fromisoformat(manifest['created_at'])
        if created.tzinfo is None:
            raise ValueError('Save-history timestamps require a timezone.')
        if manifest.get('profile') not in ROM_EXTENSIONS or not isinstance(manifest.get('bundles'), list):
            raise ValueError('Unsupported save-history profile or bundles.')
        if manifest['profile'] != profile:
            continue
        seen = set()
        for bundle in manifest['bundles']:
            digest = bundle['identity_sha256']
            if not re.fullmatch(r'[a-f0-9]{64}', digest) or digest in seen:
                raise ValueError('Invalid or duplicate ROM identity in save history.')
            seen.add(digest)
            roms = bundle.get('source_roms')
            if not isinstance(roms, list) or not roms:
                raise ValueError('Save history is missing its source ROM association.')
            for name in roms:
                _relative(name)
            files = bundle['files']
            if not isinstance(files, list) or not files:
                raise ValueError('Invalid save bundle in history.')
            keys = set()
            for row in files:
                relative = _relative(row['stored_path'])
                _relative(row['path'])
                source = directory / relative
                key = row['key']
                valid_key = key in {'srm', 'rtc'} if profile == 'chromatic' else _ed64_save(key)
                if (not valid_key or key in keys or relative.parts[0] != 'files'
                        or not isinstance(row['bytes'], int) or isinstance(row['bytes'], bool)
                        or not 0 < row['bytes'] <= MAX_SAVE_BYTES
                        or not re.fullmatch(r'[a-f0-9]{64}', row['sha256'])
                        or not source.is_file() or source.stat().st_size != row['bytes']
                        or not source.resolve().is_relative_to(directory.resolve())):
                    raise ValueError('Invalid file in save history.')
                keys.add(key)
            required_clock = bundle.get('rtc_required', False)
            if not isinstance(required_clock, bool):
                raise ValueError('Invalid save-history clock requirement.')
            if profile == 'chromatic' and ('srm' not in keys or (required_clock and 'rtc' not in keys)
                    or any(row['key'] == 'rtc' and row['bytes'] != 512 for row in files)):
                raise ValueError('Incomplete X7 save bundle in history.')
            if identity is None or digest == identity:
                entries.append({**bundle, 'snapshot_id': directory.name, 'profile': profile,
                                'created_at': manifest['created_at'], 'verified': False,
                                'bundle_sha256': _bundle_fingerprint(profile, bundle)})
    return sorted(entries, key=lambda row: (dt.datetime.fromisoformat(row['created_at']), row['snapshot_id']), reverse=True)


def history_entries(history: Path | None, profile: str, identity: str | None = None) -> list[dict]:
    try:
        return _history_entries(history, profile, identity)
    except (KeyError, TypeError, AttributeError) as error:
        raise ValueError('Invalid save-history manifest structure.') from error


def validate_choices(choices, profile: str | None = None) -> list[dict]:
    if not isinstance(choices, (list, tuple)) or len(choices) > 4096:
        raise ValueError('Save choices must be a bounded array.')
    result = []
    seen = set()
    for choice in choices:
        if not isinstance(choice, dict) or set(choice) != {'profile', 'identity_sha256', 'snapshot_id', 'bundle_sha256'}:
            raise ValueError('A save choice must identify exactly one immutable bundle.')
        if (not all(isinstance(choice[key], str) for key in choice)
                or choice['profile'] not in ROM_EXTENSIONS or (profile is not None and choice['profile'] != profile)
                or not re.fullmatch(r'[a-f0-9]{64}', str(choice['identity_sha256']))
                or not re.fullmatch(r'[a-f0-9]{32}', str(choice['snapshot_id']))
                or not re.fullmatch(r'[a-f0-9]{64}', str(choice['bundle_sha256']))):
            raise ValueError('The selected save has an invalid profile, identity or snapshot.')
        key = (choice['profile'], choice['identity_sha256'])
        if key in seen:
            raise ValueError('Only one save revision can be selected for each exact ROM.')
        seen.add(key)
        result.append(dict(choice))
    return sorted(result, key=lambda row: (row['profile'], row['identity_sha256']))


def _verified_entry(history: Path, row: dict) -> dict:
    directory = Path(history) / row['snapshot_id']
    files = []
    for file in row['files']:
        source = directory / file['stored_path']
        before = source.stat()
        if _digest(source) != file['sha256']:
            raise ValueError('An archived save changed after verification.')
        after = source.stat()
        if (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino):
            raise OSError('An archived save changed during verification.')
        files.append({**file, 'source_path': str(source)})
    return {**row, 'files': files, 'history_snapshot': str(directory), 'verified': True}


def verified_history_bundle(history: Path, choice: dict) -> dict:
    choice = validate_choices([choice])[0]
    rows = history_entries(history, choice['profile'], choice['identity_sha256'])
    row = next((row for row in rows if row['snapshot_id'] == choice['snapshot_id']), None)
    if row is None or row['bundle_sha256'] != choice['bundle_sha256']:
        raise ValueError('The selected save-history bundle changed or is unavailable.')
    return _verified_entry(history, row)


def _history_bundles(history: Path | None, profile: str) -> dict[str, dict]:
    result = {}
    for row in history_entries(history, profile):
        bundle = _verified_entry(history, row)
        result.setdefault(row['identity_sha256'], bundle)
    return result


def build_restore_plan(backup: Path, pack: Path, profile: str, history: Path | None = None, *,
                       archive: bool = True, choices=(), excluded_root_names=()) -> dict:
    if profile not in ROM_EXTENSIONS:
        raise ValueError('Choose the M64 or Chromatic save profile.')
    backup, pack = Path(backup).absolute(), Path(pack).absolute()
    backup_files, pack_files = _files(backup, excluded_root_names), _files(pack)
    choices = validate_choices(choices, profile)
    chosen = {choice['identity_sha256']: choice for choice in choices}
    target_clocks = {}
    old_names = defaultdict(list)
    targets = defaultdict(list)
    target_save_names = defaultdict(set)
    groups = defaultdict(list)
    skipped = []
    warnings = []
    cache = {}

    def identity(path):
        if path not in cache:
            cache[path] = _rom(path, profile)
        return cache[path]

    for path in pack_files:
        if path.suffix.casefold() in ROM_EXTENSIONS[profile]:
            try:
                digest, rtc = identity(path)
                target_clocks[digest] = rtc
            except ValueError as error:
                warnings.append(path.relative_to(pack).as_posix() + ': ' + str(error))
                digest = _digest(path)
            else:
                targets[digest].append(path)
            name = path.stem if profile == 'chromatic' else path.name
            target_save_names[name.casefold()].add(digest)
    collisions = sorted(name for name, identities in target_save_names.items() if len(identities) > 1)
    if collisions:
        raise ValueError('Different ROMs share a cartridge save filename. Rebuild this pack with distinct filenames: ' + ', '.join(collisions))
    for path in backup_files:
        relative = path.relative_to(backup)
        parts = relative.parts
        if path.suffix.casefold() in ROM_EXTENSIONS[profile]:
            key = path.stem if profile == 'chromatic' else path.name
            old_names[key.casefold()].append(path)
        if profile == 'chromatic' and len(parts) == 3 and tuple(p.casefold() for p in parts[:2]) == ('gbcsys', 'save') and path.suffix.casefold() in {'.srm', '.rtc'}:
            groups[path.stem.casefold()].append((path, path.suffix.casefold()[1:]))
        elif profile == 'm64' and len(parts) == 4 and tuple(p.casefold() for p in parts[:2]) == ('ed64', 'gamedata') and _ed64_save(path.name.casefold()):
            groups[parts[2].casefold()].append((path, path.name.casefold()))
        elif path.suffix.casefold() in SAVE_EXTENSIONS or tuple(p.casefold() for p in parts[:2]) in {('gbcsys', 'save'), ('gbcsys', 'snap'), ('ed64', 'gamedata')}:
            skipped.append({'path': relative.as_posix(), 'reason': 'Unknown save layout or runtime file; retained in the backup.'})

    candidates = defaultdict(list)
    blocked = set()
    for name, saves in groups.items():
        roms = old_names[name]
        found = {}
        invalid = False
        for path in roms:
            try:
                found[path] = identity(path)
            except ValueError:
                invalid = True
        identities = {value[0] for value in found.values()}
        reason = ''
        if not roms:
            reason = 'The original ROM is absent; its save identity cannot be established.'
        elif invalid or len(identities) != 1:
            reason = 'The original ROM filename is ambiguous or has a malformed cartridge header.'
            blocked.update(identities)
        else:
            digest = next(iter(identities))
            try:
                files = [_save_record(path, backup, key) for path, key in saves]
                keys = {f['key'] for f in files}
                if len(keys) != len(files) or (profile == 'chromatic' and ('srm' not in keys or (any(rtc for _, rtc in found.values()) and 'rtc' not in keys))):
                    raise ValueError('The SRAM/clock bundle is incomplete or has duplicate save slots.')
                candidates[digest].append({'identity_sha256': digest, 'source_roms': [p.relative_to(backup).as_posix() for p in roms], 'files': files, 'rtc_required': any(rtc for _, rtc in found.values())})
            except ValueError as error:
                reason = str(error)
                blocked.add(digest)
        if reason:
            skipped.extend({'path': path.relative_to(backup).as_posix(), 'reason': reason} for path, _ in saves)
    current = {}
    for digest, bundles in candidates.items():
        if digest in blocked or len({_signature(bundle) for bundle in bundles}) != 1:
            blocked.add(digest)
            skipped.extend({'path': f['path'], 'reason': 'Conflicting current saves exist for this exact ROM; historical fallback is blocked.'} for b in bundles for f in b['files'])
        else:
            current[digest] = bundles[0]
    if history is not None:
        history = Path(history).absolute()
    archived = _history_bundles(history, profile)
    if chosen and history is None:
        raise ValueError('Selected older saves require verified save history.')
    if set(chosen) - set(targets):
        raise ValueError('A selected save does not match an exact ROM in this pack.')
    selected = {digest: verified_history_bundle(history, choice) for digest, choice in chosen.items()}
    output = {}
    for digest, roms in targets.items():
        if digest in blocked and digest not in selected:
            continue
        bundle = selected.get(digest) or current.get(digest) or archived.get(digest)
        if bundle is None:
            continue
        if target_clocks.get(digest) and 'rtc' not in {row['key'] for row in bundle['files']}:
            raise ValueError('The selected timer ROM requires a complete SRAM and clock bundle.')
        for target in roms:
            for file in bundle['files']:
                relative = (Path('GBCSYS/SAVE') / (target.stem + '.' + file['key'])) if profile == 'chromatic' else Path('ed64/gamedata') / target.name / file['key']
                name = relative.as_posix()
                if (pack / relative).exists():
                    raise ValueError('The pack already contains a save at the restore destination: ' + name)
                row = {'source_path': file['source_path'], 'path': name, 'bytes': file['bytes'], 'sha256': file['sha256'],
                       'profile': profile, 'identity_sha256': digest, 'source_roms': bundle['source_roms'],
                       'source_save_path': file['path'], 'target_rom': target.relative_to(pack).as_posix(),
                       'source_kind': 'selected_earlier' if digest in selected else 'current' if digest in current else 'history',
                       'history_snapshot': bundle.get('history_snapshot', '')}
                folded = name.casefold()
                if folded in output and (output[folded]['sha256'], output[folded]['identity_sha256']) != (row['sha256'], digest):
                    raise ValueError('Different ROM saves collide at the same restore destination: ' + name)
                output[folded] = row
    for digest, bundle in current.items():
        if digest not in targets:
            skipped.extend({'path': f['path'], 'reason': 'This ROM is absent from the new pack; its save is retained in the backup and save history.' if history else 'This ROM is absent from the new pack; its save is retained in the backup.'} for f in bundle['files'])
    if history is not None and archive:
        _archive(history, current, profile)
        for row in output.values():
            if row['source_kind'] == 'current':
                bundle = current[row['identity_sha256']]
                file = next(file for file in bundle['files'] if file['path'] == row['source_save_path'])
                row['source_path'] = file['source_path']
                row['history_snapshot'] = bundle['history_snapshot']
    return {'files': sorted(output.values(), key=lambda row: row['path']), 'skipped': skipped,
            'warnings': warnings, 'archived_bundles': len(current) if history and archive else 0}
