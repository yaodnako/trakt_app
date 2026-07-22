from __future__ import annotations

import heapq
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass
from threading import Condition, Thread
from time import monotonic
from typing import Iterator

from trakt_tracker.infrastructure.artwork_cache import fetch_and_cache_image, has_cached_image, is_trusted_image_url
from trakt_tracker.infrastructure.cache import BinaryCache


@dataclass(slots=True)
class _ImageTask:
    url: str
    priority: int


class ArtworkQueue:
    """Bounded, deduplicated image work that never occupies an HTTP response."""

    def __init__(self, cache: BinaryCache, *, max_workers: int = 4, timeout: float = 8.0) -> None:
        self._cache = cache
        self._timeout = max(1.0, float(timeout))
        self._condition = Condition()
        self._pending: dict[str, tuple[int, _ImageTask]] = {}
        self._running: set[str] = set()
        self._heap: list[tuple[int, int, str, int]] = []
        self._next_submission = 1
        self._paused = False
        self._closed = False
        self._failed = 0
        self._recent_failures: deque[str] = deque(maxlen=20)
        self._workers = [
            Thread(target=self._worker_loop, name=f"artwork-queue-{index + 1}", daemon=True)
            for index in range(max(1, int(max_workers)))
        ]
        for worker in self._workers:
            worker.start()

    def submit(self, url: str, *, priority: int = 3) -> bool:
        target_url = str(url or "").strip()
        if not is_trusted_image_url(target_url) or has_cached_image(self._cache, target_url):
            return False
        normalized_priority = max(1, int(priority or 1))
        with self._condition:
            if self._closed:
                return False
            if target_url in self._running:
                return False
            existing = self._pending.get(target_url)
            if existing is not None:
                _existing_seq, existing_task = existing
                if normalized_priority >= existing_task.priority:
                    return False
            task = _ImageTask(url=target_url, priority=normalized_priority)
            submission = self._next_submission
            self._next_submission += 1
            self._pending[target_url] = (submission, task)
            heapq.heappush(self._heap, (task.priority, submission, target_url, submission))
            self._condition.notify()
            return True

    def submit_many(self, urls, *, priority: int = 3) -> int:
        return sum(1 for url in dict.fromkeys(urls) if self.submit(str(url or ""), priority=priority))

    def pause(self) -> None:
        with self._condition:
            self._paused = True

    def resume(self) -> None:
        with self._condition:
            self._paused = False
            self._condition.notify_all()

    def wait_for_idle(self, timeout: float | None = None) -> bool:
        deadline = None if timeout is None else monotonic() + max(0.0, float(timeout))
        with self._condition:
            while self._running:
                remaining = None if deadline is None else deadline - monotonic()
                if remaining is not None and remaining <= 0:
                    return False
                self._condition.wait(timeout=remaining)
            return True

    @contextmanager
    def exclusive_pause(self, timeout: float | None = None) -> Iterator[bool]:
        self.pause()
        idle = self.wait_for_idle(timeout=timeout)
        try:
            yield idle
        finally:
            self.resume()

    def status_snapshot(self) -> dict:
        with self._condition:
            return {
                "pending": len(self._pending),
                "running": len(self._running),
                "paused": self._paused,
                "failed": self._failed,
                "recent_failures": list(self._recent_failures),
            }

    def close(self, timeout: float = 2.0) -> bool:
        with self._condition:
            self._closed = True
            self._pending.clear()
            self._heap.clear()
            self._condition.notify_all()
        for worker in self._workers:
            worker.join(timeout=max(0.0, float(timeout)))
        return all(not worker.is_alive() for worker in self._workers)

    def _worker_loop(self) -> None:
        while True:
            task = self._next_task()
            if task is None:
                return
            failed = False
            try:
                failed = fetch_and_cache_image(self._cache, task.url, self._timeout) is None
            except Exception:
                failed = True
            finally:
                with self._condition:
                    self._running.discard(task.url)
                    if failed:
                        self._failed += 1
                        self._recent_failures.append(task.url)
                    self._condition.notify_all()

    def _next_task(self) -> _ImageTask | None:
        with self._condition:
            while True:
                if self._closed:
                    return None
                if not self._paused:
                    while self._heap:
                        _priority, _submission, url, generation = heapq.heappop(self._heap)
                        current = self._pending.get(url)
                        if current is None or current[0] != generation:
                            continue
                        _current_generation, task = self._pending.pop(url)
                        self._running.add(url)
                        return task
                self._condition.wait()
