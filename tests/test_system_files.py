import contextlib
from dataclasses import replace
import errno
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest import mock
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from library_manager import Library
import library_experience as experience
import system_files


class SystemFilesTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="cartshelf-system-files-")
        self.base = Path(self.temporary.name).resolve()
        self.root = self.base / "library"
        self.root.mkdir()
        self.library = Library(self.root)
        self.payloads = {"chromatic": b"Authored stock OS fixture.\n" * 31,
                         "m64": b"Authored updater fixture.\n" * 79}
        self.specs = {profile: replace(spec, size=len(self.payloads[profile]),
                                      sha256=hashlib.sha256(self.payloads[profile]).hexdigest())
                      for profile, spec in system_files.SPECS.items()}
        self.pin_patch = mock.patch.object(system_files, "SPECS", self.specs)
        self.pin_patch.start()
        self.addCleanup(self.pin_patch.stop)

    def tearDown(self):
        self.temporary.cleanup()

    def source(self, profile, name=None, data=None):
        file = self.base / (name or self.specs[profile].filename)
        file.write_bytes(self.payloads[profile] if data is None else data)
        return file

    def archive(self, entries, name="system.zip"):
        file = self.base / name
        with zipfile.ZipFile(file, "w", zipfile.ZIP_DEFLATED) as archive:
            for path, data in entries:
                archive.writestr(path, data)
        return file

    def binary(self, profile):
        return self.root / self.specs[profile].binary

    def receipt(self, profile):
        return self.root / self.specs[profile].provenance

    def test_missing_status_does_not_create_cache(self):
        for profile in self.specs:
            result = system_files.status(self.library, profile)
            self.assertEqual(result["status"], "missing")
            self.assertEqual(result["version"], self.specs[profile].version)
            self.assertNotIn("path", result)
            self.assertNotIn("installed_version", result)
        self.assertFalse((self.root / "cache").exists())

    def test_raw_import_publishes_exact_data_and_provenance_for_both_profiles(self):
        for profile in self.specs:
            result = system_files.import_file(self.library, profile, self.source(profile))
            self.assertEqual(result["status"], "ready")
            self.assertEqual(self.binary(profile).read_bytes(), self.payloads[profile])
            receipt = json.loads(self.receipt(profile).read_text())
            self.assertEqual(receipt["sha256"], self.specs[profile].sha256)
            self.assertEqual(receipt["profile"], profile)
            self.assertEqual(receipt["import_method"], "user-selected offline file")
            self.assertNotIn("source_path", receipt)
            self.assertFalse(list((self.root / "cache/firmware").glob(".system-file-*")))

    def test_gb_import_cache_is_consumable_by_library(self):
        system_files.import_file(self.library, "chromatic", self.source("chromatic"))
        data, sha = self.library._handheld_os()
        self.assertEqual(data, self.payloads["chromatic"])
        self.assertEqual(sha, self.specs["chromatic"].sha256)

    def test_existing_uppercase_hash_provenance_remains_compatible(self):
        source = self.source("chromatic")
        system_files.import_file(self.library, "chromatic", source)
        self.receipt("chromatic").write_text(json.dumps({"sha256": self.specs["chromatic"].sha256.upper()}))
        before = self.receipt("chromatic").read_bytes()
        self.assertEqual(system_files.status(self.library, "chromatic")["status"], "ready")
        system_files.import_file(self.library, "chromatic", source)
        self.assertEqual(self.receipt("chromatic").read_bytes(), before)

    def test_n64_import_is_found_by_existing_pack_builder(self):
        system_files.import_file(self.library, "m64", self.source("m64"))
        from library_manager import N64
        self.assertEqual(N64.find_cached_firmware(self.root / "cache/firmware"), self.binary("m64"))

    def test_zip_import_selects_only_required_file(self):
        file = self.archive([("GBCSYS/GBCOS.BIN", self.payloads["chromatic"]),
                             ("readme.txt", b"Authored fixture, not a vendor download."),
                             ("unrelated.bin", b"Do not extract me.")])
        result = system_files.import_file(self.library, "chromatic", file)
        self.assertEqual(result["status"], "ready")
        self.assertFalse((self.root / "cache/firmware/gb-x7/readme.txt").exists())
        self.assertFalse(list(self.root.rglob("unrelated.bin")))

    def test_duplicate_or_missing_zip_member_fails_without_cache(self):
        for index, entries in enumerate([
                [("readme.txt", b"No system file")],
                [("GBCSYS/GBCOS.BIN", self.payloads["chromatic"]),
                 ("copy/GBCOS.BIN", self.payloads["chromatic"])]]):
            with self.assertRaisesRegex(ValueError, "exactly one"):
                system_files.import_file(self.library, "chromatic", self.archive(entries, str(index) + ".zip"))
        self.assertFalse((self.root / "cache").exists())

    def test_zip_traversal_absolute_and_symlink_entries_fail(self):
        for index, name in enumerate(["../GBCOS.BIN", "/GBCOS.BIN", "C:/GBCOS.BIN", "GBCSYS\\GBCOS.BIN"]):
            with self.assertRaisesRegex(ValueError, "unsupported path"):
                system_files.import_file(self.library, "chromatic",
                                         self.archive([(name, self.payloads["chromatic"])], str(index) + ".zip"))
        file = self.base / "link.zip"
        info = zipfile.ZipInfo("GBCSYS/GBCOS.BIN")
        info.create_system = 3
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        with zipfile.ZipFile(file, "w") as archive:
            archive.writestr(info, "elsewhere")
        with self.assertRaisesRegex(ValueError, "unsupported path"):
            system_files.import_file(self.library, "chromatic", file)
        self.assertFalse((self.root / "cache").exists())

    def test_wrong_bytes_and_wrong_version_never_replace_known_cache(self):
        source = self.source("chromatic")
        system_files.import_file(self.library, "chromatic", source)
        before = self.binary("chromatic").read_bytes(), self.receipt("chromatic").read_bytes()
        source.write_bytes(b"A different system file")
        with self.assertRaisesRegex(ValueError, "does not match"):
            system_files.import_file(self.library, "chromatic", source)
        self.assertEqual(before, (self.binary("chromatic").read_bytes(), self.receipt("chromatic").read_bytes()))

    def test_changed_cache_and_provenance_are_not_overwritten(self):
        source = self.source("m64")
        system_files.import_file(self.library, "m64", source)
        self.binary("m64").write_bytes(b"Keep these changed bytes")
        with self.assertRaises(ValueError):
            system_files.import_file(self.library, "m64", source)
        self.assertEqual(self.binary("m64").read_bytes(), b"Keep these changed bytes")
        self.assertEqual(system_files.status(self.library, "m64")["status"], "changed")
        self.binary("m64").write_bytes(self.payloads["m64"])
        self.receipt("m64").write_text('{"sha256":"changed"}')
        with self.assertRaisesRegex(ValueError, "provenance changed"):
            system_files.import_file(self.library, "m64", source)
        self.assertEqual(self.receipt("m64").read_text(), '{"sha256":"changed"}')

    def test_idempotent_import_keeps_binary_and_receipt_bytes_and_inodes(self):
        source = self.source("chromatic")
        system_files.import_file(self.library, "chromatic", source)
        before = [(path.read_bytes(), path.stat().st_ino) for path in [self.binary("chromatic"), self.receipt("chromatic")]]
        system_files.import_file(self.library, "chromatic", source)
        self.assertEqual(before, [(path.read_bytes(), path.stat().st_ino) for path in [self.binary("chromatic"), self.receipt("chromatic")]])

    def test_known_missing_partner_can_complete_without_replacing_existing_data(self):
        source = self.source("chromatic")
        system_files.import_file(self.library, "chromatic", source)
        self.binary("chromatic").unlink()
        receipt = self.receipt("chromatic").read_bytes()
        self.assertEqual(system_files.status(self.library, "chromatic")["status"], "changed")
        system_files.import_file(self.library, "chromatic", source)
        self.assertEqual(self.receipt("chromatic").read_bytes(), receipt)
        self.receipt("chromatic").unlink()
        inode = self.binary("chromatic").stat().st_ino
        system_files.import_file(self.library, "chromatic", source)
        self.assertEqual(self.binary("chromatic").stat().st_ino, inode)

    def test_input_and_cache_symlinks_are_rejected(self):
        file = self.source("m64")
        alias = self.base / "alias.efu"
        alias.symlink_to(file)
        with self.assertRaisesRegex(ValueError, "symbolic link"):
            system_files.import_file(self.library, "m64", alias)
        outside = self.base / "outside"
        outside.mkdir()
        (self.root / "cache").symlink_to(outside)
        with self.assertRaises(ValueError):
            system_files.import_file(self.library, "m64", file)
        self.assertEqual(list(outside.iterdir()), [])

    def test_changed_root_stops_publication(self):
        source = self.source("chromatic")
        actual_write = system_files._write
        moved = self.base / "old-library"

        def replaced_root(path, data):
            actual_write(path, data)
            if path.name == "source.json":
                self.root.rename(moved)
                self.root.mkdir()

        with mock.patch.object(system_files, "_write", side_effect=replaced_root):
            with self.assertRaisesRegex(ValueError, "selected library changed"):
                system_files.import_file(self.library, "chromatic", source)
        self.assertFalse((self.root / "cache").exists())
        self.assertFalse((moved / self.specs["chromatic"].binary).exists())

    def test_concurrent_different_destination_is_not_overwritten(self):
        source = self.source("m64")
        actual_link = os.link

        def other_writer(staged, target):
            if Path(staged).name == "binary":
                Path(target).write_bytes(b"Keep a concurrent writer's bytes")
            return actual_link(staged, target)

        with mock.patch.object(system_files.os, "link", side_effect=other_writer):
            with self.assertRaisesRegex(ValueError, "does not match"):
                system_files.import_file(self.library, "m64", source)
        self.assertEqual(self.binary("m64").read_bytes(), b"Keep a concurrent writer's bytes")

    @unittest.skipUnless(sys.platform == "darwin", "Mac exclusive rename fallback")
    def test_mac_import_can_publish_without_hard_link_support(self):
        source = self.source("chromatic")
        with mock.patch.object(system_files.os, "link", side_effect=OSError(errno.ENOTSUP, "Fixture filesystem has no hard links")):
            result = system_files.import_file(self.library, "chromatic", source)
        self.assertEqual(result["status"], "ready")
        self.assertEqual(self.binary("chromatic").read_bytes(), self.payloads["chromatic"])

    @unittest.skipUnless(sys.platform == "darwin", "Mac exclusive rename fallback")
    def test_mac_exclusive_rename_does_not_replace_a_destination(self):
        source = self.base / "staged"
        destination = self.base / "existing"
        source.write_bytes(b"new")
        destination.write_bytes(b"keep")
        with mock.patch.object(system_files.os, "link", side_effect=OSError(errno.ENOTSUP, "Fixture filesystem has no hard links")):
            with self.assertRaises(FileExistsError):
                system_files._publish(source, destination)
        self.assertEqual(destination.read_bytes(), b"keep")
        self.assertEqual(source.read_bytes(), b"new")

    def test_invalid_profile_and_malformed_receipt_fail_safely(self):
        with self.assertRaisesRegex(ValueError, "Choose M64"):
            system_files.status(self.library, "other")
        source = self.source("chromatic")
        system_files.import_file(self.library, "chromatic", source)
        self.receipt("chromatic").write_text("not JSON")
        self.assertEqual(system_files.status(self.library, "chromatic")["status"], "changed")
        with self.assertRaisesRegex(ValueError, "provenance is unreadable"):
            system_files.import_file(self.library, "chromatic", source)

    def test_oversized_input_and_zip_entry_count_are_bounded(self):
        file = self.source("m64", data=b"x" * 128)
        with mock.patch.object(system_files, "MAX_INPUT_BYTES", 64):
            with self.assertRaisesRegex(ValueError, "bounded regular"):
                system_files.import_file(self.library, "m64", file)
        file = self.archive([("readme.txt", b""), ("GBCOS.BIN", self.payloads["chromatic"])])
        with mock.patch.object(system_files, "MAX_ZIP_ENTRIES", 1):
            with self.assertRaisesRegex(ValueError, "too many"):
                system_files.import_file(self.library, "chromatic", file)

    def test_cli_status_and_import_return_compact_json(self):
        for arguments, expected in [(["system-files-status", "--profile", "chromatic"], "missing"),
                                    (["system-files-import", "--profile", "chromatic", "--path", str(self.source("chromatic"))], "ready")]:
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                self.assertEqual(experience.main(["--root", str(self.root)] + arguments), 0, err.getvalue())
            result = json.loads(out.getvalue())
            self.assertEqual(result["status"], expected)
            self.assertEqual(result["profile"], "chromatic")
            self.assertEqual(result["sha256"], self.specs["chromatic"].sha256)


if __name__ == "__main__":
    unittest.main()
