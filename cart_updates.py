"""Read ModRetro's public release notes without sending ROM data."""

from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path
import re
import tempfile
import unicodedata
import urllib.parse
import urllib.request

import yaml


SOURCE = "https://s3.us-east-1.amazonaws.com/updates.modretro.com/apps/games_changelog.yaml?v=2"
MAX_FEED_BYTES = 256 * 1024

TITLE_ALIASES = {
    "babytrex": "baby_trex",
    "buckandthecursedcartridge": "cursed_cartridge",
    "croc25thanniversaryedition": "croc",
    "gravitorquedx": "gravitorque",
    "inthedark2": "in_the_dark",
    "pineapplekiddx": "pineapple_kid",
    "rayman2forever": "rayman2",
    "sabrinatheanimatedserieszapped": "sabrina_zapped",
    "talesofmonsterlanddx": "tales_of_monsterland",
    "tokitoriultimateedition": "toki_tori",
    "traumatariumpenitent": "traumatarium",
    "chikyuukaihougunzas": "zas",
}


def _version(value: str) -> tuple[int, ...] | None:
    if not isinstance(value, str):
        return None
    label = value.strip().removeprefix("v").removeprefix("V")
    if not re.fullmatch(r"[0-9]{1,4}(?:\.[0-9]{1,4}){1,3}", label):
        return None
    numbers = tuple(int(part) for part in label.split("."))
    return numbers + (0,) * (4 - len(numbers))


def _title_key(title: str) -> str:
    title = unicodedata.normalize("NFKD", title).casefold()
    return "".join(c for c in title if c.isascii() and c.isalnum())


def _text(value: object, field: str, limit: int) -> str:
    if not isinstance(value, str) or len(value) > limit:
        raise ValueError(f"Invalid ModRetro {field}.")
    return "".join(c for c in value if c >= " " or c in "\n\t").strip()


def _normalize(payload: bytes, fetched_at: str) -> dict:
    if len(payload) > MAX_FEED_BYTES:
        raise ValueError("ModRetro release feed exceeds the size limit.")
    try:
        text = payload.decode("utf-8")
        if any(isinstance(token, (yaml.tokens.AliasToken, yaml.tokens.AnchorToken))
               for token in yaml.scan(text)):
            raise ValueError("ModRetro release feed contains YAML aliases.")
        document = yaml.safe_load(text)
    except (UnicodeError, yaml.YAMLError) as error:
        raise ValueError("Invalid ModRetro release feed.") from error
    if not isinstance(document, dict):
        raise ValueError("ModRetro release feed must be a mapping.")
    metadata, games = document.get("metadata"), document.get("games")
    if not isinstance(metadata, dict) or not isinstance(games, dict) or not 1 <= len(games) <= 1000:
        raise ValueError("ModRetro release feed is missing metadata or games.")
    manifest_version = metadata.get("manifest_version")
    if type(manifest_version) is not int or manifest_version < 1:
        raise ValueError("Invalid ModRetro manifest version.")
    published_at = _text(metadata.get("timestamp"), "publication timestamp", 256)
    normalized = {}
    for slug, game in games.items():
        if not isinstance(slug, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", slug):
            raise ValueError("Invalid ModRetro game identifier.")
        versions = game.get("versions") if isinstance(game, dict) else None
        if not isinstance(versions, dict) or not 1 <= len(versions) <= 100:
            raise ValueError(f"Invalid ModRetro versions for {slug}.")
        releases = []
        for label, release in versions.items():
            if not isinstance(label, str) or _version(label) is None or not isinstance(release, dict):
                raise ValueError(f"Invalid ModRetro release for {slug}.")
            date = release.get("date")
            if isinstance(date, dt.date) and not isinstance(date, dt.datetime):
                date = date.isoformat()
            if not isinstance(date, str):
                raise ValueError(f"Invalid ModRetro release date for {slug}.")
            try:
                dt.date.fromisoformat(date)
            except ValueError as error:
                raise ValueError(f"Invalid ModRetro release date for {slug}.") from error
            releases.append({
                "version": label,
                "date": date,
                "changes": _text(release.get("changes"), "release notes", 128 * 1024),
            })
        normalized[slug] = {"versions": sorted(releases, key=lambda release: _version(release["version"]))}
    return {
        "source": SOURCE,
        "fetched_at": fetched_at,
        "published_at": published_at,
        "manifest_version": manifest_version,
        "games": normalized,
    }


def refresh_updates(root: Path) -> dict:
    """Fetch and atomically cache release notes. Failure retains the old cache."""
    root = Path(root).resolve(strict=True)
    if root.is_relative_to(Path(__file__).resolve().parent):
        raise ValueError("Choose a library folder outside the application source or bundle.")
    if not root.is_dir() or not os.access(root, os.W_OK | os.X_OK):
        raise ValueError("Choose an existing writable library folder for release notes.")
    original = root.stat()
    root_identity = (original.st_dev, original.st_ino)
    library = root / "library"
    target = library / "modretro-updates.json"

    def validate_destination():
        current = root.stat()
        if not root.is_dir() or (current.st_dev, current.st_ino) != root_identity:
            raise ValueError("The selected library changed during the release check.")
        if not library.resolve().is_relative_to(root):
            raise ValueError("The release cache directory escapes the selected library.")
        if target.is_symlink():
            raise ValueError("The release cache cannot be a symbolic link.")

    validate_destination()
    request = urllib.request.Request(SOURCE, headers={"User-Agent": "CartShelf", "Accept": "application/yaml,text/yaml,text/plain"})
    with urllib.request.urlopen(request, timeout=30) as response:
        original = urllib.parse.urlsplit(SOURCE)
        final = urllib.parse.urlsplit(response.geturl())
        if final.scheme != "https" or final.netloc != original.netloc:
            raise ValueError("Unexpected ModRetro release feed redirect.")
        feed = _normalize(response.read(MAX_FEED_BYTES + 1), dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"))
    validate_destination()
    library.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w", dir=library, prefix=".modretro-updates-", suffix=".json", delete=False, encoding="utf-8") as output:
            temporary = Path(output.name)
            json.dump(feed, output, ensure_ascii=False, separators=(",", ":"))
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        validate_destination()
        os.replace(temporary, target)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return feed


def status_for(title: str, version: str, feed: dict) -> dict | None:
    """Compare a known ModRetro title's filename label with public release notes.

    The caller establishes publisher lineage. A title alone cannot distinguish
    the ModRetro Tetris release from the older Nintendo game with that name.
    """
    key = _title_key(title)
    games = feed.get("games", {})
    slug = TITLE_ALIASES.get(key)
    if slug is None:
        slug = next((name for name in games if _title_key(name) == key), None)
    if slug is None or slug not in games:
        return None
    releases = games[slug]["versions"]
    latest = releases[-1]
    local, listed = _version(version), _version(latest["version"])
    if local is None:
        status = "unknown-local-version"
        local_label = "Unknown"
        message = f"Local version unknown; ModRetro lists {latest['version']}."
    elif local < listed:
        status = "update-listed"
        local_label = version
        message = f"Local filename says {version}; ModRetro lists {latest['version']}."
    elif local == listed:
        status = "at-latest-listed"
        local_label = version
        message = f"Filename matches latest listed {latest['version']}; ROM bytes are unverified."
    else:
        status = "local-newer-than-feed"
        local_label = version
        message = f"Local filename says {version}; the feed lists {latest['version']}."
    relevant = [r for r in releases if local is None or _version(r["version"]) > local]
    if not relevant:
        relevant = [latest]
    changes = "\n\n".join(f"## {r['version']} ({r['date']})\n\n{r['changes']}" for r in relevant)
    return {
        "status": status,
        "local_version": local_label,
        "latest_version": latest["version"],
        "date": latest["date"],
        "changes": changes,
        "source": feed["source"],
        "confidence": "filename-based",
        "message": message + " Release notes do not establish save compatibility.",
    }
