from __future__ import annotations

import heapq
from collections import deque
from dataclasses import dataclass
from threading import Condition, Thread
from time import monotonic
from typing import Callable


TASK_KIND_HISTORY_TITLE = "history_title"
TASK_KIND_HISTORY_EPISODE = "history_episode"
TASK_KIND_PROGRESS_TITLE = "progress_title"
TASK_KIND_PROGRESS_EPISODE = "progress_episode"

TASK_STATUS_PENDING = "pending"
TASK_STATUS_RUNNING = "running"
TASK_STATUS_COMPLETED = "completed"
TASK_STATUS_FAILED = "failed"
TASK_STATUS_DROPPED = "dropped"

TASK_RESULT_READY = "ready"
TASK_RESULT_CHECKED_NO_DATA = "checked_no_data"
TASK_RESULT_RETRYABLE_FAILURE = "retryable_failure"
TASK_RESULT_SKIPPED_ALREADY_RESOLVED = "skipped_already_resolved"


@dataclass(slots=True)
class EnrichTask:
    kind: str
    task_key: str
    priority: int
    affected_title_keys: tuple[str, ...]
    payload: dict


@dataclass(slots=True)
class EnrichTaskUpdate:
    revision: int
    task_key: str
    kind: str
    status: str
    result: str | None
    affected_title_keys: tuple[str, ...]

    def to_dict(self) -> dict:
        return {
            "revision": self.revision,
            "task_key": self.task_key,
            "kind": self.kind,
            "status": self.status,
            "result": self.result,
            "affected_title_keys": list(self.affected_title_keys),
        }


def build_history_title_task(*, title_key: str, trakt_id: int, title_type: str, priority: int = 3) -> EnrichTask:
    return EnrichTask(
        kind=TASK_KIND_HISTORY_TITLE,
        task_key=f"title:{title_type}:{trakt_id}",
        priority=priority,
        affected_title_keys=(title_key,),
        payload={"trakt_id": int(trakt_id), "title_type": str(title_type)},
    )


def build_history_episode_task(
    *,
    title_key: str,
    show_trakt_id: int,
    season: int,
    episode: int,
    priority: int = 3,
) -> EnrichTask:
    return EnrichTask(
        kind=TASK_KIND_HISTORY_EPISODE,
        task_key=f"episode:{show_trakt_id}:{season}:{episode}",
        priority=priority,
        affected_title_keys=(title_key,),
        payload={
            "show_trakt_id": int(show_trakt_id),
            "season": int(season),
            "episode": int(episode),
        },
    )


def build_progress_title_task(*, title_key: str, trakt_id: int, title_type: str, priority: int = 3) -> EnrichTask:
    return EnrichTask(
        kind=TASK_KIND_PROGRESS_TITLE,
        task_key=f"title:{title_type}:{trakt_id}",
        priority=priority,
        affected_title_keys=(title_key,),
        payload={"trakt_id": int(trakt_id), "title_type": str(title_type)},
    )


def build_progress_episode_task(
    *,
    title_key: str,
    show_trakt_id: int,
    season: int,
    episode: int,
    priority: int = 3,
) -> EnrichTask:
    return EnrichTask(
        kind=TASK_KIND_PROGRESS_EPISODE,
        task_key=f"episode:{show_trakt_id}:{season}:{episode}",
        priority=priority,
        affected_title_keys=(title_key,),
        payload={
            "show_trakt_id": int(show_trakt_id),
            "season": int(season),
            "episode": int(episode),
        },
    )


class EnrichQueueService:
    def __init__(
        self,
        handlers: dict[str, Callable[[EnrichTask], str]],
        *,
        max_workers: int = 2,
        max_updates: int = 1000,
        retry_backoff_seconds: float = 30.0,
    ) -> None:
        self._handlers = dict(handlers)
        self._condition = Condition()
        self._pending: dict[str, tuple[int, EnrichTask]] = {}
        self._running: dict[str, EnrichTask] = {}
        self._heap: list[tuple[int, int, str, int]] = []
        self._updates: deque[EnrichTaskUpdate] = deque(maxlen=max_updates)
        self._next_revision = 1
        self._next_submission_seq = 1
        self._cooldowns: dict[str, float] = {}
        self._retry_backoff_seconds = float(retry_backoff_seconds)
        self._workers = [
            Thread(target=self._worker_loop, name=f"enrich-queue-{index + 1}", daemon=True)
            for index in range(max(1, int(max_workers)))
        ]
        for worker in self._workers:
            worker.start()

    def submit_history_refresh(
        self,
        *,
        viewport_tasks: list[EnrichTask],
        nearby_tasks: list[EnrichTask],
        page_tasks: list[EnrichTask],
    ) -> int:
        for task in viewport_tasks:
            self.submit(self._with_priority(task, 1))
        for task in nearby_tasks:
            self.submit(self._with_priority(task, 2))
        for task in page_tasks:
            self.submit(self._with_priority(task, 3))
        with self._condition:
            return self._next_revision - 1

    def submit_progress_refresh(
        self,
        *,
        viewport_tasks: list[EnrichTask],
        nearby_tasks: list[EnrichTask],
        page_tasks: list[EnrichTask],
    ) -> int:
        return self.submit_history_refresh(
            viewport_tasks=viewport_tasks,
            nearby_tasks=nearby_tasks,
            page_tasks=page_tasks,
        )

    def submit(self, task: EnrichTask) -> None:
        with self._condition:
            cooldown_until = self._cooldowns.get(task.task_key)
            if cooldown_until is not None:
                if monotonic() < cooldown_until:
                    return
                self._cooldowns.pop(task.task_key, None)
            running = self._running.get(task.task_key)
            if running is not None:
                merged_running = self._merge_tasks(running, task)
                if merged_running != running:
                    self._running[task.task_key] = merged_running
                return

            existing = self._pending.get(task.task_key)
            if existing is not None:
                existing_submission, existing_task = existing
                merged_task = self._merge_tasks(existing_task, task)
                if merged_task == existing_task:
                    return
                submission_seq = self._next_submission_seq
                self._next_submission_seq += 1
                self._pending[task.task_key] = (submission_seq, merged_task)
                heapq.heappush(self._heap, (merged_task.priority, submission_seq, task.task_key, submission_seq))
                self._emit_update_locked(merged_task, TASK_STATUS_PENDING, None)
                self._condition.notify()
                return

            submission_seq = self._next_submission_seq
            self._next_submission_seq += 1
            self._pending[task.task_key] = (submission_seq, task)
            heapq.heappush(self._heap, (task.priority, submission_seq, task.task_key, submission_seq))
            self._emit_update_locked(task, TASK_STATUS_PENDING, None)
            self._condition.notify()

    def list_updates(self, after_revision: int = 0, relevant_title_keys: set[str] | None = None) -> dict:
        with self._condition:
            filtered_updates = [
                update
                for update in self._updates
                if update.revision > after_revision and self._update_relevant(update, relevant_title_keys)
            ]
            running = self._has_relevant_work_locked(relevant_title_keys)
            revision = self._next_revision - 1
        return {
            "revision": revision,
            "running": running,
            "updates": [update.to_dict() for update in filtered_updates],
        }

    def is_running(self, relevant_title_keys: set[str] | None = None) -> bool:
        with self._condition:
            return self._has_relevant_work_locked(relevant_title_keys)

    @staticmethod
    def _with_priority(task: EnrichTask, priority: int) -> EnrichTask:
        return EnrichTask(
            kind=task.kind,
            task_key=task.task_key,
            priority=priority,
            affected_title_keys=task.affected_title_keys,
            payload=dict(task.payload),
        )

    @staticmethod
    def _merge_tasks(existing: EnrichTask, incoming: EnrichTask) -> EnrichTask:
        affected_title_keys = tuple(dict.fromkeys([*existing.affected_title_keys, *incoming.affected_title_keys]))
        priority = min(existing.priority, incoming.priority)
        return EnrichTask(
            kind=existing.kind,
            task_key=existing.task_key,
            priority=priority,
            affected_title_keys=affected_title_keys,
            payload=dict(existing.payload),
        )

    def _worker_loop(self) -> None:
        while True:
            task = self._next_task()
            handler = self._handlers.get(task.kind)
            if handler is None:
                self._finish_task(task, TASK_STATUS_FAILED, TASK_RESULT_RETRYABLE_FAILURE)
                continue
            try:
                result = handler(task)
            except Exception:
                result = TASK_RESULT_RETRYABLE_FAILURE
            if result == TASK_RESULT_SKIPPED_ALREADY_RESOLVED:
                self._finish_task(task, TASK_STATUS_DROPPED, result)
            elif result == TASK_RESULT_RETRYABLE_FAILURE:
                self._finish_task(task, TASK_STATUS_FAILED, result)
            else:
                self._finish_task(task, TASK_STATUS_COMPLETED, result)

    def _next_task(self) -> EnrichTask:
        with self._condition:
            while True:
                while self._heap:
                    _priority, _seq, task_key, submission_seq = heapq.heappop(self._heap)
                    current = self._pending.get(task_key)
                    if current is None:
                        continue
                    current_submission_seq, task = current
                    if current_submission_seq != submission_seq:
                        continue
                    self._pending.pop(task_key, None)
                    self._running[task_key] = task
                    self._emit_update_locked(task, TASK_STATUS_RUNNING, None)
                    return task
                self._condition.wait()

    def _finish_task(self, task: EnrichTask, status: str, result: str | None) -> None:
        with self._condition:
            self._running.pop(task.task_key, None)
            if result == TASK_RESULT_RETRYABLE_FAILURE:
                self._cooldowns[task.task_key] = monotonic() + self._retry_backoff_seconds
            else:
                self._cooldowns.pop(task.task_key, None)
            self._emit_update_locked(task, status, result)

    def _emit_update_locked(self, task: EnrichTask, status: str, result: str | None) -> None:
        update = EnrichTaskUpdate(
            revision=self._next_revision,
            task_key=task.task_key,
            kind=task.kind,
            status=status,
            result=result,
            affected_title_keys=task.affected_title_keys,
        )
        self._next_revision += 1
        self._updates.append(update)

    def _has_relevant_work_locked(self, relevant_title_keys: set[str] | None) -> bool:
        pending_tasks = [task for _submission, task in self._pending.values()]
        running_tasks = list(self._running.values())
        if not relevant_title_keys:
            return bool(pending_tasks or running_tasks)
        return any(
            any(title_key in relevant_title_keys for title_key in task.affected_title_keys)
            for task in [*pending_tasks, *running_tasks]
        )

    @staticmethod
    def _update_relevant(update: EnrichTaskUpdate, relevant_title_keys: set[str] | None) -> bool:
        if not relevant_title_keys:
            return True
        return any(title_key in relevant_title_keys for title_key in update.affected_title_keys)
