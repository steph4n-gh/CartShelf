#!/usr/bin/env python3
"""Build the original CartShelf ED64 theme and labelled, authored simulations."""
from __future__ import annotations

import configparser
import hashlib
import json
import struct
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent
ZIP_OUTPUT = ROOT.parents[1] / 'build/themes/CartShelf-64.zip'
SOURCE = ROOT / 'source'
DIST = ROOT / 'dist'
PREVIEW = ROOT / 'preview'
FONT = SOURCE / 'font10.png'
CONFIG = SOURCE / 'theme.ini'
BRAND_FONT = SOURCE / 'Silkscreen-Bold.ttf'
COLORS = {
    'black': '#000000', 'panel': '#0b0b0b', 'edge': '#3d3d3d',
    'light': '#7a7a7a', 'shadow': '#1f1f1f', 'scarlet': '#ff0000',
    'yellow': '#ffdb0d', 'purple': '#9470e0', 'white': '#ffffff',
}
VARIANTS = (
    ('scarlet', 'SCARLET / SELECTED', '#ff0000'),
    ('graphite', 'GRAPHITE / ALTERNATIVE', '#282828'),
    ('violet', 'VIOLET / ALTERNATIVE', '#6040a0'),
)
ROWS = (
    ('..', '[DIR]', 'directory'),
    ('Demo Adventures', '[DIR]', 'directory'),
    ('Alpine Dash.z64', ' 16MB', 'file'),
    ('Canyon Run.z64', ' 16MB', 'file'),
    ('Cloud Garden.z64', ' 12MB', 'file'),
    ('Comet Chase.z64', ' 16MB', 'file'),
    ('Crystal Harbor.z64', ' 12MB', 'file'),
    ('Pocket Racers.z64', ' 12MB', 'selected'),
    ('Moonlight Quest.z64', ' 40MB', 'file'),
    ('Orbit Hopper.z64', ' 32MB', 'file'),
    ('Pixel Patrol.z64', ' 12MB', 'file'),
    ('River Workshop.z64', '  8MB', 'file'),
    ('Rocket Rally.z64', ' 16MB', 'file'),
    ('Star Orchard.z64', ' 32MB', 'file'),
    ('Sunset Circuit.z64', '  8MB', 'file'),
    ('Tiny Planet.z64', '  8MB', 'file'),
    ('Windmill Valley.z64', ' 16MB', 'file'),
)
LONG_FILENAME = 'CartShelf Demo - A Very Long Adventure Title - Extended Edition (v1.0).z64'
BRAND_GLYPHS = {
    'A': ('01110', '11011', '11011', '11111', '11011', '11011', '11011'),
    'C': ('01111', '11000', '11000', '11000', '11000', '11000', '01111'),
    'D': ('11110', '11011', '11011', '11011', '11011', '11011', '11110'),
    'E': ('11111', '11000', '11000', '11110', '11000', '11000', '11111'),
    'F': ('11111', '11000', '11000', '11110', '11000', '11000', '11000'),
    'H': ('11011', '11011', '11011', '11111', '11011', '11011', '11011'),
    'I': ('11111', '00100', '00100', '00100', '00100', '00100', '11111'),
    'L': ('11000', '11000', '11000', '11000', '11000', '11000', '11111'),
    'O': ('01110', '11011', '11011', '11011', '11011', '11011', '01110'),
    'P': ('11110', '11011', '11011', '11110', '11000', '11000', '11000'),
    'R': ('11110', '11011', '11011', '11110', '11100', '11010', '11011'),
    'S': ('01111', '11000', '11000', '01110', '00011', '00011', '11110'),
    'T': ('11111', '00100', '00100', '00100', '00100', '00100', '00100'),
    'V': ('11011', '11011', '11011', '11011', '11011', '01010', '00100'),
    '4': ('00010', '00110', '01010', '11010', '11111', '00010', '00010'),
    '6': ('01110', '11000', '11000', '11110', '11011', '11011', '01110'),
    '-': ('00000', '00000', '00000', '11111', '00000', '00000', '00000'),
    ' ': ('00000', '00000', '00000', '00000', '00000', '00000', '00000'),
}
BRAND_RUNS = (
    ((20, 16), 'CARTSHELF', 2, None),
    ((21, 35), 'EVERDRIVE-64 PRO', 1, '#949494'),
    ((240, 20), '64', 2, COLORS['yellow']),
)


def make_font_atlas() -> Image.Image:
    """Use OFL Silkscreen outlines and original geometric menu controls."""
    font = ImageFont.truetype(str(BRAND_FONT), 10)
    atlas = Image.new('P', (160, 64), 0)
    atlas.putpalette([255, 0, 255, 255, 255, 255] + [0, 0, 0] * 254)
    for code in range(1, 128):
        tile = Image.new('1', (10, 8))
        draw = ImageDraw.Draw(tile)
        if code < 16:
            if code in (5, 6, 9, 10):
                draw.point((9 if code & 4 else 0, 7 if code & 1 else 0), fill=1)
            else:
                if code & 1:
                    draw.line((0, 7, 9, 7), fill=1)
                if code & 2:
                    draw.line((0, 0, 9, 0), fill=1)
                if code & 4:
                    draw.line((9, 0, 9, 7), fill=1)
                if code & 8:
                    draw.line((0, 0, 0, 7), fill=1)
        elif code < 32:
            if code in (16, 17):
                points = ((2, 5), (5, 2), (8, 5)) if code == 16 else ((2, 2), (5, 5), (8, 2))
                draw.line(points, fill=1, width=1)
            elif code in (18, 19):
                points = ((6, 1), (3, 4), (6, 6)) if code == 18 else ((3, 1), (6, 4), (3, 6))
                draw.line(points, fill=1, width=1)
            else:
                draw.rectangle((1, 1, 8, 6), outline=1)
                if code & 1:
                    draw.line((3, 4, 4, 5, 7, 2), fill=1)
        elif code != 32:
            character = chr(code) if code != 127 else '?'
            box = font.getbbox(character)
            glyph = Image.new('1', (max(1, box[2] - box[0]), max(1, box[3] - box[1])))
            ImageDraw.Draw(glyph).text((-box[0], -box[1]), character, font=font, fill=1)
            if glyph.width > 10 or glyph.height > 7:
                scale = min(10 / glyph.width, 7 / glyph.height)
                glyph = glyph.resize((max(1, round(glyph.width * scale)), max(1, round(glyph.height * scale))),
                                     Image.Resampling.NEAREST)
            y = 8 - glyph.height if character in '.,_' else 1
            tile.paste(glyph, ((10 - glyph.width) // 2, y))
        atlas.paste(1, ((code % 16) * 10, (code // 16) * 8,
                        (code % 16 + 1) * 10, (code // 16 + 1) * 8), tile)
    return atlas


def bevel(draw: ImageDraw.ImageDraw, box: tuple[int, int, int, int], fill: str,
          accent: str | None = None) -> None:
    x0, y0, x1, y1 = box
    draw.rectangle(box, fill=fill, outline=accent or COLORS['edge'])
    draw.line((x0 + 2, y0 + 2, x1 - 2, y0 + 2), fill=accent or COLORS['light'])
    draw.line((x0 + 2, y0 + 2, x0 + 2, y1 - 2), fill=accent or '#666666')
    draw.line((x0 + 2, y1 - 2, x1 - 2, y1 - 2), fill=COLORS['shadow'])
    draw.line((x1 - 2, y0 + 2, x1 - 2, y1 - 2), fill=COLORS['shadow'])


def brand_text(image: Image.Image, xy: tuple[int, int], text: str, scale: int,
               color: str | None = None) -> None:
    rainbow = ('#ff0000', '#ff9900', '#ffdb0d', '#34c759', '#5ac8af',
               '#32bfff', '#1678ed', '#a774eb', '#ff4d99')
    x, y = xy
    draw = ImageDraw.Draw(image)
    for index, letter in enumerate(text):
        for row, bits in enumerate(BRAND_GLYPHS[letter]):
            for column, bit in enumerate(bits):
                if bit == '1':
                    px, py = x + column * scale, y + row * scale
                    draw.rectangle((px, py, px + scale - 1, py + scale - 1),
                                   fill=color or rainbow[index % len(rainbow)])
        x += 6 * scale


def console(draw: ImageDraw.ImageDraw, x: int, y: int, tint: str) -> None:
    boxes = (
        (3, 8, 18, 10, tint), (5, 5, 14, 3, tint),
        (8, 2, 8, 4, '#b3b3b3'), (10, 1, 4, 2, '#b3b3b3'),
        (8, 7, 8, 2, '#000000'), (5, 14, 3, 2, '#000000'),
        (10, 14, 3, 2, '#000000'), (15, 14, 3, 2, '#000000'),
        (4, 18, 3, 2, '#4d4d4d'), (17, 18, 3, 2, '#4d4d4d'),
    )
    for bx, by, width, height, color in boxes:
        draw.rectangle((x + bx, y + by, x + bx + width - 1, y + by + height - 1), fill=color)


def artwork(accent: str, fallback: bool = False) -> Image.Image:
    image = Image.new('RGB', (320, 240), COLORS['black'])
    draw = ImageDraw.Draw(image)
    bevel(draw, (8, 8, 311, 231), '#000000')
    for x, y in ((12, 35), (306, 91), (12, 168), (306, 203)):
        draw.line((x - 2, y, x + 2, y), fill='#181818')
        draw.line((x, y - 2, x, y + 2), fill='#181818')
    if fallback:
        bevel(draw, (16, 32, 303, 201), COLORS['panel'])
        draw.line((17, 12, 302, 12), fill=accent)
        for i, color in enumerate(('#ff0000', '#ff9900', '#ffdb0d', '#34c759', '#5ac8af', '#32bfff', '#1678ed', '#a774eb', '#ff4d99')):
            draw.rectangle((12, 46 + i * 16, 13, 53 + i * 16), fill=color)
        return image
    for xy, text, scale, color in BRAND_RUNS:
        brand_text(image, xy, text, scale, color)
    console(draw, 267, 16, COLORS['purple'])
    draw.rectangle((16, 49, 303, 69), fill='#080808', outline='#1f1f1f')
    draw.line((17, 68, 302, 68), fill='#3d3d3d')
    draw.rectangle((19, 52, 300, 68), fill=accent)
    bevel(draw, (16, 70, 303, 211), COLORS['panel'])
    draw.line((20, 214, 299, 214), fill='#3d3d3d')
    return image


def indexed_native(image: Image.Image) -> Image.Image:
    native = image.resize((640, 240), Image.Resampling.NEAREST)
    return native.quantize(colors=256, method=Image.Quantize.MEDIANCUT,
                           dither=Image.Dither.NONE)


def ini(accent: str = '#ff0000') -> configparser.ConfigParser:
    config = configparser.ConfigParser()
    config.read(CONFIG)
    config['palette']['pal.4'] = '0x00' + accent[1:].upper()
    return config


def ini_bytes(config: configparser.ConfigParser) -> bytes:
    lines = []
    for section in config.sections():
        lines.append(f'[{section}]')
        lines.extend(f'{key}={value}' for key, value in config[section].items())
        lines.append('')
    return ('\n'.join(lines) + '\n').encode('ascii')


def pack_theme(settings: bytes, background: bytes) -> bytes:
    chunks = (settings, FONT.read_bytes(), background, bytes(16))
    offsets = []
    cursor = 64
    for chunk in chunks:
        cursor = (cursor + 15) & ~15
        offsets.extend((cursor, len(chunk)))
        cursor += len(chunk)
    data = bytearray(cursor)
    data[:64] = struct.pack('>16I', 0xED270001, *offsets, *([0] * 7))
    for index, chunk in enumerate(chunks):
        offset = offsets[index * 2]
        data[offset:offset + len(chunk)] = chunk
    return bytes(data)


def font_indices() -> tuple[Image.Image, list[tuple[int, int, int]]]:
    atlas = Image.open(FONT).convert('RGB')
    pixels = [atlas.getpixel((x, y)) for y in range(atlas.height) for x in range(atlas.width)]
    colors = list(dict.fromkeys(pixels))
    assert len(colors) <= 4
    indexed = Image.new('P', atlas.size)
    indexed.putdata([colors.index(color) for color in pixels])
    return indexed, colors


def get_pos(explicit: int, length: int, start: int, span: int, align: int) -> int:
    length = min(length, span)
    return (start + (span - length) // 2, start, start + span - length, explicit)[align]


def window(config: configparser.ConfigParser, section: str,
           size: tuple[int, int] | None = None) -> tuple[int, int, int, int]:
    style = config[section]
    bx = int(config['global']['bx']) + int(style['bx'])
    by = int(config['global']['by']) + int(style['by'])
    if int(style['bor_style']) != 2:
        bx += 1
        by += 1
    width, height = size or (int(style.get('w', 0)), int(style.get('h', 0)))
    w = min(width or 64 - 2 * bx, 64 - 2 * bx)
    h = min(height or 30 - 2 * by, 30 - 2 * by)
    position = []
    for key, align_key, length, maximum, border in (
        ('x', 'ax', w, 64, bx), ('y', 'ay', h, 30, by),
    ):
        align = int(style[align_key])
        p = get_pos(int(style.get(key, 0)), length, 0, maximum, align)
        if align == 1:
            p += border
        if align == 2:
            p -= border
        position.append(max(p, border - int(config['global']['b' + key])))
    return (*position, w, h)


def text_layout(config: configparser.ConfigParser, section: str, string: str,
                size: tuple[int, int] | None = None) -> list[tuple[int, int, str]]:
    x, y, width, height = window(config, section, size)
    lines = []
    for paragraph in string.split('\n'):
        lines.extend([paragraph[i:i + width] for i in range(0, len(paragraph), width)] or [''])
    lines = lines[:height]
    style = config[section]
    vertical = int(style['txt_ay'])
    if vertical in (0, 2):
        y = get_pos(0, len(lines), y, height, vertical)
    return [(get_pos(0, len(line), x, width, int(style['txt_ax'])), y + row, line)
            for row, line in enumerate(lines)]


def preview(background: Image.Image, config: configparser.ConfigParser,
            selected_filename: str | None = None, popup: bool = False) -> Image.Image:
    image = background.convert('RGB')
    atlas, _ = font_indices()
    palette = {int(key.split('.')[1]): int(value, 0) for key, value in config['palette'].items()}
    cells: dict[tuple[int, int], tuple[str, int]] = {}

    def text(x: int, y: int, string: str, base: int) -> None:
        for character in string:
            assert 0 < ord(character) < 128 and 0 <= x < 64 and 0 <= y < 30
            cells[x, y] = character, base
            x += 1

    hx, hy, hw, hh = window(config, 'fm_hdr')
    header = 'Page 1 of 2'
    text(hx, hy, ' ' * hw, int(config['fm_hdr']['pal_box']))
    for tx, ty, line in text_layout(config, 'fm_hdr', header):
        text(tx, ty, line, int(config['fm_hdr']['pal_txt0']))
    x, y, width, height = window(config, 'fm_list')
    assert len(ROWS) == height
    for index, (name, size, kind) in enumerate(ROWS):
        assert len(name) <= width - 5, (name, width)
        base = int(config['fm_list'][{'file': 'pal_txt0', 'directory': 'pal_txt2', 'selected': 'pal_txt1'}[kind]])
        displayed_name = selected_filename if kind == 'selected' and selected_filename else name
        text(x, y + index, displayed_name[:width - 5].ljust(width - 5) + size, base)
    selected = selected_filename or next(name for name, _, kind in ROWS if kind == 'selected')
    for tx, ty, line in text_layout(config, 'fm_foot', selected):
        text(tx, ty, line, int(config['fm_foot']['pal_txt0']))
    if popup:
        style = config['menu_base']
        x, y, width, height = window(config, 'menu_base', (26, 5))
        bx, by = int(style['bx']), int(style['by'])
        left, top = x - bx, y - by
        right, bottom = x + width + bx, y + height + by
        border = int(style['pal_bor'])
        assert int(style['bor_style']) == 0 and int(config['global']['shade']) == 0
        text(left, top - 1, '\x01' * (right - left), border)
        text(left, bottom, '\x02' * (right - left), border)
        for row in range(top, bottom):
            text(left - 1, row, '\x04', border)
            text(right, row, '\x08', border)
            text(left, row, ' ' * (right - left), int(style['pal_box']))
        for px, py, character in ((left - 1, top - 1, '\x05'), (right, top - 1, '\x09'),
                                  (left - 1, bottom, '\x06'), (right, bottom, '\x0a')):
            text(px, py, character, border)
        options = 'ROM OPTIONS\nStart game\nStart with cheats\nROM information\nCancel'
        for row, (tx, ty, line) in enumerate(text_layout(config, 'menu_base', options, (26, 5))):
            base = int(style['pal_txt1' if row == 1 else 'pal_txt0'])
            if row == 1 and int(style['sel_style']) == 0:
                text(x, ty, ' ' * width, base)
            text(tx, ty, line, base)
    for (x, y), (character, base) in cells.items():
        code = ord(character)
        tile = atlas.crop(((code % 16) * 10, (code // 16) * 8,
                           (code % 16 + 1) * 10, (code // 16 + 1) * 8))
        for ty in range(8):
            for tx in range(10):
                index = (base + tile.getpixel((tx, ty))) & 255
                color = palette.get(index, 0)
                if index and color:
                    image.putpixel((x * 10 + tx, y * 8 + ty),
                                   ((color >> 16) & 255, (color >> 8) & 255, color & 255))
    image = image.point(lambda channel: channel & 0xf8)
    return image


def save_png(image: Image.Image, path: Path) -> bytes:
    image.save(path, format='PNG', optimize=False, compress_level=9)
    return path.read_bytes()


def verify(theme_path: Path, config: bytes, background: bytes) -> dict:
    data = theme_path.read_bytes()
    header = struct.unpack('>16I', data[:64])
    assert header[0] == 0xED270001
    assert header[9:] == (0,) * 7
    assert header[7] + header[8] == len(data)
    previous_end = 64
    sections = {}
    for i, (name, expected) in enumerate((('settings', config), ('font', FONT.read_bytes()),
                                         ('background', background), ('tail', bytes(16)))):
        offset, size = header[1 + i * 2:3 + i * 2]
        assert offset % 16 == 0 and offset >= previous_end
        assert data[previous_end:offset] == bytes(offset - previous_end)
        assert data[offset:offset + size] == expected
        sections[name] = {'offset': offset, 'length': size, 'sha256': hashlib.sha256(expected).hexdigest()}
        previous_end = offset + size
    for name, expected_size in (('font', (160, 64)), ('background', (640, 240))):
        section = sections[name]
        with Image.open(BytesIO(data[section['offset']:section['offset'] + section['length']])) as image:
            assert image.size == expected_size and image.mode == 'P'
            assert len(image.getcolors()) <= (4 if name == 'font' else 256)
        with Image.open(BytesIO(data[section['offset']:section['offset'] + section['length']])) as image:
            image.verify()
    assert font_indices()[1] == [(255, 0, 255), (255, 255, 255)]
    return {'sha256': hashlib.sha256(data).hexdigest(), 'bytes': len(data), 'sections': sections}


def main() -> None:
    for directory in (SOURCE, DIST, PREVIEW):
        directory.mkdir(exist_ok=True)
    save_png(make_font_atlas(), FONT)
    results = {}
    panels = []
    label_font = ImageFont.truetype(str(BRAND_FONT), 16)
    for name, label, accent in VARIANTS:
        config = ini(accent)
        config_data = ini_bytes(config)
        background = indexed_native(artwork(accent))
        folder = SOURCE if name == 'scarlet' else PREVIEW / 'alternatives' / name
        folder.mkdir(parents=True, exist_ok=True)
        background_data = save_png(background, folder / 'background.png')
        (folder / 'theme.ini').write_bytes(config_data)
        target = DIST / 'CartShelf-64.bgr' if name == 'scarlet' else folder / f'CartShelf-64-{name}.bgr'
        target.write_bytes(pack_theme(config_data, background_data))
        results[name] = verify(target, config_data, background_data)
        native = preview(background, config)
        save_png(native, PREVIEW / f'{name}-menu-native-640x240.png')
        physical = native.resize((320, 240), Image.Resampling.NEAREST)
        save_png(physical, PREVIEW / f'{name}-menu-320x240.png')
        panel = Image.new('RGB', (640, 530), '#161616')
        panel.paste(native.resize((640, 480), Image.Resampling.NEAREST), (0, 50))
        draw = ImageDraw.Draw(panel)
        draw.text((16, 10), label, font=label_font, fill='white')
        draw.text((16, 29), 'SIMULATION / NOT A CONSOLE CAPTURE', font=label_font, fill='#a8a8a8')
        save_png(panel, PREVIEW / f'{name}-menu-labelled.png')
        panels.append(panel)
        if name == 'scarlet':
            assert int(config['fm_hdr']['txt_ax']) == int(config['fm_foot']['txt_ax']) == 0
            assert window(config, 'fm_foot') == (4, 27, 56, 1)
            assert text_layout(config, 'fm_hdr', 'Page 1 of 2') == [(26, 7, 'Page 1 of 2')]
            assert text_layout(config, 'fm_foot', 'Pocket Racers.z64') == [(23, 27, 'Pocket Racers.z64')]
            assert text_layout(config, 'fm_foot', LONG_FILENAME) == [(4, 27, LONG_FILENAME[:56])]
            assert int(config['palette']['pal.32'], 0) == 0x00080808
            for suffix, filename, show_popup, caption in (
                ('long-filename', LONG_FILENAME, False, 'LONG FILENAME / FOOTER CLIP CHECK'),
                ('popup', None, True, 'CONTEXT MENU / ILLUSTRATIVE STRINGS'),
            ):
                contextual = preview(background, config, filename, show_popup)
                save_png(contextual, PREVIEW / f'scarlet-{suffix}-native-640x240.png')
                contextual_panel = Image.new('RGB', (640, 530), '#161616')
                contextual_panel.paste(contextual.resize((640, 480), Image.Resampling.NEAREST), (0, 50))
                contextual_draw = ImageDraw.Draw(contextual_panel)
                contextual_draw.text((16, 10), caption, font=label_font, fill='white')
                contextual_draw.text((16, 29), 'SIMULATION / NOT A CONSOLE CAPTURE', font=label_font, fill='#a8a8a8')
                save_png(contextual_panel, PREVIEW / f'scarlet-{suffix}-labelled.png')
    comparison = Image.new('RGB', (640 * len(panels), 530), 'black')
    for i, panel in enumerate(panels):
        comparison.paste(panel, (i * 640, 0))
    save_png(comparison, PREVIEW / 'CartShelf-64-options.png')
    fallback = indexed_native(artwork('#ff0000', fallback=True))
    save_png(fallback, DIST / 'CartShelf-64-background.png')
    install = ROOT / 'install' / 'Themes'
    install.mkdir(parents=True, exist_ok=True)
    staged_hashes = {}
    for name in ('CartShelf-64.bgr', 'CartShelf-64-background.png'):
        content = (DIST / name).read_bytes()
        (install / name).write_bytes(content)
        staged_hashes[f'Themes/{name}'] = hashlib.sha256(content).hexdigest()
    instructions = (
        'CartShelf-64 for EverDrive-64 PRO\n\n'
        'This archive is staged off-card. No card has been modified.\n'
        'After the card is reliable, copy the Themes folder to its root.\n'
        'On the EverDrive, launch Themes/CartShelf-64.bgr for the complete theme.\n'
        'CartShelf-64-background.png is a separate wallpaper-only alternative.\n'
        'Do not launch the PNG after the BGR to keep the complete appearance.\n'
        'Hardware compatibility and display overscan remain untested.\n'
    )
    (ROOT / 'install' / 'README.txt').write_text(instructions)
    ZIP_OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(ZIP_OUTPUT, 'w', compression=ZIP_DEFLATED, compresslevel=9) as archive:
        for path in sorted((ROOT / 'install').rglob('*')):
            if path.is_file():
                entry = ZipInfo(str(path.relative_to(ROOT / 'install')), (2026, 11, 1, 0, 0, 0))
                entry.compress_type = ZIP_DEFLATED
                entry.external_attr = 0o100644 << 16
                archive.writestr(entry, path.read_bytes(), compress_type=ZIP_DEFLATED, compresslevel=9)
    source_hashes = {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                     for path in sorted(SOURCE.iterdir()) if path.is_file()}
    report = {
        'theme': 'CartShelf-64', 'chosen_variant': 'scarlet', 'format_checks_passed': True,
        'hardware_tested': False, 'card_written': False,
        'format_reference': 'https://krikzz.com/pub/support/everdrive-64/pro-series/',
        'native_framebuffer': [640, 240], 'labelled_aspect_preview': [640, 480],
        'small_aspect_preview': [320, 240],
        'body_glyph_pixels': [10, 8], 'glyph_grid': [64, 30],
        'font_rgb_first_seen': font_indices()[1],
        'menu_cells': {key: window(ini(), key) for key in ('fm_hdr', 'fm_list', 'fm_foot')},
        'text_alignment': {
            'vendor_get_pos': {'0': 'center', '1': 'start', '2': 'end', '3': 'explicit'},
            'header_runs': text_layout(ini(), 'fm_hdr', 'Page 1 of 2'),
            'footer_runs': text_layout(ini(), 'fm_foot', 'Pocket Racers.z64'),
            'long_footer_runs': text_layout(ini(), 'fm_foot', LONG_FILENAME),
            'long_footer_input': LONG_FILENAME,
            'long_footer_input_characters': len(LONG_FILENAME),
            'long_footer_visible_characters': 56,
            'footer_last_pixel_row': 223,
            'bottom_frame_first_pixel_row': 229,
        },
        'branding': {
            'method': 'Deterministic hand bitmap lettering, no TrueType rasterization in theme artwork.',
            'glyph_grid': [5, 7], 'logical_canvas': [320, 240],
            'runs': [{'xy': xy, 'text': text, 'pixel_scale': scale, 'color': color or 'rainbow'}
                     for xy, text, scale, color in BRAND_RUNS],
            'yellow_64_ink_pixel_rows': [20, 33], 'console_ink_pixel_rows': [17, 35],
            'header_slab_pixel_rows': [52, 68], 'header_ink_pixel_rows': [57, 63],
        },
        'context_popup': {
            'source_style': 'menu_base', 'illustrative_content_size_cells': [26, 5],
            'content_cells': window(ini(), 'menu_base', (26, 5)),
            'border_palette_base': 32, 'border_background_rgb': [8, 8, 8],
            'border_glyphs': [1, 2, 4, 8, 5, 9, 6, 10],
            'shade': 0,
            'scope': 'Vendor GuiWin.Draw border, padding, and GetPos semantics. Illustrative options only; firmware strings and dialog runtime remain unqualified.',
        },
        'preview_row_count': len(ROWS), 'maximum_preview_name_characters': max(len(row[0]) for row in ROWS),
        'available_name_characters': window(ini(), 'fm_list')[2] - 5,
        'preview_scope': 'Vendor tool geometry, GetPos text alignment, hard character wrapping limited to window height, palette and border semantics with illustrative strings. Native 640x240 and direct nearest-neighbor 640x480 4:3 approximation. Firmware owns real text. Capture scaling and overscan remain untested.',
        'png_fallback_scope': 'Quiet PNG-8 wallpaper with no embedded font, palette, or layout settings. It is intentionally adapted for the vendor default geometry.',
        'font_provenance': {'name': 'CartShelf Menu', 'license': 'OFL-1.1',
                            'source': 'Silkscreen-Bold.ttf', 'vendor_font_used': False,
                            'source_sha256': hashlib.sha256(BRAND_FONT.read_bytes()).hexdigest(),
                            'atlas_sha256': hashlib.sha256(FONT.read_bytes()).hexdigest()},
        'preview_titles': 'Authored fictional titles; no ROM bytes or game artwork.',
        'source_sha256': source_hashes, 'variants': results,
        'install_files_sha256': staged_hashes,
        'zip_sha256': hashlib.sha256(ZIP_OUTPUT.read_bytes()).hexdigest(),
    }
    (ROOT / 'VERIFICATION.json').write_text(json.dumps(report, indent=2) + '\n')
    print(f'Built and verified {DIST / "CartShelf-64.bgr"}')
    print(f'Preview {PREVIEW / "CartShelf-64-options.png"}')


if __name__ == '__main__':
    main()
