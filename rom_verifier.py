#!/usr/bin/env python3
"""Read selected ROMs again and report integrity and preservation-reference matches."""
from __future__ import annotations

import argparse
from collections import Counter
import csv
import datetime as dt
import hashlib
import io
import json
import os
from pathlib import Path
import re
import sys
import tempfile

import handheld_metadata
import prepare_everdrive64 as n64

LOGO = bytes.fromhex('CEED6666CC0D000B03730083000C000D0008111F8889000EDCCC6EE6DDDDD999BBBB67636E0EECCCDDDC999FBBB9333E')
REFERENCE_KINDS = ('no-intro', 'homebrew', 'hacks', 'tosec')
STATUSES = ('reference-match', 'unlisted', 'needs-review', 'changed', 'invalid')
N64_DAT_URL = 'https://raw.githubusercontent.com/libretro/libretro-database/master/metadat/no-intro/Nintendo%20-%20Nintendo%2064.dat'


def progress(message: str, completed: int, total: int) -> None:
    print(json.dumps({'event': 'progress', 'phase': 'verify', 'completed': completed,
                      'total': total, 'message': message}), file=sys.stderr, flush=True)


def workspace(root: Path, expected: os.stat_result | None = None) -> Path:
    root = Path(root).resolve(strict=True)
    if root.is_relative_to(Path(__file__).resolve().parent):
        raise ValueError('Choose a library folder outside the application source or bundle.')
    current = root.stat()
    if not root.is_dir() or not os.access(root, os.W_OK | os.X_OK):
        raise ValueError('ROM verification requires an existing writable library folder.')
    if expected is not None and (current.st_dev, current.st_ino) != (expected.st_dev, expected.st_ino):
        raise ValueError('The selected library changed during this operation.')
    return root


def managed(root: Path, path: Path) -> Path:
    root = workspace(root)
    path = Path(path)
    if not path.is_absolute():
        path = root / path
    if not path.resolve().is_relative_to(root):
        raise ValueError('A verification path escapes the selected library.')
    return path


def digest_file(path: Path) -> str:
    result = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            result.update(block)
    return result.hexdigest()


def atomic_text(root: Path, path: Path, text: str, expected: os.stat_result | None = None) -> None:
    workspace(root, expected)
    path = managed(root, path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile('w', dir=path.parent, encoding='utf-8', delete=False) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    try:
        managed(root, path)
        workspace(root, expected)
        managed(root, path)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def reference_index(root: Path, profiles: set[str]) -> tuple[dict, list[dict], list[str]]:
    indices = {'m64': {}, 'chromatic': {}}
    provenance, errors = [], []
    if 'm64' in profiles:
        path = managed(root, root / 'reference/Nintendo - Nintendo 64.dat')
        try:
            payload = path.read_bytes()
            version, records = n64.parse_dat(path)
            if not records:
                raise ValueError('no complete SHA-1 ROM records')
            source = {'path': str(path), 'source_url': N64_DAT_URL,
                      'sha256': hashlib.sha256(payload).hexdigest(), 'version': version, 'kind': 'no-intro'}
            provenance.append(source)
            for record in records:
                if re.fullmatch(r'[a-fA-F0-9]{40}', record.get('sha1', '')):
                    indices['m64'].setdefault(record['sha1'].lower(), []).append({**source, 'name': record['name'], 'rom_name': record['rom_name']})
        except (OSError, ValueError, UnicodeError) as error:
            errors.append('N64 reference unavailable: ' + str(error))
    if 'chromatic' in profiles:
        cache = managed(root, root / 'cache/handheld-metadata/index.json')
        receipts = []
        if cache.is_file():
            try:
                value = json.loads(cache.read_text())
                if not isinstance(value, dict):
                    raise ValueError('reference receipt cache must be an object')
                receipts = value.get('sources', [])
                if not isinstance(receipts, list):
                    raise ValueError('reference sources must be a list')
            except (ValueError, OSError) as error:
                receipts = []
                errors.append('Handheld reference receipts unavailable: ' + str(error))
        candidates = []
        for item in receipts:
            if not isinstance(item, dict) or not isinstance(item.get('path'), str):
                continue
            path = managed(root, Path(item['path']))
            match = re.match(r'^(gbc?)-(no-intro|homebrew|hacks|tosec)(?:-[a-f0-9]+)?\.dat$', path.name)
            if match:
                candidates.append((REFERENCE_KINDS.index(match.group(2)), path, item, match.group(2)))
        if not candidates:
            for system in ('gb', 'gbc'):
                for kind in REFERENCE_KINDS:
                    path = managed(root, root / f'reference/handheld/{system}-{kind}.dat')
                    if path.is_file():
                        url = handheld_metadata.DATABASE + kind + '/' + handheld_metadata.SYSTEMS[system].replace(' ', '%20') + '.dat'
                        candidates.append((REFERENCE_KINDS.index(kind), path, {'source_url': url}, kind))
        seen = set()
        for _, path, receipt, kind in sorted(candidates, key=lambda value: (value[0], str(value[1]))):
            if path in seen:
                continue
            seen.add(path)
            try:
                payload = path.read_bytes()
                checksum = hashlib.sha256(payload).hexdigest()
                if receipt.get('sha256') and receipt['sha256'] != checksum:
                    raise ValueError('reference snapshot SHA-256 differs from its receipt')
                records = handheld_metadata.parse_dat(payload)
                if not records:
                    raise ValueError('no recognizable ROM records')
                version_match = re.search(rb'(?m)^\s*version\s+"([^"]+)"', payload)
                source = {'path': str(path), 'source_url': receipt.get('source_url', ''), 'sha256': checksum,
                          'version': version_match.group(1).decode('ascii', 'replace') if version_match else 'unknown', 'kind': kind}
                provenance.append(source)
                for record in records:
                    if re.fullmatch(r'[a-fA-F0-9]{40}', record.get('sha1', '')):
                        indices['chromatic'].setdefault(record['sha1'].lower(), []).append({**source, 'name': record.get('name') or record.get('rom_name', ''), 'rom_name': record.get('rom_name', '')})
            except (OSError, ValueError, UnicodeError) as error:
                errors.append(path.name + ': ' + str(error))
        if not indices['chromatic']:
            errors.append('No valid handheld SHA-1 reference records are available.')
    return indices, provenance, errors


def gb_checks(data: bytes) -> tuple[dict, list[str], bool]:
    if len(data) < 0x150:
        return {'header_valid': False}, ['Game Boy cartridge header is truncated.'], False
    header = 0
    for byte in data[0x134:0x14d]:
        header = (header - byte - 1) & 255
    code = data[0x148]
    declared = (32768 << code) if code <= 8 else {0x52: 72 * 16384, 0x53: 80 * 16384, 0x54: 96 * 16384}.get(code)
    checks = {'header_valid': True, 'nintendo_logo': data[0x104:0x134] == LOGO,
              'header_checksum': header == data[0x14d],
              'global_checksum': (sum(data) - data[0x14e] - data[0x14f]) & 65535 == int.from_bytes(data[0x14e:0x150], 'big'),
              'declared_size': declared == len(data), 'declared_size_bytes': declared,
              'target_size': len(data) <= 8 * 1024 * 1024,
              'mapper': f'0x{data[0x147]:02X}', 'cgb_flag': f'0x{data[0x143]:02X}'}
    labels = {'nintendo_logo': 'Nintendo logo differs.', 'header_checksum': 'Header checksum differs.',
              'global_checksum': 'Global checksum differs.', 'declared_size': 'Declared ROM size differs from file size.'}
    warnings = [label for key, label in labels.items() if not checks[key]]
    if len(data) > 8 * 1024 * 1024:
        warnings.append('File exceeds the EverDrive-GB X7 8 MiB ROM limit.')
    return checks, warnings, True


def bad_reference(record: dict) -> bool:
    return bool(re.search(r'\[b(?:\d*| [^\]]+)\]|\(bad dump\)|\bbaddump\b', record.get('name', '') + ' ' + record.get('rom_name', ''), re.IGNORECASE))


def verify_source(root: Path, release: dict, source: dict, references: dict, checked_at: str) -> dict:
    path = managed(root, Path(source['path']))
    row = {'release_id': release['id'], 'profile': release['profile'], 'title': release['title'],
           'filename': source.get('filename', path.name), 'path': str(path), 'checked_at': checked_at,
           'expected_catalog_sha256': source.get('raw_sha256', ''), 'raw_sha256': '', 'raw_sha1': '',
           'normalized_sha256': '', 'normalized_sha1': '', 'status': 'invalid', 'summary': '',
           'reference_name': '', 'reference_source': '', 'reference_dat_sha256': '',
           'catalog_hash_matches': False, 'stable_during_read': False, 'source_preserved': False,
           'checks': {}, 'warnings': []}
    try:
        if path.is_symlink() or not path.is_file():
            raise ValueError('ROM source is missing or is a symbolic link.')
        before = path.stat()
        data = path.read_bytes()
        after = path.stat()
        row['size_bytes'] = len(data)
        row['stable_during_read'] = (before.st_size, before.st_mtime_ns, before.st_ino) == (after.st_size, after.st_mtime_ns, after.st_ino)
        row['raw_sha256'] = hashlib.sha256(data).hexdigest()
        row['raw_sha1'] = hashlib.sha1(data).hexdigest()
        row['catalog_hash_matches'] = row['raw_sha256'] == source.get('raw_sha256')
        structural = True
        if release['profile'] == 'm64':
            order = n64.MAGIC_TO_ORDER.get(data[:4], 'unknown')
            row['byte_order'] = order
            if len(data) < 0x1000 or order == 'unknown':
                structural = False
                row['warnings'].append('N64 boot header is truncated or its byte-order magic is unrecognized.')
            else:
                normalized = n64.normalize_chunk(data, order)
                row['normalized_sha256'] = hashlib.sha256(normalized).hexdigest()
                row['normalized_sha1'] = hashlib.sha1(normalized).hexdigest()
                row['checks'] = {'header_valid': True, 'byte_order': order,
                                 'header_crc1': f'{int.from_bytes(normalized[0x10:0x14], "big"):08X}',
                                 'header_crc2': f'{int.from_bytes(normalized[0x14:0x18], "big"):08X}',
                                 'boot_checksum': 'not calculated; no independently qualified CIC checker is installed'}
                if path.suffix.lower().lstrip('.') != order:
                    row['warnings'].append('Filename extension differs from the detected byte order; this is not a corruption finding.')
                if len(normalized) % 4:
                    row['warnings'].append('ROM size is not a multiple of four bytes.')
        else:
            row['checks'], row['warnings'], structural = gb_checks(data)
        digest = row['normalized_sha1'] if release['profile'] == 'm64' else row['raw_sha1']
        matches = references.get(release['profile'], {}).get(digest, [])
        good = next((record for record in matches if not bad_reference(record)), None)
        reference = good or (matches[0] if matches else None)
        if reference:
            row['reference_name'] = reference['name']
            row['reference_source'] = reference['source_url']
            row['reference_dat_sha256'] = reference['sha256']
            row['reference_kind'] = reference['kind']
        if not structural:
            row['status'], row['summary'] = 'invalid', 'The ROM does not contain a complete recognized cartridge header.'
        elif not row['catalog_hash_matches'] or not row['stable_during_read']:
            row['status'], row['summary'] = 'changed', 'Current bytes differ from the catalog or changed during reading. Rescan and review the source.'
        elif good:
            row['status'], row['summary'] = 'reference-match', 'Fresh content hash matches a preservation reference.'
            if row['warnings']:
                row['summary'] += ' Internal or filename observations remain listed separately.'
        elif reference:
            row['status'], row['summary'] = 'needs-review', 'The exact reference is explicitly tagged as a bad dump.'
        elif any(not row['checks'].get(key, True) for key in ('nintendo_logo', 'header_checksum', 'global_checksum', 'declared_size', 'target_size')):
            row['status'], row['summary'] = 'needs-review', 'No exact reference match; one or more internal checks differ. This is not proof of a broken game.'
        else:
            row['status'], row['summary'] = 'unlisted', 'Recognized cartridge structure, but no exact preservation-reference match. Homebrew or modifications can be valid.'
    except (OSError, ValueError) as error:
        row['status'], row['summary'] = 'invalid', str(error)
        row['warnings'].append(str(error))
    return row


def combine_records(observations: list[dict]) -> dict:
    records = {}
    for row in observations:
        current = records.get(row['release_id'])
        if current is None:
            current = {key: row[key] for key in ('status', 'summary', 'checked_at', 'reference_name', 'reference_source')}
            current.update({'warnings': [], 'sources': []})
            records[row['release_id']] = current
        if STATUSES.index(row['status']) > STATUSES.index(current['status']):
            for key in ('status', 'summary', 'reference_name', 'reference_source'):
                current[key] = row[key]
        current['warnings'] = list(dict.fromkeys(current['warnings'] + row['warnings']))
        current['sources'].append(row)
    return records


def verify_library(root: Path, releases: list[dict], profile: str | None = None) -> dict:
    root = workspace(root)
    original_root = root.stat()
    if profile is not None and profile not in {'m64', 'chromatic'}:
        raise ValueError('Choose M64, Chromatic, or both libraries.')
    selected = [release for release in releases if profile is None or release['profile'] == profile]
    sources = [(release, source) for release in selected for source in release['sources']]
    for release, source in sources:
        if release['profile'] not in {'m64', 'chromatic'}:
            raise ValueError('The catalog includes an unsupported device profile.')
        managed(root, Path(source['path']))
    checked_at = dt.datetime.now(dt.timezone.utc).isoformat()
    references, provenance, errors = reference_index(root, {release['profile'] for release in selected})
    observations = []
    total = len(sources) * 2
    for number, (release, source) in enumerate(sources, 1):
        observations.append(verify_source(root, release, source, references, checked_at))
        if number == 1 or number % 16 == 0 or number == len(sources):
            progress('Reading ROM bytes and checking reference hashes', number, total)
    for number, row in enumerate(observations, len(sources) + 1):
        try:
            row['source_preserved'] = bool(row['raw_sha256']) and digest_file(Path(row['path'])) == row['raw_sha256']
        except OSError:
            row['source_preserved'] = False
        if not row['source_preserved'] and row['status'] != 'invalid':
            row['status'] = 'changed'
            row['summary'] = 'The source changed after its verification read. No repair was attempted.'
        if number % 16 == 0 or number == total:
            progress('Rechecking that source bytes remain unchanged', number, total)
    counts = Counter(row['status'] for row in observations)
    report_path = managed(root, root / 'reports/ROM_VERIFICATION_REPORT.md')
    summary = {'checked_at': checked_at, 'files_checked': len(observations),
               'reference_matches': counts['reference-match'], 'unlisted': counts['unlisted'],
               'needs_review': counts['needs-review'], 'invalid': counts['invalid'], 'changed': counts['changed'],
               'source_files_preserved': sum(row['source_preserved'] for row in observations),
               'reference_errors': len(errors), 'report_path': str(report_path)}
    result = {'summary': summary, 'records': combine_records(observations), 'files': observations,
              'references': provenance, 'reference_errors': errors,
              'verifier_sha256': digest_file(Path(__file__)),
              'limits': ['This is file and preservation-reference verification, not emulator or physical-device qualification.',
                         'N64 boot CRC1/CRC2 values are recorded but not recalculated without a qualified CIC checker.',
                         'No ROMs were uploaded, repaired, deleted, or renamed.']}
    atomic_text(root, root / 'reports/ROM_VERIFICATION_REPORT.json', json.dumps(result, ensure_ascii=False, indent=2) + '\n', original_root)
    output = io.StringIO()
    columns = ['profile', 'title', 'filename', 'path', 'status', 'summary', 'raw_sha256', 'raw_sha1', 'normalized_sha256', 'normalized_sha1', 'expected_catalog_sha256', 'catalog_hash_matches', 'source_preserved', 'reference_name', 'reference_source', 'reference_dat_sha256', 'warnings']
    writer = csv.DictWriter(output, fieldnames=columns, extrasaction='ignore')
    writer.writeheader()
    for row in observations:
        writer.writerow({**row, 'warnings': ' | '.join(row['warnings'])})
    atomic_text(root, root / 'reports/ROM_VERIFICATION_INVENTORY.csv', output.getvalue(), original_root)
    lines = ['# ROM verification report', '', 'Every selected file was read again, then reread to check source preservation.', '',
             f"Checked: {checked_at}", '', f"- Files checked: {summary['files_checked']}",
             f"- Exact preservation-reference matches: {summary['reference_matches']}", f"- Structurally recognized but unlisted: {summary['unlisted']}",
             f"- Need review: {summary['needs_review']}", f"- Invalid headers or unreadable files: {summary['invalid']}",
             f"- Changed sources: {summary['changed']}", f"- Source files unchanged after verification: {summary['source_files_preserved']}", '',
             'A missing reference match is not a failure. Unlisted homebrew and modifications can be valid. A matching known dump can also retain an original internal-checksum anomaly.', '',
             'N64 identity uses byte-order-normalized SHA-1. Game Boy identity uses raw SHA-1 against current cached No-Intro, homebrew, hacks, and TOSEC snapshots. Game Boy checks include the logo, header checksum, global checksum, and declared size.', '',
             'N64 boot CRC1/CRC2 are recorded rather than recalculated. Emulator playability, console compatibility, and physical cartridge operation are unqualified by this audit.', '',
             'The complete file-by-file observations and exact reference snapshot hashes are in ROM_VERIFICATION_REPORT.json and ROM_VERIFICATION_INVENTORY.csv.', '', '## Reference snapshots', '']
    for reference in provenance:
        lines.append(f"- {Path(reference['path']).name} ({reference['source_url']}). SHA-256 {reference['sha256']}.")
    if errors:
        lines += ['', '## Reference errors', ''] + ['- ' + error for error in errors]
    flagged = [row for row in observations if row['status'] in {'needs-review', 'changed', 'invalid'}]
    if flagged:
        lines += ['', '## Files needing review', '']
        for row in flagged:
            lines.append('- ' + row['filename'] + '. ' + row['summary'] + (' ' + ' '.join(row['warnings']) if row['warnings'] else ''))
    atomic_text(root, report_path, '\n'.join(lines) + '\n', original_root)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--profile', choices=['m64', 'chromatic'])
    args = parser.parse_args()
    try:
        root = workspace(args.root)
        catalog = json.loads(managed(root, root / 'library/catalog.json').read_text())
        releases = (catalog.get('snapshot') or catalog)['releases']
        result = verify_library(root, releases, args.profile)
        print(json.dumps(result['summary'], separators=(',', ':')))
        return 0
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
