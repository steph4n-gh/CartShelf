import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import urllib.error

import cart_updates


PROJECT = Path(__file__).resolve().parents[1]
AUTHORED_FIXTURE = PROJECT / "tests/fixtures/modretro-games-changelog.yaml"


class Response(io.BytesIO):
    def __init__(self, data, url=cart_updates.SOURCE):
        super().__init__(data)
        self.url = url

    def geturl(self):
        return self.url


class CartUpdatesTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="cart-updates-test-")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name).resolve()

    def refresh(self, payload):
        with patch("cart_updates.urllib.request.urlopen", return_value=Response(payload)):
            return cart_updates.refresh_updates(self.root)

    def fixture_feed(self):
        return self.refresh(AUTHORED_FIXTURE.read_bytes())

    def test_authored_fixture_exposes_versions_and_preserves_release_notes(self):
        feed = self.fixture_feed()
        self.assertEqual(len(feed["games"]), 9)
        status = cart_updates.status_for("Self Simulated", "1.0", feed)
        self.assertEqual(status["status"], "update-listed")
        self.assertEqual(status["latest_version"], "1.2")
        self.assertEqual(status["confidence"], "filename-based")
        self.assertEqual(status["changes"].count("Example revision requires a fresh save."), 2)
        self.assertIn("## 1.1", status["changes"])
        self.assertIn("## 1.2", status["changes"])
        cached = json.loads((self.root / "library/modretro-updates.json").read_text())
        self.assertEqual(cached, feed)
        self.assertEqual(cached["source"], cart_updates.SOURCE)

    def test_unknown_local_revision_does_not_become_initial_release(self):
        feed = self.fixture_feed()
        for label in ["Unknown", "unlabelled", "", "Rev A"]:
            with self.subTest(label=label):
                status = cart_updates.status_for("Gravitorque DX", label, feed)
                self.assertEqual(status["status"], "unknown-local-version")
                self.assertEqual(status["local_version"], "Unknown")
                self.assertEqual(status["latest_version"], "1.1")
                self.assertIn("Local version unknown", status["message"])

    def test_numeric_comparison_and_leading_zero_alias(self):
        feed = self.fixture_feed()
        feed["games"]["tetris"]["versions"] = [{"version": "1.1", "date": "2025-01-08", "changes": "Notes"}]
        for local in ["1.01", "v1.1", "1.1.0"]:
            with self.subTest(local=local):
                status = cart_updates.status_for("Tetris", local, feed)
                self.assertEqual(status["status"], "at-latest-listed")
                self.assertIn("ROM bytes are unverified", status["message"])
        self.assertEqual(cart_updates.status_for("Tetris", "1.10", feed)["status"], "local-newer-than-feed")

    def test_latest_release_uses_numeric_version_instead_of_date_or_mapping_order(self):
        feed = self.refresh(b"""metadata:
  manifest_version: 1
  timestamp: test
games:
  tetris:
    versions:
      '1.10': {date: 2024-01-01, changes: ten}
      '1.9': {date: 2025-01-01, changes: nine}
""")
        status = cart_updates.status_for("Tetris", "1.9", feed)
        self.assertEqual(status["latest_version"], "1.10")
        self.assertEqual(status["status"], "update-listed")
        self.assertIn("ten", status["changes"])
        self.assertNotIn("nine", status["changes"])

    def test_explicit_aliases_match_but_similar_titles_do_not(self):
        feed = self.fixture_feed()
        cases = {
            "Croc - 25th Anniversary Edition": "1.2",
            "Buck and the Cursed Cartridge": "1.1",
            "In The Dark 2": "1.4",
            "Chikyuu Kaihou Gun ZAS": "1.0",
            "Rayman 2 Forever": "1.0",
            "Project S-11": "1.0",
        }
        for title, latest in cases.items():
            with self.subTest(title=title):
                self.assertEqual(cart_updates.status_for(title, "Unknown", feed)["latest_version"], latest)
        self.assertIsNone(cart_updates.status_for("Tetris DX", "1.0", feed))
        self.assertIsNone(cart_updates.status_for("Rayman 3", "1.0", feed))

    def test_refresh_is_a_get_and_sends_no_local_game_data(self):
        def respond(request, timeout):
            self.assertEqual(request.get_method(), "GET")
            self.assertIsNone(request.data)
            self.assertEqual(request.full_url, cart_updates.SOURCE)
            return Response(AUTHORED_FIXTURE.read_bytes())
        with patch("cart_updates.urllib.request.urlopen", side_effect=respond):
            cart_updates.refresh_updates(self.root)

    def test_network_failure_keeps_previous_cache(self):
        self.fixture_feed()
        cache = self.root / "library/modretro-updates.json"
        before = cache.read_bytes()
        with patch("cart_updates.urllib.request.urlopen", side_effect=urllib.error.URLError("offline")):
            with self.assertRaises(urllib.error.URLError):
                cart_updates.refresh_updates(self.root)
        self.assertEqual(cache.read_bytes(), before)

    def test_invalid_or_malicious_feed_keeps_previous_cache(self):
        self.fixture_feed()
        cache = self.root / "library/modretro-updates.json"
        before = cache.read_bytes()
        bad_payloads = [
            b"!!python/object/apply:os.system ['touch /tmp/cart-updates-must-not-run']",
            b"games: []",
            b"metadata: {manifest_version: true, timestamp: test}\ngames: {tetris: {versions: {}}}",
            b"metadata: {manifest_version: 1, timestamp: test}\ngames: {tetris: {versions: {'1.0': {date: wrong, changes: notes}}}}",
            b"a: &a [*a]",
            b"x" * (cart_updates.MAX_FEED_BYTES + 1),
        ]
        for payload in bad_payloads:
            with self.subTest(payload=payload[:60]):
                with self.assertRaises(ValueError):
                    self.refresh(payload)
                self.assertEqual(cache.read_bytes(), before)
        self.assertFalse(Path("/tmp/cart-updates-must-not-run").exists())

    def test_unexpected_redirect_keeps_previous_cache(self):
        self.fixture_feed()
        cache = self.root / "library/modretro-updates.json"
        before = cache.read_bytes()
        with patch("cart_updates.urllib.request.urlopen", return_value=Response(AUTHORED_FIXTURE.read_bytes(), "http://example.com/feed")):
            with self.assertRaises(ValueError):
                cart_updates.refresh_updates(self.root)
        self.assertEqual(cache.read_bytes(), before)

    def test_cache_symlink_cannot_write_outside_project(self):
        outside = Path(tempfile.mkdtemp(prefix="cart-updates-outside-"))
        self.addCleanup(lambda: outside.rmdir())
        (self.root / "library").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(ValueError):
            cart_updates.refresh_updates(self.root)
        self.assertFalse((outside / "modretro-updates.json").exists())


if __name__ == "__main__":
    unittest.main()
