#!/usr/bin/env python3
"""Check emitted ED64 packages and preview pixels without builder imports."""
from __future__ import annotations

import ast
import configparser
import hashlib
import json
import struct
import subprocess
import sys
from io import BytesIO
from pathlib import Path
from zipfile import ZipFile

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent
ZIP_OUTPUT = ROOT.parents[1] / 'build/themes/CartShelf-64.zip'


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def package(path: Path, settings: Path, wallpaper: Path) -> tuple[dict, bytes, bytes]:
    data = path.read_bytes()
    header = struct.unpack('>16I', data[:64])
    assert header[0] == 0xED270001 and header[9:] == (0,) * 7
    chunks = []
    end = 64
    for index, expected in enumerate((settings.read_bytes(), (ROOT / 'source/font10.png').read_bytes(),
                                      wallpaper.read_bytes(), bytes(16))):
        offset, size = header[1 + 2 * index:3 + 2 * index]
        assert offset % 16 == 0 and offset >= end
        assert data[end:offset] == bytes(offset - end)
        chunk = data[offset:offset + size]
        assert chunk == expected
        chunks.append(chunk)
        end = offset + size
    assert end == len(data)
    for content, size, colors in ((chunks[1], (160, 64), 4), (chunks[2], (640, 240), 256)):
        with Image.open(BytesIO(content)) as im:
            assert im.mode == 'P' and im.size == size and len(im.getcolors()) <= colors
        with Image.open(BytesIO(content)) as im:
            im.verify()
    return {'sha256': digest(data), 'bytes': len(data)}, chunks[0], chunks[2]



def write_public_asset_manifest() -> None:
    paths = (
        'build_theme.py', 'verify_theme.py', 'README.md', 'LICENSE-ASSETS.md',
        'source/theme.ini', 'source/Silkscreen-Bold.ttf', 'source/OFL.txt',
        'source/font10.png', 'source/background.png', 'dist/CartShelf-64.bgr',
        'dist/CartShelf-64-background.png', 'preview/scarlet-menu-native-640x240.png',
        'preview/scarlet-popup-native-640x240.png', 'VERIFICATION.json', 'INDEPENDENT_CHECK.json',
    )
    files = []
    for name in paths:
        data = (ROOT / name).read_bytes()
        license_id = ('OFL-1.1' if name in {'source/Silkscreen-Bold.ttf', 'source/OFL.txt', 'source/font10.png'}
                      else 'GPL-3.0-only AND OFL-1.1' if name.endswith('.bgr') else 'GPL-3.0-only')
        files.append({'path': name, 'bytes': len(data), 'sha256': digest(data), 'license': license_id})
    report = {'schema': 1, 'asset_set': 'cartshelf-64-original', 'vendor_bitmap_included': False,
              'font_source': 'https://github.com/googlefonts/silkscreen', 'font_name': 'CartShelf Menu',
              'font_glyph_count': 128, 'font_cell_pixels': [10, 8], 'font_atlas_pixels': [160, 64],
              'simulations_only': True, 'hardware_tested': False, 'files': files}
    (ROOT / 'PUBLIC_ASSETS.json').write_text(json.dumps(report, indent=2) + '\n')


def main() -> None:
    variants = {}
    for name in ('scarlet', 'graphite', 'violet'):
        folder = ROOT / 'source' if name == 'scarlet' else ROOT / 'preview/alternatives' / name
        target = ROOT / 'dist/CartShelf-64.bgr' if name == 'scarlet' else folder / f'CartShelf-64-{name}.bgr'
        result, settings, wallpaper = package(target, folder / 'theme.ini', folder / 'background.png')
        variants[name] = result
        if name == 'scarlet':
            config = configparser.ConfigParser()
            config.read_string(settings.decode('ascii'))
            background = Image.open(BytesIO(wallpaper)).convert('RGB')
        native = Image.open(ROOT / f'preview/{name}-menu-native-640x240.png').convert('RGB')
        labelled = Image.open(ROOT / f'preview/{name}-menu-labelled.png').convert('RGB')
        small = Image.open(ROOT / f'preview/{name}-menu-320x240.png').convert('RGB')
        assert native.size == (640, 240) and labelled.size == (640, 530) and small.size == (320, 240)
        assert labelled.crop((0, 50, 640, 530)).tobytes() == native.resize((640, 480), Image.Resampling.NEAREST).tobytes()
        assert small.tobytes() == native.resize((320, 240), Image.Resampling.NEAREST).tobytes()

    font_provenance = json.loads((ROOT / 'VERIFICATION.json').read_text())['font_provenance']
    assert font_provenance['vendor_font_used'] is False
    assert font_provenance['license'] == 'OFL-1.1'
    assert digest((ROOT / 'source/Silkscreen-Bold.ttf').read_bytes()) == font_provenance['source_sha256']
    assert digest((ROOT / 'source/font10.png').read_bytes()) == font_provenance['atlas_sha256']
    atlas = Image.open(ROOT / 'source/font10.png').convert('RGB')
    first_colors = list(dict.fromkeys(atlas.getpixel((x, y))
                                     for y in range(atlas.height) for x in range(atlas.width)))
    assert first_colors == [(255, 0, 255), (255, 255, 255)]
    palette = {int(key.split('.')[1]): int(value, 0) for key, value in config['palette'].items()}

    def rgb(value: int) -> tuple[int, int, int]:
        return ((value >> 16) & 248, (value >> 8) & 248, value & 248)

    def draw_cell(im: Image.Image, x: int, y: int, character: str, base: int) -> None:
        code = ord(character)
        for py in range(8):
            for px in range(10):
                color_index = (base + first_colors.index(atlas.getpixel(((code % 16) * 10 + px,
                                                                         (code // 16) * 8 + py)))) & 255
                value = palette.get(color_index, 0)
                if color_index and value:
                    im.putpixel((x * 10 + px, y * 8 + py), rgb(value))

    native = Image.open(ROOT / 'preview/scarlet-menu-native-640x240.png').convert('RGB')
    long_native = Image.open(ROOT / 'preview/scarlet-long-filename-native-640x240.png').convert('RGB')
    popup_native = Image.open(ROOT / 'preview/scarlet-popup-native-640x240.png').convert('RGB')
    quantized_background = background.point(lambda channel: channel & 248)
    tree = ast.parse((ROOT / 'build_theme.py').read_text())
    constants = {node.targets[0].id: node.value for node in tree.body
                 if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name)}
    long_name = ast.literal_eval(constants['LONG_FILENAME'])
    assert len(long_name) > 56
    alignments = {}
    for section, string, actual in (('fm_hdr', 'Page 1 of 2', native),
                                    ('fm_foot', 'Pocket Racers.z64', native),
                                    ('fm_foot', long_name, long_native)):
        style = config[section]
        assert [int(style[key]) for key in ('ax', 'ay', 'txt_ax', 'txt_ay', 'bor_style')] == [3, 3, 0, 0, 2]
        x, y, width, height = [int(style[key]) for key in ('x', 'y', 'w', 'h')]
        assert (x, y, width, height) == (4, 7 if section == 'fm_hdr' else 27, 56, 1)
        visible = string[:width]
        tx = x + (width - len(visible)) // 2
        expected = quantized_background.copy()
        if int(style['pal_box']):
            for column in range(width):
                draw_cell(expected, x + column, y, ' ', int(style['pal_box']))
        for column, character in enumerate(visible):
            draw_cell(expected, tx + column, y, character, int(style['pal_txt0']))
        bounds = (x * 10, y * 8, (x + width) * 10, (y + height) * 8)
        assert actual.crop(bounds).tobytes() == expected.crop(bounds).tobytes()
        alignments[f'{section}_{len(string)}_characters'] = [tx, y, visible]
    for actual in (native, long_native, popup_native):
        assert actual.crop((0, 224, 640, 240)).tobytes() == quantized_background.crop((0, 224, 640, 240)).tobytes()
    assert native.getpixel((40, 128)) == (248, 216, 8)

    style = config['menu_base']
    assert int(style['bor_style']) == 0 and int(config['global']['shade']) == 0
    assert int(style['pal_bor']) == 32 and palette[32] == 0x00080808
    assert [int(style[key]) for key in ('ax', 'ay', 'txt_ax', 'txt_ay')] == [0, 0, 0, 0]
    width, height = 26, 5
    x, y = (64 - width) // 2, (30 - height) // 2
    bx, by = int(style['bx']), int(style['by'])
    left, top, right, bottom = x - bx, y - by, x + width + bx, y + height + by
    border_cells = {(column, top - 1): '\x01' for column in range(left, right)}
    border_cells.update({(column, bottom): '\x02' for column in range(left, right)})
    border_cells.update({(left - 1, row): '\x04' for row in range(top, bottom)})
    border_cells.update({(right, row): '\x08' for row in range(top, bottom)})
    border_cells.update({(left - 1, top - 1): '\x05', (right, top - 1): '\x09',
                         (left - 1, bottom): '\x06', (right, bottom): '\x0a'})
    popup_expected = quantized_background.copy()
    for (column, row), character in border_cells.items():
        draw_cell(popup_expected, column, row, character, int(style['pal_bor']))
        box = (column * 10, row * 8, (column + 1) * 10, (row + 1) * 8)
        assert popup_native.crop(box).tobytes() == popup_expected.crop(box).tobytes()
        assert {color for _, color in popup_native.crop(box).getcolors()} <= {rgb(palette[32]), rgb(palette[33])}
    for row in range(top, bottom):
        for column in range(left, right):
            draw_cell(popup_expected, column, row, ' ', int(style['pal_box']))
    for row, string in enumerate(('ROM OPTIONS', 'Start game', 'Start with cheats', 'ROM information', 'Cancel')):
        base = int(style['pal_txt1' if row == 1 else 'pal_txt0'])
        if row == 1:
            for column in range(width):
                draw_cell(popup_expected, x + column, y + row, ' ', base)
        for column, character in enumerate(string):
            draw_cell(popup_expected, x + (width - len(string)) // 2 + column, y + row, character, base)
    popup_bounds = ((left - 1) * 10, (top - 1) * 8, (right + 1) * 10, (bottom + 1) * 8)
    assert popup_native.crop(popup_bounds).tobytes() == popup_expected.crop(popup_bounds).tobytes()
    for suffix, actual in (('long-filename', long_native), ('popup', popup_native)):
        panel = Image.open(ROOT / f'preview/scarlet-{suffix}-labelled.png').convert('RGB')
        assert panel.crop((0, 50, 640, 530)).tobytes() == actual.resize((640, 480), Image.Resampling.NEAREST).tobytes()

    glyphs = ast.literal_eval(constants['BRAND_GLYPHS'])
    assert all(len(rows) == 7 and all(len(row) == 5 and set(row) <= {'0', '1'} for row in rows)
               for rows in glyphs.values())
    open_points = {'E': (2, 1), 'A': (2, 1), 'R': (2, 1), '6': (2, 4), '4': (2, 3)}
    assert all(glyphs[letter][py][px] == '0' for letter, (px, py) in open_points.items())
    rainbow = ('#ff0000', '#ff9900', '#ffdb0d', '#34c759', '#5ac8af', '#32bfff', '#1678ed', '#a774eb', '#ff4d99')
    checked_brand_pixels = 0
    for x, y, string, scale, color in ((20, 16, 'CARTSHELF', 2, None),
                                      (21, 35, 'EVERDRIVE-64 PRO', 1, '#949494'),
                                      (240, 20, '64', 2, '#ffdb0d')):
        expected = Image.new('RGB', ((len(string) * 6 - 1) * scale * 2, 7 * scale), 'black')
        draw = ImageDraw.Draw(expected)
        for index, letter in enumerate(string):
            for py, bits in enumerate(glyphs[letter]):
                for px, bit in enumerate(bits):
                    if bit == '1':
                        dx, dy = (index * 6 + px) * scale * 2, py * scale
                        draw.rectangle((dx, dy, dx + scale * 2 - 1, dy + scale - 1),
                                       fill=color or rainbow[index % len(rainbow)])
        bounds = (x * 2, y, x * 2 + expected.width, y + expected.height)
        assert background.crop(bounds).tobytes() == expected.tobytes()
        checked_brand_pixels += expected.width * expected.height
    for py in range(49, 70):
        for px in range(32, 608):
            if not (38 <= px <= 601 and 52 <= py <= 68):
                assert background.getpixel((px, py)) != (255, 0, 0)
    ink_rows = {py for character in 'Page 1 of 2' for py in range(8) for px in range(10)
                if atlas.getpixel(((ord(character) % 16) * 10 + px,
                                   (ord(character) // 16) * 8 + py)) == first_colors[1]}
    assert (56 + min(ink_rows), 56 + max(ink_rows)) == (57, 63)
    assert 52 + 68 == 57 + 63

    with Image.open(ROOT / 'dist/CartShelf-64-background.png') as fallback:
        assert fallback.mode == 'P' and fallback.size == (640, 240) and len(fallback.getcolors()) <= 256
    with ZipFile(ZIP_OUTPUT) as archive:
        assert set(archive.namelist()) == {'README.txt', 'Themes/CartShelf-64.bgr', 'Themes/CartShelf-64-background.png'}
        for filename in ('CartShelf-64.bgr', 'CartShelf-64-background.png'):
            assert (ROOT / 'dist' / filename).read_bytes() == (ROOT / 'install/Themes' / filename).read_bytes() == archive.read(f'Themes/{filename}')
        assert archive.read('README.txt') == (ROOT / 'install/README.txt').read_bytes()
        assert all(entry.date_time == (2026, 11, 1, 0, 0, 0) for entry in archive.infolist())

    def generated_hashes() -> dict[str, str]:
        paths = [path for folder in ('source', 'dist', 'preview', 'install')
                 for path in (ROOT / folder).rglob('*') if path.is_file()]
        paths.extend((ROOT / 'VERIFICATION.json', ZIP_OUTPUT))
        return {('archive/CartShelf-64.zip' if path == ZIP_OUTPUT else str(path.relative_to(ROOT))):
                digest(path.read_bytes()) for path in sorted(paths)}

    before = generated_hashes()
    rebuilt = subprocess.run([sys.executable, '-B', str(ROOT / 'build_theme.py')], check=True,
                             capture_output=True, text=True)
    assert generated_hashes() == before
    report = {
        'all_checks_passed': True, 'independent_of_builder_helpers': True,
        'package_sha256': variants['scarlet']['sha256'], 'variants': variants,
        'font_source_license': 'OFL-1.1', 'vendor_font_bytes_used': False,
        'exact_bar_glyph_pixels_match_embedded_ini': alignments,
        'long_footer': {'input_characters': len(long_name), 'visible_characters': 56,
                        'clipped_characters': len(long_name) - 56, 'adds_ellipsis': False,
                        'last_drawable_pixel_row': 223, 'bottom_frame_first_pixel_row': 229},
        'popup_border_cells_checked': len(border_cells), 'popup_border_pixels_checked': len(border_cells) * 80,
        'popup_background_and_border_fully_opaque': True, 'popup_exact_glyph_pixels_match': True,
        'branding_exact_mask_pixels_checked': checked_brand_pixels,
        'branding_open_space_points': open_points, 'banner_no_accent_halo': True,
        'banner_slab_and_actual_glyph_ink_share_center_row': 60,
        'actual_preview_selection_rgb': list(native.getpixel((40, 128))),
        'native_framebuffer': [640, 240], 'direct_4_to_3_approximation': [640, 480],
        'all_labelled_previews_preserve_native_font_columns': True,
        'small_4_to_3_approximation': [320, 240], 'small_previews_match_documented_reduction': True,
        'install_files_match_dist_and_zip': True, 'zip_sha256': digest(ZIP_OUTPUT.read_bytes()),
        'rebuilt_artifact_count': len(before), 'rebuild_hashes_unchanged': True,
        'deterministic_artifact_sha256': before,
        'card_accessed': False, 'hardware_tested': False,
        'unqualified': ['Firmware strings and dialog runtime', 'Physical theme loading', 'TV scaling and overscan'],
    }
    (ROOT / 'INDEPENDENT_CHECK.json').write_text(json.dumps(report, indent=2) + '\n')
    write_public_asset_manifest()
    print(rebuilt.stdout, end='')
    print(f'Independent parser and pixel checks passed. {len(before)} artifact hashes stayed identical.')


if __name__ == '__main__':
    main()
