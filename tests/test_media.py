import contextlib
import io
import array
import os
import socket
import json
from pathlib import Path
import struct
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import media
from card_saves import validate_choices


ROOT = Path(__file__).resolve().parents[1]


def fake_raw_image(path, size=8192):
    data = bytearray(size)
    data[510:512] = b"\x55\xaa"
    data[450] = 0x0C
    data[454:462] = struct.pack("<II", 1, size // 512 - 1)
    data[512 + 11:512 + 13] = struct.pack("<H", 512)
    data[512 + 82:512 + 90] = b"FAT32   "
    data[512 + 510:1024] = b"\x55\xaa"
    path.write_bytes(data)
    return data


class MediaTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="media-test-")
        self.root = Path(self.temporary.name).resolve()
        self.fixture_os = b'authored test OS'
        for target, value in [('GB_X7_OS_BYTES', len(self.fixture_os)),
                              ('GB_X7_OS_SHA256', {__import__('hashlib').sha256(self.fixture_os).hexdigest()})]:
            mocked = patch.object(media, target, value)
            mocked.start()
            self.addCleanup(mocked.stop)
        self.reviewed_saves = {'current': [], 'history': [], 'selected_earlier': [], 'kept_in_backup': [], 'warnings': []}
        self.reviewed_preview = {"fixture": "An authored review for installer pipeline tests.", "saves": self.reviewed_saves}
        formatter = patch.object(media, '_preview_saves', return_value=self.reviewed_saves)
        formatter.start()
        self.addCleanup(formatter.stop)
        for target in ("plan_preview", "replacement_preview"):
            mocked = patch.object(media, target, return_value=self.reviewed_preview)
            mocked.start()
            self.addCleanup(mocked.stop)
        (self.root / "exports").mkdir()
        (self.root / "library").mkdir()
        authorization = patch.object(media, "authorized_device_descriptor", side_effect=lambda path, flags: os.open(path, flags))
        authorization.start()
        self.addCleanup(authorization.stop)
        self.image = self.root / "exports/sample.img"
        fake_raw_image(self.image)
        self.card = {
            "id": "disk99", "name": "Disposable test fixture", "size_bytes": 16384,
            "mount_points": [], "identity": "card-fingerprint", "physical_identity": "media-generation",
        }

    def tearDown(self):
        self.temporary.cleanup()

    def make_plan(self, raw_backup=False):
        if raw_backup:
            backup = self.root / "backups/source.img"
            backup.parent.mkdir(exist_ok=True)
            backup.write_bytes(self.image.read_bytes())
            media.write_json(backup.with_suffix(".img.json"), {
                "backup_path": str(backup), "sha256": media.sha256(backup),
                "size_bytes": backup.stat().st_size, "verified": True,
            })
            self.image = backup
        with patch.object(media, "current_card", return_value=self.card):
            return media.plan_install(self.root, self.image, self.card["id"], backup_kind="raw")

    def make_pack(self):
        pack = self.root / "packs/puzzle-night"
        game = pack / "GB/T-Z/Tetris.gb"
        game.parent.mkdir(parents=True)
        game.write_bytes(bytes(range(256)) * 256)
        os_file = pack / 'GBCSYS/GBCOS.BIN'
        os_file.parent.mkdir()
        os_file.write_bytes(self.fixture_os)
        files = [{"path": "GB/T-Z/Tetris.gb", "bytes": game.stat().st_size, "sha256": media.sha256(game)},
                 {"path": 'GBCSYS/GBCOS.BIN', "bytes": len(self.fixture_os), "sha256": media.sha256(os_file)}]
        (pack / "MANIFEST.json").write_text(json.dumps({"profile": "chromatic", "name": "Puzzle night", "files": files}))
        return pack, game.read_bytes()


    def test_apfs_protects_physical_store_not_only_virtual_container(self):
        infos = {
            "disk7s1": {"DeviceIdentifier": "disk7s1", "APFSPhysicalStores": [{"APFSPhysicalStore": "disk6s2"}]},
            "disk6s2": {"DeviceIdentifier": "disk6s2", "ParentWholeDisk": "disk6", "WholeDisk": False},
            "disk6": {"DeviceIdentifier": "disk6", "ParentWholeDisk": "disk6", "WholeDisk": True, "VirtualOrPhysical": "Physical"},
        }
        self.assertEqual(media.physical_disks(infos["disk7s1"], infos.__getitem__), {"disk6"})

    def test_internal_startup_whole_disk_is_protected_when_apple_reports_unknown(self):
        info = {"DeviceIdentifier": "disk0", "WholeDisk": True, "VirtualOrPhysical": "Unknown", "Internal": True}
        self.assertEqual(media.physical_disks(info), {"disk0"})

    def test_only_removable_external_physical_writable_cards_are_listed(self):
        info = {
            "DeviceIdentifier": "disk99", "WholeDisk": True, "Internal": False,
            "VirtualOrPhysical": "Physical", "WritableMedia": True, "RemovableMedia": True,
            "TotalSize": 16384, "MediaName": "SD card", "DeviceTreePath": "USB:1",
        }
        self.assertIsNotNone(media.card_record(info, {}, set()))
        for change in [{"Internal": True}, {"VirtualOrPhysical": "Virtual"}, {"WritableMedia": False}, {"RemovableMedia": False}, {"TotalSize": 0}]:
            with self.subTest(change=change):
                self.assertIsNone(media.card_record({**info, **change}, {}, set()))
        self.assertIsNone(media.card_record(info, {}, {"disk99"}))

    def test_partition_uuid_changes_device_identity(self):
        info = {
            "DeviceIdentifier": "disk99", "WholeDisk": True, "Internal": False,
            "VirtualOrPhysical": "Physical", "WritableMedia": True, "RemovableMedia": True,
            "TotalSize": 16384, "MediaName": "SD card", "DeviceTreePath": "USB:1",
        }
        first = media.card_record(info, {"Partitions": [{"VolumeUUID": "A"}]}, set())
        replaced = media.card_record(info, {"Partitions": [{"VolumeUUID": "B"}]}, set())
        self.assertNotEqual(first["identity"], replaced["identity"])
        self.assertEqual(first["physical_identity"], replaced["physical_identity"])
        later_media = media.card_record({**info, "IORegistryEntryID": 987654}, {"Partitions": [{"VolumeUUID": "B"}]}, set())
        self.assertNotEqual(first["physical_identity"], later_media["physical_identity"])

    def test_actual_library_and_startup_disks_are_excluded(self):
        volumes = {str(self.root): 'disk6', '/': 'disk0', '/runtime': 'disk8'}
        listing = {'AllDisksAndPartitions': []}
        def info(path):
            return {'DeviceIdentifier': volumes[path], 'WholeDisk': True, 'VirtualOrPhysical': 'Physical'}
        with patch.object(media, 'command', return_value=listing), \
                patch.object(media, 'disk_info', side_effect=info), \
                patch.object(media.sys, 'executable', '/runtime/bin/python3'), \
                patch.object(Path, 'resolve', lambda path, **_: path), \
                patch.object(Path, 'is_mount', lambda path: str(path) in volumes), \
                patch.dict(media.__dict__, {'__file__': None}):
            result = media.cards(self.root)
        self.assertEqual(set(result['protected_disks']), {'disk0', 'disk6', 'disk8'})
        self.assertEqual(result['cards'], [])

    def test_create_raw_image_mounts_and_preserves_real_pack_bytes(self):
        pack, original = self.make_pack()
        receipt = media.create_image(self.root, pack)
        image = Path(receipt["image_path"])
        self.assertTrue(image.is_file())
        self.assertEqual(media.raw_image_info(image)["sha256"], receipt["sha256"])
        self.assertEqual((pack / "GB/T-Z/Tetris.gb").read_bytes(), original)
        with media.mounted_image(image, readonly=True) as mount:
            self.assertEqual((mount / "GB/T-Z/Tetris.gb").read_bytes(), original)
            self.assertEqual(json.loads((mount / "MANIFEST.json").read_text())["profile"], "chromatic")
            self.assertFalse(any(path.name.startswith("._") or path.name in {".DS_Store", ".Spotlight-V100"}
                                 for path in mount.rglob("*")))
        self.assertFalse(mount.is_mount())
        self.assertTrue(image.with_suffix(".img.json").is_file())
        self.assertTrue(image.with_suffix(".img.sha256").is_file())
        with self.assertRaises(FileExistsError):
            media.create_image(self.root, pack)

    def test_changed_pack_manifest_prevents_image_creation(self):
        pack, _ = self.make_pack()
        (pack / "GB/T-Z/Tetris.gb").write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "no longer matches"):
            media.create_image(self.root, pack)
        self.assertFalse((self.root / "exports/puzzle-night.img").exists())

    def test_fat32_invalid_names_and_historical_saves_are_rejected(self):
        pack = self.root / "pack"
        pack.mkdir()
        (pack / "question?.gb").write_bytes(b"one")
        with self.assertRaisesRegex(ValueError, "not supported"):
            media.pack_files(pack)
        (pack / "question?.gb").unlink()
        (pack / "old.sav").write_bytes(b"save")
        with self.assertRaisesRegex(ValueError, "save files"):
            media.pack_files(pack)

    def test_portable_pack_rejects_ed64_eeprom_and_other_historical_saves(self):
        pack = self.root / "pack"
        directory = pack / "ed64/gamedata/Game.z64"
        directory.mkdir(parents=True)
        for name in ("bram.eep", "bram.srm", "old.fla", "old.mpk"):
            with self.subTest(name=name):
                save = directory / name
                save.write_bytes(b"historical progress")
                with self.assertRaisesRegex(ValueError, "save files"):
                    media.pack_files(pack)
                save.unlink()

    def test_raw_image_bounds_and_partition_type_are_checked(self):
        data = bytearray(self.image.read_bytes())
        data[458:462] = struct.pack("<I", 999999)
        self.image.write_bytes(data)
        with self.assertRaisesRegex(ValueError, "beyond"):
            media.raw_image_info(self.image)
        fake_raw_image(self.image)
        data = bytearray(self.image.read_bytes())
        data[450] = 0x07
        self.image.write_bytes(data)
        with self.assertRaisesRegex(ValueError, "one FAT32"):
            media.raw_image_info(self.image)

    def test_plan_binds_image_card_capacity_and_confirmation(self):
        plan = self.make_plan()
        self.assertEqual(plan["device"], self.card)
        self.assertEqual(plan["image_sha256"], media.sha256(self.image))
        self.assertTrue(Path(plan["plan_path"]).is_file())
        self.assertTrue(Path(plan["backup_path"]).is_relative_to(self.root / "backups"))
        with patch.object(media, "current_card", return_value=self.card):
            media.validate_plan(self.root, plan, plan["token"])
            with self.assertRaisesRegex(ValueError, "confirmation"):
                media.validate_plan(self.root, plan, "wrong token")
        for change in [{"identity": "different-card"}, {"size_bytes": 32768}]:
            with patch.object(media, "current_card", return_value={**self.card, **change}):
                with self.assertRaisesRegex(ValueError, "card changed"):
                    media.validate_plan(self.root, plan, plan["token"])
        with patch.object(media, "current_card", return_value=self.card):
            self.image.write_bytes(self.image.read_bytes() + b"\0" * 512)
            with self.assertRaisesRegex(ValueError, "image changed"):
                media.validate_plan(self.root, plan, plan["token"])

    def test_verified_non_fat_backup_can_be_reviewed_and_restored_exactly(self):
        source = self.root / "backups/prior-gpt-card.img"
        source.parent.mkdir()
        original = bytearray(b"old GPT card".ljust(8192, b"."))
        original[512:520] = b"EFI PART"
        original[-512:-504] = b"EFI PART"
        source.write_bytes(original)
        record = {"backup_path": str(source), "sha256": media.sha256(source),
                  "size_bytes": len(original), "verified": True}
        media.write_json(source.with_suffix(".img.json"), record)
        with self.assertRaises(ValueError):
            media.raw_image_info(source)
        with patch.object(media, "current_card", return_value=self.card):
            plan = media.plan_install(self.root, source, self.card["id"])
        self.assertEqual(plan["image_kind"], "backup")
        self.assertIn("restores a complete verified card backup", plan["message"])
        fixture = self.root / "fixture-card"
        previous = b"selected card before restore".ljust(self.card["size_bytes"], b"!")
        fixture.write_bytes(previous)
        with patch.object(media, "current_card", return_value=self.card), patch.object(media, "command"), patch.object(media, "raw_device", return_value=fixture), patch.object(media, "extract_save_vault", return_value={}):
            receipt = media.install(self.root, Path(plan["plan_path"]), plan["token"])
        self.assertTrue(receipt["verified"])
        self.assertEqual(receipt["image_kind"], "backup")
        self.assertEqual(fixture.read_bytes()[:len(original)], original)
        self.assertEqual(fixture.read_bytes()[len(original):], bytes(self.card["size_bytes"] - len(original)))
        self.assertEqual(Path(receipt["backup_path"]).read_bytes(), previous)
        self.assertEqual(source.read_bytes(), original)

    def test_unverified_or_changed_backup_receipt_is_rejected(self):
        backup = self.root / "backups/prior.img"
        backup.parent.mkdir()
        backup.write_bytes(b"opaque card bytes".ljust(8192, b"."))
        with self.assertRaisesRegex(ValueError, "requires its verified"):
            media.image_info(self.root, backup)
        record = {"backup_path": str(backup), "sha256": media.sha256(backup),
                  "size_bytes": backup.stat().st_size, "verified": False}
        receipt = backup.with_suffix(".img.json")
        media.write_json(receipt, record)
        with self.assertRaisesRegex(ValueError, "receipt does not match"):
            media.image_info(self.root, backup)
        record["verified"] = True
        media.write_json(receipt, {**record, "sha256": 123}, replace=True)
        with self.assertRaisesRegex(ValueError, "receipt does not match"):
            media.image_info(self.root, backup)
        media.write_json(receipt, record, replace=True)
        self.assertEqual(media.image_info(self.root, backup)["kind"], "backup")
        backup.write_bytes(b"changed card data".ljust(8192, b"."))
        with self.assertRaisesRegex(ValueError, "backup changed"):
            media.image_info(self.root, backup)

    def test_backup_kind_is_bound_to_the_reviewed_plan(self):
        plan = self.make_plan()
        plan["image_kind"] = "backup"
        with patch.object(media, "current_card", return_value=self.card):
            with self.assertRaisesRegex(ValueError, "confirmation"):
                media.validate_plan(self.root, plan, plan["token"])

    def test_image_and_backup_must_fit_before_plan_publication(self):
        with patch.object(media, "current_card", return_value={**self.card, "size_bytes": 512}):
            with self.assertRaisesRegex(ValueError, "larger"):
                media.plan_install(self.root, self.image, self.card["id"])
        with patch.object(media, "current_card", return_value=self.card), patch.object(media.shutil, "disk_usage", return_value=SimpleNamespace(free=0)):
            with self.assertRaisesRegex(OSError, "free space"):
                media.plan_install(self.root, self.image, self.card["id"])
        self.assertFalse((self.root / "library/card-plans").exists())

    def test_confirmation_is_bound_to_the_reviewed_backup_path(self):
        plan = self.make_plan()
        plan["backup_path"] = str(self.root / "backups/changed-after-review.img")
        with patch.object(media, "current_card", return_value=self.card):
            with self.assertRaisesRegex(ValueError, "confirmation"):
                media.validate_plan(self.root, plan, plan["token"])

    def test_only_one_card_operation_can_run_at_a_time(self):
        with media.card_lock(self.root):
            with self.assertRaisesRegex(RuntimeError, "already in progress"):
                with media.card_lock(self.root):
                    self.fail("A second card operation acquired the same lock")

    def test_partial_backup_is_not_published(self):
        source = self.root / "short-card"
        source.write_bytes(b"short")
        destination = self.root / "backup.img"
        with source.open("rb", buffering=0) as stream:
            with self.assertRaisesRegex(OSError, "source ended"):
                media.backup_to_file(stream, destination, 1000)
        self.assertFalse(destination.exists())
        self.assertFalse(destination.with_suffix(".img.json").exists())

    def test_regular_file_backup_and_readback_are_exact(self):
        source = self.root / "fixture-card"
        original = bytes(range(256)) * 64
        source.write_bytes(original)
        with source.open("rb", buffering=0) as stream:
            receipt = media.backup_to_file(stream, self.root / "backup.img", len(original))
        self.assertTrue(receipt["verified"])
        self.assertEqual(Path(receipt["backup_path"]).read_bytes(), original)
        image_bytes = self.image.read_bytes()
        with self.image.open("rb", buffering=0) as image, source.open("r+b", buffering=0) as card:
            media.write_and_verify(image, card, len(image_bytes), media.sha256(self.image))
        self.assertEqual(source.read_bytes()[:len(image_bytes)], image_bytes)

    def test_partial_backup_prevents_any_replacement(self):
        plan = self.make_plan()
        fixture = self.root / "fixture-card"
        fixture.write_bytes(b"original card contents")
        with patch.object(media, "current_card", return_value=self.card), patch.object(media, "command"), patch.object(media, "raw_device", return_value=fixture), patch.object(media, "backup_to_file", side_effect=OSError("Incomplete backup")), patch.object(media, "write_and_verify") as write:
            with self.assertRaisesRegex(OSError, "Incomplete backup"):
                media.install(self.root, Path(plan["plan_path"]), plan["token"])
            write.assert_not_called()
        self.assertEqual(fixture.read_bytes(), b"original card contents")

    def test_full_install_runs_only_on_disposable_file_and_keeps_original_backup(self):
        plan = self.make_plan(raw_backup=True)
        fixture = self.root / "fixture-card"
        original = bytes(range(256)) * 64
        fixture.write_bytes(original)
        with patch.object(media, "current_card", return_value=self.card), patch.object(media, "command"), patch.object(media, "raw_device", return_value=fixture), patch.object(media, "extract_save_vault", return_value={}):
            receipt = media.install(self.root, Path(plan["plan_path"]), plan["token"])
            with self.assertRaisesRegex(ValueError, "backup destination"):
                media.install(self.root, Path(plan["plan_path"]), plan["token"])
        self.assertTrue(receipt["verified"])
        self.assertEqual(Path(receipt["backup_path"]).read_bytes(), original)
        self.assertEqual(fixture.read_bytes()[:self.image.stat().st_size], self.image.read_bytes())
        self.assertTrue(Path(plan["plan_path"]).with_suffix(".completed.json").exists())

    def test_raw_backup_restore_clears_old_gpt_tail_after_preserving_the_backup(self):
        plan = self.make_plan(raw_backup=True)
        fixture = self.root / "fixture-card"
        original = bytearray(b"old card bytes".ljust(self.card["size_bytes"], b"."))
        original[-512:-504] = b"EFI PART"
        fixture.write_bytes(original)
        with patch.object(media, "current_card", return_value=self.card), patch.object(media, "command"), patch.object(media, "raw_device", return_value=fixture), patch.object(media, "extract_save_vault", return_value={}):
            receipt = media.install(self.root, Path(plan["plan_path"]), plan["token"])
        image = self.image.read_bytes()
        self.assertEqual(fixture.read_bytes()[:len(image)], image)
        self.assertEqual(fixture.read_bytes()[len(image):], bytes(len(original) - len(image)))
        self.assertEqual(receipt["tail_cleared_bytes"], len(original) - len(image))
        self.assertEqual(Path(receipt["backup_path"]).read_bytes(), original)

    def compilation_fixture(self):
        pack, _ = self.make_pack()
        game = pack / "GB/T-Z/Tetris.gb"
        game.write_bytes(bytes(range(256)) * 8)
        manifest = json.loads((pack / "MANIFEST.json").read_text())
        manifest["files"][0].update(bytes=game.stat().st_size, sha256=media.sha256(game))
        (pack / "MANIFEST.json").write_text(json.dumps(manifest))
        prior = self.root / "prior-card-files"
        save = prior / "GBCSYS/SAVE/Old Tetris.srm"
        save.parent.mkdir(parents=True)
        save.write_bytes(b"the player's existing progress")
        mount = self.root / "formatted-card"
        mount.mkdir()
        restore = {"files": [{"source_path": str(save), "path": "GBCSYS/SAVE/Tetris.srm",
                               "bytes": save.stat().st_size, "sha256": media.sha256(save)}],
                   "skipped": [{"path": "GBCSYS/ramdump01.srm", "reason": "Unknown runtime state"}], "warnings": []}
        info = {"VolumeUUID": "new-full-volume", "DeviceIdentifier": "disk99s1",
                "TotalSize": 16000, "VolumeAllocationBlockSize": 16384}
        return pack, prior, save, mount, restore, info

    def test_compilation_installs_files_and_matching_saves_after_closing_raw_backup(self):
        plan = self.make_plan()
        pack, prior, save, mount, restore, info = self.compilation_fixture()
        fixture = self.root / "raw-card-fixture"
        original = b"old card with a larger filesystem".ljust(self.card["size_bytes"], b".")
        fixture.write_bytes(original)
        held = False
        commands = []
        original_open = media.opened_device

        @contextlib.contextmanager
        def opened(identifier, *, writable):
            nonlocal held
            self.assertFalse(writable, "Compiled-pack backup must not open a writable raw FD")
            with original_open(identifier, writable=writable) as stream:
                held = True
                yield stream
            held = False

        def command(args, **kwargs):
            commands.append(args)
            if args[1] == "eraseDisk":
                self.assertFalse(held, "Exclusive raw device FD must close before formatting")
                self.assertEqual(Path(plan["backup_path"]).read_bytes(), original)
            return b""

        @contextlib.contextmanager
        def mounted(image, *, readonly=False):
            self.assertTrue(readonly)
            yield pack if image == self.image else prior

        saver = SimpleNamespace(validate_choices=validate_choices, build_restore_plan=lambda *_args, **_kwargs: restore)
        with patch.dict(sys.modules, {"card_saves": saver}), \
                patch.object(media, "current_card", return_value=self.card), \
                patch.object(media, "command", side_effect=command), \
                patch.object(media, "raw_device", return_value=fixture), \
                patch.object(media, "opened_device", side_effect=opened), \
                patch.object(media, "mounted_image", side_effect=mounted), \
                patch.object(media, "formatted_card", return_value=(info, mount)), \
                patch.object(media, "extract_save_vault", return_value={"save_files": 3}):
            result = media.install(self.root, Path(plan["plan_path"]), plan["token"])
        self.assertEqual((mount / "GBCSYS/SAVE/Tetris.srm").read_bytes(), save.read_bytes())
        self.assertEqual((mount / "GB/T-Z/Tetris.gb").read_bytes(), (pack / "GB/T-Z/Tetris.gb").read_bytes())
        self.assertEqual(fixture.read_bytes(), original, "The file-flow fixture must never receive raw image bytes")
        self.assertEqual(result["restored_save_files"], 1)
        self.assertEqual(result["save_files"], 3)
        self.assertEqual(result["save_restore_skipped"], restore["skipped"])
        self.assertEqual(result["filesystem_bytes"], 16000)
        self.assertEqual(result["install_mode"], "full-capacity-files")
        self.assertTrue(result["full_capacity"] and result["verified"] and result["ejected"])
        self.assertEqual(result["read_back_passes"], 2)
        self.assertIn([media.DISKUTIL, "eraseDisk", "MS-DOS FAT32", "GBX7", "MBRFormat", "/dev/disk99"], commands)
        self.assertIn([media.DISKUTIL, "mount", "readOnly", "disk99s1"], commands)

    def test_compilation_requires_cart_shelf_manifest_before_formatting(self):
        plan = self.make_plan()
        pack = self.root / "unrecognized-image-files"
        pack.mkdir()
        backup = self.root / "backups/already-verified.img"
        backup.write_bytes(b"prior")
        saver = SimpleNamespace(validate_choices=validate_choices, build_restore_plan=lambda *_args, **_kwargs: self.fail("No save scan before valid pack"))
        with patch.dict(sys.modules, {"card_saves": saver}), \
                patch.object(media, "mounted_image", return_value=contextlib.nullcontext(pack)), \
                patch.object(media, "command") as command:
            with self.assertRaisesRegex(ValueError, "pack manifest"):
                media.install_compilation(self.root, self.card, self.image, backup, plan, {"sha256": media.sha256(backup), "size_bytes": backup.stat().st_size, "verified": True}, {})
            command.assert_not_called()

    def test_unreadable_backup_or_failed_save_scan_prevents_formatting(self):
        plan = self.make_plan()
        pack, prior, _, _, _, _ = self.compilation_fixture()
        backup = self.root / "backups/already-verified.img"
        backup.write_bytes(b"prior")

        @contextlib.contextmanager
        def mounted(image, *, readonly=False):
            if image == backup:
                raise OSError("The verified backup cannot be read as a filesystem")
            yield pack

        saver = SimpleNamespace(validate_choices=validate_choices, build_restore_plan=lambda *_args, **_kwargs: self.fail("Backup could not mount"))
        with patch.dict(sys.modules, {"card_saves": saver}), \
                patch.object(media, "mounted_image", side_effect=mounted), \
                patch.object(media, "command") as command:
            with self.assertRaisesRegex(OSError, "cannot be read"):
                media.install_compilation(self.root, self.card, self.image, backup, plan, {"sha256": media.sha256(backup), "size_bytes": backup.stat().st_size, "verified": True}, {})
            command.assert_not_called()
        saver.build_restore_plan = lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("Save history verification failed"))
        with patch.dict(sys.modules, {"card_saves": saver}), \
                patch.object(media, "mounted_image", side_effect=lambda image, **_: contextlib.nullcontext(pack if image == self.image else prior)), \
                patch.object(media, "command") as command:
            with self.assertRaisesRegex(OSError, "history verification failed"):
                media.install_compilation(self.root, self.card, self.image, backup, plan, {"sha256": media.sha256(backup), "size_bytes": backup.stat().st_size, "verified": True}, {})
            command.assert_not_called()

    def test_changed_media_generation_is_rejected_after_partition_uuid_changes(self):
        with patch.object(media, "current_card", return_value={**self.card, "identity": "new-volume-uuid"}):
            self.assertEqual(media.same_physical_card(self.root, self.card)["physical_identity"], "media-generation")
        with patch.object(media, "current_card", return_value={**self.card, "physical_identity": "replacement-in-the-same-reader"}):
            with self.assertRaisesRegex(ValueError, "physical card changed"):
                media.same_physical_card(self.root, self.card)

    def test_formatted_volume_requires_new_uuid_full_capacity_fat32_and_small_clusters(self):
        listing = {"AllDisksAndPartitions": [{"DeviceIdentifier": "disk99", "Content": "FDisk_partition_scheme",
                                             "Partitions": [{"DeviceIdentifier": "disk99s1"}]}]}
        info = {"ParentWholeDisk": "disk99", "FilesystemType": "msdos", "WritableVolume": False,
                "VolumeUUID": "new-uuid", "MountPoint": "/Volumes/GBX7", "IOKitSize": 16384,
                "TotalSize": 16000, "VolumeAllocationBlockSize": 512}
        with patch.object(media, "current_card", return_value={**self.card, "identity": "new-partition"}), \
                patch.object(media, "command", return_value=listing) as command, \
                patch.object(media, "disk_info", return_value=info) as disk_info, \
                patch.object(Path, "is_dir", return_value=True):
            actual, mount = media.formatted_card(self.root, self.card, "new-uuid", writable=False)
            self.assertEqual(actual["TotalSize"], 16000)
            self.assertEqual(mount, Path("/Volumes/GBX7"))
            command.assert_called_once_with([media.DISKUTIL, "list", "-plist", "/dev/disk99"], plist=True)
            disk_info.assert_called_once_with("disk99s1")
            for change in [{"VolumeUUID": "another-volume"}, {"IOKitSize": 8192}, {"TotalSize": 8192},
                           {"VolumeAllocationBlockSize": 65536}, {"VolumeAllocationBlockSize": 123},
                           {"ParentWholeDisk": "disk0"}, {"WritableVolume": True}, {"FilesystemType": "apfs"}]:
                with self.subTest(change=change), patch.object(media, "disk_info", return_value={**info, **change}):
                    with self.assertRaisesRegex(ValueError, "did not verify"):
                        media.formatted_card(self.root, self.card, "new-uuid", writable=False)

    def test_format_failure_keeps_failed_receipt_and_verified_backup(self):
        plan = self.make_plan()
        pack, prior, _, _, restore, _ = self.compilation_fixture()
        backup = Path(plan["backup_path"])
        original = b"whole original card".ljust(self.card["size_bytes"], b".")
        backup.write_bytes(original)
        saver = SimpleNamespace(validate_choices=validate_choices, build_restore_plan=lambda *_args, **_kwargs: restore)
        with patch.dict(sys.modules, {"card_saves": saver}), \
                patch.object(media, "mounted_image", side_effect=lambda image, **_: contextlib.nullcontext(pack if image == self.image else prior)), \
                patch.object(media, "current_card", return_value=self.card), \
                patch.object(media, "command", side_effect=RuntimeError("Formatting stopped")):
            with self.assertRaisesRegex(RuntimeError, "Formatting stopped"):
                media.install_compilation(self.root, self.card, self.image, backup, plan, {"sha256": media.sha256(backup), "size_bytes": backup.stat().st_size, "verified": True}, {})
        result = json.loads(Path(plan["plan_path"]).with_suffix(".completed.json").read_text())
        self.assertFalse(result["verified"])
        self.assertTrue(result["manual_eject_required"])
        self.assertEqual(result["operation_phase"], "format-card")
        self.assertEqual(result["backup_path"], str(backup))
        self.assertEqual(result["restored_save_files"], 0)
        self.assertEqual(backup.read_bytes(), original)

    def test_pack_and_restored_saves_capacity_is_checked_before_format(self):
        plan = self.make_plan()
        pack, prior, save, _, restore, _ = self.compilation_fixture()
        save.write_bytes(b"oversized historical save".ljust(16384, b"."))
        restore["files"][0].update(bytes=save.stat().st_size, sha256=media.sha256(save))
        backup = Path(plan["backup_path"])
        backup.write_bytes(b"verified original".ljust(self.card["size_bytes"], b"."))
        saver = SimpleNamespace(validate_choices=validate_choices, build_restore_plan=lambda *_args, **_kwargs: restore)
        with patch.dict(sys.modules, {"card_saves": saver}), \
                patch.object(media, "mounted_image", side_effect=lambda image, **_: contextlib.nullcontext(pack if image == self.image else prior)), \
                patch.object(media, "command") as command:
            with self.assertRaisesRegex(ValueError, "do not fit safely"):
                media.install_compilation(self.root, self.card, self.image, backup, plan, {"sha256": media.sha256(backup), "size_bytes": backup.stat().st_size, "verified": True}, {})
            command.assert_not_called()

    def test_restored_save_readback_failure_retains_recovery_receipt(self):
        plan = self.make_plan()
        pack, prior, _, mount, restore, info = self.compilation_fixture()
        backup = Path(plan["backup_path"])
        original = b"complete original card".ljust(self.card["size_bytes"], b".")
        backup.write_bytes(original)

        def command(args, **kwargs):
            if args[1] == "unmountDisk":
                target = mount / "GBCSYS/SAVE/Tetris.srm"
                data = bytearray(target.read_bytes())
                data[0] ^= 1
                target.write_bytes(data)
            return b""

        saver = SimpleNamespace(validate_choices=validate_choices, build_restore_plan=lambda *_args, **_kwargs: restore)
        with patch.dict(sys.modules, {"card_saves": saver}), \
                patch.object(media, "mounted_image", side_effect=lambda image, **_: contextlib.nullcontext(pack if image == self.image else prior)), \
                patch.object(media, "current_card", return_value=self.card), \
                patch.object(media, "formatted_card", return_value=(info, mount)), \
                patch.object(media, "command", side_effect=command):
            with self.assertRaisesRegex(OSError, "read-back verification failed"):
                media.install_compilation(self.root, self.card, self.image, backup, plan, {"sha256": media.sha256(backup), "size_bytes": backup.stat().st_size, "verified": True}, {})
        result = json.loads(Path(plan["plan_path"]).with_suffix(".completed.json").read_text())
        self.assertFalse(result["verified"])
        self.assertEqual(result["operation_phase"], "verify-card-files")
        self.assertEqual(result["volume_uuid"], "new-full-volume")
        self.assertTrue(result["manual_eject_required"])
        self.assertEqual(backup.read_bytes(), original)

    def test_only_validated_mac_companions_of_planned_files_and_directories_can_be_removed(self):
        mount = self.root / "card-payload"
        game = mount / "GB/Tetris.gb"
        game.parent.mkdir(parents=True)
        game.write_bytes(b"game")
        companion = struct.pack(">II16sH", 0x00051607, 0x00020000, bytes(16), 0)
        (mount / "._GB").write_bytes(companion)
        (game.parent / "._Tetris.gb").write_bytes(companion)
        found = media.card_payload_inventory(mount, {"GB/Tetris.gb"})
        self.assertEqual({p.relative_to(mount).as_posix() for p in found}, {"._GB", "GB/._Tetris.gb"})
        (mount / "unknown.txt").write_bytes(b"preserve unexpected data")
        with self.assertRaisesRegex(ValueError, "unexpected file"):
            media.card_payload_inventory(mount, {"GB/Tetris.gb"})
        self.assertEqual((mount / "unknown.txt").read_bytes(), b"preserve unexpected data")

    def test_public_mount_system_metadata_is_ignored_while_payload_companions_need_cleanup(self):
        mount = self.root / "public-card"
        game = mount / "GB/Tetris.gb"
        game.parent.mkdir(parents=True)
        game.write_bytes(b"game")
        companion = struct.pack(">II16sH", 0x00051607, 0x00020000, bytes(16), 0)
        for name in (".Trashes", ".fseventsd", ".Spotlight-V100"):
            (mount / name).mkdir()
            (mount / name / "system-state").write_bytes(b"retained system state")
        metadata = ("._.", "._.Trashes", "._.fseventsd", "._.Spotlight-V100",
                    "._.DS_Store", "GB/._.DS_Store")
        for name in metadata:
            (mount / name).write_bytes(companion)
        for name in (".DS_Store", "GB/.DS_Store"):
            (mount / name).write_bytes(b"\x00\x00\x00\x01Bud1" + bytes(24))
        for name in ("._GB", "GB/._Tetris.gb"):
            (mount / name).write_bytes(companion)
        found = media.card_payload_inventory(mount, {"GB/Tetris.gb"})
        self.assertEqual({p.relative_to(mount).as_posix() for p in found}, {"._GB", "GB/._Tetris.gb"})
        self.assertTrue(all((mount / name).is_file() for name in metadata))
        self.assertEqual(game.read_bytes(), b"game")

    def test_malformed_system_companions_are_rejected(self):
        header = struct.pack(">II16sH", 0x00051607, 0x00020000, bytes(16), 1)
        for name in ("._.", "._.Trashes", "._.DS_Store", "GB/._.DS_Store"):
            for index, data in enumerate((b"not AppleDouble", header, header + struct.pack(">III", 1, 1000, 8))):
                with self.subTest(name=name, malformed=index):
                    mount = self.root / ("invalid-" + name.replace('/', '-') + str(index))
                    game = mount / "GB/Tetris.gb"
                    game.parent.mkdir(parents=True)
                    game.write_bytes(b"game")
                    (mount / ".Trashes").mkdir()
                    (mount / name).write_bytes(data)
                    with self.assertRaises(ValueError):
                        media.card_payload_inventory(mount, {"GB/Tetris.gb"})
                    self.assertEqual((mount / name).read_bytes(), data)

    def test_metadata_lookalikes_and_malformed_payload_companions_stay_strict(self):
        companion = struct.pack(">II16sH", 0x00051607, 0x00020000, bytes(16), 0)
        for index, (name, data) in enumerate((("._unknown", companion), ("._.Trashes-note", companion),
                                             (".DS_Store.backup", b"user data"), ("GB/._Tetris.gb", b"invalid metadata"))):
            with self.subTest(name=name):
                mount = self.root / ("strict-" + str(index))
                game = mount / "GB/Tetris.gb"
                game.parent.mkdir(parents=True)
                game.write_bytes(b"game")
                (mount / name).write_bytes(data)
                with self.assertRaises(ValueError):
                    media.card_payload_inventory(mount, {"GB/Tetris.gb"})
                self.assertEqual((mount / name).read_bytes(), data)

    def test_public_card_unmount_retries_transient_busy_without_force(self):
        with patch.object(media, "current_card", return_value=self.card) as guard, \
                patch.object(media, "command", side_effect=[RuntimeError("busy"), b""]) as command, \
                patch.object(media.time, "sleep") as sleep:
            media.unmount_card(self.root, self.card)
        self.assertEqual(guard.call_count, 2)
        self.assertEqual(command.call_args_list[0], command.call_args_list[1])
        self.assertEqual(command.call_args_list[0].args[0], [media.DISKUTIL, "unmountDisk", "/dev/disk99"])
        sleep.assert_called_once_with(0.25)

    def test_public_card_unmount_stops_before_retry_if_media_changes(self):
        changed = {**self.card, "physical_identity": "different-media-generation"}
        with patch.object(media, "current_card", side_effect=[self.card, changed]), \
                patch.object(media, "command", side_effect=RuntimeError("busy")) as command, \
                patch.object(media.time, "sleep"):
            with self.assertRaisesRegex(ValueError, "physical card changed"):
                media.unmount_card(self.root, self.card)
        self.assertEqual(command.call_count, 1)

    def test_public_card_unmount_preserves_persistent_failure_after_bounded_attempts(self):
        failure = RuntimeError("still busy")
        with patch.object(media, "current_card", return_value=self.card), \
                patch.object(media, "command", side_effect=failure) as command, \
                patch.object(media.time, "sleep") as sleep:
            with self.assertRaises(RuntimeError) as stopped:
                media.unmount_card(self.root, self.card)
        self.assertIs(stopped.exception, failure)
        self.assertEqual(command.call_count, 4)
        self.assertEqual(sleep.call_count, 3)

    def test_compilation_with_public_metadata_and_one_busy_unmount_still_verifies(self):
        plan = self.make_plan()
        pack, prior, save, mount, restore, info = self.compilation_fixture()
        backup = Path(plan["backup_path"])
        backup.write_bytes(b"verified original card".ljust(self.card["size_bytes"], b"."))
        original = backup.read_bytes()
        companion = struct.pack(">II16sH", 0x00051607, 0x00020000, bytes(16), 0)
        (mount / ".Trashes").mkdir()
        (mount / "._.Trashes").write_bytes(companion)
        (mount / "._.").write_bytes(companion)
        (mount / ".DS_Store").write_bytes(b"\x00\x00\x00\x01Bud1" + bytes(24))
        commands = []
        unmounts = 0

        def command(args, **kwargs):
            nonlocal unmounts
            commands.append(args)
            if args[1] == "unmountDisk":
                unmounts += 1
                if unmounts == 1:
                    raise RuntimeError("Fixture Spotlight holds a transient open file")
            return b""

        saver = SimpleNamespace(validate_choices=validate_choices, build_restore_plan=lambda *_args, **_kwargs: restore)
        with patch.dict(sys.modules, {"card_saves": saver}), \
                patch.object(media, "mounted_image", side_effect=lambda image, **_: contextlib.nullcontext(pack if image == self.image else prior)), \
                patch.object(media, "current_card", return_value=self.card), \
                patch.object(media, "formatted_card", return_value=(info, mount)), \
                patch.object(media, "command", side_effect=command), \
                patch.object(media.time, "sleep"):
            result = media.install_compilation(self.root, self.card, self.image, backup, plan,
                                               {"sha256": media.sha256(backup), "size_bytes": backup.stat().st_size, "verified": True}, {})
        self.assertTrue(result["verified"])
        self.assertEqual(result["read_back_passes"], 2)
        self.assertEqual(result["restored_save_files"], 1)
        self.assertEqual(unmounts, 2)
        self.assertFalse(any("force" in arg for args in commands for arg in args))
        self.assertEqual((mount / "GBCSYS/SAVE/Tetris.srm").read_bytes(), save.read_bytes())
        self.assertEqual(backup.read_bytes(), original)

    def test_old_compilation_plan_requires_fresh_review_for_new_method(self):
        plan = self.make_plan()
        plan.pop("install_mode")
        plan["token"] = media.plan_token(plan)
        with patch.object(media, "current_card", return_value=self.card):
            with self.assertRaisesRegex(ValueError, "installation method changed"):
                media.validate_plan(self.root, plan, plan["token"])

    def test_tail_cleanup_is_bounded_to_34_sectors_outside_the_image(self):
        fixture = self.root / "larger-card"
        original = bytes(range(256)) * 1024
        fixture.write_bytes(original)
        with fixture.open("r+b", buffering=0) as output:
            cleared = media.clear_old_partition_tail(output, 8192, len(original), 512)
        self.assertEqual(cleared, 34 * 512)
        self.assertEqual(fixture.read_bytes()[:-cleared], original[:-cleared])
        self.assertEqual(fixture.read_bytes()[-cleared:], bytes(cleared))

    def test_eject_failure_keeps_completed_install_receipt(self):
        plan = self.make_plan(raw_backup=True)
        fixture = self.root / "fixture-card"
        original = b"prior card".ljust(self.card["size_bytes"], b".")
        fixture.write_bytes(original)
        completed = Path(plan["plan_path"]).with_suffix(".completed.json")

        def disk_command(args, **kwargs):
            if args[1] == "eject":
                self.assertTrue(json.loads(completed.read_text())["verified"])
                raise RuntimeError("Fixture has an eject dissenter")
            return b""

        with patch.object(media, "current_card", return_value=self.card), patch.object(media, "command", side_effect=disk_command), patch.object(media, "raw_device", return_value=fixture), patch.object(media, "extract_save_vault", return_value={}):
            receipt = media.install(self.root, Path(plan["plan_path"]), plan["token"])
        self.assertTrue(receipt["verified"])
        self.assertFalse(receipt["ejected"])
        self.assertTrue(receipt["manual_eject_required"])
        self.assertIn("Automatic ejection failed", receipt["message"])
        self.assertEqual(json.loads(completed.read_text()), receipt)
        self.assertEqual(Path(receipt["backup_path"]).read_bytes(), original)

    def test_backup_only_eject_failure_keeps_a_verified_restorable_backup(self):
        fixture = self.root / "fixture-card"
        original = b"complete card".ljust(self.card["size_bytes"], b".")
        fixture.write_bytes(original)

        def disk_command(args, **kwargs):
            if args[1] == "eject":
                receipts = list((self.root / "backups").glob("*.img.json"))
                self.assertEqual(len(receipts), 1)
                self.assertTrue(json.loads(receipts[0].read_text())["verified"])
                raise RuntimeError("Fixture has an eject dissenter")
            return b""

        with patch.object(media, "current_card", return_value=self.card), patch.object(media, "command", side_effect=disk_command), patch.object(media, "raw_device", return_value=fixture), patch.object(media, "extract_save_vault", return_value={}):
            receipt = media.backup_card(self.root, self.card["id"], backup_kind="raw")
        self.assertTrue(receipt["verified"])
        self.assertFalse(receipt["ejected"])
        backup = Path(receipt["backup_path"])
        self.assertEqual(backup.read_bytes(), original)
        self.assertEqual(media.image_info(self.root, backup)["kind"], "backup")

    def test_card_swap_during_long_hash_does_not_write_either_card(self):
        plan = self.make_plan()
        target = self.root / "fixture-card"
        original = bytes(range(256)) * 64
        target.write_bytes(original)
        removed = self.root / "removed-card"
        replacement = b"replacement card".ljust(self.card["size_bytes"], b".")
        selected = dict(self.card)
        original_hash = media.sha256
        swapped = False

        def hash_and_swap(path, length=None):
            nonlocal swapped
            digest = original_hash(path, length)
            if path == Path(plan["backup_path"]) and not swapped:
                target.rename(removed)
                target.write_bytes(replacement)
                selected["identity"] = "new-card"
                swapped = True
            return digest

        with patch.object(media, "current_card", side_effect=lambda *_: dict(selected)), patch.object(media, "command"), patch.object(media, "raw_device", return_value=target) as raw_path, patch.object(media, "extract_save_vault", return_value={}), patch.object(media, "sha256", side_effect=hash_and_swap), patch.object(media, "write_and_verify") as write:
            with self.assertRaisesRegex(ValueError, "card changed"):
                media.install(self.root, Path(plan["plan_path"]), plan["token"])
            write.assert_not_called()
            self.assertEqual(raw_path.call_count, 1)
        self.assertTrue(swapped)
        self.assertEqual(target.read_bytes(), replacement)
        self.assertEqual(removed.read_bytes(), original)
        self.assertEqual(Path(plan["backup_path"]).read_bytes(), original)

    def test_held_device_descriptor_is_not_redirected_by_a_path_replacement(self):
        target = self.root / "held-card"
        original = b"old".ljust(8192, b".")
        replacement = b"new".ljust(8192, b".")
        target.write_bytes(original)
        removed = self.root / "removed-card"
        with patch.object(media, "raw_device", return_value=target):
            with media.opened_device("disk99", writable=True) as stream:
                target.rename(removed)
                target.write_bytes(replacement)
                self.assertEqual(media.stream_sha256(stream, len(original)), media.hashlib.sha256(original).hexdigest())
                stream.seek(0)
                stream.write(b"kept")
        self.assertEqual(target.read_bytes(), replacement)
        self.assertTrue(removed.read_bytes().startswith(b"kept"))

    def test_remounted_card_aborts_before_backup(self):
        plan = self.make_plan()
        fixture = self.root / "fixture-card"
        fixture.write_bytes(b"original card contents")
        with patch.object(media, "current_card", return_value={**self.card, "mount_points": ["/Volumes/REMOUNTED"]}), patch.object(media, "command"), patch.object(media, "raw_device", return_value=fixture), patch.object(media, "backup_to_file") as backup:
            with self.assertRaisesRegex(ValueError, "mounted again"):
                media.install(self.root, Path(plan["plan_path"]), plan["token"])
            backup.assert_not_called()

    def test_created_media_directories_and_files_inherit_workspace_owner(self):
        owner = self.root.stat()
        with patch.object(media.os, "geteuid", return_value=0), patch.object(media.os, "chown") as chown, patch.object(media.os, "fchown") as fchown:
            with media.card_lock(self.root):
                backup = media.media_directory(self.root, self.root / "backups")
                media.write_json(backup / "receipt.json", {"verified": True})
            changed = {call.args[0] for call in chown.call_args_list}
            self.assertIn(self.root / "library/card-plans", changed)
            self.assertIn(self.root / "backups", changed)
            self.assertTrue(all(call.args[1:3] == (owner.st_uid, owner.st_gid) for call in chown.call_args_list))
            self.assertGreaterEqual(fchown.call_count, 2)
            self.assertTrue(all(call.args[1:] == (owner.st_uid, owner.st_gid) for call in fchown.call_args_list))

    def test_image_failure_cleans_work_and_never_publishes_an_image(self):
        pack, _ = self.make_pack()
        with patch.object(media, "verify_files", side_effect=OSError("fixture verification failure")):
            with self.assertRaisesRegex(OSError, "fixture verification failure"):
                media.create_image(self.root, pack)
        self.assertEqual(list((self.root / "exports").iterdir()), [self.image])
        self.assertFalse(list((self.root / "tmp").glob("card-image-*")))

    def test_command_pins_external_tempdir_and_disables_appledouble_copying(self):
        runner = SimpleNamespace(returncode=0, stdout=b"ok", stderr=b"")
        with patch.object(media.subprocess, "run", return_value=runner) as run:
            self.assertEqual(media.command(["test-command"], temp_dir=self.root), b"ok")
        environment = run.call_args.kwargs["env"]
        self.assertEqual(environment["TMPDIR"], str(self.root))
        self.assertEqual(environment["COPYFILE_DISABLE"], "1")

    def test_virtual_detach_retries_only_the_owned_image_device(self):
        attached = {"system-entities": [{"dev-entry": "/dev/disk98"}, {"dev-entry": "/dev/disk98s1"}]}
        calls = []

        def respond(args, **kwargs):
            calls.append(args)
            if args[1] == "attach":
                attached["system-entities"][1]["mount-point"] = args[args.index("-mountpoint") + 1]
                return attached
            if "-force" not in args:
                raise RuntimeError("busy fixture")
            return b""

        with patch.object(media, "command", side_effect=respond):
            with media.mounted_image(self.image):
                pass
        self.assertEqual(calls[-2], [media.HDIUTIL, "detach", "/dev/disk98"])
        self.assertEqual(calls[-1], [media.HDIUTIL, "detach", "-force", "/dev/disk98"])

    def test_image_detach_failure_retains_the_working_backing_file(self):
        pack, _ = self.make_pack()

        @contextlib.contextmanager
        def refusing_detach(image, **kwargs):
            image.write_bytes(b"temporary image still attached")
            raise media.ImageDetachError("fixture refused detach")
            yield

        with patch.object(media, "command"), patch.object(media, "mounted_image", side_effect=refusing_detach):
            with self.assertRaisesRegex(media.ImageDetachError, "refused detach"):
                media.create_image(self.root, pack)
        work = list((self.root / "tmp").glob("card-image-*"))
        self.assertEqual(len(work), 1)
        self.assertEqual((work[0] / "disk.cdr").read_bytes(), b"temporary image still attached")
        self.assertFalse((self.root / "exports/puzzle-night.img").exists())

    def test_save_vault_copies_recognized_saves_and_ignores_symlinks(self):
        mount = self.root / "mounted-backup"
        files = {"ed64/gamedata/GAME/save.eep": b"eep", "GBCSYS/SAVE/TEST.srm": b"sram", "GB/Title.gb.rtc": b"rtc", "GB/Title.gb": b"rom"}
        for name, data in files.items():
            path = mount / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        (mount / "GB/leak.sav").symlink_to(self.image)
        backup = self.root / "backups/fixture.img"
        backup.parent.mkdir()
        backup.write_bytes(b"verified full image")
        with patch.object(media, "mounted_image", return_value=contextlib.nullcontext(mount)):
            receipt = media.extract_save_vault(self.root, backup)
        self.assertEqual(receipt["save_files"], 3)
        vault = Path(receipt["save_vault_path"])
        saved = json.loads((vault / "MANIFEST.json").read_text())["files"]
        self.assertEqual({record["path"] for record in saved}, set(files) - {"GB/Title.gb"})
        self.assertEqual((vault / "GBCSYS/SAVE/TEST.srm").read_bytes(), b"sram")
        self.assertFalse((vault / "GB/leak.sav").exists())
        self.assertEqual(backup.read_bytes(), b"verified full image")

    def test_unmountable_save_vault_keeps_verified_full_backup(self):
        backup = self.root / "backups/fixture.img"
        backup.parent.mkdir()
        backup.write_bytes(b"verified full image")
        with patch.object(media, "mounted_image", side_effect=RuntimeError("unrecognized filesystem")):
            receipt = media.extract_save_vault(self.root, backup)
        self.assertEqual(receipt["save_vault_path"], "")
        self.assertIn("unrecognized filesystem", receipt["save_vault_warning"])
        self.assertEqual(backup.read_bytes(), b"verified full image")

    def test_copy_progress_is_json_on_stderr_only(self):
        output = self.root / "copy-output"
        stderr = io.StringIO()
        stdout = io.StringIO()
        with contextlib.redirect_stderr(stderr), contextlib.redirect_stdout(stdout), output.open("wb") as stream:
            media.copy_stream(io.BytesIO(b"payload"), stream, 7, "backup-card")
        events = [json.loads(line) for line in stderr.getvalue().splitlines()]
        self.assertTrue(events)
        self.assertTrue(all(event["event"] == "progress" and event["phase"] == "backup-card" for event in events))
        self.assertEqual(events[-1]["completed"], 7)
        self.assertEqual(stdout.getvalue(), "")

    def test_managed_paths_do_not_follow_an_escape(self):
        (self.root / "escape").symlink_to("/Users")
        with self.assertRaisesRegex(ValueError, "inside"):
            media.managed(self.root, self.root / "escape/anything.img")


class AllFilesBackupTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="all-files-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.fixture_os = b'authored test OS'
        for target, value in [('GB_X7_OS_BYTES', len(self.fixture_os)),
                              ('GB_X7_OS_SHA256', {__import__('hashlib').sha256(self.fixture_os).hexdigest()})]:
            mocked = patch.object(media, target, value)
            mocked.start()
            self.addCleanup(mocked.stop)
        self.reviewed_saves = {'current': [], 'history': [], 'selected_earlier': [], 'kept_in_backup': [], 'warnings': []}
        self.reviewed_preview = {"fixture": "An authored review for installer pipeline tests.", "saves": self.reviewed_saves}
        formatter = patch.object(media, '_preview_saves', return_value=self.reviewed_saves)
        formatter.start()
        self.addCleanup(formatter.stop)
        for target in ("plan_preview", "replacement_preview"):
            mocked = patch.object(media, target, return_value=self.reviewed_preview)
            mocked.start()
            self.addCleanup(mocked.stop)
        for name in ("library", "exports", "backups", "source"):
            (self.root / name).mkdir()
        self.source = self.root / "source"
        self.destination = self.root / "backups/original.files"
        self.image = self.root / "exports/pack.img"
        fake_raw_image(self.image)
        self.card = {"id": "disk99", "name": "Owned directory fixture", "size_bytes": 128 * media.MIB,
                     "block_size": 512, "identity": "source-partition", "physical_identity": "same-media", "mount_points": []}

    def seed(self):
        files = {"N64 Games/A.z64": b"original cartridge", "GBCSYS/SAVE/A.srm": b"original progress",
                 ".DS_Store": b"recorded Finder bytes", "._.": b"recorded volume metadata",
                 ".custom-state/metadata.json": b'{"private":true}', "Private Notes/progress.txt": b"player notes",
                 "Private Notes/._progress.txt": b"arbitrary recorded resource fork", "empty-file": b""}
        for name, data in files.items():
            path = self.source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            os.utime(path, ns=(1680000000000000000, 1680000000000000000))
        (self.source / ".empty-hidden").mkdir()
        (self.source / "Private Notes/empty-shelf").mkdir()
        for path in sorted(self.source.rglob('*'), key=lambda path: len(path.parts), reverse=True):
            if path.is_dir():
                os.utime(path, ns=(1680000000000000000, 1680000000000000000))
        return files

    def capture(self, source=None, destination=None, guard=None):
        mount = source or self.source
        with patch.object(media, "readonly_card_contents", return_value=contextlib.nullcontext((mount, guard or (lambda: None)))):
            return media.backup_all_files(self.root, self.card, destination or self.destination)

    def test_snapshot_preserves_every_hidden_file_empty_directory_timestamp_and_save(self):
        original = self.seed()
        before = media.file_inventory(self.source)
        receipt = self.capture()
        self.assertTrue(receipt["verified"])
        self.assertEqual(receipt["backup_kind"], "files")
        self.assertEqual(receipt["size_bytes"], sum(map(len, original.values())))
        self.assertEqual({row["path"] for row in receipt["files"]}, set(original))
        for name, data in original.items():
            stored = self.destination / "files" / name
            self.assertEqual(stored.read_bytes(), data)
            self.assertEqual(stored.stat().st_mtime_ns, (self.source / name).stat().st_mtime_ns)
        self.assertTrue((self.destination / "files/.empty-hidden").is_dir())
        self.assertTrue((self.destination / "files/Private Notes/empty-shelf").is_dir())
        self.assertEqual(media.file_inventory(self.source), before)
        self.assertEqual(media.image_info(self.root, self.destination)["kind"], "file-backup")
        self.assertFalse(self.destination.with_name(self.destination.name + ".partial").exists())

    def test_card_files_lists_root_system_directories_without_opening_them(self):
        original = self.seed()
        private = {self.source / name for name in media.MACOS_SYSTEM_DIRECTORIES}
        for path in private:
            path.mkdir()
            (path / "private-index").write_bytes(b"macOS runtime data")
        scan = os.scandir

        def denied(path):
            if Path(path) in private:
                raise PermissionError("macOS denies system directory enumeration")
            return scan(path)

        with patch.object(media.os, "scandir", side_effect=denied), \
                patch.object(media, "current_card", return_value=self.card), \
                patch.object(media, "readonly_card_contents", return_value=contextlib.nullcontext((self.source, lambda: None))), \
                patch.object(media, "command"):
            receipt = media.backup_card(self.root, self.card["id"])
        self.assertTrue(receipt["verified"] and receipt["ejected"])
        self.assertEqual(receipt["backup_scope"], "card-files-v1")
        self.assertEqual(receipt["excluded_system_paths"], sorted(media.MACOS_SYSTEM_DIRECTORIES, key=str.casefold))
        manifest = json.loads((Path(receipt["backup_path"]) / "MANIFEST.json").read_text())
        self.assertEqual(manifest["scope"], "card-files-v1")
        self.assertEqual(manifest["excluded_system_paths"], receipt["excluded_system_paths"])
        self.assertEqual({row["path"] for row in manifest["files"]}, set(original))
        for name, data in original.items():
            self.assertEqual((Path(receipt["backup_path"]) / "files" / name).read_bytes(), data)

    def test_nested_system_names_custom_names_and_custom_hidden_files_are_preserved(self):
        names = ("Private Notes/.Spotlight-V100/note.txt", ".Spotlight-V100-custom/note.txt",
                 ".metadata_never_index", ".Trashes-custom")
        for name in names:
            path = self.source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"user content")
        receipt = self.capture()
        self.assertEqual(receipt["excluded_system_paths"], [])
        self.assertEqual({row["path"] for row in receipt["files"]}, set(names))
        for name in names:
            self.assertEqual((self.destination / "files" / name).read_bytes(), b"user content")

    def test_root_system_case_aliases_are_excluded_with_actual_spelling(self):
        original = self.seed()
        aliases = (".SPOTLIGHT-V100", ".FSEVENTSD", ".tRaShEs")
        private = {self.source / name for name in aliases}
        for path in private:
            path.mkdir()
            (path / "private-index").write_bytes(b"macOS runtime data")
        scan = os.scandir

        def denied(path):
            if Path(path) in private:
                raise PermissionError("protected aliases must not be opened")
            return scan(path)

        with patch.object(media.os, "scandir", side_effect=denied):
            receipt = self.capture()
        self.assertEqual(receipt["excluded_system_paths"], sorted(aliases, key=str.casefold))
        self.assertEqual({row["path"] for row in receipt["files"]}, set(original))
        self.assertEqual(media.image_info(self.root, self.destination)["kind"], "file-backup")
        directories = {row["path"] for row in receipt["directories"]}
        self.assertEqual(media.card_payload_inventory(self.source, set(original), directories), [])

    def test_reserved_root_regular_files_require_raw_backup_and_block_legacy_restore(self):
        self.seed()
        self.capture()
        manifest_path = self.destination / "MANIFEST.json"
        baseline = json.loads(manifest_path.read_text())
        aliases = (*sorted(media.MACOS_SYSTEM_DIRECTORIES), ".TRASHES", ".spotLIGHT-v100", ".FSEVENTSD")
        for name in aliases:
            with self.subTest(name=name):
                source = self.source / name
                source.write_bytes(b"user bytes cannot be silently excluded")
                unused = self.root / "backups/reserved-source.files"
                with patch.object(media, "command") as command:
                    with self.assertRaisesRegex(ValueError, "whole card image"):
                        self.capture(destination=unused)
                command.assert_not_called()
                self.assertFalse(unused.exists())
                self.assertFalse(unused.with_name(unused.name + ".partial").exists())
                self.assertEqual(source.read_bytes(), b"user bytes cannot be silently excluded")
                with self.assertRaisesRegex(ValueError, "unexpected file"):
                    media.card_payload_inventory(self.source, {row["path"] for row in baseline["files"]},
                                                 {row["path"] for row in baseline["directories"]})
                source.unlink()
                stored = self.destination / "files" / name
                stored.write_bytes(b"old complete archive root file")
                data = json.loads(json.dumps(baseline))
                data["files"].append({"path": name, "bytes": stored.stat().st_size,
                                      "mtime_ns": stored.stat().st_mtime_ns, "sha256": media.sha256(stored)})
                data["files"].sort(key=lambda row: row["path"])
                data["size_bytes"] += stored.stat().st_size
                manifest_path.write_text(json.dumps(data))
                (self.destination / "MANIFEST.sha256").write_text(media.sha256(manifest_path) + "\n")
                with self.assertRaisesRegex(ValueError, "protected macOS root paths"):
                    media.file_backup_info(self.root, self.destination)
                data.pop("scope")
                data.pop("excluded_system_paths")
                manifest_path.write_text(json.dumps(data))
                (self.destination / "MANIFEST.sha256").write_text(media.sha256(manifest_path) + "\n")
                self.assertTrue(media.file_backup_info(self.root, self.destination)["verified"])
                with patch.object(media, "command") as command, patch.object(media, "install_file_tree") as write:
                    with self.assertRaisesRegex(ValueError, "whole card image"):
                        media.image_info(self.root, self.destination)
                    with self.assertRaisesRegex(ValueError, "whole card image"):
                        media.install_file_backup(self.root, self.card, self.destination, self.image, {}, {}, {})
                command.assert_not_called()
                write.assert_not_called()
                stored.unlink()

    def test_unreadable_nested_system_named_or_custom_directories_are_not_skipped(self):
        scan = os.scandir
        for name in ("Private Notes/.Spotlight-V100", ".Spotlight-V100-custom", ".custom-private"):
            with self.subTest(path=name):
                hidden = self.source / name
                hidden.mkdir(parents=True, exist_ok=True)

                def denied(path):
                    if Path(path) == hidden:
                        raise PermissionError("user content must never be skipped")
                    return scan(path)

                with patch.object(media.os, "scandir", side_effect=denied):
                    with self.assertRaises(PermissionError):
                        self.capture()
                self.assertFalse(self.destination.exists())
                hidden.rmdir()

    def test_unreadable_custom_hidden_file_aborts_without_publication_or_card_write(self):
        hidden = self.source / ".private-progress"
        hidden.write_bytes(b"user progress must be preserved")
        source_open = media.uncached_file

        def denied(path):
            if path == hidden:
                raise PermissionError("user file must never be skipped")
            return source_open(path)

        with patch.object(media, "uncached_file", side_effect=denied), patch.object(media, "command") as command:
            with self.assertRaises(PermissionError):
                self.capture()
        command.assert_not_called()
        self.assertFalse(self.destination.exists())
        self.assertEqual(hidden.read_bytes(), b"user progress must be preserved")

    def test_system_named_symlink_or_special_file_is_rejected_before_exclusion(self):
        path = self.source / ".Spotlight-V100"
        outside = self.root / "outside"
        outside.mkdir()
        path.symlink_to(outside)
        with self.assertRaisesRegex(ValueError, "link, special"):
            self.capture()
        path.unlink()
        os.mkfifo(path)
        with self.assertRaisesRegex(ValueError, "link, special"):
            self.capture()
        self.assertFalse(self.destination.exists())

    def test_snapshot_validation_never_excludes_unrecorded_system_directory_extras(self):
        self.seed()
        self.capture()
        with patch.object(media, "current_card", return_value=self.card):
            plan = media.plan_install(self.root, self.destination, self.card["id"])
        extra = self.destination / "files/.Spotlight-V100"
        extra.mkdir()
        (extra / "unrecorded").write_bytes(b"unexpected snapshot data")
        with self.assertRaisesRegex(ValueError, "inventory"):
            media.file_backup_info(self.root, self.destination)
        with patch.object(media, "current_card", return_value=self.card), patch.object(media, "command") as command:
            with self.assertRaisesRegex(ValueError, "inventory"):
                media.install(self.root, Path(plan["plan_path"]), plan["token"])
        command.assert_not_called()
        scan = os.scandir

        def denied(path):
            if Path(path) == extra:
                raise PermissionError("snapshot validation must inspect every directory")
            return scan(path)

        with patch.object(media.os, "scandir", side_effect=denied):
            with self.assertRaises(PermissionError):
                media.file_backup_info(self.root, self.destination)

    def test_unrecognized_scope_or_nonwhitelisted_exclusions_are_rejected(self):
        self.capture()
        manifest = self.destination / "MANIFEST.json"
        baseline = json.loads(manifest.read_text())
        changes = ({"scope": "all-hidden-files"}, {"excluded_system_paths": [".custom-private"]},
                   {"excluded_system_paths": ["Private Notes/.Spotlight-V100"]},
                   {"excluded_system_paths": [".Trashes", ".Trashes"]},
                   {"excluded_system_paths": [".TRASHES", ".Trashes"]}, {"excluded_system_paths": ".Trashes"})
        for change in changes:
            with self.subTest(change=change):
                manifest.write_text(json.dumps({**baseline, **change}))
                (self.destination / "MANIFEST.sha256").write_text(media.sha256(manifest) + "\n")
                with self.assertRaisesRegex(ValueError, "scope or exclusions"):
                    media.file_backup_info(self.root, self.destination)

    def test_changed_source_bytes_are_caught_by_second_uncached_read(self):
        path = self.source / "save.srm"
        path.write_bytes(b"initial progress")
        timestamp = path.stat().st_mtime_ns
        original_open = media.uncached_file
        opens = 0

        def changed(source):
            nonlocal opens
            opens += 1
            if opens == 2:
                path.write_bytes(b"changed progress")
                os.utime(path, ns=(timestamp, timestamp))
            return original_open(source)

        with patch.object(media, "uncached_file", side_effect=changed):
            with self.assertRaisesRegex(OSError, "source file changed"):
                self.capture()
        self.assertFalse(self.destination.exists())
        partial = self.destination.with_name(self.destination.name + ".partial")
        with self.assertRaisesRegex(ValueError, "published"):
            media.image_info(self.root, partial)

    def test_original_file_timestamps_are_fsynced_before_snapshot_publication(self):
        original = self.seed()
        synced = set()
        fsync, rename = os.fsync, Path.rename
        publications = []

        def record_sync(descriptor):
            details = os.fstat(descriptor)
            synced.add((details.st_ino, details.st_mtime_ns))
            return fsync(descriptor)

        def publish(work, destination):
            for name in original:
                details = (work / "files" / name).stat()
                self.assertIn((details.st_ino, details.st_mtime_ns), synced)
                self.assertEqual(details.st_mtime_ns, (self.source / name).stat().st_mtime_ns)
            publications.append(destination)
            return rename(work, destination)

        with patch.object(media.os, "fsync", side_effect=record_sync), patch.object(Path, "rename", publish):
            self.capture()
        self.assertEqual(publications, [self.destination])

    def test_permission_error_in_hidden_directory_is_not_silently_omitted(self):
        hidden = self.source / ".private"
        hidden.mkdir()
        (hidden / "save.srm").write_bytes(b"must preserve")
        scan = os.scandir

        def denied(path):
            if Path(path) == hidden:
                raise PermissionError("Fixture denies directory enumeration")
            return scan(path)

        with patch.object(media.os, "scandir", side_effect=denied), patch.object(media, "command") as command:
            with self.assertRaises(PermissionError):
                self.capture()
        command.assert_not_called()
        self.assertFalse(self.destination.exists())

    def test_symlinks_and_special_files_fail_before_backup_publication(self):
        outside = self.root / "outside"
        outside.write_bytes(b"not a card file")
        link = self.source / "link"
        link.symlink_to(outside)
        with self.assertRaisesRegex(ValueError, "link, special"):
            self.capture()
        link.unlink()
        os.mkfifo(self.source / "pipe")
        with self.assertRaisesRegex(ValueError, "link, special"):
            self.capture()
        self.assertFalse(self.destination.exists())
        self.assertEqual(outside.read_bytes(), b"not a card file")

    def test_card_swap_during_file_backup_never_publishes_snapshot(self):
        self.seed()
        calls = 0

        def guard():
            nonlocal calls
            calls += 1
            if calls == 2:
                raise ValueError("Fixture physical card changed")

        with self.assertRaisesRegex(ValueError, "card changed"):
            self.capture(guard=guard)
        self.assertFalse(self.destination.exists())

    def test_corrupt_snapshot_or_manifest_cannot_be_reviewed_for_recovery(self):
        self.seed()
        self.capture()
        path = self.destination / "files/GBCSYS/SAVE/A.srm"
        timestamp = path.stat().st_mtime_ns
        path.write_bytes(b"corrupted progress")
        os.utime(path, ns=(timestamp, timestamp))
        with self.assertRaises(ValueError):
            media.image_info(self.root, self.destination)
        path.write_bytes(b"original progress")
        os.utime(path, ns=(timestamp, timestamp))
        manifest = self.destination / "MANIFEST.json"
        manifest.write_text(manifest.read_text() + " ")
        with self.assertRaisesRegex(ValueError, "manifest changed"):
            media.image_info(self.root, self.destination)

    def test_file_mode_is_default_bound_to_plan_and_does_not_require_card_capacity_on_host(self):
        large = {**self.card, "size_bytes": 128 * 1024**3}
        with patch.object(media, "current_card", return_value=large), \
                patch.object(media.shutil, "disk_usage", return_value=SimpleNamespace(free=128 * media.MIB)):
            plan = media.plan_install(self.root, self.image, large["id"])
            self.assertEqual(plan["backup_kind"], "files")
            self.assertEqual(plan["backup_scope"], "card-files-v1")
            self.assertTrue(plan["backup_path"].endswith('.files'))
            with self.assertRaises(OSError):
                media.plan_install(self.root, self.image, large["id"], "raw")
            changed = {**plan, "backup_kind": "raw"}
            with self.assertRaisesRegex(ValueError, "confirmation"):
                media.validate_plan(self.root, changed, plan["token"])
            changed["token"] = media.plan_token(changed)
            with self.assertRaisesRegex(ValueError, "destination does not match"):
                media.validate_plan(self.root, changed, changed["token"])
            old = {key: value for key, value in plan.items() if key != "backup_scope"}
            old["token"] = media.plan_token(old)
            with self.assertRaisesRegex(ValueError, "backup scope changed"):
                media.validate_plan(self.root, old, old["token"])

    def test_raw_historical_restore_forces_raw_prebackup(self):
        source = self.root / "backups/original.img"
        source.write_bytes(self.image.read_bytes())
        media.write_json(source.with_suffix('.img.json'), {"backup_path": str(source), "verified": True,
                         "size_bytes": source.stat().st_size, "sha256": media.sha256(source)})
        with patch.object(media, "current_card", return_value=self.card):
            plan = media.plan_install(self.root, source, self.card["id"], "files")
        self.assertEqual(plan["backup_kind"], "raw")
        self.assertNotIn("backup_scope", plan)
        self.assertEqual(plan["install_mode"], "raw-backup")
        self.assertTrue(plan["backup_path"].endswith('.img'))

    def test_backup_only_keeps_internal_snapshot_manifest_restorable_after_ejection(self):
        self.seed()
        with patch.object(media, "current_card", return_value=self.card), \
                patch.object(media, "readonly_card_contents", return_value=contextlib.nullcontext((self.source, lambda: None))), \
                patch.object(media, "command") as command:
            receipt = media.backup_card(self.root, self.card["id"])
        self.assertEqual(receipt["backup_kind"], "files")
        self.assertTrue(receipt["verified"] and receipt["ejected"])
        command.assert_called_once_with([media.DISKUTIL, "eject", "/dev/disk99"])
        snapshot = Path(receipt["backup_path"])
        verified = media.file_backup_info(self.root, snapshot)
        self.assertEqual(verified["sha256"], receipt["sha256"])
        self.assertNotIn("ejected", json.loads((snapshot / "MANIFEST.json").read_text()))
        self.assertTrue(snapshot.with_suffix('.completed.json').is_file())
        self.assertEqual(media.image_info(self.root, snapshot)["kind"], "file-backup")

    def test_empty_formatted_card_has_a_complete_restorable_empty_snapshot(self):
        receipt = self.capture()
        self.assertEqual(receipt["files"], [])
        self.assertEqual(receipt["directories"], [])
        self.assertEqual(receipt["size_bytes"], 0)
        self.assertEqual(media.image_info(self.root, self.destination)["kind"], "file-backup")

    def test_untrusted_manifest_paths_collisions_and_timestamp_types_are_rejected(self):
        self.seed()
        self.capture()
        manifest = self.destination / "MANIFEST.json"
        baseline = json.loads(manifest.read_text())
        for change in ('../outside', '/absolute', '.', 'collision', 'timestamp'):
            with self.subTest(change=change):
                data = json.loads(json.dumps(baseline))
                if change == 'collision':
                    data['files'].append(dict(data['files'][0]))
                elif change == 'timestamp':
                    data['files'][0]['mtime_ns'] = True
                else:
                    data['files'][0]['path'] = change
                manifest.write_text(json.dumps(data))
                (self.destination / "MANIFEST.sha256").write_text(media.sha256(manifest) + '\n')
                with self.assertRaises(ValueError):
                    media.file_backup_info(self.root, self.destination)

    def test_changed_folder_restore_source_aborts_before_any_card_command(self):
        self.seed()
        self.capture()
        with patch.object(media, "current_card", return_value=self.card):
            plan = media.plan_install(self.root, self.destination, self.card["id"])
        changed = self.destination / "files/Private Notes/progress.txt"
        stamp = changed.stat().st_mtime_ns
        changed.write_bytes(b"edited notes")
        os.utime(changed, ns=(stamp, stamp))
        with patch.object(media, "current_card", return_value=self.card), patch.object(media, "command") as command:
            with self.assertRaises(ValueError):
                media.install(self.root, Path(plan["plan_path"]), plan["token"])
        command.assert_not_called()
        self.assertEqual((self.source / "Private Notes/progress.txt").read_bytes(), b"player notes")

    def test_source_partition_uuid_and_readonly_state_are_guarded(self):
        listing = {"AllDisksAndPartitions": [{"DeviceIdentifier": "disk99", "Partitions": [{"DeviceIdentifier": "disk99s1"}]}]}
        info = {"DeviceIdentifier": "disk99s1", "ParentWholeDisk": "disk99", "FilesystemType": "msdos",
                "VolumeUUID": "original-volume", "WritableVolume": True, "MountPoint": "/Volumes/Fixture"}
        with patch.object(media, "current_card", return_value=self.card), \
                patch.object(media, "command", return_value=listing), patch.object(media, "disk_info", return_value=info):
            with self.assertRaisesRegex(ValueError, "read-only"):
                media.card_file_volume(self.root, self.card, "disk99s1", "original-volume", readonly=True)
            with self.assertRaisesRegex(ValueError, "source volume changed"):
                media.card_file_volume(self.root, self.card, "disk99s1", "wrong-volume")

    def test_folder_restore_preserves_original_metadata_and_saves_without_remapping(self):
        original = self.seed()
        self.capture()
        manifest_path = self.destination / "MANIFEST.json"
        legacy = json.loads(manifest_path.read_text())
        legacy.pop("scope")
        legacy.pop("excluded_system_paths")
        manifest_path.write_text(json.dumps(legacy))
        (self.destination / "MANIFEST.sha256").write_text(media.sha256(manifest_path) + "\n")
        self.assertNotIn("backup_scope", media.file_backup_info(self.root, self.destination))
        current = self.root / "current-card"
        current.mkdir()
        (current / "newer-game.srm").write_bytes(b"current card must also be backed up")
        mount = self.root / "restored-card"
        mount.mkdir()
        generated = b"newly generated metadata"

        def command(args, **kwargs):
            if args[1] == "eraseDisk":
                (mount / "Private Notes").mkdir()
                (mount / "Private Notes/._progress.txt").write_bytes(generated)
                (mount / "._.").write_bytes(generated)
            return b""

        info = {"VolumeUUID": "recovery-volume", "DeviceIdentifier": "disk99s1", "TotalSize": 125000000,
                "VolumeAllocationBlockSize": 16384}
        no_remapping = SimpleNamespace(validate_choices=validate_choices, build_restore_plan=lambda *_args, **_kwargs: self.fail("Folder recovery must not remap saves"))
        with patch.object(media, "current_card", return_value=self.card), \
                patch.object(media, "readonly_card_contents", return_value=contextlib.nullcontext((current, lambda: None))), \
                patch.object(media, "formatted_card", return_value=(info, mount)), \
                patch.object(media, "command", side_effect=command), \
                patch.object(media, "opened_device", side_effect=AssertionError("File mode must not open raw devices")), \
                patch.dict(sys.modules, {"card_saves": no_remapping}):
            plan = media.plan_install(self.root, self.destination, self.card["id"])
            result = media.install(self.root, Path(plan["plan_path"]), plan["token"])
        self.assertTrue(result["verified"] and result["ejected"])
        self.assertEqual(result["image_kind"], "file-backup")
        self.assertEqual(result["backup_kind"], "files")
        self.assertEqual(result["backup_scope"], "card-files-v1")
        self.assertEqual(result["excluded_system_paths"], [])
        self.assertEqual(result["restored_files"], len(original))
        self.assertEqual(result["read_back_passes"], 2)
        for name, data in original.items():
            self.assertEqual((mount / name).read_bytes(), data)
        for row in media.file_backup_info(self.root, self.destination)["files"]:
            self.assertEqual((mount / row['path']).stat().st_mtime_ns, row['mtime_ns'])
        self.assertTrue((mount / ".empty-hidden").is_dir())
        self.assertTrue((mount / "Private Notes/empty-shelf").is_dir())
        self.assertEqual((Path(result["backup_path"]) / "files/newer-game.srm").read_bytes(), b"current card must also be backed up")
        archived = Path(result["backup_path"]).with_suffix('.mac-metadata') / "replaced/Private Notes/._progress.txt"
        self.assertEqual(archived.read_bytes(), generated)

    def test_recorded_protected_root_folders_are_inspectable_but_refused_before_restore(self):
        self.seed()
        self.capture()
        manifest_path = self.destination / "MANIFEST.json"
        baseline = json.loads(manifest_path.read_text())
        baseline.pop("scope")
        baseline.pop("excluded_system_paths")
        for name in (*sorted(media.MACOS_SYSTEM_DIRECTORIES), ".SPOTLIGHT-V100"):
            with self.subTest(folder=name):
                folder = self.destination / "files" / name
                folder.mkdir()
                stored = folder / "recorded-index"
                stored.write_bytes(b"original protected metadata stays readable in the backup")
                legacy = json.loads(json.dumps(baseline))
                legacy["files"].append({"path": name + "/recorded-index", "bytes": stored.stat().st_size,
                                        "mtime_ns": stored.stat().st_mtime_ns, "sha256": media.sha256(stored)})
                legacy["files"].sort(key=lambda row: row["path"])
                legacy["directories"].append({"path": name, "mtime_ns": folder.stat().st_mtime_ns})
                legacy["directories"].sort(key=lambda row: row["path"])
                legacy["size_bytes"] += stored.stat().st_size
                manifest_path.write_text(json.dumps(legacy))
                (self.destination / "MANIFEST.sha256").write_text(media.sha256(manifest_path) + "\n")
                self.assertTrue(media.file_backup_info(self.root, self.destination)["verified"])
                scoped = {**legacy, "scope": media.CARD_FILE_SCOPE, "excluded_system_paths": []}
                manifest_path.write_text(json.dumps(scoped))
                (self.destination / "MANIFEST.sha256").write_text(media.sha256(manifest_path) + "\n")
                with self.assertRaisesRegex(ValueError, "protected macOS root paths"):
                    media.file_backup_info(self.root, self.destination)
                manifest_path.write_text(json.dumps(legacy))
                (self.destination / "MANIFEST.sha256").write_text(media.sha256(manifest_path) + "\n")
                with patch.object(media, "current_card") as current, patch.object(media, "command") as command, \
                        patch.object(media, "verify_backup") as verify, patch.object(media, "install_file_tree") as write:
                    with self.assertRaisesRegex(ValueError, "protected macOS root folders"):
                        media.image_info(self.root, self.destination)
                    with self.assertRaisesRegex(ValueError, "whole card image"):
                        media.plan_install(self.root, self.destination, self.card["id"])
                    with self.assertRaisesRegex(ValueError, "protected macOS root folders"):
                        media.install_file_backup(self.root, self.card, self.destination, self.image, {}, {}, {})
                current.assert_not_called()
                command.assert_not_called()
                verify.assert_not_called()
                write.assert_not_called()
                stored.unlink()
                folder.rmdir()


class AuthopenDescriptorTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="authopen-fixture-")
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "card-fixture"
        self.path.write_bytes(b"unchanged card fixture")

    @unittest.skipUnless(sys.platform == 'darwin' and Path(media.AUTHOPEN).is_file(), 'Apple authopen integration')
    def test_real_apple_helper_accepts_safe_flags_and_returns_null_device_descriptor(self):
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_EXLOCK | os.O_NONBLOCK
        descriptor = media.authorized_device_descriptor(Path('/dev/null'), flags)
        try:
            self.assertEqual(os.fstat(descriptor).st_rdev, Path('/dev/null').stat().st_rdev)
            self.assertFalse(os.get_inheritable(descriptor))
        finally:
            os.close(descriptor)

    def process(self, returncode=0, diagnostic=b""):
        return SimpleNamespace(returncode=returncode,
                               communicate=lambda **_kwargs: (b"", diagnostic),
                               poll=lambda: returncode)

    def test_receives_authorized_descriptor_over_real_private_socket(self):
        calls = []

        def launch(args, **kwargs):
            calls.append((args, kwargs))
            descriptor = os.open(self.path, os.O_RDONLY)
            try:
                kwargs['stdout'].sendmsg([b'\0'], [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array('i', [descriptor]))])
            finally:
                os.close(descriptor)
            return self.process()

        with patch.object(media.subprocess, 'Popen', side_effect=launch):
            descriptor = media.authorized_device_descriptor(self.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            self.assertEqual(os.read(descriptor, 64), b"unchanged card fixture")
            self.assertFalse(os.get_inheritable(descriptor))
        finally:
            os.close(descriptor)
        self.assertEqual(calls[0][0], [media.AUTHOPEN, '-stdoutpipe', '-o', str(os.O_RDONLY | 0x20000000 | os.O_CLOEXEC), str(self.path)])
        self.assertEqual(calls[0][1]['stdin'], media.subprocess.DEVNULL)
        self.assertNotIn('-w', calls[0][0])
        self.assertEqual(self.path.read_bytes(), b"unchanged card fixture")

    def transport(self, descriptors=(), *, message=b'x', flags=0, exit_code=0, error=None):
        class Channel:
            def __init__(self):
                self.closed = False
            def settimeout(self, value):
                self.timeout = value
            def close(self):
                self.closed = True
            def recvmsg(self, *_):
                if error:
                    raise error
                ancillary = [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array('i', descriptors).tobytes())] if descriptors else []
                return message, ancillary, flags, None
        parent, child = Channel(), Channel()
        return parent, child, self.process(exit_code, b'Authorization cancelled' if exit_code else b'')

    def test_writable_authorized_descriptor_does_not_truncate_existing_bytes(self):
        def launch(_args, **kwargs):
            descriptor = os.open(self.path, os.O_RDWR)
            try:
                kwargs['stdout'].sendmsg([b'x'], [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array('i', [descriptor]))])
            finally:
                os.close(descriptor)
            return self.process()

        with patch.object(media.subprocess, 'Popen', side_effect=launch):
            descriptor = media.authorized_device_descriptor(self.path, os.O_RDWR | os.O_NOFOLLOW)
        try:
            self.assertEqual(self.path.read_bytes(), b"unchanged card fixture")
            os.write(descriptor, b'changed')
        finally:
            os.close(descriptor)
        self.assertEqual(self.path.read_bytes(), b"changeded card fixture")

    def test_cancelled_authorization_does_not_open_or_change_card(self):
        parent, child, process = self.transport(message=b'', exit_code=1)
        with patch.object(media.socket, 'socketpair', return_value=(parent, child)), patch.object(media.subprocess, 'Popen', return_value=process):
            with self.assertRaisesRegex(PermissionError, 'Authorization cancelled'):
                media.authorized_device_descriptor(self.path, os.O_RDONLY)
        self.assertTrue(parent.closed and child.closed)
        self.assertEqual(self.path.read_bytes(), b"unchanged card fixture")

    def test_zero_or_multiple_descriptors_are_rejected_and_closed(self):
        for count in (0, 2):
            with self.subTest(count=count):
                descriptors = [os.open(self.path, os.O_RDONLY) for _ in range(count)]
                parent, child, process = self.transport(descriptors)
                with patch.object(media.socket, 'socketpair', return_value=(parent, child)), patch.object(media.subprocess, 'Popen', return_value=process):
                    with self.assertRaisesRegex(RuntimeError, 'invalid file descriptor'):
                        media.authorized_device_descriptor(self.path, os.O_RDONLY)
                for descriptor in descriptors:
                    with self.assertRaises(OSError):
                        os.fstat(descriptor)
                self.assertTrue(parent.closed and child.closed)

    def test_nonzero_helper_exit_closes_any_received_descriptor(self):
        descriptor = os.open(self.path, os.O_RDONLY)
        parent, child, process = self.transport([descriptor], exit_code=1)
        with patch.object(media.socket, 'socketpair', return_value=(parent, child)), patch.object(media.subprocess, 'Popen', return_value=process):
            with self.assertRaises(PermissionError):
                media.authorized_device_descriptor(self.path, os.O_RDONLY)
        with self.assertRaises(OSError):
            os.fstat(descriptor)

    def test_truncated_ancillary_frame_is_rejected_and_descriptor_closed(self):
        descriptor = os.open(self.path, os.O_RDONLY)
        parent, child, process = self.transport([descriptor], flags=socket.MSG_CTRUNC)
        with patch.object(media.socket, 'socketpair', return_value=(parent, child)), patch.object(media.subprocess, 'Popen', return_value=process):
            with self.assertRaisesRegex(RuntimeError, 'invalid file descriptor'):
                media.authorized_device_descriptor(self.path, os.O_RDONLY)
        with self.assertRaises(OSError):
            os.fstat(descriptor)

    def test_wrong_access_mode_is_rejected_and_descriptor_closed(self):
        descriptor = os.open(self.path, os.O_RDONLY)
        parent, child, process = self.transport([descriptor])
        with patch.object(media.socket, 'socketpair', return_value=(parent, child)), patch.object(media.subprocess, 'Popen', return_value=process):
            with self.assertRaisesRegex(RuntimeError, 'wrong access mode'):
                media.authorized_device_descriptor(self.path, os.O_RDWR)
        with self.assertRaises(OSError):
            os.fstat(descriptor)

    def test_authorization_timeout_terminates_owned_helper_and_closes_channels(self):
        parent, child, process = self.transport(error=socket.timeout())
        stopped = []
        process.poll = lambda: None
        process.terminate = lambda: stopped.append(True)
        with patch.object(media.socket, 'socketpair', return_value=(parent, child)), patch.object(media.subprocess, 'Popen', return_value=process):
            with self.assertRaisesRegex(TimeoutError, 'did not finish'):
                media.authorized_device_descriptor(self.path, os.O_RDONLY)
        self.assertEqual(stopped, [True])
        self.assertTrue(parent.closed and child.closed)

    def test_unsafe_creation_flags_are_rejected_before_authorization(self):
        for flags in (os.O_CREAT, os.O_TRUNC, os.O_APPEND):
            with self.subTest(flags=flags), patch.object(media.subprocess, 'Popen') as launch:
                with self.assertRaisesRegex(ValueError, 'cannot create'):
                    media.authorized_device_descriptor(self.path, flags)
                launch.assert_not_called()
        self.assertEqual(self.path.read_bytes(), b"unchanged card fixture")

    def test_received_descriptor_must_still_match_reviewed_device(self):
        other = self.path.with_name('other-card')
        other.write_bytes(b'wrong device')
        descriptor = os.open(other, os.O_RDONLY)
        with patch.object(media, 'raw_device', return_value=self.path), patch.object(media.os, 'geteuid', return_value=501), patch.object(media, 'authorized_device_descriptor', return_value=descriptor):
            with self.assertRaisesRegex(ValueError, 'device changed'):
                with media.opened_device('disk99', writable=False):
                    self.fail('A mismatched descriptor reached card IO')
        with self.assertRaises(OSError):
            os.fstat(descriptor)
        self.assertEqual(other.read_bytes(), b'wrong device')


class MediaRepositoryBoundary(unittest.TestCase):
    def test_refuses_backup_destination_inside_git_checkout(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root / '.git').mkdir()
            child = root / 'data'
            child.mkdir()
            for library in (root, child):
                with self.subTest(library=library), self.assertRaisesRegex(ValueError, 'Git repositories'):
                    media.media_directory(library, library / 'backups')
                self.assertFalse((library / 'backups').exists())

    def test_refuses_git_worktree_marker(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root / '.git').write_text('gitdir: ../other-worktree\n')
            with self.assertRaisesRegex(ValueError, 'Git repositories'):
                media.workspace(root)

    def test_refuses_unpacked_source_archive(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root / 'app').mkdir()
            for name in ('release.json', 'library_manager.py', 'app/CartShelf.swift'):
                (root / name).write_text('authored source marker')
            with self.assertRaisesRegex(ValueError, 'source folders'):
                media.workspace(root)


if __name__ == "__main__":
    unittest.main()
