"""Public fixture checks for the trusted packaged worker and runtime artifacts."""
import hashlib
import importlib.util
import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path


APP = Path(__file__).resolve().parents[1] / 'app'


def module(name, filename):
    spec = importlib.util.spec_from_file_location(name, APP / filename)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


runtime = module('cartshelf_test_runtime', 'runtime.py')
bundle = module('cartshelf_test_media_bundle', 'media_bundle.py')
verifier = module('cartshelf_test_runtime_verifier', 'verify_runtime.py')


class PackageBoundaryTests(unittest.TestCase):
    def test_worker_rejects_an_unfrozen_project_import(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'card_saves.py').write_text('import library_manager\n')
            (root / 'media.py').write_text('import card_saves\n')
            (root / 'library_manager.py').write_text("raise RuntimeError('must never run')\n")
            with self.assertRaisesRegex(ValueError, 'not frozen'):
                bundle.media_source(root)

    def test_frozen_worker_does_not_import_a_neighboring_save_module(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'card_saves.py').write_text("value='frozen fixture'\n")
            (root / 'media.py').write_text('import card_saves\nresult=card_saves.value\n')
            source, hashes = bundle.media_source(root)
            (root / 'card_saves.py').write_text("raise RuntimeError('mutated source must not run')\n")
            previous_main = sys.modules['__main__']
            previous_saves = sys.modules.get('card_saves')
            try:
                exec(compile(source, 'FixtureWorker', 'exec'), {})
                self.assertEqual(sys.modules['__main__'].result, 'frozen fixture')
                self.assertEqual(hashes['card_saves.py'], hashlib.sha256(b"value='frozen fixture'\n").hexdigest())
            finally:
                sys.modules['__main__'] = previous_main
                if previous_saves is None:
                    sys.modules.pop('card_saves', None)
                else:
                    sys.modules['card_saves'] = previous_saves

    def test_runtime_wheel_path_cannot_escape_the_installation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            wheel = root / 'fixture.whl'
            with zipfile.ZipFile(wheel, 'w') as output:
                output.writestr('../escaped.txt', 'generated malicious-path fixture')
            with self.assertRaisesRegex(ValueError, 'unsafe path'):
                runtime.wheel_contents(wheel, root / 'site-packages')
            self.assertFalse((root / 'escaped.txt').exists())

    def test_runtime_inventory_rejects_an_external_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            installed = root / 'runtime'
            installed.mkdir()
            (root / 'outside.txt').write_text('generated fixture')
            (installed / 'escape').symlink_to('../outside.txt')
            with self.assertRaisesRegex(ValueError, 'outside'):
                runtime.tree_manifest(installed)

    def test_locked_dependency_versions_match_developer_requirements(self):
        lock = json.loads((APP / 'runtime-lock.json').read_text())
        expected = {row['name'].lower(): row['version'] for row in lock['dependencies']}
        actual = dict(line.split('==') for line in (APP.parent / 'requirements-dev.txt').read_text().splitlines() if line)
        self.assertEqual({name.lower(): version for name, version in actual.items()}, expected)

    def test_verification_library_cannot_be_created_in_the_checkout(self):
        with tempfile.TemporaryDirectory() as directory:
            app = Path(directory) / 'Fixture.app'
            app.mkdir()
            with self.assertRaisesRegex(ValueError, 'outside the source checkout'):
                verifier.verify(app, tls=False, work_dir=APP.parent / 'build')


if __name__ == '__main__':
    unittest.main()
