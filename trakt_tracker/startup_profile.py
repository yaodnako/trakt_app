from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from time import perf_counter


@dataclass(slots=True)
class _Mark:
    name: str
    elapsed_ms: float


class StartupProfiler:
    def __init__(self, path: Path, start_time: float | None = None) -> None:
        self._path = path
        self._start = start_time if start_time is not None else perf_counter()
        self._last = self._start
        self._marks: list[_Mark] = []
        self._external_prefix: _Mark | None = None
        self._finished = False

    def set_external_prefix(self, name: str, elapsed_ms: float) -> None:
        if self._finished:
            return
        self._external_prefix = _Mark(name=name, elapsed_ms=max(0.0, elapsed_ms))

    def mark(self, name: str) -> None:
        if self._finished:
            return
        now = perf_counter()
        self._marks.append(_Mark(name=name, elapsed_ms=(now - self._last) * 1000))
        self._last = now

    def finish(self, name: str = "startup complete") -> None:
        if self._finished:
            return
        self.mark(name)
        total_ms = (perf_counter() - self._start) * 1000
        lines = ["startup profile:"]
        cumulative = 0.0
        if self._external_prefix is not None:
            cumulative += self._external_prefix.elapsed_ms
            lines.append(
                f"  {self._external_prefix.name}: +{self._external_prefix.elapsed_ms:.1f} ms ({cumulative:.1f} ms total)"
            )
        for mark in self._marks:
            cumulative += mark.elapsed_ms
            lines.append(f"  {mark.name}: +{mark.elapsed_ms:.1f} ms ({cumulative:.1f} ms total)")
        total_ms += self._external_prefix.elapsed_ms if self._external_prefix is not None else 0.0
        lines.append(f"  total: {total_ms:.1f} ms")
        output = "\n".join(lines)
        print(output)
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(output + "\n", encoding="utf-8")
        except OSError:
            pass
        self._finished = True
