from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime
from threading import Lock


@dataclass(slots=True)
class OperationEvent:
    seq: int
    source: str
    message: str
    created_at: datetime


class OperationLog:
    def __init__(self, max_events: int = 200) -> None:
        self._events: deque[OperationEvent] = deque(maxlen=max_events)
        self._lock = Lock()
        self._next_seq = 1

    def publish(self, source: str, message: str) -> OperationEvent:
        with self._lock:
            event = OperationEvent(
                seq=self._next_seq,
                source=source,
                message=message,
                created_at=datetime.now(tz=UTC),
            )
            self._next_seq += 1
            self._events.append(event)
            return event

    def current_seq(self) -> int:
        with self._lock:
            return self._next_seq - 1

    def list_after(self, seq: int) -> list[dict]:
        with self._lock:
            events = [event for event in self._events if event.seq > seq]
        return [
            {
                "seq": event.seq,
                "source": event.source,
                "message": event.message,
                "created_at": event.created_at.isoformat(),
            }
            for event in events
        ]
