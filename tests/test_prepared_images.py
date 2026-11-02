"""Inventory tests use constructed MBR images and constructed mount directories."""
import contextlib
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import media
import prepared_images
import library_manager


ROOT = Path(__file__).resolve().parents[1]


class PreparedImageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="constructed-prepared-image-")
        self.base = Path(self.temporary.name).resolve()
        self.root = self.base / "library"
        self.image, self.receipt, self.manifest = self.make_image(self.root)
        self.errors = io.StringIO()
        self.redirect = contextlib.redirect_stderr(self.errors)
        self.redirect.__enter__()

    def tearDown(self):
        self.redirect.__exit__(None, None, None)
        self.temporary.cleanup()

    def make_image(self, root, profile="chromatic", build="Puzzle night build 2"):
        exports = root / "exports"
        pack = exports / build
        pack.mkdir(parents=True)
        image = exports / (build + ".img")
        raw = bytearray(8192)
        raw[510:512] = b"\x55\xaa"
        raw[450] = 0x0C
        raw[454:462] = struct.pack("<II", 1, len(raw) // 512 - 1)
        raw[523:525] = struct.pack("<H", 512)
        raw[594:602] = b"FAT32   "
        raw[1022:1024] = b"\x55\xaa"
        image.write_bytes(raw)
        manifest = {
            "profile": profile, "name": "Puzzle night", "build": build,
            "release_ids": [profile + ":" + "b" * 64, profile + ":" + "a" * 64],
            "files": [{"path": "Games/First.gb", "bytes": 8, "sha256": "a" * 64},
                      {"path": "Games/Second.gb", "bytes": 16, "sha256": "b" * 64}],
        }
        manifest_bytes = (json.dumps(manifest, indent=2) + "\n").encode()
        (pack / "MANIFEST.json").write_bytes(manifest_bytes)
        receipt = {
            "image_path": str(image), "pack_path": str(pack),
            "size_bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest(),
            "partition_start": 1, "partition_sectors": len(raw) // 512 - 1,
            "created_at": "2026-10-31T12:34:56+00:00",
            "file_count": len(manifest["files"]) + 1,
            "payload_bytes": len(manifest_bytes) + sum(record["bytes"] for record in manifest["files"]),
        }
        self.write_receipt(image, receipt)
        return image, receipt, manifest

    def write_receipt(self, image, receipt):
        image.with_suffix(".img.json").write_text(json.dumps(receipt))

    def constructed_mount(self, image=None, data=None):
        image = image or self.image
        mount = self.base / "constructed-mount"
        mount.mkdir(exist_ok=True)
        if data is None:
            data = (image.with_suffix("") / "MANIFEST.json").read_bytes()
        (mount / "MANIFEST.json").write_bytes(data)
        return mount

    def assert_skipped(self, root=None):
        result = prepared_images.list_images(root or self.root)
        self.assertEqual(result["images"], [])
        self.assertEqual(len(result["issues"]), 1)
        self.assertTrue(result["issues"][0]["message"])

    def test_list_retains_exact_profile_build_ids_and_never_hashes_or_mounts(self):
        with patch.object(media, "raw_image_info", side_effect=AssertionError("must not read image")), \
                patch.object(media, "sha256", side_effect=AssertionError("must not hash image")), \
                patch.object(media, "mounted_image", side_effect=AssertionError("must not mount image")):
            result = prepared_images.list_images(self.root)
        self.assertEqual(result["issues"], [])
        self.assertEqual(result["images"], [{
            "path": str(self.image), "name": self.manifest["name"],
            "profile": "chromatic", "release_ids": self.manifest["release_ids"],
            "game_count": 2, "size_bytes": self.receipt["size_bytes"],
            "sha256": self.receipt["sha256"], "created_at": self.receipt["created_at"],
            "verification": "at-build",
        }])
        m64_image, _, m64_manifest = self.make_image(self.root, profile="m64", build="M64 test")
        listed = {item["path"]: item for item in prepared_images.list_images(self.root)["images"]}
        self.assertEqual(listed[str(m64_image)]["profile"], "m64")
        self.assertEqual(listed[str(m64_image)]["release_ids"], m64_manifest["release_ids"])

    def test_reports_and_ordinary_unreceipted_images_are_not_inventory(self):
        self.image.with_suffix(".img.json").unlink()
        reports = self.root / "reports"
        reports.mkdir()
        (reports / "PREPARED_PACKS.json").write_text(json.dumps([self.receipt]))
        (reports / "IMAGE_VERIFICATION.json").write_text(json.dumps([{"verified": True, **self.receipt}]))
        self.assertEqual(prepared_images.list_images(self.root), {"images": [], "issues": []})
        with self.assertRaises((ValueError, OSError)):
            prepared_images.inspect_image(self.root, self.image)

    def test_inventory_orders_by_actual_creation_time_then_path(self):
        images = []
        for build, created_at in [
            ("A newest image", "2026-10-31T23:00:00Z"),
            ("B same instant", "2026-11-01T01:00:00+02:00"),
            ("Z older image", "2026-11-01T00:30:00+02:00"),
        ]:
            image, receipt, _ = self.make_image(self.root, build=build)
            self.write_receipt(image, {**receipt, "created_at": created_at})
            images.append(str(image))
        result = prepared_images.list_images(self.root)
        self.assertEqual(result["issues"], [])
        self.assertEqual([item["path"] for item in result["images"]], images + [str(self.image)])

    def test_cli_inventory_does_not_rescan_hash_or_mount(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), \
                patch.object(library_manager.Library, "scan", side_effect=AssertionError("must not scan")), \
                patch.object(media, "raw_image_info", side_effect=AssertionError("must not read image")), \
                patch.object(media, "sha256", side_effect=AssertionError("must not hash image")), \
                patch.object(media, "mounted_image", side_effect=AssertionError("must not mount image")):
            status = library_manager.main(["--root", str(self.root), "prepared-images"])
        self.assertEqual(status, 0)
        self.assertEqual(json.loads(output.getvalue()), prepared_images.list_images(self.root))

    def test_malformed_receipt_schemas_and_stale_facts_are_skipped(self):
        changes = [None, [], {"sha256": "invalid"}, {"sha256": []}, {"size_bytes": True},
                   {"size_bytes": 8192 + 512}, {"partition_start": 0}, {"partition_sectors": 100},
                   {"file_count": 1}, {"payload_bytes": 1}, {"created_at": "yesterday"},
                   {"created_at": "2026-10-31T12:34:56"},
                   {"image_path": str(self.root / "exports/other.img")},
                   {"pack_path": str(self.root / "exports/other")}]
        for change in changes:
            with self.subTest(change=change):
                value = {**self.receipt, **change} if isinstance(change, dict) else change
                self.write_receipt(self.image, value)
                self.assert_skipped()
        self.image.with_suffix(".img.json").write_text("{not JSON}")
        self.assert_skipped()

    def test_malformed_manifest_schema_profile_build_and_ids_are_skipped(self):
        changes = [None, [], {"profile": "other"}, {"profile": []}, {"name": ""},
                   {"build": "a different build"}, {"release_ids": []},
                   {"release_ids": ["m64:" + "a" * 64]},
                   {"release_ids": ["chromatic:wrong"]},
                   {"release_ids": ["chromatic:" + "a" * 64] * 2},
                   {"files": [{"path": "../escape", "bytes": 8, "sha256": "a" * 64}]},
                   {"files": [{"path": "/absolute", "bytes": 8, "sha256": "a" * 64}]},
                   {"files": [{"path": ".", "bytes": 8, "sha256": "a" * 64}]},
                   {"files": [{"path": "game.gb", "bytes": True, "sha256": "a" * 64}]},
                   {"files": [{"path": "game.gb", "bytes": 8, "sha256": "wrong"}]}]
        for change in changes:
            with self.subTest(change=change):
                value = {**self.manifest, **change} if isinstance(change, dict) else change
                (self.image.with_suffix("") / "MANIFEST.json").write_text(json.dumps(value))
                self.assert_skipped()

    def test_missing_image_manifest_or_pack_is_skipped(self):
        for missing in ("image", "manifest", "pack"):
            with self.subTest(missing=missing):
                root = self.base / missing
                image, _, _ = self.make_image(root)
                target = image if missing == "image" else image.with_suffix("") / "MANIFEST.json"
                target.unlink()
                if missing == "pack":
                    target.parent.rmdir()
                self.assert_skipped(root)

    def test_symlinked_receipt_image_manifest_pack_or_exports_cannot_escape(self):
        for kind in ("receipt", "image", "manifest", "pack", "exports"):
            with self.subTest(kind=kind):
                root = self.base / kind
                image, _, _ = self.make_image(root)
                target = {"receipt": image.with_suffix(".img.json"), "image": image,
                          "manifest": image.with_suffix("") / "MANIFEST.json",
                          "pack": image.with_suffix(""), "exports": image.parent}[kind]
                outside = self.base / ("outside-" + kind)
                target.rename(outside)
                target.symlink_to(outside, target_is_directory=outside.is_dir())
                self.assert_skipped(root)

    def test_same_size_changed_bytes_remain_at_build_and_fail_inspection(self):
        raw = bytearray(self.image.read_bytes())
        raw[-1] = 1
        self.image.write_bytes(raw)
        item = prepared_images.list_images(self.root)["images"][0]
        self.assertEqual(item["verification"], "at-build")
        self.assertNotIn("verified", item)
        with patch.object(media, "mounted_image", side_effect=AssertionError("must reject before mount")):
            with self.assertRaisesRegex(ValueError, "bytes no longer match"):
                prepared_images.inspect_image(self.root, self.image)

    def test_current_raw_mbr_is_checked_even_with_matching_receipt_hash(self):
        raw = bytearray(self.image.read_bytes())
        raw[450] = 0x07
        self.image.write_bytes(raw)
        self.write_receipt(self.image, {**self.receipt, "sha256": hashlib.sha256(raw).hexdigest()})
        with self.assertRaisesRegex(ValueError, "one FAT32"):
            prepared_images.inspect_image(self.root, self.image)

    def test_constructed_mount_inspection_binds_verified_path_hash_and_ids(self):
        mount = self.constructed_mount()
        with patch.object(media, "mounted_image", return_value=contextlib.nullcontext(mount)) as attach:
            result = prepared_images.inspect_image(self.root, Path("exports") / self.image.name)
        attach.assert_called_once_with(self.image, readonly=True)
        self.assertTrue(result["verified"])
        self.assertEqual(result["verification"], "current")
        self.assertEqual(result["path"], str(self.image))
        self.assertEqual(result["sha256"], media.sha256(self.image))
        self.assertEqual(result["release_ids"], self.manifest["release_ids"])
        self.assertEqual(result["profile"], self.manifest["profile"])
        self.assertTrue(result["verified_at"])
        self.assertTrue(all(json.loads(line)["event"] == "progress" for line in self.errors.getvalue().splitlines()))

    def test_constructed_mount_rejects_mutable_folder_certifying_different_ids(self):
        original = (self.image.with_suffix("") / "MANIFEST.json").read_bytes()
        changed = copy.deepcopy(self.manifest)
        changed["release_ids"][0] = "chromatic:" + "c" * 64
        (self.image.with_suffix("") / "MANIFEST.json").write_text(json.dumps(changed, indent=2) + "\n")
        self.assertEqual(prepared_images.list_images(self.root)["images"][0]["release_ids"], changed["release_ids"])
        mount = self.constructed_mount(data=original)
        with patch.object(media, "mounted_image", return_value=contextlib.nullcontext(mount)):
            with self.assertRaisesRegex(ValueError, "embedded manifest does not match"):
                prepared_images.inspect_image(self.root, self.image)

    def test_constructed_mount_requires_identical_manifest_bytes(self):
        mount = self.constructed_mount(data=json.dumps(self.manifest).encode())
        with patch.object(media, "mounted_image", return_value=contextlib.nullcontext(mount)):
            with self.assertRaisesRegex(ValueError, "embedded manifest does not match"):
                prepared_images.inspect_image(self.root, self.image)

    def test_constructed_mount_rejects_image_changes_even_if_mtime_is_restored(self):
        mount = self.constructed_mount()

        @contextlib.contextmanager
        def changing_image(*args, **kwargs):
            yield mount
            before = self.image.stat()
            with self.image.open("r+b") as stream:
                stream.seek(-1, 2)
                stream.write(b"X")
            os.utime(self.image, ns=(before.st_atime_ns, before.st_mtime_ns))

        with patch.object(media, "mounted_image", side_effect=changing_image):
            with self.assertRaisesRegex(ValueError, "changed during inspection"):
                prepared_images.inspect_image(self.root, self.image)

    def test_constructed_mount_rejects_identity_record_changes_during_inspection(self):
        mount = self.constructed_mount()

        @contextlib.contextmanager
        def changing_receipt(*args, **kwargs):
            yield mount
            self.write_receipt(self.image, {**self.receipt, "created_at": "2026-10-31T12:34:57+00:00"})

        with patch.object(media, "mounted_image", side_effect=changing_receipt):
            with self.assertRaisesRegex(ValueError, "changed during inspection"):
                prepared_images.inspect_image(self.root, self.image)


if __name__ == "__main__":
    unittest.main()
