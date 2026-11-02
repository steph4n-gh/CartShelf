#!/usr/bin/env python3
"""Fetch exact-build handheld metadata and available artwork into a chosen library."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import datetime as dt
from functools import lru_cache
import hashlib
from html.parser import HTMLParser
import io
import json
import os
from pathlib import Path
import re
import tempfile
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import zlib

from PIL import Image

SYSTEMS = {'gb': 'Nintendo - Game Boy', 'gbc': 'Nintendo - Game Boy Color'}
FIELDS = ('developer', 'publisher', 'genre', 'releaseyear')
RAW = 'https://raw.githubusercontent.com/'
DATABASE = RAW + 'libretro/libretro-database/master/metadat/'
PRODUCTS = 'https://modretro.com/products.json?limit=250'
MAX_REFERENCE = 12 * 1024 * 1024
MAX_IMAGE = 6 * 1024 * 1024
HOSTS = {'raw.githubusercontent.com', 'api.github.com', 'modretro.com', 'cdn.shopify.com', 'media.githubusercontent.com', 'pearacidic.itch.io', 'img.itch.zone'}
PRODUCT_ALIASES = {
    'croc25thanniversaryedition': 'croc', 'croc2': 'croc-2',
    'buckandthecursedcartridge': 'buck', 'chikyuukaihougunzas': 'zas',
    'rayman2forever': 'rayman-2', 'sabrinatheanimatedserieszapped': 'sabrina-zapped',
    'tokitoriultimateedition': 'toki-tori-ultimate-edition',
}
ATARI = {'asteroids': 'asteroids', 'centipede': 'centipede', 'superbreakout': 'breakout'}


def workspace(root: Path, expected: os.stat_result | None = None) -> Path:
    root = Path(root).resolve(strict=True)
    if root.is_relative_to(Path(__file__).resolve().parent):
        raise ValueError('Choose a library folder outside the application source or bundle.')
    current = root.stat()
    if not root.is_dir() or not os.access(root, os.W_OK | os.X_OK):
        raise ValueError('Handheld metadata requires an existing writable library folder.')
    if expected is not None and (current.st_dev, current.st_ino) != (expected.st_dev, expected.st_ino):
        raise ValueError('The selected library changed during this operation.')
    return root


def managed(root: Path, relative: str | Path) -> Path:
    root = workspace(root)
    path = root / relative
    if not path.resolve().is_relative_to(root):
        raise ValueError('A handheld metadata path escapes the selected library.')
    return path


def atomic_json(root: Path, path: Path, value: object, expected: os.stat_result | None = None) -> None:
    workspace(root, expected)
    path = managed(root, path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            json.dump(value, stream, ensure_ascii=False, separators=(',', ':'))
            stream.write('\n'); stream.flush(); os.fsync(stream.fileno())
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    try:
        workspace(root, expected)
        managed(root, path)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def save_reference(root: Path, name: str, payload: bytes) -> Path:
    path = Path(name)
    digest = hashlib.sha256(payload).hexdigest()
    target = managed(root, Path('reference/handheld') / path.parent / (path.stem + '-' + digest[:16] + path.suffix))
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        if target.read_bytes() != payload: raise ValueError('A cached reference snapshot changed.')
    else:
        with target.open('xb') as output: output.write(payload)
    return target


def fetch(url: str, limit: int = MAX_REFERENCE) -> bytes:
    target = urllib.parse.urlsplit(url)
    if target.scheme != 'https' or target.hostname not in HOSTS or target.username or target.password:
        raise ValueError('Unsupported handheld metadata source URL.')
    request = urllib.request.Request(url, headers={'User-Agent': 'CartShelf', 'Accept': '*/*'})
    with urllib.request.urlopen(request, timeout=30) as response:
        final = urllib.parse.urlsplit(response.geturl())
        if final.scheme != 'https' or final.hostname not in HOSTS:
            raise ValueError('Unexpected handheld metadata redirect.')
        payload = response.read(limit + 1)
    if len(payload) > limit:
        raise ValueError('Handheld metadata response exceeds its size limit.')
    return payload


def key(value: str) -> str:
    return ''.join(c for c in unicodedata.normalize('NFKD', value).casefold() if c.isascii() and c.isalnum())


def base_title(value: str) -> str:
    return re.split(r'\s*[\[(]', value, maxsplit=1)[0].strip()


def parse_dat(payload: bytes) -> list[dict]:
    text = payload.decode('utf-8-sig')
    records = []
    for block in re.split(r'(?m)^game\s*\(', text)[1:]:
        record = dict(re.findall(r'(?m)^\s*([a-z_]+)\s+"([^"\n]*)"', block))
        rom_name = re.search(r'\brom\s*\(\s*name\s+"([^"]+)"', block)
        if rom_name: record['rom_name'] = rom_name.group(1)
        for name in ('sha1', 'crc'):
            match = re.search(r'\b' + name + r'\s+([0-9A-Fa-f]{8,40})\b', block)
            if match:
                record[name] = match.group(1).lower()
        if record.get('sha1') or record.get('crc'):
            records.append(record)
    return records


class PlainHTML(HTMLParser):
    def __init__(self):
        super().__init__(); self.parts = []; self.hidden = 0
    def handle_starttag(self, tag, attrs):
        if tag in {'script', 'style'}: self.hidden += 1
        if tag in {'p', 'br', 'li', 'h1', 'h2', 'h3'}: self.parts.append('\n')
    def handle_endtag(self, tag):
        if tag in {'script', 'style'}: self.hidden = max(0, self.hidden - 1)
        if tag in {'p', 'li'}: self.parts.append('\n')
    def handle_data(self, text):
        if not self.hidden: self.parts.append(text)


def plain_html(value: str) -> str:
    parser = PlainHTML(); parser.feed(value)
    paragraphs = [' '.join(line.split()) for line in ''.join(parser.parts).splitlines()]
    return '\n\n'.join(line for line in paragraphs if line)[:1200]


def publisher_lineage(release: dict) -> bool:
    return any('(modretro chromatic)' in source['filename'].casefold()
               or '/library/gb-gbc/chromatic/' in source['path'].casefold()
               for source in release.get('sources', []))


def product_for(release: dict, products: list[dict]) -> tuple[dict, int] | None:
    if not publisher_lineage(release):
        return None
    title = key(release['title'])
    by_handle = {p.get('handle'): p for p in products}
    if title in ATARI:
        product = by_handle.get('atari')
        images = product.get('images', []) if product else []
        matches = [i for i, image in enumerate(images) if ATARI[title] in urllib.parse.urlsplit(image.get('src', '')).path.casefold()]
        return (product, matches[0]) if product and len(matches) == 1 else None
    handle = PRODUCT_ALIASES.get(title)
    if handle:
        product = by_handle.get(handle)
        return (product, 0) if product else None
    matches = [p for p in products if key(p.get('title', '')) == title]
    return (matches[0], 0) if len(matches) == 1 else None


def art_for(name: str, images: dict[str, str]) -> tuple[str, str] | None:
    direct = name + '.png'
    if direct in images:
        return images[direct], 'canonical DAT title'
    wanted = key(base_title(name))
    candidates = [(file, url) for file, url in images.items() if key(base_title(Path(file).stem)) == wanted]
    if not candidates:
        return None
    region = re.search(r'\(([^)]*)\)', name)
    tokens = set(region.group(1).split(', ')) if region else set()
    matching = [(file, url) for file, url in candidates if any(token in file for token in tokens)]
    candidates = matching or candidates
    file, url = sorted(candidates)[0]
    return url, 'same verified game title; artwork does not identify a ROM revision'


def download_art(root: Path, url: str) -> dict:
    root = workspace(root)
    original_root = root.stat()
    directory = managed(root, 'cache/handheld-metadata/artwork')
    directory.mkdir(parents=True, exist_ok=True)
    prefix = hashlib.sha256(url.encode()).hexdigest()[:24]
    existing = list(directory.glob(prefix + '.*'))
    if existing:
        path = managed(root, existing[0])
        payload = path.read_bytes()
    else:
        resolved_url = url
        visited = set()
        for _ in range(5):
            if resolved_url in visited: raise ValueError('Artwork repository link cycle.')
            visited.add(resolved_url)
            payload = fetch(resolved_url, MAX_IMAGE)
            if payload.startswith(b'version https://git-lfs.github.com/spec/v1\n'):
                pointer = re.fullmatch(rb'version https://git-lfs.github.com/spec/v1\noid sha256:([0-9a-f]{64})\nsize ([0-9]+)\n?', payload)
                if pointer is None or int(pointer.group(2)) > MAX_IMAGE or not resolved_url.startswith(RAW + 'libretro-thumbnails/'):
                    raise ValueError('Invalid or oversized public artwork LFS pointer.')
                media_url = 'https://media.githubusercontent.com/media/' + resolved_url.removeprefix(RAW)
                payload = fetch(media_url, MAX_IMAGE)
                if len(payload) != int(pointer.group(2)) or hashlib.sha256(payload).hexdigest() != pointer.group(1).decode():
                    raise ValueError('Public artwork does not match its repository LFS hash.')
                break
            if resolved_url.startswith(RAW + 'libretro-thumbnails/') and len(payload) < 1024:
                try: sibling = payload.decode('utf-8').strip()
                except UnicodeError: sibling = ''
                if sibling.endswith('.png') and Path(sibling).name == sibling and len(Path(sibling).parts) == 1:
                    resolved_url = urllib.parse.urljoin(resolved_url, urllib.parse.quote(sibling, safe=''))
                    continue
            break
        else:
            raise ValueError('Artwork repository link chain is too long.')
        path = None
    with Image.open(io.BytesIO(payload)) as image:
        if image.width < 16 or image.height < 16 or image.width * image.height > 32_000_000 or image.format not in {'PNG', 'JPEG', 'WEBP'}:
            raise ValueError('Invalid or oversized handheld artwork.')
        extension = {'PNG': '.png', 'JPEG': '.jpg', 'WEBP': '.webp'}[image.format]
        image.verify()
    if path is None:
        workspace(root, original_root)
        path = managed(root, directory / (prefix + extension))
        with tempfile.NamedTemporaryFile('wb', dir=directory, delete=False) as output:
            temporary = Path(output.name); output.write(payload)
        try:
            workspace(root, original_root)
            managed(root, path)
            temporary.replace(path)
        finally: temporary.unlink(missing_ok=True)
    return {'artwork_path': str(path), 'artwork_url': url, 'artwork_sha256': hashlib.sha256(payload).hexdigest()}


def _reference(root: Path, category: str, system: str, receipts: list[dict], *, optional: bool = False) -> list[dict]:
    root = workspace(root)
    original_root = root.stat()
    url = DATABASE + category + '/' + urllib.parse.quote(SYSTEMS[system] + '.dat', safe='')
    try:
        payload = fetch(url)
    except urllib.error.HTTPError as error:
        if optional and error.code == 404: return []
        raise
    workspace(root, original_root)
    target = save_reference(root, f'{system}-{category}.dat', payload)
    receipts.append({'source_url': url, 'path': str(target), 'sha256': hashlib.sha256(payload).hexdigest(), 'bytes': len(payload)})
    records = parse_dat(payload)
    for record in records: record['_source_url'] = url
    return records


def _art_tree(root: Path, system: str, receipts: list[dict]) -> dict[str, str]:
    root = workspace(root)
    original_root = root.stat()
    repository = SYSTEMS[system].replace(' ', '_')
    url = f'https://api.github.com/repos/libretro-thumbnails/{repository}/git/trees/master?recursive=1'
    payload = fetch(url)
    document = json.loads(payload)
    if document.get('truncated') or not isinstance(document.get('tree'), list):
        raise ValueError('The handheld artwork directory is incomplete.')
    result = {}
    for item in document['tree']:
        name = item.get('path', '')
        if item.get('type') == 'blob' and name.startswith('Named_Boxarts/') and name.endswith('.png') and len(Path(name).parts) == 2:
            result[Path(name).name] = RAW + f'libretro-thumbnails/{repository}/master/' + urllib.parse.quote(name, safe='/')
    workspace(root, original_root)
    target = save_reference(root, f'{system}-artwork-tree.json', payload)
    receipts.append({'source_url': url, 'path': str(target), 'sha256': hashlib.sha256(payload).hexdigest(), 'bytes': len(payload), 'commit': document.get('sha', '')})
    return result


def publisher_details(root: Path, product: dict) -> tuple[dict, dict]:
    root = workspace(root)
    original_root = root.stat()
    handle = product['handle']
    if not re.fullmatch(r'[a-z0-9-]+', handle): raise ValueError('Invalid publisher product handle.')
    url = 'https://modretro.com/products/' + handle
    payload = fetch(url)
    workspace(root, original_root)
    path = save_reference(root, 'publisher-pages/' + handle + '.html', payload)
    parser = PlainHTML(); parser.feed(payload.decode('utf-8'))
    text = '\n'.join(' '.join(line.split()) for line in ''.join(parser.parts).splitlines())
    details = {}
    for label, field in [('Genre', 'genre'), ('Developer', 'developer')]:
        match = re.search(r'(?:^|\n)' + label + r':\s*([^\n]+)', text)
        if match: details[field] = match.group(1).strip()[:256]
    receipt = {'source_url': url, 'path': str(path), 'sha256': hashlib.sha256(payload).hexdigest(), 'bytes': len(payload)}
    return details, receipt


def developer_fallback(root: Path) -> tuple[dict, dict]:
    root = workspace(root)
    original_root = root.stat()
    url = 'https://pearacidic.itch.io/in-the-dark-2'
    payload = fetch(url)
    page = payload.decode('utf-8')
    tags = re.findall(r'<meta\b[^>]*>', page)
    image = next((re.search(r'content=["\']([^"\']+)', tag).group(1) for tag in tags if re.search(r'property=["\']og:image', tag) and re.search(r'content=["\']', tag)), '')
    if not image.startswith('https://img.itch.zone/'):
        raise ValueError('The developer page has no recognized promotional image.')
    workspace(root, original_root)
    path = save_reference(root, 'in-the-dark-2-developer.html', payload)
    details = {'source_url': url, 'artwork_url': image, 'canonical_title': 'In The Dark 2', 'genre': 'Puzzle', 'developer': 'Pearacidic Games and Gumpy Function', 'description': 'A science-fiction puzzle adventure built around switching adjacent lights. Story, free-play, and challenge modes use 4×4 and 5×5 grids. A bonus shooting game requires a color device.'}
    receipt = {'source_url': url, 'path': str(path), 'sha256': hashlib.sha256(payload).hexdigest(), 'bytes': len(payload)}
    return details, receipt


def _fingerprint(root: Path, release: dict) -> tuple[str, str, str]:
    source = release['sources'][0]
    path = managed(root, Path(source['path']))
    payload = path.read_bytes()
    if hashlib.sha256(payload).hexdigest() != source['raw_sha256']:
        raise ValueError('ROM changed before metadata matching: ' + source['filename'])
    system = 'gbc' if path.suffix.casefold() == '.gbc' else 'gb'
    return system, hashlib.sha1(payload).hexdigest(), f'{zlib.crc32(payload) & 0xffffffff:08x}'


def refresh_metadata(root: Path, releases: list[dict] | None = None) -> dict:
    root = workspace(root)
    original_root = root.stat()
    if releases is None:
        catalog = json.loads(managed(root, 'library/catalog.json').read_text())
        releases = catalog.get('snapshot', catalog)['releases']
    releases = [release for release in releases if release['profile'] == 'chromatic']
    receipts = []; dats = {}; fields = {}; trees = {}
    for system in SYSTEMS:
        records = _reference(root, 'no-intro', system, receipts)
        for category in ('hacks', 'homebrew', 'tosec'):
            records += _reference(root, category, system, receipts, optional=True)
        dats[system] = {}
        for record in records:
            if record.get('sha1'):
                existing = dats[system].setdefault(record['sha1'], record)
                field_sources = existing.setdefault('_field_sources', {})
                for field in ('developer', 'publisher', 'genre', 'releaseyear', 'description'):
                    if record.get(field) and not existing.get(field): existing[field] = record[field]
                    if record.get(field) and existing.get(field) == record[field]: field_sources.setdefault(field, record['_source_url'])
        fields[system] = {}
        for category in FIELDS:
            for record in _reference(root, category, system, receipts, optional=True):
                if category in record and record.get('crc'):
                    fields[system].setdefault(record['crc'], {})[category] = record[category]
        trees[system] = _art_tree(root, system, receipts)
        print(f'References ready: {SYSTEMS[system]} ({len(dats[system])} identities, {len(trees[system])} artwork files)', file=__import__('sys').stderr, flush=True)
    workspace(root, original_root)
    products_payload = fetch(PRODUCTS)
    workspace(root, original_root)
    products = json.loads(products_payload).get('products', [])
    if not isinstance(products, list): raise ValueError('Invalid ModRetro product catalog.')
    product_path = save_reference(root, 'modretro-products.json', products_payload)
    receipts.append({'source_url': PRODUCTS, 'path': str(product_path), 'bytes': len(products_payload), 'sha256': hashlib.sha256(products_payload).hexdigest()})
    entries = {}; artwork_requests = {}; errors = []
    used_products = {match[0]['handle']: match[0] for release in releases if (match := product_for(release, products))}
    details_by_handle = {}
    with ThreadPoolExecutor(max_workers=5) as workers:
        pending = {workers.submit(publisher_details, root, product): handle for handle, product in used_products.items()}
        for future in as_completed(pending):
            try:
                details, receipt = future.result(); details_by_handle[pending[future]] = details; receipts.append(receipt)
            except (OSError, ValueError, urllib.error.URLError) as error:
                errors.append({'product': pending[future], 'error': str(error)})
    fallback = None
    if any(key(release['title']) == 'inthedark2' and publisher_lineage(release) for release in releases):
        try:
            fallback, receipt = developer_fallback(root); receipts.append(receipt)
        except (OSError, ValueError, urllib.error.URLError) as error:
            errors.append({'product': 'in-the-dark-2', 'error': str(error)})
    for release in releases:
        system, digest, crc = _fingerprint(root, release)
        match = dats[system].get(digest)
        if match is None:
            other = 'gb' if system == 'gbc' else 'gbc'
            match = dats[other].get(digest)
            if match: system = other
        entry = {'artwork_path': '', 'description': '', 'genre': '', 'developer': '', 'publisher': '', 'release_year': '',
                 'canonical_title': '', 'source_url': '', 'confidence': 'unmatched', 'identity_verified': bool(match), 'raw_sha1': digest}
        if match:
            entry.update(canonical_title=match.get('name', match.get('comment', release['title'])), confidence='raw SHA-1 matches preservation DAT', source_url=match['_source_url'])
            data = fields[system].get(crc, {})
            for field in ('genre', 'developer', 'publisher'):
                entry[field] = data.get(field, '') or match.get(field, '')
            entry['release_year'] = data.get('releaseyear', '') or match.get('releaseyear', '')
            if not re.fullmatch(r'(?:19|20)[0-9]{2}', entry['release_year']): entry['release_year'] = ''
            entry['field_sources'] = {('release_year' if field == 'releaseyear' else field): url for field, url in match.get('_field_sources', {}).items()}
            if '[b' in match.get('rom_name', '').casefold():
                entry['warnings'] = ['The matched preservation catalog labels this exact dump as bad. Review its source record before using it.']
            artwork = art_for(entry['canonical_title'], trees[system])
            if artwork:
                artwork_requests[release['id']] = artwork[0]; entry['artwork_match'] = artwork[1]
            facts = []
            if entry['genre']: facts.append(entry['genre'] + ' game')
            if entry['developer']: facts.append('Developed by ' + entry['developer'])
            if entry['publisher']: facts.append('Published by ' + entry['publisher'])
            if entry['release_year']: facts.append('Listed release year ' + entry['release_year'])
            entry['description'] = match.get('description', '') or '. '.join(facts) + ('.' if facts else '')
        official = product_for(release, products)
        if official:
            product, image_index = official
            entry.update(description=plain_html(product.get('body_html', '')), publisher='ModRetro', source_url='https://modretro.com/products/' + product['handle'])
            entry['confidence'] = 'raw SHA-1 match plus publisher title match' if match else 'publisher lineage and reviewed title; ROM revision unverified'
            entry['canonical_title'] = entry['canonical_title'] or product['title']
            entry.update(details_by_handle.get(product['handle'], {}))
            images = product.get('images', [])
            if images:
                image_index = min(image_index, len(images) - 1)
                image_url = images[image_index].get('src', '')
                parts = urllib.parse.urlsplit(image_url)
                query = dict(urllib.parse.parse_qsl(parts.query)); query['width'] = '512'
                artwork_requests[release['id']] = urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(query)))
                entry['artwork_match'] = 'official publisher product artwork; ROM revision unverified'
        if key(release['title']) == 'inthedark2' and publisher_lineage(release) and fallback:
            entry.update({k: v for k, v in fallback.items() if k != 'artwork_url'})
            entry['confidence'] = 'developer title match; cartridge edition and ROM revision unverified'
            entry['artwork_match'] = 'developer promotional artwork, not a cartridge box scan'
            artwork_requests[release['id']] = fallback['artwork_url']
        entries[release['id']] = entry
    with ThreadPoolExecutor(max_workers=5) as workers:
        futures = {workers.submit(download_art, root, url): release_id for release_id, url in artwork_requests.items()}
        for count, future in enumerate(as_completed(futures), 1):
            release_id = futures[future]
            try: entries[release_id].update(future.result())
            except (OSError, ValueError, urllib.error.URLError) as error:
                errors.append({'release_id': release_id, 'error': str(error)})
            if count % 50 == 0 or count == len(futures):
                print(f'Handheld artwork {count}/{len(futures)} checked', file=__import__('sys').stderr, flush=True)
    result = {'schema': 1, 'fetched_at': dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds'), 'sources': receipts, 'entries': entries, 'errors': errors}
    old_path = managed(root, 'cache/handheld-metadata/index.json')
    if old_path.is_file():
        old = json.loads(old_path.read_text()).get('entries', {})
        for release_id, entry in entries.items():
            if not entry['artwork_path'] and old.get(release_id, {}).get('artwork_path'):
                previous = old[release_id]
                path = managed(root, previous['artwork_path'])
                if path.is_file():
                    for field in ('artwork_path', 'artwork_url', 'artwork_sha256', 'artwork_match'):
                        if field in previous: entry[field] = previous[field]
    atomic_json(root, old_path, result, original_root)
    stats = {'release_count': len(entries), 'identity_matches': sum(e['identity_verified'] for e in entries.values()), 'artwork_count': sum(bool(e['artwork_path']) for e in entries.values()), 'description_count': sum(bool(e['description']) for e in entries.values()), 'download_errors': len(errors), 'genre_count': sum(bool(e['genre']) for e in entries.values()), 'developer_count': sum(bool(e['developer']) for e in entries.values()), 'publisher_count': sum(bool(e['publisher']) for e in entries.values()), 'release_year_count': sum(bool(e['release_year']) for e in entries.values())}
    workspace(root, original_root)
    _report(root, releases, result, stats, original_root)
    return {**stats, 'report_path': str(root / 'reports/HANDHELD_METADATA_REPORT.md'), 'cache_path': str(old_path)}


@lru_cache(maxsize=4)
def _cached_index(path: str, modified: int, size: int) -> dict:
    return json.loads(Path(path).read_text()).get('entries', {})


def load_index(root: Path) -> dict:
    root = Path(root).resolve()
    path = managed(root, 'cache/handheld-metadata/index.json')
    if not path.is_file(): return {}
    stat = path.stat()
    return _cached_index(str(path), stat.st_mtime_ns, stat.st_size)


def enrich(root: Path, release: dict) -> dict:
    if release.get('profile') != 'chromatic': return {}
    return load_index(root).get(release['id'], {}).copy()


def _report(root: Path, releases: list[dict], result: dict, stats: dict, expected: os.stat_result | None = None) -> None:
    workspace(root, expected)
    reports = managed(root, 'reports'); reports.mkdir(exist_ok=True)
    titles = {r['id']: r['title'] for r in releases}
    missing = [{'release_id': ident, 'title': titles[ident], 'identity_verified': entry['identity_verified'], 'confidence': entry['confidence'], 'missing': [field for field in ('artwork_path', 'description', 'genre', 'developer', 'publisher', 'release_year') if not entry.get(field)]} for ident, entry in result['entries'].items() if any(not entry.get(field) for field in ('artwork_path', 'description', 'genre', 'developer', 'publisher', 'release_year'))]
    atomic_json(root, reports / 'handheld-metadata-coverage.json', {'stats': stats, 'missing': missing, 'errors': result['errors'], 'sources': result['sources']}, expected)
    lines = ['# Handheld metadata coverage', '', f"Fetched: {result['fetched_at']}", '', f"- Distinct handheld builds: {stats['release_count']}", f"- Exact preservation-DAT matches: {stats['identity_matches']}", f"- Builds with available artwork: {stats['artwork_count']}", f"- Builds with sourced descriptions or factual summaries: {stats['description_count']}", f"- Artwork download errors: {stats['download_errors']}", f"- Builds with a sourced genre: {stats.get('genre_count', 0)}", f"- Builds with a sourced developer: {stats.get('developer_count', 0)}", f"- Builds with a sourced publisher: {stats.get('publisher_count', 0)}", f"- Builds with a sourced release year: {stats.get('release_year_count', 0)}", '', 'Identity matches use raw SHA-1 from the current local ROM bytes. Historical fields use CRC-keyed Libretro metadata or an exact-SHA-1 preservation record. Unrecognized hashes receive no borrowed game facts. Artwork can cover the same verified game without identifying its cartridge revision.', '', 'ModRetro product metadata applies only to positively identified Chromatic import lineage and reviewed exact title aliases. Publisher product matches alone do not verify local ROM revisions. Unknown modifications remain unmatched rather than inheriting an unrelated original game’s art or description.', '', 'The GB X7 menu has no box-art metadata browser. These handheld assets serve CartShelf’s collection browser and remain in its external cache; game packs retain the GB X7 folder layout.', '', '## Refresh', '', '```sh', 'python3 handheld_metadata.py --root /path/to/library refresh', '```', '', '## Sources', '']
    for source in result['sources']:
        lines.append(f"- [{Path(source['path']).name}]({source['source_url']}) · SHA-256 `{source['sha256']}`")
    lines += ['', '## Remaining gaps', '', 'The complete release list and missing fields are in `reports/handheld-metadata-coverage.json`.', '']
    for entry in missing:
        lines.append(f"- {entry['title']} · {entry['release_id']} · missing {', '.join(entry['missing'])}")
    workspace(root, expected)
    managed(root, reports / 'HANDHELD_METADATA_REPORT.md').write_text('\n'.join(lines) + '\n')


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__); parser.add_argument('--root', type=Path, required=True); parser.add_argument('command', choices=['refresh']); args = parser.parse_args()
    try:
        print(json.dumps(refresh_metadata(args.root), separators=(',', ':')))
        return 0
    except (OSError, ValueError, KeyError, urllib.error.URLError) as error:
        print(str(error), file=__import__('sys').stderr); return 1


if __name__ == '__main__':
    raise SystemExit(main())
