from pathlib import Path
import hashlib
import io
import json
import tempfile
import unittest
from unittest import mock
import urllib.error

import prepare_everdrive64 as metadata


class MetadataDownload(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.cache = Path(self.temporary.name) / 'cache'
        self.revision = '1' * 40
        self.commit = {'sha': self.revision, 'commit': {'tree': {'sha': '2' * 40},
                       'committer': {'date': '2026-11-01T00:00:00Z'}, 'message': 'Source update'}}
        self.payloads = {'metadata/N/A/A/E/metadata.ini': b'[meta]\nname=Alpha\n',
                         'metadata/N/A/A/E/boxart_front.png': b'constructed artwork bytes',
                         'metadata/N/B/B/E/metadata.ini': b'[meta]\nname=Beta\n'}
        self.entries = [{'path': path, 'type': 'blob', 'mode': '100644', 'size': len(payload),
                         'sha': hashlib.sha1(f'blob {len(payload)}\0'.encode() + payload).hexdigest()}
                        for path, payload in self.payloads.items()]
        self.tree = {'truncated': False, 'tree': self.entries}
        self.urls = []

    def response(self, url, limit):
        self.urls.append(url)
        if url.endswith('/commits/main'):
            return json.dumps(self.commit).encode()
        if '?recursive=1' in url:
            return json.dumps(self.tree).encode()
        return self.payloads[url.split('/' + self.revision + '/')[1]]

    def refresh(self, codes):
        with mock.patch.object(metadata, '_metadata_response', side_effect=self.response):
            with mock.patch.object(metadata, 'run', side_effect=AssertionError('Git must not be invoked')):
                metadata.sync_metadata_files(self.cache, codes)

    def test_fresh_cache_downloads_only_present_codes_and_records_source(self):
        self.refresh({'NAAE'})
        self.assertEqual((self.cache / 'metadata/N/A/A/E/metadata.ini').read_bytes(), self.payloads['metadata/N/A/A/E/metadata.ini'])
        self.assertFalse((self.cache / 'metadata/N/B/B/E').exists())
        raw_urls = [url for url in self.urls if url.startswith(metadata.METADATA_RAW_URL)]
        self.assertEqual(len(raw_urls), 2)
        self.assertTrue(all('/' + self.revision + '/' in url for url in raw_urls))
        receipt = json.loads((self.cache / metadata.METADATA_SOURCE_FILE).read_text())
        self.assertEqual(receipt['game_codes'], ['NAAE'])
        self.assertEqual(receipt['scope'], 'selected game codes')
        self.assertEqual(metadata.metadata_source_info(self.cache)['commit'], self.revision)
        self.assertEqual({item['sha256'] for item in receipt['files']},
                         {hashlib.sha256(self.payloads[path]).hexdigest() for path in self.payloads if '/N/A/A/E/' in path})

    def test_existing_git_cache_retains_other_codes_and_custom_files_without_git(self):
        (self.cache / '.git').mkdir(parents=True)
        existing = self.cache / 'metadata/N/B/B/E/metadata.ini'
        existing.parent.mkdir(parents=True)
        existing.write_bytes(b'previous unrelated cache')
        self.refresh({'NAAE'})
        self.assertEqual(existing.read_bytes(), b'previous unrelated cache')
        self.assertTrue((self.cache / '.git').is_dir())

    def test_missing_code_clears_only_its_stale_supported_files(self):
        existing = self.cache / 'metadata/N/O/P/E/metadata.ini'
        existing.parent.mkdir(parents=True)
        existing.write_bytes(b'stale community metadata')
        custom = existing.with_name('local-notes.txt')
        custom.write_bytes(b'keep')
        self.refresh({'NOPE'})
        self.assertFalse(existing.exists())
        self.assertEqual(custom.read_bytes(), b'keep')
        self.assertEqual(len(self.urls), 2)
        self.assertEqual(json.loads((self.cache / metadata.METADATA_SOURCE_FILE).read_text())['files'], [])

    def test_network_failure_or_wrong_blob_preserves_previous_cache_and_receipt(self):
        self.refresh({'NAAE'})
        before = {path.relative_to(self.cache): path.read_bytes() for path in self.cache.rglob('*') if path.is_file()}
        path = 'metadata/N/A/A/E/boxart_front.png'
        self.payloads[path] += b' updated'
        entry = next(item for item in self.entries if item['path'] == path)
        entry.update(size=len(self.payloads[path]), sha=hashlib.sha1(f'blob {len(self.payloads[path])}\0'.encode() + self.payloads[path]).hexdigest())
        original = self.response
        for failure in ('404', 'checksum'):
            def response(url, limit):
                if url.endswith('/boxart_front.png'):
                    if failure == '404':
                        raise urllib.error.HTTPError(url, 404, 'missing file', {}, None)
                    return b'wrong blob'
                return original(url, limit)
            with self.subTest(failure=failure), mock.patch.object(metadata, '_metadata_response', side_effect=response):
                with self.assertRaises((ValueError, urllib.error.HTTPError)) as caught:
                    metadata.sync_metadata_files(self.cache, {'NAAE'})
                if isinstance(caught.exception, urllib.error.HTTPError): caught.exception.close()
            self.assertEqual({path.relative_to(self.cache): path.read_bytes() for path in self.cache.rglob('*') if path.is_file()}, before)

    def test_incomplete_or_invalid_tree_is_refused_before_cache_changes(self):
        for mutation in ('truncated', 'duplicate', 'symlink'):
            self.tree = {'truncated': False, 'tree': [dict(item) for item in self.entries]}
            if mutation == 'truncated': self.tree['truncated'] = True
            if mutation == 'duplicate': self.tree['tree'].append(dict(self.entries[0]))
            if mutation == 'symlink': self.tree['tree'][0]['mode'] = '120000'
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                self.refresh({'NAAE'})
            self.assertFalse(self.cache.exists())

    def test_empty_library_records_source_without_any_game_downloads(self):
        self.refresh(set())
        self.assertEqual(len(self.urls), 2)
        self.assertEqual(json.loads((self.cache / metadata.METADATA_SOURCE_FILE).read_text())['game_codes'], [])

    def test_valid_cache_avoids_repeated_download_but_changed_bytes_are_repaired(self):
        self.refresh({'NAAE'})
        self.urls.clear()
        self.refresh({'NAAE'})
        self.assertEqual(len(self.urls), 2)
        cached = self.cache / 'metadata/N/A/A/E/metadata.ini'
        cached.write_bytes(b'bad cache')
        self.urls.clear()
        self.refresh({'NAAE'})
        self.assertEqual(len(self.urls), 3)
        self.assertEqual(cached.read_bytes(), self.payloads['metadata/N/A/A/E/metadata.ini'])

    def test_explicit_whole_library_refresh_of_selective_cache_fetches_all_supported_files(self):
        self.refresh({'NAAE'})
        with mock.patch.object(metadata, '_metadata_response', side_effect=self.response), \
             mock.patch.object(metadata, 'run', side_effect=AssertionError('Git must not be invoked')), \
             mock.patch.object(metadata, 'download'), \
             mock.patch.object(metadata.urllib.request, 'urlopen', return_value=io.BytesIO(b'ed64-pro-fw-v26.1014.efu')):
            result = metadata.sync_inputs(self.cache, self.cache.parent / 'reference.dat', self.cache.parent / 'firmware')
        self.assertEqual(result.name, 'ed64-pro-fw-v26.1014.efu')
        self.assertEqual((self.cache / 'metadata/N/B/B/E/metadata.ini').read_bytes(), self.payloads['metadata/N/B/B/E/metadata.ini'])
        self.assertEqual(json.loads((self.cache / metadata.METADATA_SOURCE_FILE).read_text())['scope'], 'all supported files')

    def test_whole_library_refresh_removes_retired_code_only_after_downloads_verify(self):
        self.refresh(None)
        retired = self.cache / 'metadata/N/A/A/E'
        notes = retired / 'local-notes.txt'
        notes.write_bytes(b'keep local notes')
        source = self.cache / metadata.METADATA_SOURCE_FILE
        old_receipt = source.read_bytes()
        path = 'metadata/N/B/B/E/metadata.ini'
        self.payloads[path] += b'author=Updated\n'
        payload = self.payloads[path]
        self.tree['tree'] = [{'path': path, 'type': 'blob', 'mode': '100644', 'size': len(payload),
                              'sha': hashlib.sha1(f'blob {len(payload)}\0'.encode() + payload).hexdigest()}]
        original = self.response

        def broken_response(url, limit):
            return b'bad download' if url.endswith('/metadata.ini') else original(url, limit)

        with mock.patch.object(metadata, '_metadata_response', side_effect=broken_response), self.assertRaises(ValueError):
            metadata.sync_metadata_files(self.cache, None)
        self.assertTrue((retired / 'metadata.ini').is_file())
        self.assertTrue((retired / 'boxart_front.png').is_file())
        self.assertEqual(source.read_bytes(), old_receipt)

        with mock.patch.object(metadata, '_metadata_response', side_effect=self.response), \
             mock.patch.object(metadata, 'run', side_effect=AssertionError('Git must not be invoked')), \
             mock.patch.object(metadata, 'download'), \
             mock.patch.object(metadata.urllib.request, 'urlopen', return_value=io.BytesIO(b'ed64-pro-fw-v26.1014.efu')):
            metadata.sync_inputs(self.cache, self.cache.parent / 'reference.dat', self.cache.parent / 'firmware')
        self.assertFalse((retired / 'metadata.ini').exists())
        self.assertFalse((retired / 'boxart_front.png').exists())
        self.assertEqual(notes.read_bytes(), b'keep local notes')
        self.assertEqual((self.cache / path).read_bytes(), payload)
        receipt = json.loads(source.read_text())
        self.assertEqual(receipt['game_codes'], ['NBBE'])
        self.assertEqual([item['path'] for item in receipt['files']], [path])


if __name__ == '__main__':
    unittest.main()
