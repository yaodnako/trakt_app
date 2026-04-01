from __future__ import annotations

import hashlib
import json
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from trakt_tracker.config import get_app_data_dir


class ProviderCache:
    def __init__(self, provider: str) -> None:
        self.provider = provider
        self._dir = get_app_data_dir() / "cache" / provider
        self._dir.mkdir(parents=True, exist_ok=True)

    def get_json(self, key: str, ttl_hours: int) -> Any | None:
        path = self._path_for_key(key)
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        created_at_raw = payload.get("created_at")
        if not isinstance(created_at_raw, str):
            return None
        try:
            created_at = datetime.fromisoformat(created_at_raw)
        except ValueError:
            return None
        if datetime.now(tz=UTC) - created_at > timedelta(hours=ttl_hours):
            return None
        return payload.get("value")

    def set_json(self, key: str, value: Any) -> None:
        path = self._path_for_key(key)
        payload = {
            "created_at": datetime.now(tz=UTC).isoformat(),
            "value": value,
        }
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def clear(self) -> None:
        if self._dir.exists():
            shutil.rmtree(self._dir, ignore_errors=True)
        self._dir.mkdir(parents=True, exist_ok=True)

    def _path_for_key(self, key: str) -> Path:
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        return self._dir / f"{digest}.json"


class BinaryCache:
    def __init__(self, provider: str) -> None:
        self.provider = provider
        self._dir = get_app_data_dir() / "cache" / provider
        self._dir.mkdir(parents=True, exist_ok=True)

    def get_bytes(self, key: str, ttl_hours: int) -> bytes | None:
        for path in self._candidate_paths(key):
            if not path.exists():
                continue
            modified = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
            if datetime.now(tz=UTC) - modified > timedelta(hours=ttl_hours):
                continue
            try:
                return path.read_bytes()
            except OSError:
                continue
        return None

    def set_bytes(self, key: str, value: bytes, suffix: str = ".img") -> None:
        path = self._path_for_key(key, suffix=suffix)
        path.write_bytes(value)

    def clear(self) -> None:
        if self._dir.exists():
            shutil.rmtree(self._dir, ignore_errors=True)
        self._dir.mkdir(parents=True, exist_ok=True)

    def _path_for_key(self, key: str, suffix: str = ".bin") -> Path:
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        return self._dir / f"{digest}{suffix}"

    def _candidate_paths(self, key: str) -> list[Path]:
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        preferred = [
            self._dir / f"{digest}.img",
            self._dir / f"{digest}.bin",
        ]
        discovered = sorted(self._dir.glob(f"{digest}.*"))
        unique_paths: list[Path] = []
        seen: set[Path] = set()
        for path in [*preferred, *discovered]:
            if path in seen:
                continue
            seen.add(path)
            unique_paths.append(path)
        return unique_paths
