import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import card_saves
import library_experience as experience
from library_manager import Library
import recipes


ROOT = Path(__file__).resolve().parents[1]
VOLUME_A = "11111111-1111-4111-8111-111111111111"
VOLUME_B = "22222222-2222-4222-8222-222222222222"
VOLUME_C = "33333333-3333-4333-8333-333333333333"


def fixture_rom():
    data = bytearray(32768)
    data[0x104:0x134] = card_saves.GB_LOGO
    data[0x134:0x13b] = b"FIXTURE"
    data[0x147] = 0x10
    checksum = 0
    for byte in data[0x134:0x14d]:
        checksum = (checksum - byte - 1) & 255
    data[0x14d] = checksum
    return bytes(data)


class ExperienceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="cartshelf-experience-")
        self.root = Path(self.temporary.name).resolve()
        self.library = Library(self.root)
        with contextlib.redirect_stderr(io.StringIO()):
            self.library.initialize()

    def tearDown(self):
        self.temporary.cleanup()

    def write(self, path, data):
        path = self.root / path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def add_game(self):
        self.write("library/GB-GBC/Imports/Fixture (v1.0).gb", fixture_rom())
        with contextlib.redirect_stderr(io.StringIO()):
            return self.library.scan()["releases"][0]

    def archive_saves(self, version=b"a"):
        game = fixture_rom()
        self.write("fixture-backup/GB/A-F/Old.gb", game)
        self.write("fixture-pack/GB/A-F/New.gb", game)
        self.write("fixture-backup/GBCSYS/SAVE/Old.srm", version * 8192)
        self.write("fixture-backup/GBCSYS/SAVE/Old.rtc", version * 512)
        card_saves.build_restore_plan(self.root / "fixture-backup", self.root / "fixture-pack", "chromatic", self.root / "backups/save-history")
        return hashlib.sha256(game).hexdigest()

    def receipt(self, name, old, new, *, verified=True, date="2026-01-01T00:00:00+00:00", image="Fixture pack.img",
                image_kind="compilation", install_mode="full-capacity-files"):
        backup = self.root / "backups" / (name + ".files")
        backup.mkdir(parents=True)
        record = {"verified": verified, "device": {"volume_uuid": old}, "volume_uuid": new,
                  "ejected_at_utc": date, "image_path": image, "backup_path": str(backup),
                  "image_kind": image_kind, "install_mode": install_mode}
        path = self.root / "library/card-plans" / (name + ".completed.json")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(record))
        return path, record

    def test_recipe_round_trip_creates_new_exact_selection(self):
        release = self.add_game()
        with contextlib.redirect_stderr(io.StringIO()):
            recipe = experience.export_recipe(self.library, "chromatic", "Fixture night", [release["id"]])
            path = self.write("incoming.cartshelf.json", recipes.encode_recipe(recipe))
            inspected = experience.inspect_recipe(self.library, path)
            first = experience.import_recipe(self.library, path, inspected["sha256"], "Imported fixture")
            second = experience.import_recipe(self.library, path, inspected["sha256"], "Another fixture")
        self.assertNotEqual(first["collection_id"], second["collection_id"])
        self.assertEqual(first["matched_release_ids"], [release["id"]])
        saved = json.loads((self.library.collections_dir / (first["collection_id"] + ".json")).read_text())
        self.assertEqual(saved["release_ids"], [release["id"]])
        self.assertEqual(first["missing"], [])

    def test_recipe_edited_after_review_creates_no_collection(self):
        release = self.add_game()
        recipe = recipes.recipe_for_selection("Fixture", "chromatic", [release["id"]], [release])
        path = self.write("incoming.cartshelf.json", recipes.encode_recipe(recipe))
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        recipe["description"] = "Changed after inspection"
        path.write_bytes(recipes.encode_recipe(recipe))
        with self.assertRaisesRegex(ValueError, "changed after inspection"):
            experience.import_recipe(self.library, path, digest, "Import")
        self.assertEqual(list(self.library.collections_dir.glob("*.json")), [])

    def test_generated_collection_collision_preserves_existing_set(self):
        release = self.add_game()
        recipe = recipes.recipe_for_selection("Fixture", "chromatic", [release["id"]], [release])
        path = self.write("incoming.cartshelf.json", recipes.encode_recipe(recipe))
        collection_id = "a" * 32
        original_bytes = json.dumps({"id": collection_id, "name": "Existing", "profile": "chromatic", "release_ids": [release["id"]]}).encode()
        original = self.write("library/collections/" + collection_id + ".json", original_bytes)
        with mock.patch.object(experience.uuid, "uuid4", return_value=mock.Mock(hex=collection_id)):
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaisesRegex(ValueError, "already exists"):
                experience.import_recipe(self.library, path, hashlib.sha256(path.read_bytes()).hexdigest(), "Import")
        self.assertEqual(original.read_bytes(), original_bytes)

    def test_fully_missing_recipe_never_imports_full_library(self):
        release = self.add_game()
        recipe = recipes.recipe_for_selection("Missing", "chromatic", [release["id"]], [release])
        recipe["games"][0]["sha256"] = "d" * 64
        path = self.write("incoming.cartshelf.json", recipes.encode_recipe(recipe))
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaisesRegex(ValueError, "No exact"):
            experience.import_recipe(self.library, path, hashlib.sha256(path.read_bytes()).hexdigest(), "Missing")
        self.assertEqual(list(self.library.collections_dir.glob("*.json")), [])

    def test_partial_recipe_reports_missing_exact_revision(self):
        release = self.add_game()
        recipe = recipes.recipe_for_selection("Mixed", "chromatic", [release["id"]], [release])
        recipe["games"].append({"sha256": "d" * 64, "title": "Fixture Adventure", "version": "2.0"})
        path = self.write("incoming.cartshelf.json", recipes.encode_recipe(recipe))
        with contextlib.redirect_stderr(io.StringIO()):
            inspected = experience.inspect_recipe(self.library, path)
            imported = experience.import_recipe(self.library, path, inspected["sha256"], "Mixed")
        self.assertEqual(imported["matched_release_ids"], [release["id"]])
        self.assertEqual(imported["missing"], [recipe["games"][1]])

    def test_recipe_reader_refuses_symlink_and_oversized_input(self):
        original = self.write("input.json", b"{}")
        link = self.root / "link.json"
        link.symlink_to(original)
        with self.assertRaises(ValueError):
            experience.inspect_recipe(self.library, link)
        original.write_bytes(b" " * (recipes.MAX_RECIPE_BYTES + 1))
        with self.assertRaises(ValueError):
            experience.inspect_recipe(self.library, original)
        fifo = self.root / "pipe.json"
        os.mkfifo(fifo)
        with self.assertRaises(ValueError):
            experience.inspect_recipe(self.library, fifo)

    def test_timeline_keeps_all_versions_and_inspection_is_read_only(self):
        identity = self.archive_saves()
        self.archive_saves(b"b")
        history = self.root / "backups/save-history"
        before = {str(path.relative_to(history)): path.read_bytes() for path in history.rglob("*") if path.is_file()}
        entries = experience.save_history(self.library, "chromatic", identity)["entries"]
        self.assertEqual(len(entries), 2)
        self.assertTrue(all(row["verified"] is False for row in entries))
        inspected = experience.inspect_save(self.library, "chromatic", identity, entries[-1]["snapshot_id"])
        self.assertTrue(inspected["verified"])
        self.assertEqual({row["key"] for row in inspected["files"]}, {"srm", "rtc"})
        self.assertTrue(all("source_path" not in row for row in inspected["files"]))
        after = {str(path.relative_to(history)): path.read_bytes() for path in history.rglob("*") if path.is_file()}
        self.assertEqual(before, after)

    def test_export_is_immutable_complete_verified_and_portable(self):
        identity = self.archive_saves()
        entry = experience.save_history(self.library, "chromatic", identity)["entries"][0]
        result = experience.export_save(self.library, "chromatic", identity, entry["snapshot_id"], entry["bundle_sha256"])
        path = Path(result["path"])
        self.assertTrue(path.is_relative_to(self.root / "exports/saves"))
        self.assertEqual(result["file_count"], 2)
        encoded = (path / "MANIFEST.json").read_bytes()
        self.assertNotIn(str(self.root).encode(), encoded)
        self.assertEqual((path / "MANIFEST.sha256").read_text().strip(), hashlib.sha256(encoded).hexdigest())
        manifest = json.loads(encoded)
        for row in manifest["files"]:
            self.assertEqual(hashlib.sha256((path / row["path"]).read_bytes()).hexdigest(), row["sha256"])
        second = experience.export_save(self.library, "chromatic", identity, entry["snapshot_id"])
        self.assertNotEqual(result["path"], second["path"])
        self.assertTrue(path.exists())

    def test_changed_archive_and_wrong_confirmation_cannot_export(self):
        identity = self.archive_saves()
        entry = experience.save_history(self.library, "chromatic", identity)["entries"][0]
        with self.assertRaisesRegex(ValueError, "changed after inspection"):
            experience.export_save(self.library, "chromatic", identity, entry["snapshot_id"], "d" * 64)
        stored = self.root / "backups/save-history" / entry["snapshot_id"] / entry["files"][0]["stored_path"]
        stored.write_bytes(b"z" * stored.stat().st_size)
        with self.assertRaisesRegex(ValueError, "changed after verification"):
            experience.export_save(self.library, "chromatic", identity, entry["snapshot_id"])
        self.assertFalse((self.root / "exports/saves").exists())

    def test_labels_follow_only_verified_uuid_transitions(self):
        experience.label_card(self.library, VOLUME_A.lower(), "Fixture card")
        self.receipt("first", VOLUME_A, VOLUME_B)
        self.receipt("second", VOLUME_B, VOLUME_C, date="2026-01-02T00:00:00+00:00")
        rows = {row["volume_uuid"]: row for row in experience.card_history(self.library)["cards"]}
        self.assertEqual(rows[VOLUME_B]["friendly_name"], "Fixture card")
        self.assertEqual(rows[VOLUME_C]["friendly_name"], "Fixture card")
        self.assertEqual(rows[VOLUME_C]["last_pack"], "Fixture pack")
        self.assertTrue(rows[VOLUME_C]["last_backup_path"].startswith(str(self.root)))

    def test_failed_format_does_not_transfer_name_and_explicit_name_wins(self):
        experience.label_card(self.library, VOLUME_A, "Original")
        self.receipt("failed", VOLUME_A, VOLUME_B, verified=False)
        rows = experience.card_history(self.library)["cards"]
        self.assertNotIn(VOLUME_B, {row["volume_uuid"] for row in rows})
        self.receipt("passed", VOLUME_A, VOLUME_B)
        experience.label_card(self.library, VOLUME_B, "Renamed")
        rows = {row["volume_uuid"]: row for row in experience.card_history(self.library)["cards"]}
        self.assertEqual(rows[VOLUME_B]["friendly_name"], "Renamed")
        experience.label_card(self.library, VOLUME_B, "")
        rows = {row["volume_uuid"]: row for row in experience.card_history(self.library)["cards"]}
        self.assertEqual(rows[VOLUME_B]["friendly_name"], "")

    def test_backup_only_history_preserves_prior_pack_and_updates_backup(self):
        self.receipt("pack", VOLUME_A, VOLUME_B)
        backup = self.root / "backups/only.files"
        backup.mkdir()
        receipt = {"verified": True, "device": {"volume_uuid": VOLUME_B},
                   "ejected_at_utc": "2026-01-03T00:00:00+00:00", "backup_path": str(backup)}
        self.write("backups/only.completed.json", json.dumps(receipt).encode())
        row = next(row for row in experience.card_history(self.library)["cards"] if row["volume_uuid"] == VOLUME_B)
        self.assertEqual(row["last_pack"], "Fixture pack")
        self.assertEqual(row["last_backup_path"], str(backup))

    def test_raw_restore_without_known_pack_is_not_named_as_pack(self):
        self.receipt("restore", VOLUME_A, VOLUME_B, image="backups/Old card snapshot.img",
                     image_kind="backup", install_mode="raw-backup")
        result = experience.card_history(self.library)
        self.assertEqual(result["cards"][0]["last_pack"], "")
        self.assertEqual(result["warnings"], [])

    def test_raw_and_file_restores_clear_overwritten_target_pack(self):
        self.receipt("pack", VOLUME_A, VOLUME_B)
        for index, (kind, mode, image) in enumerate((
                ("backup", "raw-backup", "backups/Old card snapshot.img"),
                ("file-backup", "file-backup-restore", "backups/Recorded files.files")), start=2):
            with self.subTest(kind=kind):
                when = f"2026-01-0{index}T00:00:00+00:00"
                self.receipt("restore-" + kind, VOLUME_B, VOLUME_B, date=when, image=image,
                             image_kind=kind, install_mode=mode)
                row = next(row for row in experience.card_history(self.library)["cards"]
                           if row["volume_uuid"] == VOLUME_B)
                self.assertEqual(row["last_pack"], "")
                self.assertEqual(row["last_verified_at"], when)

    def test_legacy_unclassified_write_has_unknown_pack(self):
        path, receipt = self.receipt("legacy", VOLUME_A, VOLUME_B)
        receipt.pop("image_kind")
        receipt.pop("install_mode")
        path.write_text(json.dumps(receipt))
        row = experience.card_history(self.library)["cards"][0]
        self.assertEqual(row["last_pack"], "")

    def test_conflicting_operation_markers_do_not_claim_pack(self):
        self.receipt("conflicting", VOLUME_A, VOLUME_B, image_kind="compilation", install_mode="raw-backup")
        row = experience.card_history(self.library)["cards"][0]
        self.assertEqual(row["last_pack"], "")

    def test_bad_advisory_records_do_not_authorize_or_crash(self):
        self.write("library/card-plans/bad.completed.json", b"{invalid")
        _, receipt = self.receipt("outside", VOLUME_A, VOLUME_B)
        receipt["backup_path"] = str(self.root.parent / "outside")
        self.write("library/card-plans/outside.completed.json", json.dumps(receipt).encode())
        result = experience.card_history(self.library)
        self.assertEqual(len(result["warnings"]), 2)
        self.assertEqual(result["cards"][0]["last_backup_path"], "")

    def test_malformed_device_is_an_advisory_warning(self):
        self.write("library/card-plans/bad.completed.json", json.dumps({"verified": True, "device": []}).encode())
        result = experience.card_history(self.library)
        self.assertEqual(result["cards"], [])
        self.assertEqual(len(result["warnings"]), 1)

    def test_label_validation_does_not_clobber_existing_data(self):
        experience.label_card(self.library, VOLUME_A, "Original")
        path = self.root / "library/card-labels.json"
        before = path.read_bytes()
        for volume, name in (("disk15", "Invalid"), (VOLUME_A, "x" * 65), (VOLUME_A, "line\nbreak")):
            with self.subTest(volume=volume, name=name), self.assertRaises(ValueError):
                experience.label_card(self.library, volume, name)
        self.assertEqual(path.read_bytes(), before)

    def test_cli_json_round_trip_and_explicit_root(self):
        command = [sys.executable, "-B", str(ROOT / "library_experience.py")]
        no_root = subprocess.run(command + ["card-history"], capture_output=True)
        self.assertNotEqual(no_root.returncode, 0)
        result = subprocess.run(command + ["--root", str(self.root), "card-label", "--volume", VOLUME_A, "--name", "CLI fixture"], capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(result.stdout)["friendly_name"], "CLI fixture")
        result = subprocess.run(command + ["--root", str(self.root), "card-history"], capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(result.stdout)["cards"][0]["friendly_name"], "CLI fixture")


if __name__ == "__main__":
    unittest.main()
