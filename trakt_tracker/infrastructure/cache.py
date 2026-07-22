from __future__ import annotations

import hashlib
import json
import os
import shutil
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Lock
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
        _atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2))

    def clear(self) -> None:
        if self._dir.exists():
            shutil.rmtree(self._dir, ignore_errors=True)
        self._dir.mkdir(parents=True, exist_ok=True)

    def _path_for_key(self, key: str) -> Path:
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        return self._dir / f"{digest}.json"


class BinaryCache:
    _DIRECT_SUFFIXES = (
        ".img",
        ".bin",
        ".jpg",
        ".jpeg",
        ".jpe",
        ".png",
        ".webp",
        ".avif",
        ".gif",
        ".bmp",
        ".svg",
        ".html",
    )

    def __init__(self, provider: str) -> None:
        self.provider = provider
        self._dir = get_app_data_dir() / "cache" / provider
        self._dir.mkdir(parents=True, exist_ok=True)
        self._index_lock = Lock()
        self._index: dict[str, tuple[Path, ...]] | None = None

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

    def get_any_bytes(self, key: str) -> bytes | None:
        for path in self._candidate_paths(key):
            if not path.exists():
                continue
            try:
                return path.read_bytes()
            except OSError:
                continue
        return None

    def contains(self, key: str) -> bool:
        digest = self._digest(key)
        direct_paths = self._direct_paths(digest)
        if any(path.is_file() for path in direct_paths):
            return True
        return any(path.is_file() for path in self._indexed_paths(digest))

    def set_bytes(self, key: str, value: bytes, suffix: str = ".img") -> None:
        path = self._path_for_key(key, suffix=suffix)
        _atomic_write_bytes(path, value)
        with self._index_lock:
            if self._index is not None:
                existing = self._index.get(path.stem, ())
                if path not in existing:
                    self._index[path.stem] = (*existing, path)

    def clear(self) -> None:
        if self._dir.exists():
            shutil.rmtree(self._dir, ignore_errors=True)
        self._dir.mkdir(parents=True, exist_ok=True)
        with self._index_lock:
            self._index = None

    def _path_for_key(self, key: str, suffix: str = ".bin") -> Path:
        digest = self._digest(key)
        return self._dir / f"{digest}{suffix}"

    def _candidate_paths(self, key: str) -> list[Path]:
        digest = self._digest(key)
        direct_paths = self._direct_paths(digest)
        if any(path.is_file() for path in direct_paths):
            return direct_paths
        indexed_paths = self._indexed_paths(digest)
        return [*direct_paths, *(path for path in indexed_paths if path not in direct_paths)]

    @staticmethod
    def _digest(key: str) -> str:
        return hashlib.sha256(key.encode("utf-8")).hexdigest()

    def _direct_paths(self, digest: str) -> list[Path]:
        return [self._dir / f"{digest}{suffix}" for suffix in self._DIRECT_SUFFIXES]

    def _indexed_paths(self, digest: str) -> tuple[Path, ...]:
        with self._index_lock:
            if self._index is None:
                self._index = self._build_index()
            return self._index.get(digest, ())

    def _build_index(self) -> dict[str, tuple[Path, ...]]:
        discovered: dict[str, list[Path]] = {}
        try:
            paths = self._dir.iterdir()
            for path in paths:
                if not path.is_file():
                    continue
                digest = path.name.split(".", 1)[0]
                discovered.setdefault(digest, []).append(path)
        except OSError:
            return {}
        return {digest: tuple(sorted(paths)) for digest, paths in discovered.items()}


def _atomic_write_text(path: Path, value: str) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(value, encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _atomic_write_bytes(path: Path, value: bytes) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_bytes(value)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
