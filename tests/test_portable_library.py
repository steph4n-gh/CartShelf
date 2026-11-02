import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import cart_updates
import handheld_metadata
from library_manager import Library
import rom_verifier


CODE = Path(__file__).resolve().parents[1]


class PortableLibraryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='cartshelf-portable-')
        self.addCleanup(self.temporary.cleanup)
        self.parent = Path(self.temporary.name).resolve()
        self.root = self.parent / 'Chosen library'
        self.root.mkdir()

    def cli(self, *args):
        return subprocess.run([sys.executable, str(CODE / 'library_manager.py'),
                               '--root', str(self.root), *args],
                              capture_output=True, text=True)

    def test_initialize_selected_folder_outside_application(self):
        library = Library(self.root)
        result = library.initialize()
        self.assertEqual(result['releases'], [])
        self.assertTrue((self.root / 'library/catalog.json').is_file())
        for relative in ['library/N64/Imports', 'library/GB-GBC/Imports',
                         'library/collections', 'exports', 'backups', 'cache',
                         'reference', 'quarantine', 'tmp']:
            self.assertTrue((self.root / relative).is_dir(), relative)
        self.assertFalse(self.root.is_relative_to(CODE))
        catalog = (self.root / 'library/catalog.json').read_bytes()
        with self.assertRaisesRegex(ValueError, 'already contains'):
            library.initialize()
        self.assertEqual((self.root / 'library/catalog.json').read_bytes(), catalog)

    def test_cli_initializes_and_scans_only_the_explicit_folder(self):
        created = self.cli('init')
        self.assertEqual(created.returncode, 0, created.stderr)
        self.assertEqual(json.loads(created.stdout)['releases'], [])
        scanned = self.cli('scan')
        self.assertEqual(scanned.returncode, 0, scanned.stderr)
        self.assertEqual(json.loads(scanned.stdout)['releases'], [])

    def test_init_does_not_overwrite_an_existing_catalog_or_collection(self):
        target = self.root / 'library/catalog.json'
        target.parent.mkdir()
        target.write_bytes(b'preserve this existing catalog exactly')
        with self.assertRaisesRegex(ValueError, 'already contains'):
            Library(self.root).initialize()
        self.assertEqual(target.read_bytes(), b'preserve this existing catalog exactly')
        self.assertFalse((self.root / 'exports').exists())
        target.unlink()
        definition = self.root / 'library/collections/existing.json'
        definition.parent.mkdir()
        definition.write_bytes(b'preserve this collection exactly')
        with self.assertRaisesRegex(ValueError, 'already contains'):
            Library(self.root).initialize()
        self.assertEqual(definition.read_bytes(), b'preserve this collection exactly')
        self.assertFalse(target.exists())

    def test_replaced_library_path_does_not_receive_a_new_catalog(self):
        library = Library(self.root)
        original = self.parent / 'Original library'
        self.root.rename(original)
        self.root.mkdir()
        with self.assertRaisesRegex(ValueError, 'changed'):
            library.scan()
        self.assertEqual(list(self.root.iterdir()), [])
        self.assertEqual(list(original.iterdir()), [])

    def test_disconnected_library_does_not_recreate_its_path(self):
        library = Library(self.root)
        self.root.rmdir()
        with self.assertRaisesRegex(ValueError, 'unavailable'):
            library.initialize()
        self.assertFalse(self.root.exists())

    def test_root_must_exist_be_a_directory_and_be_writable(self):
        with self.assertRaises(FileNotFoundError):
            Library(self.root / 'Missing')
        file = self.root / 'A file'
        file.write_text('not a folder')
        with self.assertRaisesRegex(ValueError, 'writable library folder'):
            Library(file)
        with mock.patch('library_manager.os.access', return_value=False):
            with self.assertRaisesRegex(ValueError, 'writable library folder'):
                Library(self.root)

    def test_application_source_cannot_be_the_library(self):
        with self.assertRaisesRegex(ValueError, 'outside the application'):
            Library(CODE)
        with self.assertRaisesRegex(ValueError, 'outside the application'):
            Library(CODE / 'tests')
        for workspace in [rom_verifier.workspace, handheld_metadata.workspace,
                          cart_updates.refresh_updates]:
            with self.subTest(tool=workspace.__module__):
                with self.assertRaisesRegex(ValueError, 'outside the application'):
                    workspace(CODE)

    def test_cli_requires_an_explicit_library(self):
        result = subprocess.run([sys.executable, str(CODE / 'library_manager.py'), 'scan'],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn('--root', result.stderr)
        self.assertEqual(result.stdout, '')

    def test_untrusted_library_theme_script_never_executes(self):
        marker = self.root / 'Executed'
        untrusted = self.root / 'themes/CartShelf-GB/apply_theme.py'
        untrusted.parent.mkdir(parents=True)
        untrusted.write_text('from pathlib import Path\nPath(' + repr(str(marker)) + ").write_text('executed')\n")
        scratch = self.root / 'scratch'
        scratch.mkdir()
        target = self.root / 'exports/GBCSYS/GBCOS.BIN'
        with self.assertRaisesRegex(ValueError, 'verified stock OS'):
            Library(self.root)._handheld_theme(b'constructed invalid stock fixture', target, scratch)
        self.assertFalse(marker.exists())
        self.assertFalse(target.exists())

    def test_verifier_and_metadata_accept_a_chosen_non_source_folder(self):
        verified = rom_verifier.verify_library(self.root, [])
        self.assertEqual(verified['summary']['files_checked'], 0)
        self.assertTrue((self.root / 'reports/ROM_VERIFICATION_REPORT.json').is_file())
        self.assertEqual(handheld_metadata.workspace(self.root), self.root)
        with self.assertRaises(ValueError):
            handheld_metadata.managed(self.root, self.parent / 'outside')

    def test_release_check_stops_if_library_changes_during_download(self):
        def replace_library(request, timeout):
            self.root.rename(self.parent / 'Original library')
            self.root.mkdir()
            response = io.BytesIO(b"metadata: {manifest_version: 1, timestamp: fixture}\ngames: {tetris: {versions: {'1.0': {date: 2025-01-01, changes: Fixture}}}}")
            response.geturl = lambda: cart_updates.SOURCE
            return response

        with mock.patch('cart_updates.urllib.request.urlopen', side_effect=replace_library):
            with self.assertRaisesRegex(ValueError, 'changed'):
                cart_updates.refresh_updates(self.root)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_verification_stops_before_reports_if_library_was_replaced(self):
        def replace_library(root, profiles):
            self.root.rename(self.parent / 'Original library')
            self.root.mkdir()
            return {}, [], []

        with mock.patch('rom_verifier.reference_index', side_effect=replace_library):
            with self.assertRaisesRegex(ValueError, 'changed'):
                rom_verifier.verify_library(self.root, [])
        self.assertEqual(list(self.root.iterdir()), [])

    def test_artwork_download_cannot_write_to_a_replacement_library(self):
        from PIL import Image

        image = io.BytesIO()
        Image.new('RGB', (16, 16), 'white').save(image, 'PNG')

        def replace_library(url, limit):
            self.root.rename(self.parent / 'Original library')
            self.root.mkdir()
            return image.getvalue()

        with mock.patch('handheld_metadata.fetch', side_effect=replace_library):
            with self.assertRaisesRegex(ValueError, 'changed'):
                handheld_metadata.download_art(self.root, 'https://example.invalid/fixture.png')
        self.assertEqual(list(self.root.iterdir()), [])


if __name__ == '__main__':
    unittest.main()
