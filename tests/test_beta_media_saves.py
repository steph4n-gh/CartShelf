import contextlib
import hashlib
import json
import shutil
import subprocess
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import card_saves
import media
from test_card_saves import gb_rom, n64_rom


class BetaSaveTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='cartshelf-save-fixture-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.current = self.root / 'current'
        self.pack = self.root / 'pack'
        self.history = self.root / 'backups/save-history'
        self.fixture_os = b'Authored non-executable fixture OS'.ljust(media.GB_X7_OS_BYTES, b'.')
        pin = patch.object(media, 'GB_X7_OS_SHA256', {hashlib.sha256(self.fixture_os).hexdigest()})
        pin.start()
        self.addCleanup(pin.stop)
        self.current.mkdir()
        self.pack.mkdir()

    def write(self, root, name, data):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def seed(self, timer=False):
        self.write(self.current, 'GB/A-F/Original.gb', gb_rom(timer=timer))
        self.write(self.pack, 'GB/A-F/Renamed.gb', gb_rom(timer=timer))
        self.save = self.write(self.current, 'GBCSYS/SAVE/Original.srm', b'older progress')
        if timer:
            self.clock = self.write(self.current, 'GBCSYS/SAVE/Original.rtc', b'C' * 512)
        self.manifest()

    def manifest(self, profile='chromatic'):
        if profile == 'chromatic':
            self.write(self.pack, 'GBCSYS/GBCOS.BIN', self.fixture_os)
        files = [{'path': path.relative_to(self.pack).as_posix(), 'bytes': path.stat().st_size,
                  'sha256': media.sha256(path)} for path in sorted(self.pack.rglob('*')) if path.is_file() and path.name != 'MANIFEST.json']
        (self.pack / 'MANIFEST.json').write_text(json.dumps({'profile': profile, 'name': 'Authored fixture', 'files': files}))

    def archive(self):
        return card_saves.build_restore_plan(self.current, self.pack, 'chromatic', self.history)

    def choice(self, entry=None):
        entry = entry or card_saves.history_entries(self.history, 'chromatic')[0]
        return {key: entry[key] for key in ('profile', 'identity_sha256', 'snapshot_id', 'bundle_sha256')}

    def history_bytes(self):
        return {path.relative_to(self.history).as_posix(): path.read_bytes()
                for path in self.history.rglob('*') if path.is_file()}

    def test_preview_does_not_create_history(self):
        self.seed()
        result = card_saves.build_restore_plan(self.current, self.pack, 'chromatic', self.history, archive=False)
        self.assertFalse(self.history.exists())
        self.assertEqual(result['archived_bundles'], 0)
        self.assertEqual(result['files'][0]['source_kind'], 'current')

    def test_history_lists_all_snapshots_without_hashing_file_payloads(self):
        self.seed()
        self.archive()
        self.save.write_bytes(b'newer progress')
        self.archive()
        before = self.history_bytes()
        with patch.object(card_saves, '_digest', side_effect=AssertionError('Listing is manifest-only')):
            entries = card_saves.history_entries(self.history, 'chromatic')
        self.assertEqual(len(entries), 2)
        self.assertTrue(all(not entry['verified'] for entry in entries))
        self.assertEqual(before, self.history_bytes())
        verified = card_saves.verified_history_bundle(self.history, self.choice(entries[-1]))
        self.assertTrue(verified['verified'])
        self.assertEqual(Path(verified['files'][0]['source_path']).read_bytes(), b'older progress')

    def test_explicit_older_save_overrides_current_for_only_one_plan(self):
        self.seed()
        self.archive()
        choice = self.choice()
        self.save.write_bytes(b'newer progress')
        before = self.history_bytes()
        selected = card_saves.build_restore_plan(self.current, self.pack, 'chromatic', self.history,
                                                 archive=False, choices=[choice])
        default = card_saves.build_restore_plan(self.current, self.pack, 'chromatic', self.history, archive=False)
        self.assertEqual(selected['files'][0]['source_kind'], 'selected_earlier')
        self.assertEqual(Path(selected['files'][0]['source_path']).read_bytes(), b'older progress')
        self.assertEqual(default['files'][0]['source_kind'], 'current')
        self.assertEqual(Path(default['files'][0]['source_path']).read_bytes(), b'newer progress')
        self.assertEqual(before, self.history_bytes())
        self.assertEqual(self.save.read_bytes(), b'newer progress')

    def test_automatic_fallback_blocks_conflict_but_explicit_exact_choice_is_allowed(self):
        self.seed()
        self.archive()
        choice = self.choice()
        self.write(self.current, 'GB/A-F/Alias.gb', gb_rom())
        self.write(self.current, 'GBCSYS/SAVE/Alias.srm', b'conflicting current')
        default = card_saves.build_restore_plan(self.current, self.pack, 'chromatic', self.history, archive=False)
        self.assertEqual(default['files'], [])
        self.assertTrue(any('fallback is blocked' in row['reason'] for row in default['skipped']))
        selected = card_saves.build_restore_plan(self.current, self.pack, 'chromatic', self.history,
                                                 archive=False, choices=[choice])
        self.assertEqual(selected['files'][0]['source_kind'], 'selected_earlier')

    def test_clock_and_sram_are_selected_as_a_complete_bundle(self):
        self.seed(timer=True)
        self.archive()
        choice = self.choice()
        self.save.write_bytes(b'new SRAM')
        self.clock.write_bytes(b'N' * 512)
        selected = card_saves.build_restore_plan(self.current, self.pack, 'chromatic', self.history,
                                                 archive=False, choices=[choice])
        self.assertEqual({row['path'].rsplit('.', 1)[1] for row in selected['files']}, {'srm', 'rtc'})
        self.assertEqual({row['source_kind'] for row in selected['files']}, {'selected_earlier'})
        self.assertEqual(next(Path(row['source_path']).read_bytes() for row in selected['files'] if row['path'].endswith('.rtc')), b'C' * 512)

    def test_incomplete_timer_history_is_rejected(self):
        self.seed(timer=True)
        self.archive()
        entry = card_saves.history_entries(self.history, 'chromatic')[0]
        folder = self.history / entry['snapshot_id']
        manifest = json.loads((folder / 'MANIFEST.json').read_text())
        manifest['bundles'][0]['files'] = [row for row in manifest['bundles'][0]['files'] if row['key'] != 'rtc']
        encoded = json.dumps(manifest).encode()
        (folder / 'MANIFEST.json').write_bytes(encoded)
        (folder / 'MANIFEST.sha256').write_text(hashlib.sha256(encoded).hexdigest())
        with self.assertRaisesRegex(ValueError, 'Incomplete X7'):
            card_saves.history_entries(self.history, 'chromatic')

    def test_unrelated_choice_and_duplicate_choice_reject_without_archival(self):
        self.seed()
        self.archive()
        choice = self.choice()
        self.write(self.pack, 'GB/A-F/Renamed.gb', gb_rom(seed=2))
        before = self.history_bytes()
        with self.assertRaisesRegex(ValueError, 'does not match'):
            card_saves.build_restore_plan(self.current, self.pack, 'chromatic', self.history, choices=[choice])
        self.assertEqual(before, self.history_bytes())
        with self.assertRaisesRegex(ValueError, 'Only one'):
            card_saves.validate_choices([choice, choice])
        with self.assertRaises(ValueError):
            card_saves.validate_choices([{**choice, 'profile': []}])

    def test_history_tampering_stops_selection_without_new_archive(self):
        self.seed()
        self.archive()
        choice = self.choice()
        entry = card_saves.history_entries(self.history, 'chromatic')[0]
        path = self.history / entry['snapshot_id'] / entry['files'][0]['stored_path']
        path.write_bytes(b'changed bytes!')
        before = self.history_bytes()
        with self.assertRaisesRegex(ValueError, 'archived save changed'):
            card_saves.build_restore_plan(self.current, self.pack, 'chromatic', self.history, choices=[choice])
        self.assertEqual(before, self.history_bytes())

    def test_casefold_root_exclusion_does_not_walk_protected_directory(self):
        self.seed()
        protected = self.current / '.SPOTLIGHT-V100'
        protected.mkdir()
        original_iterdir = Path.iterdir
        def iterdir(path):
            if path == protected:
                raise PermissionError('Protected metadata must not be visited')
            return original_iterdir(path)
        with patch.object(Path, 'iterdir', iterdir):
            result = media.replacement_preview(self.root, self.current, self.pack, 'chromatic')
        self.assertEqual(len(result['games']['retained']), 1)
        self.assertFalse(self.history.exists())
        self.assertEqual(result['firmware']['installed_status'], 'unknown')

    def test_changed_rom_revision_is_added_and_removed(self):
        self.seed()
        self.write(self.pack, 'GB/A-F/Renamed.gb', gb_rom(seed=2))
        self.manifest()
        result = media.replacement_preview(self.root, self.current, self.pack, 'chromatic')
        self.assertEqual(len(result['games']['added']), 1)
        self.assertEqual(len(result['games']['removed']), 1)
        self.assertEqual(result['games']['retained'], [])
        self.assertEqual(result['saves']['current'], [])
        self.assertEqual(len(result['saves']['kept_in_backup']), 1)

    def test_byte_swapped_n64_identity_is_retained(self):
        self.write(self.current, 'N64 Games/Original.v64', n64_rom('v64'))
        self.write(self.pack, 'N64 Games/Renamed.z64', n64_rom())
        self.write(self.current, 'ed64/gamedata/Original.v64/bram.eep', b'E' * 512)
        self.manifest('m64')
        result = media.replacement_preview(self.root, self.current, self.pack, 'm64')
        self.assertEqual(len(result['games']['retained']), 1)
        self.assertEqual(result['saves']['current'][0]['path'], 'ed64/gamedata/Renamed.z64/bram.eep')

    def test_preview_is_stable_after_verified_file_copy(self):
        self.seed()
        reviewed = media.replacement_preview(self.root, self.current, self.pack, 'chromatic')
        target = self.root / 'backups/recorded.files'
        card = {'id': 'disk99', 'size_bytes': 128 * media.MIB}
        with patch.object(media, 'readonly_card_contents', return_value=contextlib.nullcontext((self.current, lambda: None))):
            media.backup_all_files(self.root, card, target)
        recorded = media.replacement_preview(self.root, target / 'files', self.pack, 'chromatic')
        self.assertEqual(reviewed, recorded)
        self.assertFalse(self.history.exists())

    def test_changed_save_stops_before_archival_or_format(self):
        self.seed()
        reviewed = media.replacement_preview(self.root, self.current, self.pack, 'chromatic')
        self.save.write_bytes(b'changed after review')
        backup = self.root / 'old.img'
        backup.write_bytes(b'owned raw fixture')
        image = self.root / 'pack.img'
        image.write_bytes(b'owned image fixture')
        plan = {'preview': reviewed, 'save_choices': [], 'image_sha256': media.sha256(image)}
        receipt = {'sha256': media.sha256(backup), 'size_bytes': backup.stat().st_size, 'verified': True}
        with patch.object(media, 'mounted_image', side_effect=lambda path, **_: contextlib.nullcontext(self.pack if path == image else self.current)), \
                patch.object(media, 'install_file_tree') as install_tree:
            with self.assertRaisesRegex(ValueError, 'changed after review'):
                media.install_compilation(self.root, {'id': 'disk99'}, image, backup, plan, receipt, {})
        install_tree.assert_not_called()
        self.assertFalse(self.history.exists())

    def test_changed_selected_history_stops_before_format(self):
        self.seed()
        self.archive()
        choice = self.choice()
        reviewed = media.replacement_preview(self.root, self.current, self.pack, 'chromatic', [choice])
        bundle = card_saves.verified_history_bundle(self.history, choice)
        Path(bundle['files'][0]['source_path']).write_bytes(b'tampered saved')
        backup = self.root / 'old.img'
        backup.write_bytes(b'owned raw fixture')
        image = self.root / 'pack.img'
        image.write_bytes(b'owned image fixture')
        plan = {'preview': reviewed, 'save_choices': [choice], 'image_sha256': media.sha256(image)}
        receipt = {'sha256': media.sha256(backup), 'size_bytes': backup.stat().st_size, 'verified': True}
        count = len(list(self.history.iterdir()))
        with patch.object(media, 'mounted_image', side_effect=lambda path, **_: contextlib.nullcontext(self.pack if path == image else self.current)), \
                patch.object(media, 'install_file_tree') as install_tree:
            with self.assertRaises(ValueError):
                media.install_compilation(self.root, {'id': 'disk99'}, image, backup, plan, receipt, {})
        install_tree.assert_not_called()
        self.assertEqual(len(list(self.history.iterdir())), count)

    def test_unassociated_current_bytes_remain_visible_when_history_differs(self):
        self.seed()
        self.archive()
        (self.current / 'GB/A-F/Original.gb').unlink()
        self.save.write_bytes(b'newer unassociated progress')
        result = media.replacement_preview(self.root, self.current, self.pack, 'chromatic')
        self.assertEqual(len(result['saves']['history']), 1)
        self.assertEqual(len(result['saves']['kept_in_backup']), 1)
        self.assertEqual(result['saves']['kept_in_backup'][0]['path'], 'GBCSYS/SAVE/Original.srm')
        self.save.write_bytes(b'older progress')
        matching = media.replacement_preview(self.root, self.current, self.pack, 'chromatic')
        self.assertEqual(matching['saves']['kept_in_backup'], [])

    def test_reserved_root_regular_file_never_disappears_from_preview(self):
        self.seed()
        self.write(self.current, '.SPOTLIGHT-V100', b'user file')
        with self.assertRaisesRegex(ValueError, 'whole.?card image'):
            media.replacement_preview(self.root, self.current, self.pack, 'chromatic')

    def test_cli_choice_boundaries_reject_payloads_and_invalid_json(self):
        for value in ['null', '{}', '[{"path":"/tmp/save"}]', '{', '"' + 'x' * (1024 * 1024) + '"']:
            with self.subTest(value_length=len(value)), self.assertRaises(ValueError):
                media.save_choices_json(value)

    def test_runtime_and_code_volumes_are_protected_alongside_library(self):
        volumes = {self.root: 'disk1', Path('/'): 'disk0', Path('/runtime'): 'disk2', Path('/code'): 'disk3'}
        def info(path):
            return {'DeviceIdentifier': volumes[Path(path)], 'WholeDisk': True, 'VirtualOrPhysical': 'Physical'}
        with patch.object(media, 'command', return_value={'AllDisksAndPartitions': []}), \
                patch.object(media, 'disk_info', side_effect=info), \
                patch.object(media.sys, 'executable', '/runtime/bin/python3'), \
                patch.dict(media.__dict__, {'__file__': '/code/media.py'}), \
                patch.object(Path, 'is_mount', lambda path: path in volumes), \
                patch.object(Path, 'is_file', lambda path: path == Path('/code/media.py')), \
                patch.object(Path, 'resolve', lambda path, **_: path):
            result = media.cards(self.root)
        self.assertEqual(set(result['protected_disks']), {'disk0', 'disk1', 'disk2', 'disk3'})
        self.assertEqual(result['cards'], [])

    def test_library_replacement_is_rejected_during_an_operation(self):
        media.workspace(self.root)
        original = self.root / 'chosen'
        original.mkdir()
        media.workspace(original)
        original.rename(self.root / 'old-chosen')
        original.mkdir()
        with self.assertRaisesRegex(ValueError, 'filesystem or folder changed'):
            media.managed(original, original / 'exports')

    def test_history_arriving_after_preview_cannot_change_the_final_restore(self):
        self.seed()
        self.archive()
        (self.current / 'GB/A-F/Original.gb').unlink()
        self.save.unlink()
        reviewed = media.replacement_preview(self.root, self.current, self.pack, 'chromatic')
        newer = self.root / 'concurrent-card'
        newer.mkdir()
        self.write(newer, 'GB/A-F/Other.gb', gb_rom())
        self.write(newer, 'GBCSYS/SAVE/Other.srm', b'newer concurrent history')
        original = card_saves.build_restore_plan
        def interleaved(current, pack, profile, history=None, **kwargs):
            if kwargs.get('archive', True):
                original(newer, pack, profile, history)
            return original(current, pack, profile, history, **kwargs)
        backup = self.root / 'old.img'
        backup.write_bytes(b'owned raw fixture')
        image = self.root / 'pack.img'
        image.write_bytes(b'owned image fixture')
        plan = {'preview': reviewed, 'save_choices': [], 'image_sha256': media.sha256(image)}
        receipt = {'sha256': media.sha256(backup), 'size_bytes': backup.stat().st_size, 'verified': True}
        with patch.object(card_saves, 'build_restore_plan', side_effect=interleaved), \
                patch.object(media, 'mounted_image', side_effect=lambda path, **_: contextlib.nullcontext(self.pack if path == image else self.current)), \
                patch.object(media, 'install_file_tree') as install_tree:
            with self.assertRaisesRegex(ValueError, 'final save restoration changed'):
                media.install_compilation(self.root, {'id': 'disk99'}, image, backup, plan, receipt, {})
        install_tree.assert_not_called()

    def test_fresh_gb_pack_requires_the_supported_os_at_every_media_entry(self):
        self.seed()
        (self.pack / 'GBCSYS/GBCOS.BIN').unlink()
        manifest = json.loads((self.pack / 'MANIFEST.json').read_text())
        manifest['files'] = [row for row in manifest['files'] if row['path'] != 'GBCSYS/GBCOS.BIN']
        (self.pack / 'MANIFEST.json').write_text(json.dumps(manifest))
        with self.assertRaisesRegex(ValueError, 'requires the verified'):
            media.compilation_files(self.pack)
        with self.assertRaisesRegex(ValueError, 'requires the verified'):
            media.create_image(self.root, self.pack)
        self.assertFalse((self.root / 'exports/pack.img').exists())
        unknown = self.write(self.pack, 'GBCSYS/GBCOS.BIN', b'unknown OS'.ljust(media.GB_X7_OS_BYTES, b'.'))
        manifest['files'].append({'path': 'GBCSYS/GBCOS.BIN', 'bytes': unknown.stat().st_size,
                                  'sha256': media.sha256(unknown)})
        (self.pack / 'MANIFEST.json').write_text(json.dumps(manifest))
        with self.assertRaisesRegex(ValueError, 'requires the verified'):
            media.compilation_files(self.pack)

    def test_library_locator_cannot_be_rebound_through_a_symlink(self):
        chosen = self.root / 'chosen'
        replacement = self.root / 'replacement'
        chosen.mkdir()
        replacement.mkdir()
        media.workspace(chosen)
        chosen.rename(self.root / 'original-chosen')
        chosen.symlink_to(replacement, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, 'filesystem or folder changed'):
            media.workspace(chosen)

    def test_fresh_process_refuses_a_copied_plan_in_a_replaced_library(self):
        selected = self.root / 'selected-library'
        (selected / 'exports').mkdir(parents=True)
        (selected / 'exports/fixture.img').write_bytes(b'authored image fixture')
        code = """
import json, sys
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, sys.argv[1])
import media
root = Path(sys.argv[2])
image = root / 'exports/fixture.img'
card = {'id':'disk99','identity':'authored-device','physical_identity':'authored-generation','size_bytes':1048576}
info = {'kind':'compilation','sha256':media.sha256(image),'size_bytes':image.stat().st_size}
with patch.object(media,'image_info',return_value=info), patch.object(media,'plan_preview',return_value={'saves':{}}):
    if sys.argv[3] == 'plan':
        with patch.object(media,'current_card',return_value=card):
            result = media.plan_install(root,image,'disk99',backup_kind='raw')
        print(json.dumps(result))
    else:
        plan = json.loads(next((root / 'library/card-plans').glob('*.json')).read_text())
        with patch.object(media,'current_card',side_effect=AssertionError('No card discovery before library binding')):
            try:
                media.validate_plan(root,plan,plan['token'])
            except ValueError as error:
                print(json.dumps({'accepted':False,'reason':str(error)}))
            else:
                print(json.dumps({'accepted':True}))
"""
        command = [sys.executable, '-I', '-B', '-c', code, str(Path(media.__file__).parent), str(selected)]
        first = subprocess.run(command + ['plan'], check=True, capture_output=True, text=True)
        original_plan = json.loads(first.stdout)
        selected.rename(self.root / 'original-library')
        shutil.copytree(self.root / 'original-library', selected)
        self.assertNotEqual(selected.stat().st_ino, original_plan['library_identity']['inode'])
        second = subprocess.run(command + ['validate'], check=True, capture_output=True, text=True)
        result = json.loads(second.stdout)
        self.assertFalse(result['accepted'])
        self.assertIn('filesystem changed after review', result['reason'])

    def test_non_fat_raw_preview_explains_the_mode_already_selected(self):
        with patch.object(media, 'readonly_card_contents', side_effect=media.UnsupportedCardFilesystem('Authored exFAT fixture')), \
                patch.object(media, 'mounted_image') as mount:
            with self.assertRaisesRegex(ValueError, 'Whole Card Image is selected') as failure:
                media.plan_preview(self.root, {'id': 'disk99'}, self.root / 'fixture.img', {'kind': 'compilation'}, backup_kind='raw')
        self.assertIn('Back Up Only', str(failure.exception))
        mount.assert_not_called()

    def test_legacy_timer_bundle_without_clock_cannot_be_applied(self):
        self.seed(timer=True)
        self.archive()
        entry = card_saves.history_entries(self.history, 'chromatic')[0]
        directory = self.history / entry['snapshot_id']
        manifest = json.loads((directory / 'MANIFEST.json').read_text())
        bundle = manifest['bundles'][0]
        bundle.pop('rtc_required')
        bundle['files'] = [row for row in bundle['files'] if row['key'] == 'srm']
        encoded = json.dumps(manifest).encode()
        (directory / 'MANIFEST.json').write_bytes(encoded)
        (directory / 'MANIFEST.sha256').write_text(hashlib.sha256(encoded).hexdigest())
        choice = self.choice()
        with self.assertRaisesRegex(ValueError, 'complete SRAM and clock bundle'):
            card_saves.build_restore_plan(self.current, self.pack, 'chromatic', self.history,
                                          archive=False, choices=[choice])

    def test_preview_and_choices_are_both_bound_by_plan_token(self):
        plan = {'nonce': 'fixture', 'preview': {'source_fingerprint': 'before'}, 'save_choices': []}
        original = media.plan_token(plan)
        plan['preview']['source_fingerprint'] = 'after'
        self.assertNotEqual(original, media.plan_token(plan))
        plan['save_choices'] = [{'identity_sha256': 'a' * 64}]
        self.assertNotEqual(original, media.plan_token(plan))


if __name__ == '__main__':
    unittest.main()
