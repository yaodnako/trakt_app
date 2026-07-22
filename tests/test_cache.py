from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from trakt_tracker.infrastructure.cache import BinaryCache


class BinaryCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.cache_root = Path(self.tmpdir.name)
        patcher = patch("trakt_tracker.infrastructure.cache.get_app_data_dir", return_value=self.cache_root)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmpdir.cleanup)

    def test_contains_checks_known_suffix_without_reading_file(self) -> None:
        cache = BinaryCache("images")
        cache.set_bytes("poster", b"payload", suffix=".webp")

        with patch.object(Path, "read_bytes", side_effect=AssertionError("contains must not read payload")):
            self.assertTrue(cache.contains("poster"))

    def test_unknown_suffix_uses_one_lazy_directory_index(self) -> None:
        cache = BinaryCache("images")
        digest = hashlib.sha256(b"legacy").hexdigest()
        (cache._dir / f"{digest}.custom").write_bytes(b"payload")

        with patch.object(cache, "_build_index", wraps=cache._build_index) as build_index:
            self.assertTrue(cache.contains("legacy"))
            self.assertFalse(cache.contains("missing-one"))
            self.assertFalse(cache.contains("missing-two"))

        build_index.assert_called_once()

    def test_set_bytes_updates_existing_lazy_index(self) -> None:
        cache = BinaryCache("images")
        self.assertFalse(cache.contains("new-key"))

        cache.set_bytes("new-key", b"payload", suffix=".custom")

        self.assertTrue(cache.contains("new-key"))


if __name__ == "__main__":
    unittest.main()
