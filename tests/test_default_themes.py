from pathlib import Path
import hashlib
import importlib.util
import json
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import library_manager
from library_manager import Library, N64


class DefaultThemeBehavior(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='default-theme-tests-')
        self.addCleanup(self.temporary.cleanup)
        scratch = Path(self.temporary.name)
        self.root = scratch / 'library'
        self.root.mkdir()
        self.code = (scratch / 'application').resolve()
        self.code.mkdir()
        self.library = Library(self.root)
        shutil.copytree(ROOT / 'themes/CartShelf-64/dist', self.code / 'themes/CartShelf-64/dist')
        theme = self.code / 'themes/CartShelf-GB'
        theme.mkdir()
        shutil.copytree(ROOT / 'themes/CartShelf-GB/dist', theme / 'dist')
        spec = importlib.util.spec_from_file_location('palette_fixture', ROOT / 'themes/CartShelf-GB/apply_theme.py')
        applier = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(applier)
        stock = bytearray([0xA5] * applier.SIZE)
        for index, offset in enumerate(applier.OFFSETS):
            stock[offset:offset + 2] = applier.STOCK_BYTES[index * 2:index * 2 + 2]
        self.stock = bytes(stock)
        themed = bytearray(stock)
        for index, offset in enumerate(applier.OFFSETS):
            themed[offset:offset + 2] = applier.palette_bytes()[index * 2:index * 2 + 2]
        self.themed = bytes(themed)
        fixture_code = (ROOT / 'themes/CartShelf-GB/apply_theme.py').read_text()
        fixture_code = fixture_code.replace(applier.STOCK_SHA256, hashlib.sha256(self.stock).hexdigest())
        fixture_code = fixture_code.replace(applier.THEMED_SHA256, hashlib.sha256(self.themed).hexdigest())
        (theme / 'apply_theme.py').write_text(fixture_code)
        self.asset_patch = patch.object(library_manager, 'CODE_ROOT', self.code)
        self.asset_patch.start()
        self.addCleanup(self.asset_patch.stop)

    def source(self, profile):
        if profile == 'm64':
            data = bytearray(4096)
            data[:4] = bytes.fromhex('80371240')
            data[0x3b:0x3f] = b'NTEE'
            path = self.root / 'N64ROMsPACK/Test.z64'
        else:
            data = bytearray(32768)
            data[0x104:0x134] = bytes.fromhex('CEED6666CC0D000B03730083000C000D0008111F8889000EDCCC6EE6DDDDD999BBBB67636E0EECCCDDDC999FBBB9333E')
            data[0x134:0x138] = b'TEST'
            data[0x14d] = (-sum(data[0x134:0x14d]) - 25) & 255
            data[0x14e:0x150] = (sum(data) & 65535).to_bytes(2, 'big')
            path = self.root / 'library/GB-GBC/Imports/Test.gb'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return self.library.scan()['releases'][0]['id']

    def cache_stock(self, data=None):
        data = self.stock if data is None else data
        path = self.root / 'cache/firmware/gb-x7/GBCSYS/GBCOS.BIN'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        (path.parent.parent / 'source.json').write_text(json.dumps({'sha256': hashlib.sha256(data).hexdigest()}))
        return path

    def test_m64_pack_uses_matching_theme_on_startup_without_ultravoid(self):
        release = self.source('m64')
        result = self.library.prepare('m64', 'Default M64', [release], 'zip')
        expected = (ROOT / 'themes/CartShelf-64/dist/CartShelf-64.bgr').read_bytes()
        with zipfile.ZipFile(result['zip_path']) as archive:
            self.assertEqual(archive.read('Themes/CartShelf-64.bgr'), expected)
            self.assertEqual(archive.read('ed64/sysdata/theme.bgr'), expected)
            self.assertIn('Themes/CartShelf-64-background.png', archive.namelist())
            self.assertFalse(any('ultravoid' in p.lower() for p in archive.namelist()))
            manifest = json.loads(archive.read('MANIFEST.json'))
            for row in manifest['files']:
                self.assertEqual(hashlib.sha256(archive.read(row['path'])).hexdigest(), row['sha256'])

    def test_missing_or_changed_m64_theme_aborts_pack(self):
        release = self.source('m64')
        theme = self.code / 'themes/CartShelf-64/dist/CartShelf-64.bgr'
        theme.unlink()
        with self.assertRaisesRegex(RuntimeError, 'CartShelf-64 theme is missing'):
            self.library.prepare('m64', 'Missing Theme', [release])
        self.assertFalse((self.root / 'exports/Missing Theme').exists())
        theme.write_bytes(b'wrong theme')
        with self.assertRaisesRegex(RuntimeError, 'CartShelf-64 theme failed SHA-256'):
            self.library.prepare('m64', 'Changed Theme', [release])
        self.assertFalse((self.root / 'exports/Changed Theme').exists())

    def test_chromatic_pack_applies_exact_palette_without_changing_stock_cache(self):
        release = self.source('chromatic')
        stock = self.cache_stock()
        result = self.library.prepare('chromatic', 'Default Chromatic', [release], 'zip')
        with zipfile.ZipFile(result['zip_path']) as archive:
            self.assertEqual(archive.read('GBCSYS/GBCOS.BIN'), self.themed)
            manifest = json.loads(archive.read('MANIFEST.json'))
            row = next(r for r in manifest['files'] if r['path'] == 'GBCSYS/GBCOS.BIN')
            self.assertEqual(row['sha256'], hashlib.sha256(self.themed).hexdigest())
        self.assertEqual(stock.read_bytes(), self.stock)

    def test_new_os_or_changed_patch_is_rejected_without_publishing_pack(self):
        release = self.source('chromatic')
        self.cache_stock(b'future unknown OS')
        with self.assertRaisesRegex(ValueError, 'verified stock OS 1.06'):
            self.library.prepare('chromatic', 'Unknown OS', [release])
        self.assertFalse((self.root / 'exports/Unknown OS').exists())
        self.cache_stock()
        (self.code / 'themes/CartShelf-GB/dist/CartShelf-GB-X7-v1.06.ips').write_bytes(b'wrong patch')
        with self.assertRaisesRegex(ValueError, 'fixed IPS SHA-256'):
            self.library.prepare('chromatic', 'Changed Patch', [release])
        self.assertFalse((self.root / 'exports/Changed Patch').exists())

    def test_command_line_builder_seeds_the_same_default_startup_theme(self):
        self.source('m64')
        output = self.root / 'cli-output'
        N64.build_stage(self.root / 'N64ROMsPACK', [], self.root / 'homebrew',
                        self.root / 'metadata', self.root / 'custom-metadata',
                        N64.DEFAULT_THEME_DIR, None, output, True)
        expected = (ROOT / 'themes/CartShelf-64/dist/CartShelf-64.bgr').read_bytes()
        self.assertEqual((output / 'ed64/sysdata/theme.bgr').read_bytes(), expected)
        self.assertEqual((output / 'Themes/CartShelf-64.bgr').read_bytes(), expected)
        self.assertFalse(any('ultravoid' in p.name.lower() for p in output.rglob('*')))


if __name__ == '__main__':
    unittest.main()
