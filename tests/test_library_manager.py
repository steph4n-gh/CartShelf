from pathlib import Path
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from library_manager import Library


def gb_rom(title='FIXTURE', fill=0, color=False):
    data = bytearray([fill] * 32768)
    data[0x104:0x134] = bytes.fromhex('CEED6666CC0D000B03730083000C000D0008111F8889000EDCCC6EE6DDDDD999BBBB67636E0EECCCDDDC999FBBB9333E')
    data[0x134:0x144] = bytes(16)
    data[0x134:0x134 + len(title[:11])] = title[:11].encode('ascii')
    data[0x143] = 0xc0 if color else 0
    data[0x147] = 0
    data[0x148] = 0
    data[0x149] = 0
    checksum = 0
    for byte in data[0x134:0x14d]:
        checksum = (checksum - byte - 1) & 255
    data[0x14d] = checksum
    data[0x14e:0x150] = bytes(2)
    data[0x14e:0x150] = (sum(data) & 65535).to_bytes(2, 'big')
    return bytes(data)


def n64_rom(title='FIXTURE', code='TEST', fill=0):
    data = bytearray([fill] * 4096)
    data[:4] = bytes.fromhex('80371240')
    data[0x20:0x34] = title.encode('ascii').ljust(20, b' ')[:20]
    data[0x3b:0x3f] = code.encode('ascii')
    data[0x3f] = 0
    return bytes(data)


class LibraryBehavior(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='library-tests-')
        self.root = Path(self.temporary.name).resolve()
        self.library = Library(self.root)
        self.gb = self.root / 'library/GB-GBC/game-boy-and-game-boy-color-complete-collection'
        self.gb.mkdir(parents=True)
        self.n64 = self.root / 'N64ROMsPACK'
        self.n64.mkdir()

    def tearDown(self):
        self.temporary.cleanup()

    def write(self, path, data):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def test_scan_groups_byte_order_aliases_and_gb_names(self):
        data = n64_rom(code='NTEE')
        self.write(self.n64 / 'One.z64', data)
        self.write(self.n64 / 'One copy.v64', bytes(data[i ^ 1] for i in range(len(data))))
        self.write(self.gb / 'Puzzle (v1.1).gb', gb_rom())
        self.write(self.gb / 'Puzzle alias.gb', gb_rom())
        first = self.library.scan()
        second = self.library.scan()
        self.assertEqual(first, second)
        counts = {p['id']: p for p in first['profiles']}
        self.assertEqual((counts['m64']['file_count'], counts['m64']['release_count']), (2, 1))
        self.assertEqual((counts['chromatic']['file_count'], counts['chromatic']['release_count']), (2, 1))
        self.assertEqual({r['version'] for r in first['releases'] if r['profile'] == 'chromatic'}, {'1.1'})
        self.assertTrue(all(Path(s['path']).is_relative_to(self.root) for r in first['releases'] for s in r['sources']))

    def test_full_handheld_export_preserves_aliases_and_original_alphabet_layout(self):
        names = ['Alpha.gb', 'Golf.gb', 'Nova.gb', 'Zulu.gb', '3D Puzzle.gbc']
        for name in names:
            self.write(self.gb / name, gb_rom())
        self.write(self.gb / 'Alpha.gb.ram', b'current save')
        snapshot = self.library.scan()
        receipt = self.library.prepare('chromatic', 'Full Handheld', [], 'folder')
        folder = Path(receipt['folder'])
        self.assertEqual(receipt['game_count'], 5)
        expected = {'GB/A-F/Alpha.gb', 'GB/G-M/Golf.gb', 'GB/N-S/Nova.gb', 'GB/T-Z/Zulu.gb', 'GBC/0-9/3D Puzzle.gbc'}
        manifest = json.loads((folder / 'MANIFEST.json').read_text())
        self.assertEqual({r['path'] for r in manifest['files']}, expected)
        self.assertFalse(list(folder.rglob('*.ram')))
        for record in manifest['files']:
            self.assertEqual(hashlib.sha256((folder / record['path']).read_bytes()).hexdigest(), record['sha256'])
        exported = folder / 'GB/A-F/Alpha.gb'
        self.assertNotEqual(exported.stat().st_ino, (self.gb / 'Alpha.gb').stat().st_ino)
        exported.write_bytes(b'edited pack')
        self.assertEqual((self.gb / 'Alpha.gb').read_bytes(), gb_rom())
        self.assertEqual(snapshot['profiles'][1]['file_count'], 5)

    def test_collection_and_selected_export_choose_exact_build_and_one_alias(self):
        self.write(self.gb / 'Puzzle (v1.1).gb', gb_rom(fill=0))
        self.write(self.gb / 'Puzzle alias.gb', gb_rom(fill=0))
        self.write(self.gb / 'Puzzle (v1.2).gb', gb_rom(fill=1))
        self.write(self.n64 / 'Console.z64', n64_rom())
        snapshot = self.library.scan()
        chosen = next(r for r in snapshot['releases'] if r['version'] == '1.2')
        updated = self.library.save_collection('Puzzle Night', 'chromatic', [chosen['id']])
        self.assertEqual(updated['collections'][0]['release_ids'], [chosen['id']])
        with self.assertRaises(ValueError):
            self.library.save_collection('Mixed', 'm64', [chosen['id']])
        receipt = self.library.prepare('chromatic', 'Exact Build', [chosen['id']], 'zip')
        with zipfile.ZipFile(receipt['zip_path']) as archive:
            roms = [name for name in archive.namelist() if name.endswith('.gb')]
            self.assertEqual(roms, ['GB/N-S/Puzzle (v1.2).gb'])
            self.assertEqual(archive.read(roms[0]), gb_rom(fill=1))
        self.library.delete_collection(updated['collections'][0]['id'])
        self.assertEqual(self.library.scan()['collections'], [])
        self.assertTrue((self.gb / 'Puzzle (v1.2).gb').exists())

    def test_collection_description_survives_rename_and_can_be_replaced_or_cleared(self):
        self.write(self.gb / 'Puzzle.gb', gb_rom())
        chosen = self.library.scan()['releases'][0]['id']
        saved = self.library.save_collection('Puzzle Night', 'chromatic', [chosen], description='  One more puzzle.  ')
        collection = saved['collections'][0]
        self.assertEqual(collection['description'], 'One more puzzle.')
        collection_id = collection['id']
        renamed = self.library.save_collection('Puzzle Break', 'chromatic', [chosen], collection_id)
        self.assertEqual(renamed['collections'][0]['description'], 'One more puzzle.')
        replaced = self.library.save_collection('Puzzle Break', 'chromatic', [chosen], collection_id, 'A short break.')
        self.assertEqual(replaced['collections'][0]['description'], 'A short break.')
        cleared = self.library.save_collection('Puzzle Break', 'chromatic', [chosen], collection_id, '')
        self.assertNotIn('description', cleared['collections'][0])
        with self.assertRaisesRegex(ValueError, 'description'):
            self.library.save_collection('Puzzle Break', 'chromatic', [chosen], collection_id, 'x' * 1001)
        self.assertNotIn('description', self.library.scan()['collections'][0])

    def test_collection_archive_preserves_exact_definition_and_never_overwrites_prior_archive(self):
        source = self.write(self.gb / 'Puzzle.gb', gb_rom())
        chosen = self.library.scan()['releases'][0]['id']
        self.library.save_collection('After Dark', 'chromatic', [chosen], 'after-dark', 'The first evening.')
        definition = self.root / 'library/collections/after-dark.json'
        first_bytes = json.dumps(json.loads(definition.read_bytes()), indent=3).encode() + b'\n\n'
        definition.write_bytes(first_bytes)
        result = self.library.delete_collection('after-dark')
        self.assertEqual(result['collections'], [])
        self.assertFalse(definition.exists())
        archive = self.root / 'backups/sets'
        first_archive, = archive.glob('after-dark--*.json')
        self.assertEqual(first_archive.read_bytes(), first_bytes)
        self.assertEqual(json.loads(first_archive.read_bytes())['release_ids'], [chosen])
        self.assertEqual(json.loads(first_archive.read_bytes())['description'], 'The first evening.')

        self.library.save_collection('After Dark Again', 'chromatic', [chosen], 'after-dark', 'The next evening.')
        second_bytes = definition.read_bytes()
        self.library.delete_collection('after-dark')
        archives = list(archive.glob('after-dark--*.json'))
        self.assertEqual(len(archives), 2)
        self.assertEqual(first_archive.read_bytes(), first_bytes)
        self.assertEqual({p.read_bytes() for p in archives}, {first_bytes, second_bytes})
        self.assertEqual(self.library.scan()['collections'], [])
        self.assertEqual(source.read_bytes(), gb_rom())

    def test_collection_archive_rejects_archive_escape_and_symlinked_definition(self):
        self.write(self.gb / 'Puzzle.gb', gb_rom())
        chosen = self.library.scan()['releases'][0]['id']
        self.library.save_collection('After Dark', 'chromatic', [chosen], 'after-dark')
        definition = self.root / 'library/collections/after-dark.json'
        expected = definition.read_bytes()
        with tempfile.TemporaryDirectory(dir=self.root.parent) as outside_name:
            outside = Path(outside_name)
            (self.root / 'backups').mkdir()
            (self.root / 'backups/sets').symlink_to(outside, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, 'escapes'):
                self.library.delete_collection('after-dark')
            self.assertEqual(list(outside.iterdir()), [])
            self.assertEqual(definition.read_bytes(), expected)

        definition.rename(definition.with_suffix('.saved'))
        definition.symlink_to(definition.with_suffix('.saved'))
        with self.assertRaises(ValueError):
            self.library.delete_collection('after-dark')
        self.assertEqual(definition.read_bytes(), expected)

    def test_stale_source_aborts_without_a_published_pack(self):
        source = self.write(self.gb / 'Puzzle.gb', gb_rom())
        self.library.scan()
        source.write_bytes(gb_rom(fill=2))
        with self.assertRaisesRegex(ValueError, 'Source changed'):
            self.library.prepare('chromatic', 'Stale', [], 'folder')
        self.assertFalse((self.root / 'exports/Stale').exists())
        self.assertEqual(list((self.root / 'exports').glob('*')) if (self.root / 'exports').exists() else [], [])

    def test_existing_pack_is_never_overwritten(self):
        self.write(self.gb / 'Puzzle.gb', gb_rom())
        self.library.scan()
        receipt = self.library.prepare('chromatic', 'Keep', [], 'folder')
        marker = Path(receipt['folder']) / 'owner-note.txt'
        marker.write_text('retain this')
        rebuilt = self.library.prepare('chromatic', 'Keep', [], 'folder')
        self.assertNotEqual(rebuilt['folder'], receipt['folder'])
        self.assertTrue(Path(rebuilt['folder']).is_dir())
        self.assertTrue(Path(rebuilt['folder']).name.startswith('Keep--'))
        self.assertEqual(marker.read_text(), 'retain this')

    def test_chromatic_image_requires_the_os_before_publishing_any_pack(self):
        self.write(self.gb / 'Puzzle.gb', gb_rom())
        self.library.scan()
        with mock.patch('media.create_image') as create_image:
            with self.assertRaisesRegex(ValueError, 'requires the verified GB X7 operating system'):
                self.library.prepare('chromatic', 'Unbootable', [], 'image')
        create_image.assert_not_called()
        self.assertFalse((self.root / 'exports').exists())

    def test_library_root_is_independent_of_the_application_folder(self):
        self.assertEqual(Library(self.root).root, self.root.resolve())
        self.assertFalse(self.root.is_relative_to(ROOT))

    def test_metadata_is_shown_only_for_the_exact_build_and_stays_off_the_rom(self):
        source = self.write(self.gb / 'Puzzle.gb', gb_rom())
        snapshot = self.library.scan()
        release = snapshot['releases'][0]
        original = source.read_bytes()
        artwork = self.write(self.root / 'cache/handheld-metadata/cover.png', b'artwork fixture')
        record = {'description': 'A sourced puzzle description.', 'artwork_path': str(artwork),
                  'genre': 'Puzzle', 'release_year': 1992, 'identity_verified': True,
                  'source_url': 'https://example.com/original-reference', 'confidence': 'exact reference hash'}
        self.write(self.root / 'cache/handheld-metadata/index.json', json.dumps({'entries': {release['id']: record}}).encode())
        enriched = self.library.scan()['releases'][0]
        self.assertEqual(enriched['artwork_path'], str(artwork))
        self.assertEqual(enriched['metadata']['description'], record['description'])
        self.assertEqual(enriched['metadata']['release_year'], '1992')
        self.assertEqual(source.read_bytes(), original)

    def test_invalid_stock_game_code_can_have_exact_build_app_metadata(self):
        source = self.write(self.n64 / 'Example.z64', n64_rom(code='NED\x00'))
        release = self.library.scan()['releases'][0]
        override = {'id': release['id'], 'identity_sha256': release['identity_sha256'],
                    'title': 'Example homebrew', 'description': 'An app-only exact-build record.'}
        self.write(self.root / 'library/metadata-overrides' / (release['id'].replace(':', '-') + '.json'), json.dumps(override).encode())
        enriched = self.library.scan()['releases'][0]
        self.assertEqual(enriched['title'], 'Example homebrew')
        self.assertFalse(enriched['metadata']['ed64_metadata_supported'])
        result = self.library.prepare('m64', 'Example', [release['id']], 'folder')
        self.assertEqual((Path(result['folder']) / 'N64 Games/Example.z64').read_bytes(), source.read_bytes())

    def test_verify_covers_invalid_scan_issues_as_well_as_indexed_games(self):
        source = self.write(self.gb / 'Playable.gb', gb_rom())
        invalid = self.write(self.gb / 'Truncated.gb', b'partial header')
        self.library.scan()
        result = self.library.verify()
        summary = result['verification_summary']
        self.assertEqual(summary['files_checked'], 2)
        self.assertEqual(summary['invalid'], 1)
        self.assertTrue(Path(summary['report_path']).is_file())
        self.assertEqual(source.read_bytes(), gb_rom())
        self.assertEqual(invalid.read_bytes(), b'partial header')
        self.assertEqual(result['releases'][0]['verification']['status'], 'unlisted')

    def test_cached_verification_does_not_attach_to_a_changed_build(self):
        source = self.write(self.gb / 'Puzzle.gb', gb_rom())
        self.library.scan()
        verified = self.library.verify()
        self.assertIn('verification', verified['releases'][0])
        source.write_bytes(gb_rom(fill=3))
        changed = self.library.scan()
        self.assertNotEqual(changed['releases'][0]['id'], verified['releases'][0]['id'])
        self.assertNotIn('verification', changed['releases'][0])

    def test_zip_import_is_repeatable_and_rejects_conflicts_before_copying(self):
        archive = self.root / 'inputs.zip'
        with zipfile.ZipFile(archive, 'w') as output:
            output.writestr('nested/Puzzle (v1.1).gb', gb_rom())
        result = self.library.import_paths([archive])
        self.assertEqual(result['profiles'][1]['file_count'], 1)
        self.library.import_paths([archive])
        self.assertEqual(self.library.scan()['profiles'][1]['file_count'], 1)
        conflict = self.root / 'conflict.zip'
        with zipfile.ZipFile(conflict, 'w') as output:
            output.writestr('New.gb', gb_rom(fill=3))
            output.writestr('Puzzle (v1.1).gb', gb_rom(fill=4))
        with self.assertRaisesRegex(ValueError, 'different.*file'):
            self.library.import_paths([conflict])
        self.assertFalse((self.root / 'library/GB-GBC/Imports/New.gb').exists())
        self.assertEqual((self.root / 'library/GB-GBC/Imports/Puzzle (v1.1).gb').read_bytes(), gb_rom())

    def test_import_detects_existing_source_aliases_and_source_filename_conflicts(self):
        original = self.write(self.gb / 'Puzzle.gb', gb_rom())
        result = self.library.import_paths([original])
        self.assertEqual(result['profiles'][1]['file_count'], 1)
        self.assertFalse((self.root / 'library/GB-GBC/Imports/Puzzle.gb').exists())
        archive = self.root / 'conflict-source.zip'
        with zipfile.ZipFile(archive, 'w') as output:
            output.writestr('Puzzle.gb', gb_rom(fill=4))
        with self.assertRaisesRegex(ValueError, 'different library file'):
            self.library.import_paths([archive])
        self.assertEqual(original.read_bytes(), gb_rom())

    def test_nested_7zip_import_extracts_the_real_rom(self):
        import shutil
        executable = shutil.which('7zz') or shutil.which('7z')
        if not executable:
            self.skipTest('7-Zip is not installed')
        source = self.write(self.root / 'incoming/Archive Game.gb', gb_rom(fill=3))
        nested = self.root / 'one.7z'
        subprocess.run([executable, 'a', '-t7z', str(nested), source.name], cwd=source.parent, check=True, capture_output=True)
        outer = self.root / 'outer.zip'
        with zipfile.ZipFile(outer, 'w') as archive:
            archive.write(nested, 'nested/one.7z')
        result = self.library.import_paths([outer])
        self.assertEqual(result['profiles'][1]['file_count'], 1)
        self.assertEqual((self.root / 'library/GB-GBC/Imports/Archive Game.gb').read_bytes(), gb_rom(fill=3))

    def test_malicious_zip_and_links_are_rejected_without_importing_roms(self):
        for name in ['../escape.gb', '/absolute.gb', 'C:/drive.gb', 'dir\\escape.gb']:
            archive = self.root / 'bad.zip'
            with zipfile.ZipFile(archive, 'w') as output:
                output.writestr('Valid.gb', gb_rom())
                output.writestr(name, gb_rom())
            with self.assertRaisesRegex(ValueError, 'Unsafe archive'):
                self.library.import_paths([archive])
            self.assertFalse((self.root / 'library/GB-GBC/Imports/Valid.gb').exists())
        with zipfile.ZipFile(self.root / 'link.zip', 'w') as output:
            info = zipfile.ZipInfo('linked.gb')
            info.create_system = 3
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            output.writestr(info, '../target.gb')
        with self.assertRaisesRegex(ValueError, 'links'):
            self.library.import_paths([self.root / 'link.zip'])

    def test_symlink_escape_is_rejected_for_sources_and_managed_writes(self):
        outside = self.root.parent / (self.root.name + '-outside')
        outside.mkdir()
        try:
            self.write(outside / 'Escaped.gb', gb_rom())
            (self.gb / 'Escaped.gb').symlink_to(outside / 'Escaped.gb')
            with self.assertRaisesRegex(ValueError, 'escapes'):
                self.library.scan()
            (self.gb / 'Escaped.gb').unlink()
            (self.root / 'library/GB-GBC/Imports').symlink_to(outside, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, 'escapes'):
                self.library.import_paths([outside / 'Escaped.gb'])
            self.assertEqual(sorted(p.name for p in outside.iterdir()), ['Escaped.gb'])
        finally:
            import shutil
            shutil.rmtree(outside)

    def test_n64_export_converts_metadata_only_for_selected_codes(self):
        self.write(self.n64 / 'Alpha.z64', n64_rom(code='NAAE'))
        self.write(self.n64 / 'Beta.z64', n64_rom(code='NBBE', fill=1))
        base = self.root / 'cache/n64-flashcart-menu-metadata/metadata'
        self.write(base / 'N/A/A/E/metadata.ini', b'[meta]\nname = Alpha\n')
        self.write(base / 'N/B/B/E/metadata.ini', b'[meta]\nname = Beta\n')
        self.write(self.root / 'custom-metadata/N/A/A/E/metadata.ini', b'[meta]\nname = Custom Alpha\n')
        snapshot = self.library.scan()
        alpha = next(r for r in snapshot['releases'] if r['title'] == 'Alpha')
        receipt = self.library.prepare('m64', 'One N64', [alpha['id']], 'folder')
        folder = Path(receipt['folder'])
        self.assertEqual((folder / 'ed64/metadata/N/A/A/E/metadata.ini').read_text(), '[meta]\nname = Custom Alpha\n')
        self.assertFalse((folder / 'ed64/metadata/N/B/B/E').exists())
        self.assertEqual((folder / 'N64 Games/Alpha.z64').read_bytes(), n64_rom(code='NAAE'))

    def test_handheld_os_matches_preserved_manifest_and_excludes_saved_state(self):
        self.write(self.gb / 'Puzzle.gb', gb_rom())
        legacy = self.root / 'legacy/everdrive-gb-x7-20261012'
        os_bytes = b'constructed menu fixture, not vendor firmware'
        records = [{'path': 'GBCSYS/GBCOS.BIN', 'bytes': len(os_bytes), 'sha256': hashlib.sha256(os_bytes).hexdigest()}]
        legacy.mkdir(parents=True)
        with zipfile.ZipFile(legacy / 'EverDrive_Full_Backup_20261012.zip', 'w') as archive:
            archive.writestr('BACKUP_MANIFEST.json', json.dumps(records))
            archive.writestr('GBCSYS/GBCOS.BIN', os_bytes)
            archive.writestr('GBCSYS/SAVE/old.srm', b'old save')
            archive.writestr('GBCSYS/REGISTRY.BIN', b'old registry')
        themed = b'constructed themed menu fixture'

        def apply_theme(data, target, scratch):
            self.assertEqual(data, os_bytes)
            target.parent.mkdir(parents=True)
            target.write_bytes(themed)

        self.library.scan()
        with mock.patch.object(self.library, '_handheld_theme', side_effect=apply_theme) as applier:
            receipt = self.library.prepare('chromatic', 'Bootable', [], 'folder')
        folder = Path(receipt['folder'])
        self.assertEqual((folder / 'GBCSYS/GBCOS.BIN').read_bytes(), themed)
        self.assertEqual(applier.call_count, 1)
        self.assertFalse((folder / 'GBCSYS/SAVE').exists())
        self.assertFalse((folder / 'GBCSYS/REGISTRY.BIN').exists())

    def test_official_cached_os_is_preferred_and_a_bad_cache_aborts(self):
        self.write(self.gb / 'Puzzle.gb', gb_rom())
        base = self.root / 'cache/firmware/gb-x7'
        os_bytes = b'constructed stock menu fixture'
        self.write(base / 'GBCSYS/GBCOS.BIN', os_bytes)
        receipt = {'sha256': hashlib.sha256(os_bytes).hexdigest(), 'source_url': 'https://example.invalid/fixture.zip', 'version': '1.06'}
        self.write(base / 'source.json', json.dumps(receipt).encode())
        themed = b'constructed themed menu fixture'

        def apply_theme(data, target, scratch):
            self.assertEqual(data, os_bytes)
            target.parent.mkdir(parents=True)
            target.write_bytes(themed)

        self.library.scan()
        with mock.patch.object(self.library, '_handheld_theme', side_effect=apply_theme):
            result = self.library.prepare('chromatic', 'Official OS', [], 'folder')
        self.assertEqual((Path(result['folder']) / 'GBCSYS/GBCOS.BIN').read_bytes(), themed)
        self.assertEqual((base / 'GBCSYS/GBCOS.BIN').read_bytes(), os_bytes)
        (base / 'GBCSYS/GBCOS.BIN').write_bytes(b'changed cache')
        with self.assertRaisesRegex(ValueError, 'SHA-256'):
            self.library.prepare('chromatic', 'Bad OS', [], 'folder')
        self.assertFalse((self.root / 'exports/Bad OS').exists())

    def test_cli_outputs_one_json_document_and_errors_on_stderr(self):
        self.write(self.gb / 'Puzzle.gb', gb_rom())
        command = [sys.executable, str(ROOT / 'library_manager.py'), '--root', str(self.root)]
        success = subprocess.run(command + ['scan'], capture_output=True, text=True)
        self.assertEqual(success.returncode, 0, success.stderr)
        self.assertEqual(json.loads(success.stdout)['profiles'][1]['file_count'], 1)
        failure = subprocess.run(command + ['prepare', '--profile', 'chromatic', '--name', '../unsafe'], capture_output=True, text=True)
        self.assertNotEqual(failure.returncode, 0)
        self.assertEqual(failure.stdout, '')
        self.assertTrue(failure.stderr.strip())

    def test_cleanup_preserves_revisions_collections_and_restores_every_source(self):
        original = self.write(self.gb / 'Puzzle (v1.1).gb', gb_rom())
        duplicate = self.write(self.root / 'library/GB-GBC/Imports/Puzzle copy.gb', gb_rom())
        revised = self.write(self.gb / 'Puzzle (v1.2).gb', gb_rom(fill=1))
        console = n64_rom(code='NTEE')
        native = self.write(self.n64 / 'Console.z64', console)
        swapped = self.write(self.n64 / 'Console copy.v64', bytes(console[i ^ 1] for i in range(len(console))))
        before = {p: p.read_bytes() for p in [original, duplicate, revised, native, swapped]}
        snapshot = self.library.scan()
        ids = {r['id'] for r in snapshot['releases']}
        chosen = next(r['id'] for r in snapshot['releases'] if r['profile'] == 'chromatic' and r['version'] == '1.1')
        self.library.save_collection('Keep exact edition', 'chromatic', [chosen])
        plan = self.library.cleanup_plan()
        self.assertEqual(plan['duplicate_files'], 2)
        self.assertEqual({r['source'] for r in plan['moves']}, {str(duplicate), str(swapped)})
        cleaned = self.library.cleanup(Path(plan['plan_path']), plan['token'])
        self.assertEqual({r['id'] for r in cleaned['releases']}, ids)
        self.assertEqual(cleaned['collections'][0]['release_ids'], [chosen])
        self.assertEqual(cleaned['management']['duplicate_files'], 0)
        self.assertTrue(original.is_file() and revised.is_file() and native.is_file())
        self.assertFalse(duplicate.exists() or swapped.exists())
        receipt = json.loads((self.root / 'quarantine' / plan['id'] / 'RECEIPT.json').read_text())
        for move in receipt['moves']:
            self.assertEqual(Path(move['quarantine_path']).read_bytes(), before[Path(move['source'])])
        restored = self.library.restore_cleanup(plan['id'])
        self.assertEqual(restored['management']['duplicate_files'], 2)
        self.assertTrue(restored['management']['quarantine_batches'][0]['restored'])
        self.assertEqual({p: p.read_bytes() for p in before}, before)

    def test_checksum_findings_are_review_only_and_newer_revision_is_not_a_duplicate(self):
        bad_checksum = bytearray(gb_rom())
        bad_checksum[-1] ^= 1
        suspect = self.write(self.gb / 'Suspect.gb', bad_checksum)
        self.write(self.gb / 'Puzzle (v1.1).gb', gb_rom())
        self.write(self.gb / 'Puzzle (v1.2).gb', gb_rom(fill=1))
        plan = self.library.cleanup_plan('chromatic')
        self.assertEqual(plan['moves'], [])
        self.assertEqual(plan['warnings'][0]['title'], 'Suspect')
        self.assertIn('Global checksum mismatch.', plan['warnings'][0]['warnings'])
        result = self.library.cleanup(Path(plan['plan_path']), plan['token'])
        self.assertEqual(suspect.read_bytes(), bad_checksum)
        self.assertEqual(len(result['releases']), 3)
        self.assertEqual(list((self.root / 'quarantine').glob('*')) if (self.root / 'quarantine').exists() else [], [])

    def test_cleanup_checks_every_hash_and_plan_scope_before_moving_any_file(self):
        originals = [
            self.write(self.gb / 'Alpha.gb', gb_rom()),
            self.write(self.gb / 'Alpha copy.gb', gb_rom()),
            self.write(self.gb / 'Beta.gb', gb_rom(fill=1)),
            self.write(self.gb / 'Beta copy.gb', gb_rom(fill=1)),
        ]
        plan = self.library.cleanup_plan()
        changed = Path(plan['moves'][-1]['source'])
        changed.write_bytes(gb_rom(fill=3))
        with self.assertRaisesRegex(ValueError, 'changed'):
            self.library.cleanup(Path(plan['plan_path']), plan['token'])
        self.assertTrue(all(p.is_file() for p in originals))
        self.assertFalse((self.root / 'quarantine' / plan['id']).exists())
        fresh = self.library.cleanup_plan()
        note = self.write(self.root / 'owner-note.gb', gb_rom())
        fresh['moves'][0]['source'] = str(note)
        Path(fresh['plan_path']).write_text(json.dumps(fresh))
        with self.assertRaisesRegex(ValueError, 'outside the active library'):
            self.library.cleanup(Path(fresh['plan_path']), fresh['token'])
        self.assertTrue(note.is_file() and all(p.is_file() for p in originals))

    def test_cleanup_rolls_back_a_failed_move(self):
        paths = [self.write(self.gb / name, gb_rom()) for name in ['Alpha.gb', 'Beta.gb', 'Gamma.gb']]
        before = {p: p.read_bytes() for p in paths}
        plan = self.library.cleanup_plan()
        rename = Path.rename
        calls = 0
        def fail_second(source, target):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError('simulated disk failure')
            return rename(source, target)
        with mock.patch.object(Path, 'rename', fail_second):
            with self.assertRaises(OSError):
                self.library.cleanup(Path(plan['plan_path']), plan['token'])
        self.assertEqual({p: p.read_bytes() for p in paths}, before)
        self.assertFalse((self.root / 'quarantine' / plan['id']).exists())

    def test_restore_refuses_a_collision_without_overwriting_or_partly_restoring(self):
        for name in ['Alpha.gb', 'Beta.gb', 'Gamma.gb']:
            self.write(self.gb / name, gb_rom())
        plan = self.library.cleanup_plan()
        self.library.cleanup(Path(plan['plan_path']), plan['token'])
        occupied = Path(plan['moves'][-1]['source'])
        occupied.write_bytes(b'new owner file')
        saved = list((self.root / 'quarantine' / plan['id'] / 'files').rglob('*.gb'))
        with self.assertRaisesRegex(ValueError, 'occupied'):
            self.library.restore_cleanup(plan['id'])
        self.assertEqual(occupied.read_bytes(), b'new owner file')
        self.assertTrue(all(p.is_file() for p in saved))
        self.assertFalse(Path(plan['moves'][0]['source']).exists())

    def test_restore_recovers_an_interrupted_cleanup_process(self):
        originals = [self.write(self.gb / name, gb_rom()) for name in ['Alpha.gb', 'Beta.gb', 'Gamma.gb']]
        plan = self.library.cleanup_plan()
        script = '''
from pathlib import Path
import os, sys
sys.path.insert(0, sys.argv[1])
from library_manager import Library
rename = Path.rename
def stop_after_move(source, target):
    result = rename(source, target)
    os._exit(99)
Path.rename = stop_after_move
Library(Path(sys.argv[2])).cleanup(Path(sys.argv[3]), sys.argv[4])
'''
        run = subprocess.run([sys.executable, '-c', script, str(ROOT), str(self.root), plan['plan_path'], plan['token']], capture_output=True, text=True)
        self.assertEqual(run.returncode, 99, run.stderr)
        self.assertEqual(sum(p.is_file() for p in originals), 2)
        result = self.library.restore_cleanup(plan['id'])
        self.assertTrue(all(p.read_bytes() == gb_rom() for p in originals))
        self.assertTrue(result['management']['quarantine_batches'][0]['restored'])

    def test_invalid_files_remain_visible_as_issues_without_hiding_valid_games(self):
        good = self.write(self.gb / 'Playable.gb', gb_rom())
        invalid = [
            self.write(self.gb / 'Empty.gb', b''),
            self.write(self.gb / 'Truncated.gb', b'partial header'),
            self.write(self.n64 / 'Unknown.z64', b'not an n64 image'),
        ]
        snapshot = self.library.scan()
        self.assertEqual(len(snapshot['releases']), 1)
        self.assertEqual({r['path'] for r in snapshot['issues']}, {str(p) for p in invalid})
        pack = Path(self.library.prepare('chromatic', 'Good files', [], 'folder')['folder'])
        self.assertEqual((pack / 'GB/N-S/Playable.gb').read_bytes(), good.read_bytes())
        self.assertFalse(list(pack.rglob('Empty.gb')) or list(pack.rglob('Truncated.gb')))
        plan = self.library.cleanup_plan()
        self.assertEqual(plan['moves'], [])
        self.assertEqual(len(plan['issues']), 3)
        self.assertTrue(all(p.exists() for p in invalid))


class LibraryRepositoryBoundary(unittest.TestCase):
    def test_rejects_git_checkout_and_nested_data_folder(self):
        with tempfile.TemporaryDirectory() as directory:
            checkout = Path(directory).resolve()
            (checkout / '.git').mkdir()
            child = checkout / 'games'
            child.mkdir()
            for root in (checkout, child):
                with self.subTest(root=root), self.assertRaisesRegex(ValueError, 'Git repositories'):
                    Library(root)
            self.assertFalse((checkout / 'library').exists())

    def test_rejects_git_worktree_marker(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root / '.git').write_text('gitdir: ../other-worktree\n')
            with self.assertRaisesRegex(ValueError, 'Git repositories'):
                Library(root)

    def test_rejects_unpacked_source_archive(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root / 'app').mkdir()
            for name in ('release.json', 'library_manager.py', 'app/CartShelf.swift'):
                (root / name).write_text('authored source marker')
            with self.assertRaisesRegex(ValueError, 'source folders'):
                Library(root)


if __name__ == '__main__':
    unittest.main()
