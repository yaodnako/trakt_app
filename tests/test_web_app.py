from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from trakt_tracker.web.app import _ArtworkCacheWarmLoop


class ArtworkCacheWarmLoopTests(unittest.TestCase):
    def test_run_once_logs_warm_metrics(self) -> None:
        sync = MagicMock()
        sync.warm_missing_artwork_cache.return_value = {
            "scanned": 6320,
            "selected": 2,
            "warmed": 2,
            "failed": 0,
            "duration_ms": 18.5,
        }
        services = SimpleNamespace(sync=sync, operations=MagicMock())
        app = SimpleNamespace(
            state=SimpleNamespace(
                services=services,
                bg_tasks=SimpleNamespace(has_running_prefix=lambda *_prefixes: False),
            )
        )
        loop = _ArtworkCacheWarmLoop(app)

        with patch.object(loop, "_write_log") as write_log:
            loop._run_once()

        write_log.assert_called_once_with("scanned=6320 selected=2 warmed=2 failed=0 duration_ms=18.5")

    def test_run_once_logs_and_publishes_exception(self) -> None:
        sync = MagicMock()
        sync.warm_missing_artwork_cache.side_effect = RuntimeError("cache failure")
        operations = MagicMock()
        services = SimpleNamespace(sync=sync, operations=operations)
        app = SimpleNamespace(
            state=SimpleNamespace(
                services=services,
                bg_tasks=SimpleNamespace(has_running_prefix=lambda *_prefixes: False),
            )
        )
        loop = _ArtworkCacheWarmLoop(app)

        with patch.object(loop, "_write_log") as write_log:
            loop._run_once()

        message = write_log.call_args.args[0]
        self.assertIn("error=RuntimeError: cache failure", message)
        operations.publish.assert_called_once()
        self.assertIn("cache failure", operations.publish.call_args.args[1])


if __name__ == "__main__":
    unittest.main()
