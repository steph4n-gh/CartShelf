import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import urllib.error
import zlib

from PIL import Image
import handheld_metadata as metadata


PROJECT = Path(__file__).resolve().parents[1]


class Response(io.BytesIO):
    def __init__(self, data, url='https://raw.githubusercontent.com/example/file'):
        super().__init__(data); self.url = url
    def geturl(self): return self.url


class HandheldMetadata(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=None, prefix='handheld-metadata-test-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.payload = b'constructed ROM identity fixture'
        self.source = self.root / 'library/GB-GBC/Imports/Example (USA).gb'
        self.source.parent.mkdir(parents=True); self.source.write_bytes(self.payload)
        self.release = {
            'id': 'chromatic:' + hashlib.sha256(self.payload).hexdigest(), 'profile': 'chromatic',
            'title': 'Example', 'version': 'Unknown',
            'sources': [{'path': str(self.source), 'filename': self.source.name, 'raw_sha256': hashlib.sha256(self.payload).hexdigest()}],
        }

    def refresh(self, releases=None, matching=True):
        def reference(root, category, system, receipts, **kwargs):
            if category == 'no-intro' and system == 'gb' and matching:
                return [{'name': 'Example (USA)', 'sha1': hashlib.sha1(self.payload).hexdigest(), 'crc': f'{zlib.crc32(self.payload) & 0xffffffff:08x}', '_source_url': 'https://raw.githubusercontent.com/reference.dat'}]
            if category in metadata.FIELDS and system == 'gb':
                return [{'crc': f'{zlib.crc32(self.payload) & 0xffffffff:08x}', category: {'developer': 'Fixture developer', 'publisher': 'Fixture publisher', 'genre': 'Puzzle', 'releaseyear': '1999'}[category]}]
            return []
        with patch.object(metadata, '_reference', side_effect=reference), \
             patch.object(metadata, '_art_tree', return_value={}), \
             patch.object(metadata, 'fetch', return_value=b'{"products":[]}'):
            return metadata.refresh_metadata(self.root, releases or [self.release])

    def png(self):
        stream = io.BytesIO(); Image.new('RGB', (16, 20), (255, 255, 0)).save(stream, 'PNG'); return stream.getvalue()

    def test_dat_parser_retains_whole_build_name_and_hash(self):
        fixture = b'''clrmamepro ( name "ignored" )
game (
 name "Example (USA) (Rev 2)"
 description "Fixture translation"
 rom ( name "Example.gb" crc 12345678 sha1 AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA )
)
'''
        result = metadata.parse_dat(fixture)
        self.assertEqual(result[0]['name'], 'Example (USA) (Rev 2)')
        self.assertEqual(result[0]['sha1'], 'a' * 40)
        self.assertEqual(result[0]['description'], 'Fixture translation')

    def test_exact_identity_enrichment_retains_sources_and_is_offline(self):
        self.refresh()
        with patch.object(metadata, 'fetch', side_effect=AssertionError('Network is forbidden during browsing')):
            entry = metadata.enrich(self.root, self.release)
        self.assertTrue(entry['identity_verified'])
        self.assertEqual(entry['canonical_title'], 'Example (USA)')
        self.assertEqual(entry['genre'], 'Puzzle')
        self.assertEqual(entry['release_year'], '1999')
        self.assertEqual(entry['source_url'], 'https://raw.githubusercontent.com/reference.dat')
        self.assertEqual(self.release['title'], 'Example')
        self.assertEqual(self.source.read_bytes(), self.payload)

    def test_matching_title_with_unrecognized_hash_gets_no_borrowed_metadata(self):
        self.refresh(matching=False)
        entry = metadata.enrich(self.root, self.release)
        self.assertFalse(entry['identity_verified'])
        self.assertEqual(entry['artwork_path'], '')
        self.assertEqual(entry['description'], '')
        self.assertEqual(entry['confidence'], 'unmatched')

    def test_exact_tosec_record_can_fill_year_without_replacing_the_primary_title(self):
        digest = hashlib.sha1(self.payload).hexdigest()
        def reference(root, category, system, receipts, **kwargs):
            if system != 'gb': return []
            if category == 'no-intro': return [{'name': 'Example (USA)', 'sha1': digest, 'crc': '12345678', '_source_url': 'https://raw.githubusercontent.com/no-intro.dat'}]
            if category == 'tosec': return [{'name': 'Alternate catalog title', 'sha1': digest, 'crc': '12345678', 'releaseyear': '1991', '_source_url': 'https://raw.githubusercontent.com/tosec.dat'}]
            return []
        with patch.object(metadata, '_reference', side_effect=reference), patch.object(metadata, '_art_tree', return_value={}), patch.object(metadata, 'fetch', return_value=b'{"products":[]}'):
            metadata.refresh_metadata(self.root, [self.release])
        entry = metadata.enrich(self.root, self.release)
        self.assertEqual(entry['canonical_title'], 'Example (USA)')
        self.assertEqual(entry['release_year'], '1991')
        self.assertEqual(entry['field_sources']['release_year'], 'https://raw.githubusercontent.com/tosec.dat')

    def test_wrong_filename_extension_can_match_the_other_exact_system_dat(self):
        self.source = self.source.with_suffix('.gbc'); self.source.write_bytes(self.payload)
        self.release['sources'][0]['path'] = str(self.source)
        self.refresh()
        self.assertTrue(metadata.enrich(self.root, self.release)['identity_verified'])

    def test_changed_rom_fails_before_publishing_a_new_index(self):
        self.refresh()
        cache = self.root / 'cache/handheld-metadata/index.json'; before = cache.read_bytes()
        self.source.write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError, 'ROM changed'):
            self.refresh()
        self.assertEqual(cache.read_bytes(), before)

    def test_later_reference_download_does_not_replace_prior_receipt_bytes(self):
        first = metadata.save_reference(self.root, 'catalog.dat', b'first source snapshot')
        second = metadata.save_reference(self.root, 'catalog.dat', b'later source snapshot')
        self.assertNotEqual(first, second)
        self.assertEqual(first.read_bytes(), b'first source snapshot')
        self.assertEqual(second.read_bytes(), b'later source snapshot')

    def test_network_failure_preserves_the_previous_index(self):
        self.refresh()
        cache = self.root / 'cache/handheld-metadata/index.json'; before = cache.read_bytes()
        with patch.object(metadata, '_reference', side_effect=urllib.error.URLError('offline')):
            with self.assertRaises(urllib.error.URLError): metadata.refresh_metadata(self.root, [self.release])
        self.assertEqual(cache.read_bytes(), before)

    def test_publisher_title_cannot_match_an_original_nintendo_release(self):
        release = dict(self.release, title='Tetris')
        self.assertIsNone(metadata.product_for(release, [{'title': 'Tetris', 'handle': 'tetris'}]))
        release['sources'] = [{'path': str(self.root / 'library/GB-GBC/Chromatic/Tetris.gbc'), 'filename': 'Tetris (ModRetro Chromatic).gbc'}]
        self.assertEqual(metadata.product_for(release, [{'title': 'Tetris', 'handle': 'tetris'}])[0]['handle'], 'tetris')

    def test_atari_edition_selects_named_product_art_when_order_changes(self):
        release = dict(self.release, title='Centipede', sources=[{'path': str(self.root / 'library/GB-GBC/Chromatic/Centipede.gbc'), 'filename': 'Centipede (ModRetro Chromatic).gbc'}])
        product = {'title': 'Atari Collection', 'handle': 'atari', 'images': [{'src': 'https://cdn.shopify.com/Atari_Breakout.png'}, {'src': 'https://cdn.shopify.com/Atari_Centipede.png'}, {'src': 'https://cdn.shopify.com/Charms.png'}]}
        result = metadata.product_for(release, [product])
        self.assertEqual(result[1], 1)

    def test_artwork_matches_only_the_same_verified_title(self):
        images = {'Example (USA).png': 'https://raw.githubusercontent.com/example.png', 'Other Example (USA).png': 'https://raw.githubusercontent.com/other.png'}
        self.assertEqual(metadata.art_for('Example (USA) (Rev 2)', images)[0], images['Example (USA).png'])
        self.assertIsNone(metadata.art_for('Example 2 (USA)', images))

    def test_image_validation_and_external_cache(self):
        payload = self.png()
        with patch.object(metadata, 'fetch', return_value=payload):
            result = metadata.download_art(self.root, 'https://raw.githubusercontent.com/example.png')
        path = Path(result['artwork_path'])
        self.assertTrue(path.is_relative_to(self.root))
        self.assertEqual(path.read_bytes(), payload)
        self.assertEqual(result['artwork_sha256'], hashlib.sha256(payload).hexdigest())
        with patch.object(metadata, 'fetch', side_effect=AssertionError('Unexpected download')):
            self.assertEqual(metadata.download_art(self.root, 'https://raw.githubusercontent.com/example.png'), result)

    def test_public_git_lfs_artwork_is_bound_to_the_repository_hash(self):
        payload = self.png()
        pointer = f'version https://git-lfs.github.com/spec/v1\noid sha256:{hashlib.sha256(payload).hexdigest()}\nsize {len(payload)}\n'.encode()
        with patch.object(metadata, 'fetch', side_effect=[pointer, payload]) as get:
            result = metadata.download_art(self.root, metadata.RAW + 'libretro-thumbnails/Nintendo_-_Game_Boy/master/Named_Boxarts/Example.png')
        self.assertEqual(Path(result['artwork_path']).read_bytes(), payload)
        self.assertTrue(get.call_args_list[1].args[0].startswith('https://media.githubusercontent.com/media/libretro-thumbnails/'))
        with patch.object(metadata, 'fetch', side_effect=[pointer, b'corrupt']):
            with self.assertRaisesRegex(ValueError, 'LFS hash'):
                metadata.download_art(self.root, metadata.RAW + 'libretro-thumbnails/Nintendo_-_Game_Boy/master/Named_Boxarts/Changed.png')

    def test_public_artwork_symlink_resolves_only_a_sibling_image(self):
        payload = self.png()
        with patch.object(metadata, 'fetch', side_effect=[b'Example (USA).png', payload]) as get:
            result = metadata.download_art(self.root, metadata.RAW + 'libretro-thumbnails/Nintendo_-_Game_Boy/master/Named_Boxarts/Example (USA) (Rev 2).png')
        self.assertEqual(Path(result['artwork_path']).read_bytes(), payload)
        self.assertTrue(get.call_args_list[1].args[0].endswith('Example%20%28USA%29.png'))
        with patch.object(metadata, 'fetch', return_value=b'../../secret.png'):
            with self.assertRaises(OSError):
                metadata.download_art(self.root, metadata.RAW + 'libretro-thumbnails/Nintendo_-_Game_Boy/master/Named_Boxarts/Unsafe.png')

    def test_network_url_and_download_limits_are_enforced(self):
        for url in ['http://raw.githubusercontent.com/file', 'https://evil.test/image.png', 'https://user:password@cdn.shopify.com/image.png']:
            with self.subTest(url=url), patch.object(metadata.urllib.request, 'urlopen') as get:
                with self.assertRaises(ValueError): metadata.fetch(url)
                get.assert_not_called()
        with patch.object(metadata.urllib.request, 'urlopen', return_value=Response(b'12345')):
            with self.assertRaisesRegex(ValueError, 'size limit'): metadata.fetch('https://raw.githubusercontent.com/file', 4)

    def test_source_and_cache_symlink_escapes_are_rejected(self):
        outside = PROJECT / 'tmp'
        (self.root / 'cache').symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, 'escapes'): metadata.enrich(self.root, self.release)

    def test_publisher_specs_come_from_labeled_primary_fields(self):
        html = b'<h1>Example</h1><li><strong>Genre:</strong> Adventure Puzzle</li><li>Developer: Fixture Studio</li>'
        with patch.object(metadata, 'fetch', return_value=html):
            details, source = metadata.publisher_details(self.root, {'handle': 'example'})
        self.assertEqual(details, {'genre': 'Adventure Puzzle', 'developer': 'Fixture Studio'})
        self.assertEqual(source['source_url'], 'https://modretro.com/products/example')

    def test_cached_index_is_loaded_once_for_repeated_enrichment(self):
        self.refresh()
        metadata._cached_index.cache_clear()
        with patch.object(metadata.json, 'loads', wraps=json.loads) as parse:
            for _ in range(20): self.assertTrue(metadata.enrich(self.root, self.release)['identity_verified'])
        self.assertEqual(parse.call_count, 1)

    def test_html_description_removes_markup_and_script_content(self):
        self.assertEqual(metadata.plain_html('<p>A game.</p><script>secret()</script><p>Second fact.</p>'), 'A game.\n\nSecond fact.')


if __name__ == '__main__': unittest.main()
