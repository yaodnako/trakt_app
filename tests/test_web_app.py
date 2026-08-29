from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from trakt_tracker.web.app import _ArtworkCacheWarmLoop, _TemplateFilters


class TemplateFilterTests(unittest.TestCase):
    def test_episode_label_appends_only_different_imdb_coordinates(self) -> None:
        self.assertEqual(_TemplateFilters.season_episode_label(1, 25, 2, 1), "S01E25 (S02E01)")
        self.assertEqual(_TemplateFilters.season_episode_label(1, 25, 1, 25), "S01E25")
        self.assertEqual(_TemplateFilters.season_episode_label(1, 25), "S01E25")

    def test_full_page_render_reads_pending_notification_snapshot_only(self) -> None:
        source = (Path(__file__).resolve().parents[1] / "trakt_tracker" / "web" / "app.py").read_text(
            encoding="utf-8"
        )

        self.assertIn("services.notifications.pending_sources()", source)
        self.assertNotIn("services.notifications.refresh_pending_sources()", source)


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
