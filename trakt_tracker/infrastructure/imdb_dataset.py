from __future__ import annotations

import gzip
import shutil
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

from trakt_tracker.config import get_app_data_dir
from trakt_tracker.domain import EpisodeSummary, TitleSummary


IMDb_RATINGS_URL = "https://datasets.imdbws.com/title.ratings.tsv.gz"
IMDb_EPISODES_URL = "https://datasets.imdbws.com/title.episode.tsv.gz"
IMDb_BASICS_URL = "https://datasets.imdbws.com/title.basics.tsv.gz"


class IMDbDatasetClient:
    def __init__(self, *, timeout: float = 60.0, cache_ttl_hours: int = 24) -> None:
        self._client = httpx.Client(timeout=timeout, follow_redirects=True)
        self._cache_ttl_hours = cache_ttl_hours
        self._dir = get_app_data_dir() / "imdb"
        self._dir.mkdir(parents=True, exist_ok=True)
        self._db_path = self._dir / "ratings.sqlite3"

    @property
    def db_path(self) -> Path:
        return self._db_path

    def is_ready(self) -> bool:
        return self._db_path.exists() and self._has_required_schema()

    def is_stale(self) -> bool:
        if not self._db_path.exists() or not self._has_required_schema():
            return True
        modified = datetime.fromtimestamp(self._db_path.stat().st_mtime, tz=UTC)
        return datetime.now(tz=UTC) - modified > timedelta(hours=self._cache_ttl_hours)

    def last_updated_text(self) -> str:
        if not self._db_path.exists():
            return "not synced"
        if not self._has_required_schema():
            return "outdated schema"
        modified = datetime.fromtimestamp(self._db_path.stat().st_mtime, tz=UTC).astimezone()
        return modified.strftime("%Y-%m-%d %H:%M")

    def clear(self) -> None:
        if self._db_path.exists():
            self._db_path.unlink(missing_ok=True)

    def sync(self, *, force: bool = False, status_callback=None) -> bool:
        if not force and self.is_ready() and not self.is_stale():
            if status_callback is not None:
                status_callback(self.last_updated_text())
            return False
        tmp_ratings_gz = self._dir / "title.ratings.tsv.gz"
        tmp_episodes_gz = self._dir / "title.episode.tsv.gz"
        tmp_basics_gz = self._dir / "title.basics.tsv.gz"
        tmp_db = self._dir / "ratings.tmp.sqlite3"
        if tmp_ratings_gz.exists():
            tmp_ratings_gz.unlink(missing_ok=True)
        if tmp_episodes_gz.exists():
            tmp_episodes_gz.unlink(missing_ok=True)
        if tmp_basics_gz.exists():
            tmp_basics_gz.unlink(missing_ok=True)
        if tmp_db.exists():
            tmp_db.unlink(missing_ok=True)

        if status_callback is not None:
            status_callback("Downloading IMDb ratings...")
        self._download(IMDb_RATINGS_URL, tmp_ratings_gz, label="Downloading IMDb ratings", status_callback=status_callback)
        if status_callback is not None:
            status_callback("Downloading IMDb episode map...")
        self._download(IMDb_EPISODES_URL, tmp_episodes_gz, label="Downloading IMDb episode map", status_callback=status_callback)
        if status_callback is not None:
            status_callback("Downloading IMDb episode titles...")
        self._download(IMDb_BASICS_URL, tmp_basics_gz, label="Downloading IMDb episode titles", status_callback=status_callback)

        if status_callback is not None:
            status_callback("Building local index...")
        conn = sqlite3.connect(tmp_db)
        try:
            conn.execute("PRAGMA journal_mode=OFF")
            conn.execute("PRAGMA synchronous=OFF")
            conn.execute("CREATE TABLE ratings (tconst TEXT PRIMARY KEY, average_rating REAL, num_votes INTEGER)")
            conn.execute(
                "CREATE TABLE episodes (tconst TEXT PRIMARY KEY, parent_tconst TEXT, season_number INTEGER, episode_number INTEGER)"
            )
            conn.execute("CREATE TABLE basics (tconst TEXT PRIMARY KEY, primary_title TEXT)")
            batch: list[tuple[str, float, int]] = []
            with gzip.open(tmp_ratings_gz, "rt", encoding="utf-8", newline="") as handle:
                next(handle, None)
                for line in handle:
                    parts = line.rstrip("\n").split("\t")
                    if len(parts) != 3:
                        continue
                    tconst, average_rating, num_votes = parts
                    try:
                        batch.append((tconst, float(average_rating), int(num_votes)))
                    except ValueError:
                        continue
                    if len(batch) >= 10000:
                        conn.executemany("INSERT INTO ratings VALUES (?, ?, ?)", batch)
                        batch.clear()
                if batch:
                    conn.executemany("INSERT INTO ratings VALUES (?, ?, ?)", batch)
            episode_batch: list[tuple[str, str, int, int]] = []
            with gzip.open(tmp_episodes_gz, "rt", encoding="utf-8", newline="") as handle:
                next(handle, None)
                for line in handle:
                    parts = line.rstrip("\n").split("\t")
                    if len(parts) != 4:
                        continue
                    tconst, parent_tconst, season_number, episode_number = parts
                    if season_number == "\\N" or episode_number == "\\N":
                        continue
                    try:
                        episode_batch.append((tconst, parent_tconst, int(season_number), int(episode_number)))
                    except ValueError:
                        continue
                    if len(episode_batch) >= 10000:
                        conn.executemany("INSERT INTO episodes VALUES (?, ?, ?, ?)", episode_batch)
                        episode_batch.clear()
                if episode_batch:
                    conn.executemany("INSERT INTO episodes VALUES (?, ?, ?, ?)", episode_batch)
            basics_batch: list[tuple[str, str]] = []
            with gzip.open(tmp_basics_gz, "rt", encoding="utf-8", newline="") as handle:
                next(handle, None)
                for line in handle:
                    parts = line.rstrip("\n").split("\t")
                    if len(parts) < 3:
                        continue
                    tconst, title_type, primary_title = parts[0], parts[1], parts[2]
                    if title_type != "tvEpisode" or primary_title == "\\N":
                        continue
                    basics_batch.append((tconst, primary_title))
                    if len(basics_batch) >= 10000:
                        conn.executemany("INSERT INTO basics VALUES (?, ?)", basics_batch)
                        basics_batch.clear()
                if basics_batch:
                    conn.executemany("INSERT INTO basics VALUES (?, ?)", basics_batch)
            conn.execute("CREATE INDEX idx_ratings_votes ON ratings(num_votes)")
            conn.execute("CREATE INDEX idx_episodes_parent_season_episode ON episodes(parent_tconst, season_number, episode_number)")
            conn.execute("CREATE INDEX idx_basics_title ON basics(primary_title)")
            conn.commit()
        finally:
            conn.close()

        shutil.move(str(tmp_db), str(self._db_path))
        tmp_ratings_gz.unlink(missing_ok=True)
        tmp_episodes_gz.unlink(missing_ok=True)
        tmp_basics_gz.unlink(missing_ok=True)
        if status_callback is not None:
            status_callback(self.last_updated_text())
        return True

    def enrich_title(self, title: TitleSummary) -> TitleSummary:
        if not title.imdb_id or not self._db_path.exists():
            return title
        row = self._lookup(title.imdb_id)
        if row is None:
            title.imdb_rating = None
            title.imdb_votes = None
            return title
        title.imdb_rating = float(row[0]) if row[0] is not None else None
        title.imdb_votes = int(row[1]) if row[1] is not None else None
        return title

    def enrich_episode(self, episode: EpisodeSummary) -> EpisodeSummary:
        if not episode.imdb_id or not self._db_path.exists():
            return episode
        row = self._lookup(episode.imdb_id)
        if row is None:
            episode.imdb_rating = None
            episode.imdb_votes = None
            return episode
        episode.imdb_rating = float(row[0]) if row[0] is not None else None
        episode.imdb_votes = int(row[1]) if row[1] is not None else None
        return episode

    def lookup_episode_imdb_id(self, show_imdb_id: str, season_number: int, episode_number: int) -> str:
        if not show_imdb_id or not self._db_path.exists() or season_number <= 0 or episode_number <= 0:
            return ""
        conn = sqlite3.connect(self._db_path)
        try:
            try:
                row = conn.execute(
                    "SELECT tconst FROM episodes WHERE parent_tconst = ? AND season_number = ? AND episode_number = ?",
                    (show_imdb_id, season_number, episode_number),
                ).fetchone()
            except sqlite3.OperationalError:
                return ""
            return str(row[0]) if row and row[0] else ""
        finally:
            conn.close()

    def lookup_episode_imdb_id_by_title(self, show_imdb_id: str, episode_title: str) -> str:
        normalized_title = " ".join((episode_title or "").strip().lower().split())
        if not show_imdb_id or not normalized_title or not self._db_path.exists():
            return ""
        conn = sqlite3.connect(self._db_path)
        try:
            try:
                rows = conn.execute(
                    """
                    SELECT e.tconst
                    FROM episodes e
                    JOIN basics b ON b.tconst = e.tconst
                    WHERE e.parent_tconst = ?
                      AND lower(trim(b.primary_title)) = ?
                    """,
                    (show_imdb_id, normalized_title),
                ).fetchall()
            except sqlite3.OperationalError:
                return ""
            if len(rows) == 1 and rows[0][0]:
                return str(rows[0][0])
            return ""
        finally:
            conn.close()

    def _download(self, url: str, destination: Path, *, label: str, status_callback=None) -> None:
        with self._client.stream("GET", url) as response:
            response.raise_for_status()
            total_bytes = int(response.headers.get("Content-Length", "0") or "0")
            written = 0
            last_reported_percent = -1
            with destination.open("wb") as handle:
                for chunk in response.iter_bytes():
                    if chunk:
                        handle.write(chunk)
                        written += len(chunk)
                        if status_callback is None or total_bytes <= 0:
                            continue
                        percent = min(100, int((written / total_bytes) * 100))
                        if percent >= last_reported_percent + 5 or percent == 100:
                            last_reported_percent = percent
                            status_callback(f"{label}... {percent}%")

    def _has_required_schema(self) -> bool:
        if not self._db_path.exists():
            return False
        conn = sqlite3.connect(self._db_path)
        try:
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('ratings', 'episodes', 'basics')"
            ).fetchall()
            names = {str(row[0]) for row in rows if row and row[0]}
            return {"ratings", "episodes", "basics"}.issubset(names)
        except sqlite3.Error:
            return False
        finally:
            conn.close()

    def _lookup(self, imdb_id: str):
        conn = sqlite3.connect(self._db_path)
        try:
            return conn.execute(
                "SELECT average_rating, num_votes FROM ratings WHERE tconst = ?",
                (imdb_id,),
            ).fetchone()
        finally:
            conn.close()
