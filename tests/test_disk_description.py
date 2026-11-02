import ctypes
from pathlib import Path
import plistlib
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import media


class DiskDescriptionAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.description = {
            "DAMediaBSDName": "disk15", "DAMediaSize": 32010928128,
            "DADeviceInternal": False, "DADeviceProtocol": "USB",
            "DAMediaRemovable": True, "DAMediaWhole": True, "DAMediaWritable": True,
            "DAMediaBlockSize": 512, "DADeviceModel": "MassStorageClass",
            "DAMediaName": "Generic MassStorageClass Media", "DABusPath": "usb:1",
        }
        self.listing = {"DeviceIdentifier": "disk15", "Size": 32010928128, "OSInternal": False,
                        "Partitions": [{"DeviceIdentifier": "disk15s1", "VolumeUUID": "card-a"}]}

    def record(self, description=None, listing=None):
        listing = self.listing if listing is None else listing
        with patch.object(media, "disk_description", return_value=description or self.description):
            return media.card_record(media.card_disk_info(listing), listing, {"disk0", "disk6"})

    def test_usb_card_is_available_with_large_size_and_partition_identity(self):
        first = self.record()
        self.assertEqual(first["size_bytes"], 32010928128)
        self.assertEqual(first["name"], "MassStorageClass")
        changed = self.record(listing={**self.listing, "Partitions": [{"VolumeUUID": "card-b"}]})
        self.assertNotEqual(first["identity"], changed["identity"])

    def test_builtin_sd_requires_listing_osinternal_explicitly_false(self):
        builtin = {**self.description, "DADeviceInternal": True, "DADeviceProtocol": "Secure Digital"}
        self.assertIsNotNone(self.record(builtin))
        for value in [None, True, "false", 0]:
            with self.subTest(value=value):
                self.assertIsNone(self.record(builtin, {**self.listing, "OSInternal": value}))
        missing = {key: value for key, value in self.listing.items() if key != "OSInternal"}
        self.assertIsNone(self.record(builtin, missing))

    def test_listing_system_media_is_denied(self):
        self.assertIsNone(self.record(listing={**self.listing, "OSInternal": True}))

    def test_bsd_size_and_whole_media_mismatches_abort_discovery(self):
        for changes in [{"DAMediaBSDName": "disk16"}, {"DAMediaBSDName": None},
                        {"DAMediaSize": 32010928127}, {"DAMediaSize": 0},
                        {"DAMediaSize": True}, {"DAMediaSize": "32010928128"},
                        {"DAMediaWhole": False}, {"DAMediaWhole": None}]:
            with self.subTest(changes=changes):
                with self.assertRaises(ValueError):
                    self.record({**self.description, **changes})
        with self.assertRaises(ValueError):
            self.record(listing={**self.listing, "Size": None})

    def test_blank_physical_removable_card_needs_no_partitions(self):
        blank = {key: value for key, value in self.listing.items() if key != "Partitions"}
        card = self.record(listing=blank)
        self.assertEqual(card["id"], "disk15")
        self.assertEqual(card["mount_points"], [])

    def test_unsafe_public_flags_preserve_admission_denial(self):
        for changes in [{"DADeviceInternal": None}, {"DAMediaWritable": False},
                        {"DAMediaWritable": None}, {"DAMediaRemovable": False}]:
            with self.subTest(changes=changes):
                self.assertIsNone(self.record({**self.description, **changes}))

    def test_cards_never_runs_whole_device_diskutil_info(self):
        calls = []
        listing = {"AllDisksAndPartitions": [
            {"DeviceIdentifier": "disk0", "Size": 500277792768},
            {"DeviceIdentifier": "disk6", "Size": 4000787030016}, self.listing,
        ]}
        mounted = {
            "/Volumes/FixtureLibrary": {"DeviceIdentifier": "disk6", "WholeDisk": True, "VirtualOrPhysical": "Physical"},
            "/": {"DeviceIdentifier": "disk0", "WholeDisk": True, "Internal": True},
        }

        def command(arguments, *, plist):
            calls.append(arguments)
            self.assertTrue(plist)
            if arguments == [media.DISKUTIL, "list", "-plist", "physical"]:
                return listing
            target = arguments[-1]
            self.assertFalse(media.DISK.fullmatch(target), "Forbidden whole-device diskutil info: " + target)
            return mounted[target]

        with patch.object(media, "execution_mounts", return_value={Path("/Volumes/FixtureLibrary")}), \
                patch.object(media, "workspace", side_effect=lambda root: root), \
                patch.object(Path, "is_mount", lambda path: str(path) == "/Volumes/FixtureLibrary"), \
                patch.object(media, "command", side_effect=command), \
                patch.object(media, "disk_description", return_value=self.description, create=True) as describe:
            result = media.cards(Path("/Volumes/FixtureLibrary/fixture"))
        self.assertEqual([card["id"] for card in result["cards"]], ["disk15"])
        self.assertEqual(result["protected_disks"], ["disk0", "disk6"])
        describe.assert_called_once_with("disk15")
        self.assertEqual(calls, [[media.DISKUTIL, "list", "-plist", "physical"],
                                 [media.DISKUTIL, "info", "-plist", "/Volumes/FixtureLibrary"],
                                 [media.DISKUTIL, "info", "-plist", "/"]])


class DiskDescriptionBridgeTests(unittest.TestCase):
    def make_bridge(self, payload=None):
        self.references = {name: 2**40 + i for i, name in enumerate(
            ["session", "disk", "description", "projected", "data", "error"], 1)}
        data = plistlib.dumps({"DAMediaBSDName": "disk15", "DAMediaSize": 32010928128}) if payload is None else payload
        self.buffer = ctypes.create_string_buffer(data)
        self.cf = SimpleNamespace(
            CFDictionaryGetValue=Mock(side_effect=lambda description, key: key + 1000),
            CFDictionaryCreate=Mock(return_value=self.references["projected"]),
            CFPropertyListCreateData=Mock(return_value=self.references["data"]),
            CFDataGetLength=Mock(return_value=len(data)),
            CFDataGetBytePtr=Mock(return_value=ctypes.cast(self.buffer, ctypes.c_void_p).value),
            CFRelease=Mock(),
        )
        self.da = SimpleNamespace(
            DASessionCreate=Mock(return_value=self.references["session"]),
            DADiskCreateFromBSDName=Mock(return_value=self.references["disk"]),
            DADiskCopyDescription=Mock(return_value=self.references["description"]),
        )
        self.symbols = []

    def setUp(self):
        self.make_bridge()

    def describe(self):
        def library(path):
            return self.da if "/DiskArbitration." in path else self.cf

        def symbol(library, name):
            self.assertIs(library, self.da)
            self.symbols.append(name)
            return ctypes.c_void_p(2**41 + len(self.symbols))

        with patch.object(media.ctypes, "CDLL", side_effect=library), \
                patch.object(media.ctypes.c_void_p, "in_dll", side_effect=symbol):
            return media.disk_description("disk15")

    def released(self):
        return [call.args[0] for call in self.cf.CFRelease.call_args_list]

    def test_projects_only_documented_scalars_and_releases_all_owned_refs(self):
        self.assertEqual(self.describe(), {"DAMediaBSDName": "disk15", "DAMediaSize": 32010928128})
        expected = ["DeviceInternal", "DeviceProtocol", "MediaRemovable", "MediaWhole", "MediaWritable",
                    "MediaBSDName", "MediaSize", "MediaBlockSize", "DeviceModel", "MediaName", "BusPath"]
        self.assertEqual(self.symbols, ["kDADiskDescription" + name + "Key" for name in expected])
        projected = self.cf.CFDictionaryCreate.call_args.args
        self.assertEqual(projected[3], 11)
        self.assertEqual(projected[4:], (None, None))
        self.assertEqual(len(projected[1]), 11)
        self.assertEqual(len(projected[2]), 11)
        self.assertEqual(self.released(), [self.references[name] for name in ["data", "projected", "description", "disk", "session"]])
        self.assertIs(self.da.DADiskCreateFromBSDName.restype, ctypes.c_void_p)
        self.assertEqual(self.da.DADiskCreateFromBSDName.argtypes, [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_char_p])
        self.assertIs(self.cf.CFDataGetLength.restype, ctypes.c_long)
        self.assertEqual(self.cf.CFPropertyListCreateData.argtypes,
                         [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_long, ctypes.c_ulong, ctypes.POINTER(ctypes.c_void_p)])
        self.assertIsNone(self.cf.CFRelease.restype)

    def test_null_creation_releases_every_previously_acquired_reference(self):
        cases = [
            ("da", "DASessionCreate", []),
            ("da", "DADiskCreateFromBSDName", ["session"]),
            ("da", "DADiskCopyDescription", ["disk", "session"]),
            ("cf", "CFDictionaryCreate", ["description", "disk", "session"]),
            ("cf", "CFPropertyListCreateData", ["projected", "description", "disk", "session"]),
        ]
        for library, name, released in cases:
            with self.subTest(name=name):
                self.make_bridge()
                getattr(getattr(self, library), name).return_value = None
                with self.assertRaises(RuntimeError):
                    self.describe()
                self.assertEqual(self.released(), [self.references[name] for name in released])

    def test_serialization_error_releases_cferror_and_other_refs(self):
        def error_data(allocator, projected, format, options, error):
            ctypes.cast(error, ctypes.POINTER(ctypes.c_void_p)).contents.value = self.references["error"]
            return None

        self.cf.CFPropertyListCreateData.side_effect = error_data
        with self.assertRaises(RuntimeError):
            self.describe()
        self.assertEqual(self.released(), [self.references[name] for name in ["error", "projected", "description", "disk", "session"]])

    def test_error_ref_is_released_even_if_serializer_also_returns_data(self):
        def error_data(allocator, projected, format, options, error):
            ctypes.cast(error, ctypes.POINTER(ctypes.c_void_p)).contents.value = self.references["error"]
            return self.references["data"]

        self.cf.CFPropertyListCreateData.side_effect = error_data
        with self.assertRaises(RuntimeError):
            self.describe()
        self.assertEqual(self.released(), [self.references[name] for name in ["data", "error", "projected", "description", "disk", "session"]])

    def test_missing_key_symbol_releases_description_disk_and_session(self):
        with patch.object(media.ctypes, "CDLL", side_effect=[self.cf, self.da]), \
                patch.object(media.ctypes.c_void_p, "in_dll", side_effect=ValueError("Missing symbol")):
            with self.assertRaises(ValueError):
                media.disk_description("disk15")
        self.assertEqual(self.released(), [self.references[name] for name in ["description", "disk", "session"]])

    def test_null_key_symbol_fails_closed_without_dictionary_access(self):
        with patch.object(media.ctypes, "CDLL", side_effect=[self.cf, self.da]), \
                patch.object(media.ctypes.c_void_p, "in_dll", return_value=ctypes.c_void_p()):
            with self.assertRaises(RuntimeError):
                media.disk_description("disk15")
        self.cf.CFDictionaryGetValue.assert_not_called()
        self.assertEqual(self.released(), [self.references[name] for name in ["description", "disk", "session"]])

    def test_invalid_serialized_bytes_still_release_all_refs(self):
        self.make_bridge(b"invalid property list")
        with self.assertRaises(plistlib.InvalidFileException):
            self.describe()
        self.assertEqual(len(self.released()), 5)

    def test_null_data_pointer_or_length_fails_closed_and_releases_refs(self):
        for name, value in [("CFDataGetBytePtr", None), ("CFDataGetLength", 0), ("CFDataGetLength", -1)]:
            with self.subTest(name=name, value=value):
                self.make_bridge()
                getattr(self.cf, name).return_value = value
                with self.assertRaises(RuntimeError):
                    self.describe()
                self.assertEqual(len(self.released()), 5)

    def test_non_disk_identifier_is_rejected_before_framework_loading(self):
        with patch.object(media.ctypes, "CDLL", side_effect=AssertionError("Framework access is forbidden")):
            with self.assertRaises(ValueError):
                media.disk_description("/dev/disk15")


if __name__ == "__main__":
    unittest.main()
