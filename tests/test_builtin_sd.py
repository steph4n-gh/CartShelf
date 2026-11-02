from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import media


class BuiltInSDTests(unittest.TestCase):
    def setUp(self):
        self.info = {
            "DeviceIdentifier": "disk14", "WholeDisk": True, "Internal": True,
            "OSInternalMedia": False, "VirtualOrPhysical": "Physical", "WritableMedia": True,
            "RemovableMedia": True, "Removable": True, "BusProtocol": "Secure Digital",
            "TotalSize": 31267487744, "DeviceBlockSize": 512,
            "MediaName": "Built In SDXC Reader", "DeviceTreePath": "builtin-sd:1",
        }
        self.listing = {"Partitions": [{"VolumeUUID": "card-a", "MountPoint": "/Volumes/FixtureCard"}]}

    def test_removable_builtin_sd_is_available_with_its_mount_and_identity(self):
        card = media.card_record(self.info, self.listing, set())
        self.assertIsNotNone(card)
        self.assertEqual(card["id"], "disk14")
        self.assertEqual(card["mount_points"], ["/Volumes/FixtureCard"])
        changed = media.card_record(self.info, {"Partitions": [{"VolumeUUID": "card-b"}]}, set())
        self.assertNotEqual(card["identity"], changed["identity"])

    def test_internal_non_sd_devices_are_not_available(self):
        for bus in ["USB", "PCI-Express", "SATA", "Thunderbolt", None]:
            with self.subTest(bus=bus):
                self.assertIsNone(media.card_record({**self.info, "BusProtocol": bus}, self.listing, set()))

    def test_internal_sd_requires_a_positive_removable_media_flag(self):
        for flags in [(False, False), (None, None)]:
            with self.subTest(flags=flags):
                self.assertIsNone(media.card_record({**self.info, "RemovableMedia": flags[0], "Removable": flags[1]}, self.listing, set()))
        for flags in [(True, False), (False, True)]:
            with self.subTest(flags=flags):
                self.assertIsNotNone(media.card_record({**self.info, "RemovableMedia": flags[0], "Removable": flags[1]}, self.listing, set()))

    def test_internal_sd_requires_explicit_non_os_media(self):
        for value in [True, None]:
            with self.subTest(value=value):
                self.assertIsNone(media.card_record({**self.info, "OSInternalMedia": value}, self.listing, set()))

    def test_internal_sd_still_rejects_protected_and_unsafe_disks(self):
        self.assertIsNone(media.card_record(self.info, self.listing, {"disk14"}))
        for changes in [{"WritableMedia": False}, {"VirtualOrPhysical": "Virtual"},
                        {"WholeDisk": False}, {"TotalSize": 0}, {"Internal": None}]:
            with self.subTest(changes=changes):
                self.assertIsNone(media.card_record({**self.info, **changes}, self.listing, set()))

    def test_external_usb_behavior_is_preserved(self):
        usb = {**self.info, "Internal": False, "BusProtocol": "USB", "DeviceIdentifier": "disk15"}
        self.assertIsNotNone(media.card_record(usb, {}, set()))
        self.assertIsNone(media.card_record({**usb, "RemovableMedia": False, "Removable": False}, {}, set()))
        self.assertIsNone(media.card_record(usb, {}, {"disk15"}))

    def test_external_sd_bus_fallback_is_preserved(self):
        sd = {**self.info, "Internal": False, "RemovableMedia": False, "Removable": False}
        self.assertIsNotNone(media.card_record(sd, {}, set()))

    def test_cards_lists_builtin_and_usb_while_excluding_both_protected_disks(self):
        root = Path("/Volumes/FixtureLibrary/catalog")
        infos = {
            "disk0": {**self.info, "DeviceIdentifier": "disk0"},
            "disk6": {**self.info, "DeviceIdentifier": "disk6", "Internal": False, "BusProtocol": "USB"},
            "disk14": self.info,
            "disk15": {**self.info, "DeviceIdentifier": "disk15", "Internal": False, "BusProtocol": "USB"},
        }

        def lookup(target):
            return infos[{"/": "disk0", "/Volumes/FixtureLibrary": "disk6"}.get(target, target)]

        def listing(args, *, plist):
            self.assertTrue(plist)
            self.assertEqual(args[:3], [media.DISKUTIL, "list", "-plist"])
            devices = [info for info in infos.values() if "external" not in args or info["Internal"] is False]
            return {"AllDisksAndPartitions": [{"DeviceIdentifier": info["DeviceIdentifier"]} for info in devices]}

        with patch.object(Path, "is_mount", lambda path: str(path) in {"/", "/Volumes/FixtureLibrary"}), patch.object(media, "execution_mounts", return_value={Path("/Volumes/FixtureLibrary")}), patch.object(media, "workspace", return_value=root), patch.object(media, "disk_info", side_effect=lookup), patch.object(media, "command", side_effect=listing), patch.object(media, "card_disk_info", side_effect=lambda disk: infos[disk["DeviceIdentifier"]]):
            result = media.cards(root)
        self.assertEqual({card["id"] for card in result["cards"]}, {"disk14", "disk15"})
        self.assertEqual(set(result["protected_disks"]), {"disk0", "disk6"})


if __name__ == "__main__":
    unittest.main()
