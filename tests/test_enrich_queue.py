from __future__ import annotations

import threading
import time
import unittest

from trakt_tracker.application.enrich_queue import (
    TASK_RESULT_READY,
    TASK_RESULT_RETRYABLE_FAILURE,
    TASK_RESULT_SKIPPED_ALREADY_RESOLVED,
    TASK_STATUS_COMPLETED,
    TASK_STATUS_DROPPED,
    TASK_STATUS_FAILED,
    EnrichQueueService,
    build_history_episode_task,
    build_history_title_task,
)


class EnrichQueueTests(unittest.TestCase):
    def test_deduplicates_repeated_submissions_for_same_task(self) -> None:
        started: list[str] = []
        gate = threading.Event()

        def handler(task) -> str:
            started.append(task.task_key)
            gate.wait(timeout=1)
            return TASK_RESULT_READY

        queue = EnrichQueueService({"history_title": handler}, max_workers=1)
        task = build_history_title_task(title_key="show:1", trakt_id=1, title_type="show")
        queue.submit_history_refresh(viewport_tasks=[task], nearby_tasks=[], page_tasks=[])
        queue.submit_history_refresh(viewport_tasks=[task], nearby_tasks=[], page_tasks=[])
        queue.submit_history_refresh(viewport_tasks=[task], nearby_tasks=[], page_tasks=[])
        self._wait_until(lambda: bool(started))
        gate.set()
        self._wait_until(lambda: not queue.is_running())
        self.assertEqual(started, ["title:show:1"])

    def test_keeps_highest_priority_for_pending_task(self) -> None:
        gate = threading.Event()

        def blocker_handler(_task) -> str:
            gate.wait(timeout=1)
            return TASK_RESULT_READY

        def title_handler(_task) -> str:
            return TASK_RESULT_READY

        queue = EnrichQueueService(
            {
                "history_episode": blocker_handler,
                "history_title": title_handler,
            },
            max_workers=1,
        )
        blocker = build_history_episode_task(title_key="show:1", show_trakt_id=1, season=1, episode=1, priority=1)
        target = build_history_title_task(title_key="show:2", trakt_id=2, title_type="show", priority=3)
        queue.submit_history_refresh(viewport_tasks=[blocker], nearby_tasks=[], page_tasks=[])
        self._wait_until(lambda: queue.is_running())
        queue.submit_history_refresh(viewport_tasks=[], nearby_tasks=[], page_tasks=[target])
        queue.submit_history_refresh(viewport_tasks=[target], nearby_tasks=[], page_tasks=[])
        pending = queue._pending["title:show:2"][1]
        self.assertEqual(pending.priority, 1)
        gate.set()

    def test_emits_completed_failed_and_dropped_updates_with_results(self) -> None:
        def handler(task) -> str:
            if task.task_key.endswith(":1"):
                return TASK_RESULT_READY
            if task.task_key.endswith(":2"):
                return TASK_RESULT_RETRYABLE_FAILURE
            return TASK_RESULT_SKIPPED_ALREADY_RESOLVED

        queue = EnrichQueueService({"history_title": handler}, max_workers=2)
        queue.submit_history_refresh(
            viewport_tasks=[
                build_history_title_task(title_key="show:1", trakt_id=1, title_type="show"),
                build_history_title_task(title_key="show:2", trakt_id=2, title_type="show"),
                build_history_title_task(title_key="show:3", trakt_id=3, title_type="show"),
            ],
            nearby_tasks=[],
            page_tasks=[],
        )
        self._wait_until(lambda: not queue.is_running())
        updates = queue.list_updates(0)["updates"]
        terminal = {(item["task_key"], item["status"], item["result"]) for item in updates if item["status"] in {TASK_STATUS_COMPLETED, TASK_STATUS_FAILED, TASK_STATUS_DROPPED}}
        self.assertIn(("title:show:1", TASK_STATUS_COMPLETED, TASK_RESULT_READY), terminal)
        self.assertIn(("title:show:2", TASK_STATUS_FAILED, TASK_RESULT_RETRYABLE_FAILURE), terminal)
        self.assertIn(("title:show:3", TASK_STATUS_DROPPED, TASK_RESULT_SKIPPED_ALREADY_RESOLVED), terminal)

    def test_never_exceeds_configured_concurrency(self) -> None:
        state = {"active": 0, "max_active": 0}
        lock = threading.Lock()

        def handler(_task) -> str:
            with lock:
                state["active"] += 1
                state["max_active"] = max(state["max_active"], state["active"])
            time.sleep(0.05)
            with lock:
                state["active"] -= 1
            return TASK_RESULT_READY

        queue = EnrichQueueService({"history_title": handler}, max_workers=2)
        tasks = [
            build_history_title_task(title_key=f"show:{index}", trakt_id=index, title_type="show")
            for index in range(1, 7)
        ]
        queue.submit_history_refresh(viewport_tasks=tasks, nearby_tasks=[], page_tasks=[])
        self._wait_until(lambda: not queue.is_running(), timeout=3.0)
        self.assertLessEqual(state["max_active"], 2)

    def test_retryable_failure_enters_backoff_and_does_not_requeue_immediately(self) -> None:
        started: list[str] = []

        def handler(task) -> str:
            started.append(task.task_key)
            return TASK_RESULT_RETRYABLE_FAILURE

        queue = EnrichQueueService({"history_title": handler}, max_workers=1, retry_backoff_seconds=1.0)
        task = build_history_title_task(title_key="show:1", trakt_id=1, title_type="show")
        queue.submit_history_refresh(viewport_tasks=[task], nearby_tasks=[], page_tasks=[])
        self._wait_until(lambda: not queue.is_running())
        queue.submit_history_refresh(viewport_tasks=[task], nearby_tasks=[], page_tasks=[])
        time.sleep(0.05)
        self.assertEqual(started, ["title:show:1"])

    @staticmethod
    def _wait_until(predicate, *, timeout: float = 2.0) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if predicate():
                return
            time.sleep(0.01)
        raise AssertionError("Timed out waiting for condition")


if __name__ == "__main__":
    unittest.main()
