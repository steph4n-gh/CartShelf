# SPDX-License-Identifier: GPL-3.0-only
"""Portable exact-build collection recipes with a small public field set."""
from __future__ import annotations

import hashlib
import json
import re


MAX_RECIPE_BYTES = 1024 * 1024
MAX_GAMES = 4096
PROFILES = frozenset({"m64", "chromatic"})
SHA256 = re.compile(r"[a-f0-9]{64}\Z")
DOCUMENT_KEYS = frozenset({"schema", "kind", "name", "profile", "description", "games"})
GAME_KEYS = frozenset({"sha256", "title", "version"})
PRIVATE_PATH = re.compile(r"(?:/(?:Users|Volumes|home|private|var|tmp)/|[A-Za-z]:[\\/]|\\\\)")


def text(value, field: str, limit: int, *, empty: bool = False) -> str:
    if not isinstance(value, str) or len(value) > limit or (not empty and not value.strip()):
        raise ValueError(f"Recipe {field} must contain {'0' if empty else '1'} to {limit} characters.")
    allowed = "\n\t" if field == "description" else ""
    if any((ord(character) < 32 and character not in allowed) or ord(character) == 127 for character in value):
        raise ValueError(f"Recipe {field} contains unsupported control characters.")
    if PRIVATE_PATH.search(value):
        raise ValueError(f"Recipe {field} contains a local filesystem path.")
    return value


def validate_recipe(value) -> dict:
    if not isinstance(value, dict) or set(value) != DOCUMENT_KEYS:
        raise ValueError("The recipe contains unsupported or missing fields.")
    if type(value["schema"]) is not int or value["schema"] != 1 or value["kind"] != "cartshelf-recipe":
        raise ValueError("Unsupported CartShelf recipe format.")
    if not isinstance(value["profile"], str) or value["profile"] not in PROFILES:
        raise ValueError("The recipe requires an unsupported device profile.")
    text(value["name"], "name", 100)
    text(value["description"], "description", 1000, empty=True)
    games = value["games"]
    if not isinstance(games, list) or not 1 <= len(games) <= MAX_GAMES:
        raise ValueError(f"Choose 1 to {MAX_GAMES} exact builds for a recipe.")
    identities = set()
    for game in games:
        if not isinstance(game, dict) or set(game) != GAME_KEYS:
            raise ValueError("A recipe game contains unsupported or missing fields.")
        digest = game["sha256"]
        if not isinstance(digest, str) or not SHA256.fullmatch(digest):
            raise ValueError("A recipe game requires a lowercase SHA-256 identity.")
        if digest in identities:
            raise ValueError("A recipe repeats the same exact build.")
        identities.add(digest)
        text(game["title"], "game title", 256)
        text(game["version"], "game version", 100)
    return value


def _unique_fields(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("The recipe repeats a JSON field.")
        result[key] = value
    return result


def parse_recipe(encoded: bytes) -> dict:
    if not isinstance(encoded, bytes) or not encoded or len(encoded) > MAX_RECIPE_BYTES:
        raise ValueError("The recipe is empty or exceeds one MiB.")
    try:
        value = json.loads(encoded.decode("utf-8"), object_pairs_hook=_unique_fields)
    except (UnicodeError, json.JSONDecodeError, RecursionError) as error:
        raise ValueError("The recipe must be a bounded UTF-8 JSON document.") from error
    return validate_recipe(value)


def encode_recipe(recipe: dict) -> bytes:
    validate_recipe(recipe)
    encoded = (json.dumps(recipe, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    if len(encoded) > MAX_RECIPE_BYTES:
        raise ValueError("The recipe exceeds one MiB.")
    return encoded


def recipe_for_selection(name: str, profile: str, release_ids: list[str],
                         releases: list[dict], description: str = "") -> dict:
    if not release_ids or len(release_ids) > MAX_GAMES or len(set(release_ids)) != len(release_ids):
        raise ValueError(f"Choose 1 to {MAX_GAMES} distinct exact builds for a recipe.")
    available = {release["id"]: release for release in releases}
    games = []
    for release_id in release_ids:
        release = available.get(release_id)
        if release is None or release["profile"] != profile:
            raise ValueError("Every recipe build must exist in the selected device library.")
        games.append({"sha256": release["identity_sha256"], "title": release["title"],
                      "version": release.get("version") or "Unknown"})
    recipe = {"schema": 1, "kind": "cartshelf-recipe", "name": name.strip(),
              "profile": profile, "description": description.strip(), "games": games}
    encode_recipe(recipe)
    return recipe


def resolve_recipe(recipe: dict, releases: list[dict]) -> dict:
    validate_recipe(recipe)
    available = {(release["profile"], release["identity_sha256"]): release["id"] for release in releases}
    matched, missing = [], []
    for game in recipe["games"]:
        release_id = available.get((recipe["profile"], game["sha256"]))
        if release_id is None:
            missing.append(dict(game))
        else:
            matched.append(release_id)
    return {"matched_release_ids": matched, "missing": missing}


def document_digest(encoded: bytes) -> str:
    return hashlib.sha256(encoded).hexdigest()
