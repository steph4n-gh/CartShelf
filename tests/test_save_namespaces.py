from pathlib import Path, PurePosixPath
import copy
import hashlib
import json
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from library_manager import Library, _resolve_save_namespaces, handheld_path
from test_library_manager import gb_rom, n64_rom


class SaveNamespaceBehavior(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='save-namespace-tests-')
        self.root = Path(self.temporary.name)
        self.library = Library(self.root)

    def tearDown(self):
        self.temporary.cleanup()

    def write(self, name, data):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def paired_games(self):
        first = self.write('library/GB-GBC/Imports/Paperboy (USA).gb', gb_rom(fill=1))
        second = self.write('library/GB-GBC/Imports/Paperboy (USA).gbc', gb_rom(fill=2, color=True))
        return first, second

    def test_gb_gbc_share_no_save_name_and_source_identity_stays_exact(self):
        sources = self.paired_games()
        snapshot = self.library.scan()
        releases = snapshot['releases']
        exports = {r['sources'][0]['export_path'] for r in releases}
        self.assertEqual(exports, {'GB/N-S/Paperboy (USA) [GB].gb', 'GBC/N-S/Paperboy (USA) [GBC].gbc'})
        self.assertEqual({r['id'] for r in releases}, {'chromatic:' + hashlib.sha256(p.read_bytes()).hexdigest() for p in sources})
        self.assertEqual({r['sources'][0]['filename'] for r in releases}, {p.name for p in sources})
        self.assertEqual(snapshot, self.library.scan())

    def test_selected_and_full_pack_use_the_same_global_save_name(self):
        self.paired_games()
        releases = self.library.scan()['releases']
        selected = releases[0]
        selected_pack = Path(self.library.prepare('chromatic', 'Selected', [selected['id']])['folder'])
        full_pack = Path(self.library.prepare('chromatic', 'Full', [])['folder'])
        relative = selected['sources'][0]['export_path']
        expected = Path(selected['sources'][0]['path']).read_bytes()
        self.assertEqual((selected_pack / relative).read_bytes(), expected)
        self.assertEqual((full_pack / relative).read_bytes(), expected)
        full_manifest = json.loads((full_pack / 'MANIFEST.json').read_text())
        self.assertEqual(len({PurePosixPath(row['path']).stem.casefold() for row in full_manifest['files']}), 2)

    def test_old_cached_catalog_is_corrected_without_requiring_rescan(self):
        self.paired_games()
        releases = self.library.scan()['releases']
        expected = releases[0]['sources'][0]['export_path']
        catalog = json.loads(self.library.catalog.read_text())
        for release in catalog['snapshot']['releases']:
            for source in release['sources']:
                source['export_path'] = handheld_path(source['filename'])
        self.library.catalog.write_text(json.dumps(catalog))
        pack = Path(self.library.prepare('chromatic', 'Old Cached Catalog', [releases[0]['id']])['folder'])
        self.assertTrue((pack / expected).is_file())
        again = Path(self.library.prepare('chromatic', 'Another Pack', [releases[0]['id']])['folder'])
        self.assertEqual((pack / expected).read_bytes(), (again / expected).read_bytes())

    def test_same_platform_revisions_receive_stable_identity_suffixes(self):
        self.write('library/GB-GBC/Imports/One/Shared.gb', gb_rom(fill=1))
        self.write('library/GB-GBC/Imports/Two/Shared.gb', gb_rom(fill=2))
        releases = self.library.scan()['releases']
        names = {PurePosixPath(r['sources'][0]['export_path']).stem for r in releases}
        self.assertEqual(len(names), 2)
        for release in releases:
            self.assertIn(' [GB-' + release['identity_sha256'][:12] + ']', release['sources'][0]['export_path'])
        aliases = copy.deepcopy(releases)
        _resolve_save_namespaces(aliases)
        self.assertEqual(releases, aliases)

    def test_n64_distinct_roms_cannot_share_a_gamedata_directory(self):
        self.write('N64ROMsPACK/Shared.z64', n64_rom(fill=1))
        self.write('library/N64/Imports/Shared.z64', n64_rom(fill=2))
        releases = self.library.scan()['releases']
        names = {PurePosixPath(r['sources'][0]['export_path']).name.casefold() for r in releases}
        self.assertEqual(len(names), 2)
        self.assertTrue(all('[n64-' in name for name in names))

    def colliding_fixture_games(self):
        expected = {}
        for index, title in enumerate(['Pocket Comet', 'Puzzle Brook', 'Road Orchard']):
            for color, extension in [(False, 'gb'), (True, 'gbc')]:
                data = gb_rom(fill=index * 2 + int(color) + 1, color=color)
                name = title + '.' + extension
                self.write('library/GB-GBC/Imports/' + name, data)
                expected[hashlib.sha256(data).hexdigest()] = data
        return expected

    def test_generated_catalog_has_unique_cartridge_save_names(self):
        self.colliding_fixture_games()
        releases = self.library.scan()['releases']
        original = {(r['id'], s['path']): (s['filename'], s['raw_sha256']) for r in releases for s in r['sources']}
        _resolve_save_namespaces(releases)
        namespaces = {}
        fixed = []
        for release in releases:
            for source in release['sources']:
                self.assertEqual((source['filename'], source['raw_sha256']), original[(release['id'], source['path'])])
                path = PurePosixPath(source['export_path'])
                namespaces.setdefault((release['profile'], path.stem.casefold()), set()).add(release['id'])
                fixed.append(path.name)
        self.assertEqual(len(releases), 6)
        self.assertTrue(all(len(identities) == 1 for identities in namespaces.values()))
        self.assertEqual(len(fixed), 6)
        self.assertTrue(all(' [GB' in name for name in fixed))

    def test_six_generated_colliding_builds_export_with_exact_bytes(self):
        expected = self.colliding_fixture_games()
        self.library.scan()
        pack = Path(self.library.prepare('chromatic', 'Synthetic Save Names', [])['folder'])
        rows = json.loads((pack / 'MANIFEST.json').read_text())['files']
        rom_rows = [row for row in rows if PurePosixPath(row['path']).suffix in {'.gb', '.gbc'}]
        self.assertEqual(len(rom_rows), 6)
        self.assertEqual(len({PurePosixPath(row['path']).stem.casefold() for row in rom_rows}), 6)
        for row in rom_rows:
            self.assertEqual((pack / row['path']).read_bytes(), expected[row['sha256']])


if __name__ == '__main__':
    unittest.main()
