from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import media


class ProtectedDiskProbeTests(unittest.TestCase):
    def setUp(self):
        self.root = Path("/Volumes/FixtureLibrary/fixture")
        self.protected = {"disk0", "disk6"}
        self.card = {
            "DeviceIdentifier": "disk14", "WholeDisk": True, "Internal": True,
            "OSInternalMedia": False, "VirtualOrPhysical": "Physical",
            "WritableMedia": True, "RemovableMedia": True,
            "BusProtocol": "Secure Digital", "TotalSize": 31267487744,
            "DeviceBlockSize": 512, "DeviceTreePath": "builtin-sd:1",
            "MediaName": "Built In SDXC Reader",
        }
        self.usb = {
            **self.card, "DeviceIdentifier": "disk15", "Internal": False,
            "BusProtocol": "USB", "DeviceTreePath": "USB:1",
        }
        self.calls = []

    def scan(self, infos, disks):
        def respond(args, *, plist):
            self.assertTrue(plist)
            self.calls.append(args)
            if args == [media.DISKUTIL, "list", "-plist", "physical"]:
                return {"AllDisksAndPartitions": disks}
            self.assertEqual(args[:3], [media.DISKUTIL, "info", "-plist"])
            target = args[3]
            if target in self.protected:
                raise AssertionError("Forbidden protected whole-drive lookup: " + target)
            self.assertIn(target, infos, "Unnecessary metadata lookup for a device already proven by topology")
            return infos[target]

        with patch.object(media, "execution_mounts", return_value={Path("/Volumes/FixtureLibrary")}), \
                patch.object(media, "workspace", return_value=self.root), \
                patch.object(Path, "is_mount", lambda path: str(path) == "/Volumes/FixtureLibrary"), \
                patch.object(media, "command", side_effect=respond), \
                patch.object(media, "card_disk_info", side_effect=lambda disk: infos[disk["DeviceIdentifier"]]):
            return media.cards(self.root)

    def test_without_topology_a_physical_partition_still_resolves_its_parent(self):
        partition = {
            "DeviceIdentifier": "disk6s2", "ParentWholeDisk": "disk6",
            "WholeDisk": False, "VirtualOrPhysical": "Physical",
        }
        whole = {"DeviceIdentifier": "disk6", "WholeDisk": True, "VirtualOrPhysical": "Physical"}
        with patch.object(media, "disk_info", return_value=whole) as lookup:
            self.assertEqual(media.physical_disks(partition), {"disk6"})
        lookup.assert_called_once_with("disk6")

    def test_apfs_protects_all_physical_stores_without_whole_drive_lookups(self):
        self.protected.add("disk8")
        infos = {
            "/Volumes/FixtureLibrary": {"DeviceIdentifier": "disk7s1", "APFSPhysicalStores": [
                {"APFSPhysicalStore": "disk6s2"}, {"APFSPhysicalStore": "disk8s2"}]},
            "/": {"DeviceIdentifier": "disk0", "WholeDisk": True, "Internal": True},
            "disk6s2": {"DeviceIdentifier": "disk6s2", "ParentWholeDisk": "disk6", "WholeDisk": False},
            "disk8s2": {"DeviceIdentifier": "disk8s2", "ParentWholeDisk": "disk8", "WholeDisk": False},
            "disk14": self.card,
        }
        disks = [{"DeviceIdentifier": name, "Partitions": [{"DeviceIdentifier": name + "s2"}]}
                 for name in ["disk0", "disk6", "disk8", "disk14"]]
        result = self.scan(infos, disks)
        self.assertEqual(result["protected_disks"], ["disk0", "disk6", "disk8"])
        self.assertEqual([card["id"] for card in result["cards"]], ["disk14"])

    def test_cards_skips_protected_identifiers_before_candidate_info(self):
        infos = {
            "/Volumes/FixtureLibrary": {"DeviceIdentifier": "disk6", "WholeDisk": True,
                                 "VirtualOrPhysical": "Physical"},
            "/": {"DeviceIdentifier": "disk0", "WholeDisk": True, "Internal": True},
            "disk14": self.card, "disk15": self.usb,
        }
        disks = [{"DeviceIdentifier": name} for name in ["disk0", "disk6", "disk14", "disk15"]]
        result = self.scan(infos, disks)
        self.assertEqual(result["protected_disks"], ["disk0", "disk6"])
        self.assertEqual({card["id"] for card in result["cards"]}, {"disk14", "disk15"})

    def test_apfs_library_and_startup_are_protected_while_both_readers_are_listed(self):
        infos = {
            "/Volumes/FixtureLibrary": {"DeviceIdentifier": "disk7s1", "APFSPhysicalStores": [{"APFSPhysicalStore": "disk6s2"}]},
            "/": {"DeviceIdentifier": "disk3s1s1", "APFSPhysicalStores": [{"APFSPhysicalStore": "disk0s2"}]},
            "disk6s2": {"DeviceIdentifier": "disk6s2", "ParentWholeDisk": "disk6", "WholeDisk": False},
            "disk0s2": {"DeviceIdentifier": "disk0s2", "ParentWholeDisk": "disk0", "WholeDisk": False},
            "disk14": self.card, "disk15": self.usb,
        }
        disks = [{"DeviceIdentifier": name, "Partitions": [{"DeviceIdentifier": name + "s2", "VolumeUUID": name + "-a"}]}
                 for name in ["disk0", "disk6", "disk14", "disk15"]]
        result = self.scan(infos, disks)
        self.assertEqual(result["protected_disks"], ["disk0", "disk6"])
        self.assertEqual({card["id"] for card in result["cards"]}, {"disk14", "disk15"})
        first = {card["id"]: card["identity"] for card in result["cards"]}
        for disk in disks:
            disk["Partitions"][0]["VolumeUUID"] += "-changed"
        changed = self.scan(infos, disks)
        self.assertTrue(all(first[card["id"]] != card["identity"] for card in changed["cards"]))

    def test_virtual_unknown_and_missing_partition_kind_resolve_parent(self):
        for kind in ["Virtual", "Unknown", None]:
            with self.subTest(kind=kind):
                partition = {"DeviceIdentifier": "disk7s1", "ParentWholeDisk": "disk7",
                             "WholeDisk": False, "VirtualOrPhysical": kind, "Internal": True}
                parent = {"DeviceIdentifier": "disk7", "WholeDisk": True, "VirtualOrPhysical": "Virtual",
                          "APFSPhysicalStores": [{"APFSPhysicalStore": "disk6s2"}]}
                store = {"DeviceIdentifier": "disk6s2", "ParentWholeDisk": "disk6", "WholeDisk": False,
                         "VirtualOrPhysical": "Physical"}
                whole = {"DeviceIdentifier": "disk6", "WholeDisk": True, "VirtualOrPhysical": "Physical"}
                with patch.object(media, "disk_info", side_effect={"disk7": parent, "disk6s2": store, "disk6": whole}.__getitem__) as lookup:
                    self.assertEqual(media.physical_disks(partition), {"disk6"})
                self.assertIn(unittest.mock.call("disk7"), lookup.call_args_list)

    def test_unresolved_virtual_or_unknown_parent_fails_closed(self):
        for kind in ["Virtual", "Unknown", None]:
            with self.subTest(kind=kind):
                partition = {"DeviceIdentifier": "disk7s1", "ParentWholeDisk": "disk7", "WholeDisk": False,
                             "VirtualOrPhysical": kind}
                parent = {"DeviceIdentifier": "disk7", "WholeDisk": True, "VirtualOrPhysical": kind}
                with patch.object(media, "disk_info", return_value=parent) as lookup:
                    with self.assertRaises(ValueError):
                        media.physical_disks(partition)
                lookup.assert_called_once_with("disk7")

    def test_only_an_explicit_partition_gets_the_physical_parent_shortcut(self):
        for whole in [True, None]:
            with self.subTest(whole=whole):
                info = {"DeviceIdentifier": "disk6s2", "ParentWholeDisk": "disk6",
                        "WholeDisk": whole, "VirtualOrPhysical": "Physical"}
                with patch.object(media, "disk_info", side_effect=ValueError("Unresolved parent")) as lookup:
                    with self.assertRaisesRegex(ValueError, "Unresolved parent"):
                        media.physical_disks(info)
                lookup.assert_called_once_with("disk6")

    def test_malformed_parent_identifier_cannot_shortcut_resolution(self):
        for parent in ["disk6s2", "/dev/disk6", "disk6\n", "disk-6"]:
            with self.subTest(parent=parent):
                partition = {"DeviceIdentifier": "disk7s1", "ParentWholeDisk": parent,
                             "WholeDisk": False, "VirtualOrPhysical": "Physical"}
                with patch.object(media, "disk_info", side_effect=ValueError("Unresolved parent")) as lookup:
                    with self.assertRaisesRegex(ValueError, "Unresolved parent"):
                        media.physical_disks(partition)
                lookup.assert_called_once_with(parent)

    def test_virtual_topology_cycle_fails_closed(self):
        infos = {
            "disk7s1": {"DeviceIdentifier": "disk7s1", "ParentWholeDisk": "disk7", "VirtualOrPhysical": "Virtual"},
            "disk7": {"DeviceIdentifier": "disk7", "APFSPhysicalStores": [{"APFSPhysicalStore": "disk7s1"}]},
        }
        with self.assertRaises(ValueError):
            media.physical_disks(infos["disk7s1"], infos.__getitem__)

    def test_unresolved_protection_aborts_before_card_enumeration(self):
        infos = {"/Volumes/FixtureLibrary": {"DeviceIdentifier": "disk7s1", "VirtualOrPhysical": "Virtual"}}
        with self.assertRaises(ValueError):
            self.scan(infos, [{"DeviceIdentifier": "disk14"}])
        self.assertNotIn([media.DISKUTIL, "info", "-plist", "disk14"], self.calls)

    def test_virtual_or_unknown_disk_outside_physical_listing_cannot_be_inferred(self):
        for kind in ["Virtual", "Unknown", None]:
            with self.subTest(kind=kind):
                infos = {
                    "/Volumes/FixtureLibrary": {"DeviceIdentifier": "disk77s1", "ParentWholeDisk": "disk77",
                                         "WholeDisk": False, "VirtualOrPhysical": kind},
                    "disk77": {"DeviceIdentifier": "disk77", "WholeDisk": True, "VirtualOrPhysical": kind},
                }
                with self.assertRaises(ValueError):
                    self.scan(infos, [{"DeviceIdentifier": "disk14"}])

    def test_virtual_disk_still_resolves_its_stores_through_physical_topology(self):
        infos = {
            "/Volumes/FixtureLibrary": {"DeviceIdentifier": "disk7s1", "ParentWholeDisk": "disk7", "VirtualOrPhysical": "Virtual"},
            "disk7": {"DeviceIdentifier": "disk7", "WholeDisk": True, "VirtualOrPhysical": "Virtual",
                      "APFSPhysicalStores": [{"APFSPhysicalStore": "disk6s2"}]},
            "disk6s2": {"DeviceIdentifier": "disk6s2", "ParentWholeDisk": "disk6", "WholeDisk": False},
            "/": {"DeviceIdentifier": "disk0", "WholeDisk": True, "Internal": True},
            "disk14": self.card,
        }
        disks = [{"DeviceIdentifier": name, "Partitions": [{"DeviceIdentifier": name + "s2"}]}
                 for name in ["disk0", "disk6", "disk14"]]
        result = self.scan(infos, disks)
        self.assertEqual(result["protected_disks"], ["disk0", "disk6"])
        self.assertEqual([card["id"] for card in result["cards"]], ["disk14"])
        self.assertIn([media.DISKUTIL, "info", "-plist", "disk7"], self.calls)

    def test_known_whole_parent_is_not_probed_when_its_partition_is_not_listed(self):
        infos = {
            "/Volumes/FixtureLibrary": {"DeviceIdentifier": "disk6s2", "ParentWholeDisk": "disk6", "WholeDisk": False},
            "/": {"DeviceIdentifier": "disk0", "WholeDisk": True, "Internal": True},
            "disk14": self.card,
        }
        result = self.scan(infos, [{"DeviceIdentifier": name} for name in ["disk0", "disk6", "disk14"]])
        self.assertEqual(result["protected_disks"], ["disk0", "disk6"])
        self.assertEqual([card["id"] for card in result["cards"]], ["disk14"])

    def test_apfs_store_already_identified_by_topology_is_not_probed(self):
        infos = {
            "/Volumes/FixtureLibrary": {"DeviceIdentifier": "disk7s1", "APFSPhysicalStores": [{"APFSPhysicalStore": "disk6s2"}]},
            "/": {"DeviceIdentifier": "disk0", "WholeDisk": True, "Internal": True},
            "disk14": self.card,
        }
        disks = [{"DeviceIdentifier": name, "Partitions": [{"DeviceIdentifier": name + "s2"}]}
                 for name in ["disk0", "disk6", "disk14"]]
        result = self.scan(infos, disks)
        self.assertEqual(result["protected_disks"], ["disk0", "disk6"])
        self.assertEqual([card["id"] for card in result["cards"]], ["disk14"])


if __name__ == "__main__":
    unittest.main()
