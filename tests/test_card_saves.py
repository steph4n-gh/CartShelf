import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import card_saves


ROOT = Path(__file__).resolve().parents[1]


def gb_rom(seed=1, timer=False):
    data = bytearray([seed]) * 32768
    data[0x104:0x134] = card_saves.GB_LOGO
    data[0x147] = 0x10 if timer else 0x03
    data[0x148] = 0
    checksum = 0
    for byte in data[0x134:0x14d]:
        checksum = (checksum - byte - 1) & 255
    data[0x14d] = checksum
    return bytes(data)


def n64_rom(order='z64', seed=1):
    data = bytearray([seed]) * 4096
    data[:4] = bytes.fromhex('80371240')
    if order == 'v64':
        return b''.join(bytes([data[i+1], data[i]]) for i in range(0, len(data), 2))
    if order == 'n64':
        return b''.join(bytes(reversed(data[i:i+4])) for i in range(0, len(data), 4))
    return bytes(data)


class CardSaveTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='card-saves-test-')
        self.root = Path(self.temporary.name).resolve()
        self.backup = self.root / 'backup'
        self.pack = self.root / 'pack'
        self.history = self.root / 'history'
        self.backup.mkdir()
        self.pack.mkdir()

    def tearDown(self):
        self.temporary.cleanup()

    def write(self, root, name, data):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def game(self, root, name='Old.gb', seed=1, timer=False):
        return self.write(root, 'GB/A-F/' + name, gb_rom(seed, timer))

    def save(self, root, name='Old', data=b'a' * 8192, extension='srm'):
        return self.write(root, 'GBCSYS/SAVE/' + name + '.' + extension, data)

    def plan(self, history=False):
        return card_saves.build_restore_plan(self.backup, self.pack, 'chromatic', self.history if history else None)

    def test_exact_identity_renames_gb_save_to_exported_stem(self):
        self.game(self.backup)
        self.game(self.pack, 'Renamed.gb')
        source = self.save(self.backup)
        plan = self.plan()
        self.assertEqual(len(plan['files']), 1)
        row = plan['files'][0]
        self.assertEqual(row['path'], 'GBCSYS/SAVE/Renamed.srm')
        self.assertEqual(Path(row['source_path']).read_bytes(), source.read_bytes())
        self.assertEqual(row['sha256'], hashlib.sha256(source.read_bytes()).hexdigest())
        self.assertFalse((self.pack / row['path']).exists())

    def test_different_gb_and_gbc_roms_cannot_share_flat_save_name(self):
        self.game(self.backup)
        self.save(self.backup)
        self.write(self.pack, 'GB/A-F/Same.gb', gb_rom(1))
        self.write(self.pack, 'GBC/A-F/Same.gbc', gb_rom(2))
        with self.assertRaisesRegex(ValueError, 'save filename'):
            self.plan(history=True)
        self.assertFalse(self.history.exists())

    def test_distinct_n64_games_cannot_share_whole_filename_save_directory(self):
        self.write(self.pack, 'N64 Games/A/Same.z64', n64_rom(seed=1))
        self.write(self.pack, 'N64 Games/B/Same.z64', n64_rom(seed=2))
        with self.assertRaisesRegex(ValueError, 'save filename'):
            card_saves.build_restore_plan(self.backup, self.pack, 'm64')

    def test_header_warning_rom_still_reserves_its_cartridge_save_filename(self):
        self.write(self.pack, 'GB/A-F/Same.gb', gb_rom(1))
        warning = bytearray(gb_rom(2))
        warning[0x14d] ^= 1
        self.write(self.pack, 'GBC/A-F/Same.gbc', warning)
        with self.assertRaisesRegex(ValueError, 'save filename'):
            self.plan()

    def test_controller_pak_file_follows_exact_n64_rom_and_history(self):
        self.write(self.backup, 'N64 Games/Old.n64', n64_rom('n64'))
        saved = self.write(self.backup, 'ed64/gamedata/Old.n64/adventure.mpk', b'c' * 32768)
        self.write(self.pack, 'N64 Games/Renamed.z64', n64_rom())
        first = card_saves.build_restore_plan(self.backup, self.pack, 'm64', self.history)
        self.assertEqual(first['files'][0]['path'], 'ed64/gamedata/Renamed.z64/adventure.mpk')
        empty = self.root / 'empty'
        empty.mkdir()
        restored = card_saves.build_restore_plan(empty, self.pack, 'm64', self.history)['files'][0]
        self.assertEqual(restored['source_kind'], 'history')
        self.assertEqual(Path(restored['source_path']).read_bytes(), saved.read_bytes())

    def test_global_controller_pak_dumps_stay_preserved_without_rom_guessing(self):
        self.write(self.backup, 'dumps/cpak-slot-1.mpk', b'c' * 32768)
        self.write(self.pack, 'N64 Games/Test.z64', n64_rom())
        plan = card_saves.build_restore_plan(self.backup, self.pack, 'm64')
        self.assertEqual(plan['files'], [])
        self.assertEqual(plan['skipped'][0]['path'], 'dumps/cpak-slot-1.mpk')

    def test_gold_sram_and_clock_are_one_matching_bundle(self):
        self.game(self.backup, timer=True)
        self.game(self.pack, 'Gold Canonical.gbc', timer=True)
        self.save(self.backup)
        self.save(self.backup, data=b'c' * 512, extension='rtc')
        plan = self.plan(history=True)
        self.assertEqual({r['path'] for r in plan['files']}, {'GBCSYS/SAVE/Gold Canonical.srm', 'GBCSYS/SAVE/Gold Canonical.rtc'})
        self.assertEqual(len({r['history_snapshot'] for r in plan['files']}), 1)

    def test_timer_cartridge_with_missing_clock_is_not_partially_restored(self):
        self.game(self.backup, timer=True)
        self.game(self.pack, 'New.gb', timer=True)
        self.save(self.backup)
        self.assertEqual(self.plan()['files'], [])
        self.assertIn('incomplete', self.plan()['skipped'][0]['reason'])

    def test_adjacent_emulator_files_and_ramdump_are_preserved_but_not_restored(self):
        self.game(self.backup)
        self.game(self.pack, 'New.gb')
        self.save(self.backup)
        self.write(self.backup, 'GB/A-F/Old.gb.ram', b'\0' * 32768)
        self.write(self.backup, 'GB/A-F/Old.gb.rtc', b'\0' * 10)
        self.write(self.backup, 'GBCSYS/ramdump01.srm', b'r' * 131072)
        plan = self.plan()
        self.assertEqual(len(plan['files']), 1)
        self.assertEqual(len(plan['skipped']), 3)

    def test_changed_rom_with_same_filename_does_not_receive_old_save(self):
        self.game(self.backup)
        self.game(self.pack, seed=2)
        self.save(self.backup)
        self.assertEqual(self.plan()['files'], [])

    def test_ambiguous_old_filename_blocks_restore(self):
        self.game(self.backup)
        self.write(self.backup, 'GBC/T-Z/Old.gbc', gb_rom(2))
        self.game(self.pack, 'New.gb')
        self.save(self.backup)
        plan = self.plan()
        self.assertEqual(plan['files'], [])
        self.assertIn('ambiguous', plan['skipped'][0]['reason'])

    def test_identical_aliases_deduplicate_and_conflicting_alias_saves_do_not(self):
        self.game(self.backup)
        self.game(self.backup, 'Alias.gb')
        self.game(self.pack, 'New.gb')
        self.save(self.backup)
        self.save(self.backup, 'Alias')
        self.assertEqual(len(self.plan()['files']), 1)
        self.save(self.backup, 'Alias', b'b' * 8192)
        self.assertEqual(self.plan()['files'], [])

    def test_rotation_recovers_a_save_after_pack_b_replaces_a(self):
        self.game(self.backup, 'A.gb')
        self.save(self.backup, 'A', b'A' * 8192)
        self.game(self.pack, 'B.gb', seed=2)
        first = self.plan(history=True)
        self.assertEqual(first['files'], [])
        self.assertEqual(first['archived_bundles'], 1)
        second_backup = self.root / 'backup-b'
        second_backup.mkdir()
        self.game(second_backup, 'B.gb', seed=2)
        self.save(second_backup, 'B', b'B' * 8192)
        third_pack = self.root / 'pack-a'
        third_pack.mkdir()
        self.game(third_pack, 'A renamed.gb')
        final = card_saves.build_restore_plan(second_backup, third_pack, 'chromatic', self.history)
        self.assertEqual(len(final['files']), 1)
        self.assertEqual(final['files'][0]['source_kind'], 'history')
        self.assertEqual(Path(final['files'][0]['source_path']).read_bytes(), b'A' * 8192)
        self.assertEqual(len([p for p in self.history.iterdir() if not p.name.startswith('.')]), 2)

    def test_current_save_wins_and_newest_verified_history_wins_later(self):
        self.game(self.backup)
        self.game(self.pack, 'New.gb')
        self.save(self.backup)
        first = self.plan(history=True)
        first_path = Path(first['files'][0]['source_path'])
        self.save(self.backup, data=b'n' * 8192)
        second = self.plan(history=True)
        self.assertEqual(second['files'][0]['source_kind'], 'current')
        self.assertEqual(first_path.read_bytes(), b'a' * 8192)
        empty = self.root / 'empty'
        empty.mkdir()
        fallback = card_saves.build_restore_plan(empty, self.pack, 'chromatic', self.history)
        self.assertEqual(fallback['files'][0]['source_kind'], 'history')
        self.assertEqual(Path(fallback['files'][0]['source_path']).read_bytes(), b'n' * 8192)

    def test_conflicting_current_saves_block_stale_history(self):
        self.game(self.backup)
        self.game(self.pack, 'New.gb')
        self.save(self.backup)
        self.plan(history=True)
        self.game(self.backup, 'Alias.gb')
        self.save(self.backup, 'Alias', b'b' * 8192)
        plan = self.plan(history=True)
        self.assertEqual(plan['files'], [])
        self.assertTrue(any('fallback is blocked' in r['reason'] for r in plan['skipped']))

    def test_tampered_history_content_stops_planning(self):
        self.game(self.backup)
        self.game(self.pack, 'New.gb')
        self.save(self.backup)
        first = self.plan(history=True)
        Path(first['files'][0]['source_path']).write_bytes(b'x' * 8192)
        with self.assertRaisesRegex(ValueError, 'archived save changed'):
            self.plan(history=True)

    def test_tampered_history_manifest_stops_planning(self):
        self.game(self.backup)
        self.game(self.pack, 'New.gb')
        self.save(self.backup)
        first = self.plan(history=True)
        manifest = Path(first['files'][0]['history_snapshot']) / 'MANIFEST.json'
        manifest.write_text(manifest.read_text() + ' ')
        with self.assertRaisesRegex(ValueError, 'manifest changed'):
            self.plan(history=True)

    def test_n64_all_byte_orders_restore_to_new_whole_filename(self):
        for order in ['z64', 'v64', 'n64']:
            with self.subTest(order=order):
                backup = self.root / ('old-' + order)
                pack = self.root / ('new-' + order)
                backup.mkdir()
                pack.mkdir()
                self.write(backup, 'N64 Games/Old.' + order, n64_rom(order))
                source = self.write(backup, 'ed64/gamedata/Old.' + order + '/bram.srm', b's' * 32768)
                self.write(pack, 'N64 Games/Canonical.z64', n64_rom())
                plan = card_saves.build_restore_plan(backup, pack, 'm64')
                self.assertEqual(len(plan['files']), 1)
                self.assertEqual(plan['files'][0]['path'], 'ed64/gamedata/Canonical.z64/bram.srm')
                self.assertEqual(Path(plan['files'][0]['source_path']).read_bytes(), source.read_bytes())

    def test_unknown_ed64_runtime_files_are_not_restored(self):
        self.write(self.backup, 'N64 Games/Old.z64', n64_rom())
        self.write(self.pack, 'N64 Games/Canonical.z64', n64_rom())
        self.write(self.backup, 'ed64/gamedata/Old.z64/bram.eep', b'e' * 512)
        self.write(self.backup, 'ed64/gamedata/Old.z64/game.cfg', b'cfg')
        plan = card_saves.build_restore_plan(self.backup, self.pack, 'm64')
        self.assertEqual(len(plan['files']), 1)
        self.assertEqual(plan['skipped'][0]['path'], 'ed64/gamedata/Old.z64/game.cfg')

    def test_absent_old_rom_and_malformed_old_header_are_skipped(self):
        self.game(self.pack, 'New.gb')
        self.save(self.backup)
        self.assertIn('absent', self.plan()['skipped'][0]['reason'])
        self.write(self.backup, 'GB/A-F/Old.gb', b'bad' * 200)
        self.assertEqual(self.plan()['files'], [])
        self.assertIn('malformed', self.plan()['skipped'][0]['reason'])

    def test_symlink_in_backup_or_history_is_rejected(self):
        self.game(self.pack, 'New.gb')
        outside = self.write(self.root, 'outside.gb', gb_rom())
        (self.backup / 'Escape.gb').symlink_to(outside)
        with self.assertRaisesRegex(ValueError, 'symbolic links'):
            self.plan()
        (self.backup / 'Escape.gb').unlink()
        self.history.symlink_to(self.root)
        with self.assertRaisesRegex(ValueError, 'real directory'):
            self.plan(history=True)

    def test_pack_save_collision_is_rejected_without_overwriting(self):
        self.game(self.backup)
        self.game(self.pack, 'New.gb')
        self.save(self.backup)
        existing = self.save(self.pack, 'New', b'p' * 8192)
        with self.assertRaisesRegex(ValueError, 'already contains a save'):
            self.plan()
        self.assertEqual(existing.read_bytes(), b'p' * 8192)

    def test_empty_card_produces_empty_success(self):
        self.game(self.pack)
        self.assertEqual(self.plan(history=True)['files'], [])


if __name__ == '__main__':
    unittest.main()
