from contextlib import redirect_stderr
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import prepare_everdrive64 as n64
import rom_verifier as verifier

PROJECT = Path(__file__).resolve().parents[1]


def gb_rom():
    data = bytearray(32768)
    data[0x104:0x134] = verifier.LOGO
    data[0x134:0x13b] = b'FIXTURE'
    header = 0
    for byte in data[0x134:0x14d]:
        header = (header - byte - 1) & 255
    data[0x14d] = header
    data[0x14e:0x150] = (sum(data) & 65535).to_bytes(2, 'big')
    return bytes(data)


def n64_rom():
    data = bytearray(4096)
    data[:4] = bytes.fromhex('80371240')
    data[0x20:0x34] = b'FIXTURE'.ljust(20, b' ')
    data[0x3b:0x3f] = b'TSTE'
    return bytes(data)


def dat(name, payload):
    return ('clrmamepro (\n version "test"\n)\n'
            'game (\n name "' + name + '"\n'
            ' rom ( name "' + name + '.gb" size ' + str(len(payload)) +
            ' crc 00000000 md5 ' + hashlib.md5(payload).hexdigest() +
            ' sha1 ' + hashlib.sha1(payload).hexdigest() + ' )\n)\n').encode()


class ROMVerification(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='rom-verifier-test-', dir=None)
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name).resolve()

    def write(self, relative, payload):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        return path

    def release(self, payload, name='Example.gb', profile='chromatic', release_id=None):
        path = self.write('library/inputs/' + name, payload)
        return {'id': release_id or profile + ':' + hashlib.sha256(payload).hexdigest(),
                'profile': profile, 'title': name, 'sources': [
                    {'path': str(path), 'filename': name, 'raw_sha256': hashlib.sha256(payload).hexdigest()}]}

    def verify(self, releases, profile=None):
        output = io.StringIO()
        with redirect_stderr(output):
            result = verifier.verify_library(self.root, releases, profile)
        events = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertTrue(all(event['event'] == 'progress' and event['phase'] == 'verify' for event in events))
        return result

    def test_fresh_hash_reference_match_and_source_preservation(self):
        payload = gb_rom()
        release = self.release(payload)
        self.write('reference/handheld/gb-no-intro.dat', dat('Known cartridge', payload))
        result = self.verify([release])
        self.assertEqual(result['summary']['reference_matches'], 1)
        self.assertEqual(result['summary']['files_checked'], 1)
        self.assertEqual(result['summary']['source_files_preserved'], 1)
        row = result['files'][0]
        self.assertEqual(row['raw_sha1'], hashlib.sha1(payload).hexdigest())
        self.assertEqual(row['reference_name'], 'Known cartridge')
        self.assertTrue(all(row['checks'][key] for key in ['nintendo_logo', 'header_checksum', 'global_checksum', 'declared_size']))
        self.assertEqual(Path(release['sources'][0]['path']).read_bytes(), payload)
        cached = json.loads((self.root / 'reports/ROM_VERIFICATION_REPORT.json').read_text())
        self.assertEqual(cached['summary'], result['summary'])
        self.assertEqual(len(cached['references'][0]['sha256']), 64)

    def test_reference_match_retains_an_original_global_checksum_warning(self):
        payload = bytearray(gb_rom())
        payload[0x200] ^= 1
        release = self.release(bytes(payload))
        self.write('reference/handheld/gb-no-intro.dat', dat('Original known anomaly', payload))
        result = self.verify([release])
        row = result['files'][0]
        self.assertEqual(row['status'], 'reference-match')
        self.assertFalse(row['checks']['global_checksum'])
        self.assertIn('Global checksum differs.', row['warnings'])

    def test_valid_unlisted_homebrew_is_not_called_broken(self):
        result = self.verify([self.release(gb_rom(), name='Own homebrew.gb')])
        self.assertEqual(result['files'][0]['status'], 'unlisted')
        self.assertEqual(result['summary']['invalid'], 0)
        self.assertGreater(result['summary']['reference_errors'], 0)

    def test_changed_catalog_bytes_are_not_authorized_by_a_stale_metadata_claim(self):
        payload = gb_rom()
        release = self.release(payload)
        changed = bytearray(payload)
        changed[0x201] ^= 1
        Path(release['sources'][0]['path']).write_bytes(changed)
        release['metadata'] = {'identity_verified': True}
        self.write('reference/handheld/gb-no-intro.dat', dat('Only original bytes', payload))
        result = self.verify([release])
        self.assertEqual(result['files'][0]['status'], 'changed')
        self.assertEqual(result['summary']['reference_matches'], 0)
        self.assertEqual(result['files'][0]['reference_name'], '')

    def test_truncated_empty_and_unknown_n64_headers_are_invalid_but_all_are_counted(self):
        releases = [self.release(b'', name='Empty.gb'), self.release(b'a' * 50, name='Truncated.gb'),
                    self.release(b'unknown' * 1000, name='Unknown.z64', profile='m64')]
        result = self.verify(releases)
        self.assertEqual(result['summary']['files_checked'], 3)
        self.assertEqual(result['summary']['invalid'], 3)
        self.assertEqual(result['summary']['source_files_preserved'], 3)

    def test_byte_order_aliases_share_reference_identity_and_each_file_is_read(self):
        payload = n64_rom()
        big = self.release(payload, name='Example.z64', profile='m64', release_id='m64:known')
        little = self.release(n64.normalize_chunk(payload, 'n64'), name='Example.n64', profile='m64')
        swapped = self.release(n64.normalize_chunk(payload, 'v64'), name='Example.v64', profile='m64')
        big['sources'].extend(little['sources'] + swapped['sources'])
        self.write('reference/Nintendo - Nintendo 64.dat', dat('Known N64', payload))
        result = self.verify([big])
        self.assertEqual(result['summary']['reference_matches'], 3)
        self.assertEqual(result['summary']['files_checked'], 3)
        self.assertEqual(len(result['records']), 1)
        self.assertEqual(len(result['records']['m64:known']['sources']), 3)
        self.assertEqual({row['normalized_sha256'] for row in result['files']}, {hashlib.sha256(payload).hexdigest()})
        self.assertEqual(len({row['raw_sha256'] for row in result['files']}), 3)

    def test_bad_dump_tags_do_not_receive_verified_reference_status(self):
        payload = gb_rom()
        release = self.release(payload)
        self.write('reference/handheld/gb-tosec.dat', dat('Known bad [b1]', payload))
        result = self.verify([release])
        self.assertEqual(result['files'][0]['status'], 'needs-review')
        self.assertIn('bad dump', result['files'][0]['summary'])
        self.assertFalse(verifier.bad_reference({'name': 'Legitimate [beta]'}))

    def test_receipt_hash_mismatch_and_malformed_dat_do_not_trust_cached_identity(self):
        payload = gb_rom()
        release = self.release(payload)
        reference = self.write('reference/handheld/gb-no-intro.dat', dat('Known', payload))
        self.write('cache/handheld-metadata/index.json', json.dumps({
            'sources': [{'path': str(reference), 'sha256': '0' * 64, 'source_url': 'https://example.com/reference'}],
            'entries': {release['id']: {'identity_verified': True}},
        }).encode())
        result = self.verify([release])
        self.assertEqual(result['summary']['reference_matches'], 0)
        self.assertEqual(result['files'][0]['status'], 'unlisted')
        self.assertTrue(any('SHA-256' in error for error in result['reference_errors']))
        self.write('cache/handheld-metadata/index.json', b'[]')
        reference.write_bytes(b'not a DAT')
        result = self.verify([release])
        self.assertEqual(result['summary']['reference_matches'], 0)
        self.assertTrue(any('recognizable' in error for error in result['reference_errors']))
        self.assertEqual(Path(release['sources'][0]['path']).read_bytes(), payload)

    def test_source_changed_after_first_read_is_not_reported_as_verified(self):
        release = self.release(gb_rom())
        with patch('rom_verifier.digest_file', return_value='f' * 64):
            result = self.verify([release])
        self.assertEqual(result['files'][0]['status'], 'changed')
        self.assertFalse(result['files'][0]['source_preserved'])

    def test_outside_project_source_is_refused_without_reading_it(self):
        release = self.release(gb_rom())
        release['sources'][0]['path'] = '/etc/passwd'
        with self.assertRaisesRegex(ValueError, 'escapes'):
            self.verify([release])
        self.assertFalse((self.root / 'reports/ROM_VERIFICATION_REPORT.json').exists())

    def test_profile_scope_only_reads_selected_library(self):
        releases = [self.release(gb_rom()), self.release(n64_rom(), name='Own.z64', profile='m64')]
        result = self.verify(releases, 'm64')
        self.assertEqual(result['summary']['files_checked'], 1)
        self.assertEqual(result['files'][0]['profile'], 'm64')


if __name__ == '__main__':
    unittest.main()
