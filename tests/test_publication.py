import hashlib
import importlib.util
import io
import json
import plistlib
from pathlib import Path
import stat
import subprocess
import tarfile
import tempfile
import unittest
import zipfile


SPEC = importlib.util.spec_from_file_location('publication', Path(__file__).resolve().parents[1] / 'tools/check_publication.py')
publication = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(publication)


class PublicationBehavior(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='publication-fixture-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.manifest = {'schema': 1, 'files': {
            'source-manifest.json': {'kind': 'text'}, 'README.md': {'kind': 'text'},
            'example.py': {'kind': 'text'}, 'NOTICE.txt': {'kind': 'text'}},
            'required_files': ['source-manifest.json', 'README.md', 'NOTICE.txt']}
        self.write('README.md', b'An authored fixture.\n')
        self.write('NOTICE.txt', b'Fixture notices.\n')
        self.save_manifest()

    def write(self, name, data):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def save_manifest(self):
        self.write('source-manifest.json', json.dumps(self.manifest).encode())

    def run_git(self, *args):
        return subprocess.run(['git', '-C', str(self.root), *args], capture_output=True, check=True).stdout

    def commit(self, message):
        self.run_git('add', '--all')
        self.run_git('-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '-m', message)

    def test_authored_source_and_exact_noticed_binary_are_accepted(self):
        data = b'reviewed asset bytes\x00'
        self.manifest['files']['font.ttf'] = {'kind': 'asset', 'sha256': hashlib.sha256(data).hexdigest(), 'notice': 'NOTICE.txt'}
        self.save_manifest()
        self.write('font.ttf', data)
        self.assertEqual(publication.check_worktree(self.root, publication.load_manifest(self.root)), 4)

    def test_untracked_payloads_are_rejected_even_when_git_ignores_them(self):
        self.manifest['files']['.gitignore'] = {'kind': 'text'}
        self.save_manifest()
        self.write('.gitignore', b'library/\n*.srm\n')
        self.write('library/catalog.json', b'{}')
        self.run_git('init', '-b', 'main')
        with self.assertRaisesRegex(publication.PublicationError, 'Private data directory'):
            publication.check_worktree(self.root, self.manifest)

    def test_rom_and_save_names_cannot_be_allowlisted_as_assets(self):
        for name in ['game.gb', 'save.srm', 'os.bin', 'card.img', 'backup.zip']:
            with self.subTest(name=name):
                with self.assertRaises(publication.PublicationError):
                    publication.check_record(name, b'bytes', {'files': {name: {'kind': 'asset', 'sha256': hashlib.sha256(b'bytes').hexdigest()}}})

    def test_binary_renamed_to_source_is_rejected(self):
        self.write('example.py', b'\x00\xff\x80rom-fixture')
        with self.assertRaisesRegex(publication.PublicationError, 'binary'):
            publication.check_worktree(self.root, self.manifest)

    def test_private_absolute_paths_and_credentials_are_rejected(self):
        values = [b'/' + b'Users/private-person/game', b'/Volumes/' + b'Storage/library',
                  b'rom' + b'Project', b'-----BEGIN ' + b'PRIVATE KEY-----', b'ghp_' + b'a' * 32]
        for data in values:
            with self.subTest(data=data):
                self.write('example.py', data)
                with self.assertRaisesRegex(publication.PublicationError, 'Private paths or credentials'):
                    publication.check_worktree(self.root, self.manifest)

    def test_generated_filename_strings_are_permitted_inside_source(self):
        self.write('example.py', b"name = 'demo.gb'\nbackup = 'archive.img'\n")
        self.assertEqual(publication.check_worktree(self.root, self.manifest), 4)

    def test_unlisted_text_symlinks_and_changed_assets_fail(self):
        self.write('unlisted.txt', b'unknown')
        with self.assertRaisesRegex(publication.PublicationError, 'allowlist'):
            publication.check_worktree(self.root, self.manifest)
        (self.root / 'unlisted.txt').unlink()
        (self.root / 'example.py').symlink_to(self.root / 'README.md')
        with self.assertRaisesRegex(publication.PublicationError, 'Linked'):
            publication.check_worktree(self.root, self.manifest)
        (self.root / 'example.py').unlink()
        self.manifest['files']['font.ttf'] = {'kind': 'asset', 'sha256': hashlib.sha256(b'old').hexdigest(), 'notice': 'NOTICE.txt'}
        self.write('font.ttf', b'changed')
        with self.assertRaisesRegex(publication.PublicationError, 'asset bytes changed'):
            publication.check_worktree(self.root, self.manifest)

    def test_historical_private_blob_is_rejected_after_worktree_cleanup(self):
        self.run_git('init', '-b', 'main')
        self.write('example.py', b'/' + b'Users/private-person/data')
        self.commit('Private path fixture')
        self.write('example.py', b'clean = True\n')
        self.commit('Clean current fixture')
        publication.check_worktree(self.root, self.manifest)
        with self.assertRaisesRegex(publication.PublicationError, 'Private paths or credentials'):
            publication.check_git(self.root, self.manifest)

    def test_staged_symlink_is_rejected(self):
        self.run_git('init', '-b', 'main')
        (self.root / 'example.py').symlink_to('README.md')
        self.run_git('add', 'example.py')
        with self.assertRaisesRegex(publication.PublicationError, 'Linked or special'):
            publication.check_git(self.root, self.manifest)

    def test_clean_commit_and_git_archive_are_accepted(self):
        self.run_git('init', '-b', 'main')
        self.commit('Public fixture')
        result = publication.check_git(self.root, self.manifest, require_clean=True)
        self.assertEqual(result['commits'], 1)
        self.assertEqual(result['history_blobs'], 3)
        path = self.root.parent / (self.root.name + '.tar')
        self.addCleanup(lambda: path.unlink(missing_ok=True))
        path.write_bytes(self.run_git('archive', '--prefix=CartShelf-fixture/', 'HEAD'))
        self.assertEqual(publication.check_source_archive(path, self.manifest), 3)
        self.write('example.py', b'new = True\n')
        with self.assertRaisesRegex(publication.PublicationError, 'uncommitted'):
            publication.check_git(self.root, self.manifest, require_clean=True)

    def test_archive_traversal_and_links_are_rejected_without_extraction(self):
        path = self.root / 'bad.tar'
        with tarfile.open(path, 'w') as archive:
            for name, data in [('source-manifest.json', json.dumps(self.manifest).encode()), ('../example.py', b'bad')]:
                item = tarfile.TarInfo(name)
                item.size = len(data)
                archive.addfile(item, io.BytesIO(data))
        with self.assertRaisesRegex(publication.PublicationError, 'unsafe'):
            publication.check_source_archive(path, self.manifest)
        with tarfile.open(path, 'w') as archive:
            item = tarfile.TarInfo('example.py')
            item.type = tarfile.SYMTYPE
            item.linkname = 'README.md'
            archive.addfile(item)
        with self.assertRaisesRegex(publication.PublicationError, 'linked'):
            publication.check_source_archive(path, self.manifest)

    def test_zip_duplicate_and_mismatched_manifest_are_rejected(self):
        path = self.root / 'bad.zip'
        with zipfile.ZipFile(path, 'w') as archive:
            archive.writestr('source-manifest.json', json.dumps(self.manifest))
            archive.writestr('README.md', 'one')
            archive.writestr('README.md', 'two')
        with self.assertRaisesRegex(publication.PublicationError, 'duplicate'):
            publication.check_source_archive(path, self.manifest)
        with zipfile.ZipFile(path, 'w') as archive:
            archive.writestr('source-manifest.json', '{}')
        with self.assertRaisesRegex(publication.PublicationError, 'allowlist differs'):
            publication.check_source_archive(path, self.manifest)

    def test_archive_prefix_cannot_hide_traversal(self):
        path = self.root / 'bad-prefix.tar'
        with tarfile.open(path, 'w') as archive:
            for name in ['source-manifest.json', 'README.md', 'NOTICE.txt']:
                data = (self.root / name).read_bytes()
                item = tarfile.TarInfo('../' + name)
                item.size = len(data)
                archive.addfile(item, io.BytesIO(data))
        with self.assertRaisesRegex(publication.PublicationError, 'unsafe'):
            publication.check_source_archive(path, self.manifest)

    def test_missing_required_source_is_rejected(self):
        (self.root / 'README.md').unlink()
        with self.assertRaisesRegex(publication.PublicationError, 'Required source files are missing'):
            publication.check_worktree(self.root, self.manifest)

    def app_fixture(self):
        app = self.root / 'build/CartShelf.app'
        resources = app / 'Contents/Resources'
        resources.mkdir(parents=True)
        release = {'schema': 1, 'version': '1.0.0-beta.1', 'bundle_version': '1.0.0',
                   'build': 7, 'channel': 'beta', 'minimum_macos': '14.0',
                   'bundle_id': 'dev.cartshelf.fixture'}
        self.write('release.json', json.dumps(release).encode())
        native_source = b'// Authored native source fixture.\n'
        self.write('app/CartShelf.swift', native_source)
        packaged_release = {'schema': 1, 'version': release['version'], 'build': release['build'],
                            'minimum_macos': release['minimum_macos'],
                            'native_source_sha256': hashlib.sha256(native_source).hexdigest()}
        (resources / 'release-manifest.json').write_text(json.dumps(packaged_release))
        info = {'CartShelfCoreRelativePath': 'core', 'CartShelfPythonRelativePath': 'python/bin/python3',
                'CFBundleIdentifier': release['bundle_id'], 'CFBundleShortVersionString': release['bundle_version'],
                'CFBundleVersion': str(release['build']), 'CartShelfVersion': release['version'],
                'CartShelfReleaseChannel': release['channel'], 'LSMinimumSystemVersion': release['minimum_macos']}
        (app / 'Contents/Info.plist').write_bytes(plistlib.dumps(info))
        core = resources / 'core'
        core.mkdir()
        rows = []
        for name in ['library_manager.py', 'media.py', 'card_saves.py', 'library_experience.py']:
            data = b'fixture = True\n'
            self.manifest['files'][name] = {'kind': 'text'}
            self.write(name, data)
            (core / name).write_bytes(data)
            rows.append({'path': name, 'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()})
        (resources / 'core-manifest.json').write_text(json.dumps({'schema': 1, 'files': rows}))
        runtime = resources / 'python'
        (runtime / 'bin').mkdir(parents=True)
        data = b'fixture runtime bytes'
        (runtime / 'bin/python3.13').write_bytes(data)
        (runtime / 'bin/python3').symlink_to('python3.13')
        runtime_manifest = {'schema': 1, 'files': [{'path': 'bin/python3.13', 'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()}],
                            'symlinks': [{'path': 'bin/python3', 'target': 'python3.13'}]}
        (resources / 'runtime-manifest.json').write_text(json.dumps(runtime_manifest))
        return app, resources

    def test_app_resource_and_runtime_inventory_are_checked(self):
        app, resources = self.app_fixture()
        result = publication.check_app(app, self.root, self.manifest)
        self.assertEqual(result, {'core_files': 4, 'runtime_entries': 2})
        (resources / 'unexpected.srm').write_bytes(b'private save fixture')
        with self.assertRaisesRegex(publication.PublicationError, 'unreviewed outer app resource'):
            publication.check_app(app, self.root, self.manifest)

    def test_app_accepts_builder_font_and_notice_source_locations(self):
        app, resources = self.app_fixture()
        sources = {
            'Silkscreen-Regular.ttf': 'app/Resources/Silkscreen-Regular.ttf',
            'Silkscreen-Bold.ttf': 'app/Resources/Silkscreen-Bold.ttf',
            'OFL.txt': 'app/Resources/OFL.txt',
            'LICENSE': 'LICENSE',
            'THIRD_PARTY_NOTICES.md': 'THIRD_PARTY_NOTICES.md',
            'LICENSE-ASSETS.md': 'themes/CartShelf-64/LICENSE-ASSETS.md',
        }
        for name, origin in sources.items():
            data = ('Authored fixture for ' + origin).encode()
            self.write(origin, data)
            (resources / name).write_bytes(data)
        self.assertFalse((self.root / 'app/Resources/LICENSE').exists())
        self.assertEqual(publication.check_app(app, self.root, self.manifest),
                         {'core_files': 4, 'runtime_entries': 2})
        for name, origin in sources.items():
            with self.subTest(resource=name):
                original = (resources / name).read_bytes()
                (resources / name).write_bytes(b'changed outer resource')
                with self.assertRaisesRegex(publication.PublicationError, 'font or license differs'):
                    publication.check_app(app, self.root, self.manifest)
                (resources / name).write_bytes(original)

    def test_app_notice_cannot_match_an_unrelated_resource_directory_copy(self):
        app, resources = self.app_fixture()
        self.write('LICENSE', b'Correct root license')
        self.write('app/Resources/LICENSE', b'Unrelated misplaced copy')
        (resources / 'LICENSE').write_bytes(b'Unrelated misplaced copy')
        with self.assertRaisesRegex(publication.PublicationError, 'font or license differs'):
            publication.check_app(app, self.root, self.manifest)

    def test_app_missing_reviewed_notice_is_a_publication_error(self):
        app, resources = self.app_fixture()
        (resources / 'LICENSE').write_bytes(b'No reviewed origin')
        with self.assertRaisesRegex(publication.PublicationError, 'reviewed source'):
            publication.check_app(app, self.root, self.manifest)

    def test_app_rejects_qa_library_bindings_and_changed_runtime_bytes(self):
        app, resources = self.app_fixture()
        info_path = app / 'Contents/Info.plist'
        info = plistlib.loads(info_path.read_bytes())
        info['CartShelfQABuild'] = True
        info_path.write_bytes(plistlib.dumps(info))
        with self.assertRaisesRegex(publication.PublicationError, 'bound to a local library'):
            publication.check_app(app, self.root, self.manifest)
        info['CartShelfQABuild'] = False
        info_path.write_bytes(plistlib.dumps(info))
        (resources / 'python/bin/python3.13').write_bytes(b'changed')
        with self.assertRaisesRegex(publication.PublicationError, 'Runtime bytes differ'):
            publication.check_app(app, self.root, self.manifest)

    def test_app_rejects_native_source_changed_since_build(self):
        app, _ = self.app_fixture()
        self.write('app/CartShelf.swift', b'Changed native source after packaging.\n')
        with self.assertRaisesRegex(publication.PublicationError, 'native source'):
            publication.check_app(app, self.root, self.manifest)

    def test_app_rejects_release_manifest_drift(self):
        app, resources = self.app_fixture()
        path = resources / 'release-manifest.json'
        original = json.loads(path.read_text())
        for field, value in [('version', '2.0.0'), ('build', 8), ('minimum_macos', '15.0')]:
            with self.subTest(field=field):
                path.write_text(json.dumps({**original, field: value}))
                with self.assertRaisesRegex(publication.PublicationError, 'release metadata'):
                    publication.check_app(app, self.root, self.manifest)

    def test_app_rejects_info_release_metadata_drift(self):
        app, _ = self.app_fixture()
        path = app / 'Contents/Info.plist'
        original = plistlib.loads(path.read_bytes())
        for field, value in [('CFBundleIdentifier', 'dev.other.app'), ('CFBundleShortVersionString', '2.0.0'),
                             ('CFBundleVersion', '8'), ('CartShelfVersion', '2.0.0-beta.1'),
                             ('CartShelfReleaseChannel', 'stable'), ('LSMinimumSystemVersion', '15.0')]:
            with self.subTest(field=field):
                path.write_bytes(plistlib.dumps({**original, field: value}))
                with self.assertRaisesRegex(publication.PublicationError, 'property-list release metadata'):
                    publication.check_app(app, self.root, self.manifest)

    def test_app_uses_reviewed_release_as_authority(self):
        app, _ = self.app_fixture()
        release = json.loads((self.root / 'release.json').read_text())
        self.write('release.json', json.dumps({**release, 'build': 8}).encode())
        with self.assertRaisesRegex(publication.PublicationError, 'release metadata'):
            publication.check_app(app, self.root, self.manifest)

    def test_app_rejects_runtime_links_outside_the_bundle(self):
        app, resources = self.app_fixture()
        path = resources / 'python/bin/python3'
        path.unlink()
        path.symlink_to('../../../outside')
        with self.assertRaisesRegex(publication.PublicationError, 'Runtime link escapes'):
            publication.check_app(app, self.root, self.manifest)

    def test_packaged_app_archive_is_inspected_after_safe_extraction(self):
        app, resources = self.app_fixture()
        archive = self.root / 'candidate.zip'
        with zipfile.ZipFile(archive, 'w') as output:
            for path in app.rglob('*'):
                name = path.relative_to(app.parent).as_posix()
                if path.is_symlink():
                    item = zipfile.ZipInfo(name)
                    item.create_system = 3
                    item.external_attr = (stat.S_IFLNK | 0o777) << 16
                    output.writestr(item, path.readlink().as_posix())
                elif path.is_file():
                    output.write(path, name)
        self.assertEqual(publication.check_app_archive(archive, self.root, self.manifest)['runtime_entries'], 2)
        with zipfile.ZipFile(archive, 'a') as output:
            output.writestr('outside.txt', 'unreviewed')
        with self.assertRaisesRegex(publication.PublicationError, 'one app and no extra'):
            publication.check_app_archive(archive, self.root, self.manifest)

    def test_app_archive_rejects_traversal_before_writing(self):
        archive = self.root / 'bad-app.zip'
        with zipfile.ZipFile(archive, 'w') as output:
            output.writestr('../CartShelf.app/Contents/Info.plist', 'bad')
        with self.assertRaisesRegex(publication.PublicationError, 'unsafe'):
            publication.check_app_archive(archive, self.root, self.manifest)

    def test_host_dependency_pins_match_packaged_runtime(self):
        project = Path(__file__).resolve().parents[1]
        lock = json.loads((project / 'app/runtime-lock.json').read_text())
        expected = {row['name'].casefold(): row['version'] for row in lock['dependencies']}
        actual = {}
        for line in (project / 'requirements-dev.txt').read_text().splitlines():
            line = line.strip()
            if line and not line.startswith('#'):
                name, version = line.split('==')
                actual[name.casefold()] = version
        self.assertEqual(actual, expected)


if __name__ == '__main__':
    unittest.main()
