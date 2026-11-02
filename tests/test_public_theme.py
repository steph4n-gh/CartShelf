import contextlib
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

import prepare_everdrive64 as n64


ROOT = Path(__file__).resolve().parents[1]
THEME = ROOT / 'themes/CartShelf-64'
spec = importlib.util.spec_from_file_location('cartshelf_public_theme', THEME / 'build_theme.py')
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


class PublicThemeTests(unittest.TestCase):
    def test_generated_atlas_has_only_the_two_required_palette_indices(self):
        atlas = builder.make_font_atlas()
        self.assertEqual((atlas.mode, atlas.size), ('P', (160, 64)))
        self.assertEqual(set(atlas.tobytes()), {0, 1})
        self.assertEqual(atlas.getpalette()[:6], [255, 0, 255, 255, 255, 255])
        with Image.open(THEME / 'source/font10.png') as stored:
            self.assertEqual(stored.tobytes(), atlas.tobytes())

    def test_printable_ascii_has_ink_and_space_is_blank(self):
        atlas = builder.make_font_atlas()
        for code in range(32, 128):
            tile = atlas.crop(((code % 16) * 10, (code // 16) * 8,
                               (code % 16 + 1) * 10, (code // 16 + 1) * 8))
            with self.subTest(code=code):
                self.assertEqual(tile.getbbox() is None, code == 32)

    def test_geometric_popup_edges_meet_at_the_interior_corner(self):
        atlas = builder.make_font_atlas()
        expected = {1: {(x, 7) for x in range(10)},
                    2: {(x, 0) for x in range(10)},
                    4: {(9, y) for y in range(8)},
                    8: {(0, y) for y in range(8)},
                    5: {(9, 7)}, 6: {(9, 0)}, 9: {(0, 7)}, 10: {(0, 0)}}
        for code, points in expected.items():
            actual = {(x, y) for x in range(10) for y in range(8)
                      if atlas.getpixel(((code % 16) * 10 + x, y))}
            self.assertEqual(actual, points)

    def test_pinned_bgr_embeds_the_generated_font_and_indexed_wallpaper(self):
        data = (THEME / 'dist/CartShelf-64.bgr').read_bytes()
        header = struct.unpack('>16I', data[:64])
        self.assertEqual(header[0], 0xED270001)
        self.assertEqual(header[9:], (0,) * 7)
        offset, size = header[3:5]
        self.assertEqual(offset % 16, 0)
        self.assertEqual(data[offset:offset + size], (THEME / 'source/font10.png').read_bytes())
        offset, size = header[5:7]
        with Image.open(io.BytesIO(data[offset:offset + size])) as wallpaper:
            self.assertEqual((wallpaper.mode, wallpaper.size), ('P', (640, 240)))
            self.assertLessEqual(len(wallpaper.getcolors()), 256)
        for name, expected in n64.CARTSHELF_THEME_FILES.items():
            self.assertEqual(hashlib.sha256((THEME / 'dist' / name).read_bytes()).hexdigest(), expected)

    def test_font_provenance_retains_ofl_and_has_no_vendor_asset(self):
        report = json.loads((THEME / 'VERIFICATION.json').read_text())
        font = report['font_provenance']
        self.assertEqual(font['license'], 'OFL-1.1')
        self.assertFalse(font['vendor_font_used'])
        self.assertIn('SIL OPEN FONT LICENSE Version 1.1', (THEME / 'source/OFL.txt').read_text())
        self.assertEqual(font['source_sha256'], hashlib.sha256((THEME / 'source/Silkscreen-Bold.ttf').read_bytes()).hexdigest())
        self.assertFalse((THEME / 'reference').exists())

    def test_staging_rejects_tampering_before_publishing_any_theme_file(self):
        with tempfile.TemporaryDirectory() as folder:
            source, destination = Path(folder) / 'assets', Path(folder) / 'output'
            source.mkdir()
            for name in n64.CARTSHELF_THEME_FILES:
                (source / name).write_bytes((THEME / 'dist' / name).read_bytes())
            (source / 'CartShelf-64-background.png').write_bytes(b'changed')
            with self.assertRaisesRegex(RuntimeError, 'SHA-256'):
                n64.stage_cartshelf_theme(source, destination)
            self.assertFalse(destination.exists())

    def test_legacy_cli_requires_a_data_root(self):
        with patch.object(sys, 'argv', ['prepare_everdrive64.py', '--audit-only']), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as stopped:
                n64.parse_args()
        self.assertEqual(stopped.exception.code, 2)

    def test_legacy_cli_defaults_stay_in_the_explicit_data_root(self):
        with tempfile.TemporaryDirectory() as folder:
            with patch.object(sys, 'argv', ['prepare_everdrive64.py', '--root', folder, '--audit-only']):
                args = n64.parse_args()
            for key in ('rom_dir', 'metadata_repo', 'dat', 'reports', 'output',
                        'custom_metadata', 'homebrew_dir', 'firmware_cache'):
                self.assertTrue(getattr(args, key).is_relative_to(Path(folder).resolve()))
            self.assertIsNone(args.supplemental_rom_dir)
            self.assertEqual(args.theme_dir, n64.DEFAULT_THEME_DIR)


if __name__ == '__main__':
    unittest.main()
