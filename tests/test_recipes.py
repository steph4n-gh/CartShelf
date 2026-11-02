import copy
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import recipes


def release(identity="a" * 64, profile="chromatic", version="1.0"):
    return {"id": profile + ":" + identity, "profile": profile, "identity_sha256": identity,
            "title": "Fixture Adventure", "version": version,
            "sources": [{"path": "private data is never exported", "raw_sha256": "b" * 64}],
            "metadata": {"private": "not in a recipe"}}


class RecipeTests(unittest.TestCase):
    def setUp(self):
        self.release = release()
        self.recipe = recipes.recipe_for_selection("Fixture night", "chromatic", [self.release["id"]], [self.release], "A generated test selection.")

    def test_export_import_round_trip_contains_only_portable_public_fields(self):
        encoded = recipes.encode_recipe(self.recipe)
        parsed = recipes.parse_recipe(encoded)
        self.assertEqual(parsed, self.recipe)
        self.assertEqual(set(parsed), recipes.DOCUMENT_KEYS)
        self.assertEqual(set(parsed["games"][0]), recipes.GAME_KEYS)
        self.assertNotIn(b"sources", encoded)
        self.assertNotIn(b"private", encoded)
        self.assertEqual(recipes.resolve_recipe(parsed, [self.release]), {"matched_release_ids": [self.release["id"]], "missing": []})

    def test_missing_revision_does_not_substitute_same_title(self):
        later = release("c" * 64, version="2.0")
        result = recipes.resolve_recipe(self.recipe, [later])
        self.assertEqual(result["matched_release_ids"], [])
        self.assertEqual(result["missing"], self.recipe["games"])

    def test_identity_does_not_cross_device_profiles(self):
        result = recipes.resolve_recipe(self.recipe, [release(profile="m64")])
        self.assertEqual(result["matched_release_ids"], [])

    def test_unknown_fields_and_payloads_are_rejected(self):
        for key in ("path", "rom", "save", "url", "commands", "payload"):
            recipe = copy.deepcopy(self.recipe)
            recipe[key] = "unsupported"
            with self.subTest(key=key), self.assertRaises(ValueError):
                recipes.parse_recipe(json.dumps(recipe).encode())
            recipe = copy.deepcopy(self.recipe)
            recipe["games"][0][key] = "unsupported"
            with self.subTest(game_key=key), self.assertRaises(ValueError):
                recipes.parse_recipe(json.dumps(recipe).encode())

    def test_duplicate_json_fields_are_rejected(self):
        encoded = recipes.encode_recipe(self.recipe).replace(b'"schema": 1', b'"schema": 1, "schema": 1')
        with self.assertRaisesRegex(ValueError, "repeats"):
            recipes.parse_recipe(encoded)

    def test_duplicate_identity_is_rejected(self):
        self.recipe["games"].append(dict(self.recipe["games"][0]))
        with self.assertRaisesRegex(ValueError, "repeats"):
            recipes.validate_recipe(self.recipe)

    def test_empty_is_not_full_library(self):
        self.recipe["games"] = []
        with self.assertRaises(ValueError):
            recipes.validate_recipe(self.recipe)
        with self.assertRaises(ValueError):
            recipes.recipe_for_selection("Empty", "chromatic", [], [self.release])

    def test_bounds_schema_types_and_hashes_fail_closed(self):
        variants = [("schema", True), ("schema", 2), ("profile", []), ("profile", "other"),
                    ("name", "x" * 101), ("description", "x" * 1001), ("games", "all")]
        for key, value in variants:
            recipe = copy.deepcopy(self.recipe)
            recipe[key] = value
            with self.subTest(key=key, value=str(value)[:20]), self.assertRaises(ValueError):
                recipes.validate_recipe(recipe)
        for digest in ("A" * 64, "g" * 64, "a" * 63, None):
            recipe = copy.deepcopy(self.recipe)
            recipe["games"][0]["sha256"] = digest
            with self.subTest(digest=digest), self.assertRaises(ValueError):
                recipes.validate_recipe(recipe)
        with self.assertRaises(ValueError):
            recipes.parse_recipe(b" " * (recipes.MAX_RECIPE_BYTES + 1))

    def test_local_paths_and_control_characters_do_not_leak_as_notes(self):
        for value in ("/" + "Users/example/private", "C:\\example\\private", "Bad\x00note"):
            recipe = copy.deepcopy(self.recipe)
            recipe["description"] = value
            with self.subTest(value=value), self.assertRaises(ValueError):
                recipes.encode_recipe(recipe)
        self.recipe["description"] = "First line\nSecond line"
        self.assertEqual(recipes.parse_recipe(recipes.encode_recipe(self.recipe)), self.recipe)

    def test_game_count_bound_and_invalid_utf8_are_rejected(self):
        self.recipe["games"] = [{"sha256": f"{index:064x}", "title": "Fixture", "version": "Unknown"}
                                for index in range(recipes.MAX_GAMES + 1)]
        with self.assertRaises(ValueError):
            recipes.validate_recipe(self.recipe)
        with self.assertRaises(ValueError):
            recipes.parse_recipe(b"\xff\xfe")


if __name__ == "__main__":
    unittest.main()
