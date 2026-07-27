from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Any


_GENERIC_EPISODE_TITLE_RE = re.compile(r"^episode\s+\d+$", re.IGNORECASE)
_QUOTE_TRANSLATION = str.maketrans(
    {
        "\u2018": "'",
        "\u2019": "'",
        "\u201a": "'",
        "\u201b": "'",
        "\u201c": '"',
        "\u201d": '"',
        "\u201e": '"',
        "\u201f": '"',
        "\u2013": "-",
        "\u2014": "-",
        "\u2212": "-",
    }
)


@dataclass(frozen=True, slots=True)
class EpisodeIMDbResolution:
    imdb_id: str = ""
    imdb_rating: float | None = None
    imdb_votes: int | None = None
    imdb_season: int | None = None
    imdb_episode: int | None = None
    is_alternate_parent: bool = False


@dataclass(frozen=True, slots=True)
class _EpisodeIMDbCandidate:
    imdb_id: str
    source: str
    parent_imdb_id: str = ""
    season: int | None = None
    episode: int | None = None
    title: str = ""
    imdb_rating: float | None = None
    imdb_votes: int | None = None


class EpisodeIMDbResolver:
    def __init__(self, imdb_client) -> None:
        self._imdb_client = imdb_client

    def resolve(
        self,
        *,
        show_imdb_id: str,
        season: int,
        episode: int,
        title: str,
        trakt_imdb_id: str = "",
    ) -> EpisodeIMDbResolution:
        if not self._is_ready() or not show_imdb_id or season <= 0 or episode <= 0:
            return self._fallback_resolution(trakt_imdb_id)

        current_title = normalize_episode_title(title)
        current_title_is_generic = is_generic_episode_title(title)
        candidates = self._candidates(
            show_imdb_id=show_imdb_id,
            season=season,
            episode=episode,
            title=title,
            trakt_imdb_id=trakt_imdb_id,
        )
        if not candidates:
            return EpisodeIMDbResolution()

        trakt_candidate = next((candidate for candidate in candidates if candidate.source == "trakt"), None)
        if (
            trakt_candidate is not None
            and trakt_candidate.parent_imdb_id
            and trakt_candidate.parent_imdb_id != show_imdb_id
        ):
            if episode_titles_equivalent(trakt_candidate.title, title):
                return self._resolution_from_candidate(trakt_candidate, is_alternate_parent=True)
            return EpisodeIMDbResolution()

        title_matches = [
            candidate
            for candidate in candidates
            if current_title
            and not current_title_is_generic
            and normalize_episode_title(candidate.title) == current_title
            and self._same_parent(candidate, show_imdb_id)
        ]
        chosen = self._prefer_source(title_matches) if title_matches else None
        if chosen is None:
            current_position = [
                candidate
                for candidate in candidates
                if self._same_parent(candidate, show_imdb_id)
                and candidate.season == season
                and candidate.episode == episode
            ]
            chosen = self._prefer_source(current_position)
        if chosen is None:
            overflow_position = [
                candidate
                for candidate in candidates
                if candidate.source == "overflow"
                and self._same_parent(candidate, show_imdb_id)
            ]
            chosen = self._prefer_source(overflow_position)
        if chosen is None:
            return EpisodeIMDbResolution()

        rating = chosen.imdb_rating
        votes = chosen.imdb_votes
        if self._should_suppress_rating_for_generic_title(
            chosen=chosen,
            candidates=candidates,
            title_is_generic=current_title_is_generic,
        ):
            rating = None
            votes = None
        return EpisodeIMDbResolution(
            imdb_id=chosen.imdb_id,
            imdb_rating=rating,
            imdb_votes=votes,
            imdb_season=chosen.season,
            imdb_episode=chosen.episode,
        )

    def resolve_known_id(
        self,
        *,
        show_imdb_id: str,
        title: str,
        imdb_id: str,
    ) -> EpisodeIMDbResolution:
        imdb_id = str(imdb_id or "")
        if not imdb_id:
            return EpisodeIMDbResolution()
        metadata = self._lookup_episode_metadata(imdb_id)
        candidate = self._candidate_from_metadata(imdb_id, "trakt", metadata)
        is_alternate_parent = bool(
            candidate.parent_imdb_id
            and show_imdb_id
            and candidate.parent_imdb_id != show_imdb_id
        )
        if (
            is_alternate_parent
            and not episode_titles_equivalent(candidate.title, title)
        ):
            return EpisodeIMDbResolution(
                imdb_id=imdb_id,
                is_alternate_parent=True,
            )
        return self._resolution_from_candidate(
            candidate,
            is_alternate_parent=is_alternate_parent,
        )

    def _candidates(
        self,
        *,
        show_imdb_id: str,
        season: int,
        episode: int,
        title: str,
        trakt_imdb_id: str,
    ) -> list[_EpisodeIMDbCandidate]:
        candidates: list[_EpisodeIMDbCandidate] = []
        seen: set[str] = set()
        for imdb_id, source in (
            (trakt_imdb_id, "trakt"),
            (self._lookup_episode_imdb_id(show_imdb_id, season, episode), "number"),
            (self._lookup_episode_imdb_id_by_title(show_imdb_id, title), "title"),
            (self._lookup_overflow_episode_imdb_id(show_imdb_id, season, episode), "overflow"),
        ):
            imdb_id = str(imdb_id or "")
            if not imdb_id or imdb_id in seen:
                continue
            seen.add(imdb_id)
            metadata = self._lookup_episode_metadata(imdb_id)
            candidates.append(self._candidate_from_metadata(imdb_id, source, metadata))
        return candidates

    def _candidate_from_metadata(self, imdb_id: str, source: str, metadata: dict[str, Any] | None) -> _EpisodeIMDbCandidate:
        if not metadata:
            return _EpisodeIMDbCandidate(imdb_id=imdb_id, source=source)
        return _EpisodeIMDbCandidate(
            imdb_id=imdb_id,
            source=source,
            parent_imdb_id=str(metadata.get("parent_imdb_id", "") or ""),
            season=self._as_int(metadata.get("season")),
            episode=self._as_int(metadata.get("episode")),
            title=str(metadata.get("title", "") or ""),
            imdb_rating=self._as_float(metadata.get("imdb_rating")),
            imdb_votes=self._as_int(metadata.get("imdb_votes")),
        )

    @staticmethod
    def _same_parent(candidate: _EpisodeIMDbCandidate, show_imdb_id: str) -> bool:
        return not candidate.parent_imdb_id or candidate.parent_imdb_id == show_imdb_id

    @staticmethod
    def _prefer_source(candidates: list[_EpisodeIMDbCandidate]) -> _EpisodeIMDbCandidate | None:
        if not candidates:
            return None
        priority = {"trakt": 0, "number": 1, "title": 2}
        return sorted(candidates, key=lambda candidate: priority.get(candidate.source, 99))[0]

    @staticmethod
    def _should_suppress_rating_for_generic_title(
        *,
        chosen: _EpisodeIMDbCandidate,
        candidates: list[_EpisodeIMDbCandidate],
        title_is_generic: bool,
    ) -> bool:
        if not title_is_generic or chosen.source == "trakt":
            return False
        trakt_candidates = [candidate for candidate in candidates if candidate.source == "trakt"]
        return any(candidate.imdb_id != chosen.imdb_id for candidate in trakt_candidates)

    def _fallback_resolution(self, imdb_id: str) -> EpisodeIMDbResolution:
        imdb_id = str(imdb_id or "")
        if not imdb_id:
            return EpisodeIMDbResolution()
        metadata = self._lookup_episode_metadata(imdb_id)
        if metadata:
            return EpisodeIMDbResolution(
                imdb_id=imdb_id,
                imdb_rating=self._as_float(metadata.get("imdb_rating")),
                imdb_votes=self._as_int(metadata.get("imdb_votes")),
                imdb_season=self._as_int(metadata.get("season")),
                imdb_episode=self._as_int(metadata.get("episode")),
            )
        return EpisodeIMDbResolution(imdb_id=imdb_id)

    @staticmethod
    def _resolution_from_candidate(
        candidate: _EpisodeIMDbCandidate,
        *,
        is_alternate_parent: bool = False,
    ) -> EpisodeIMDbResolution:
        return EpisodeIMDbResolution(
            imdb_id=candidate.imdb_id,
            imdb_rating=candidate.imdb_rating,
            imdb_votes=candidate.imdb_votes,
            imdb_season=(None if is_alternate_parent else candidate.season),
            imdb_episode=(None if is_alternate_parent else candidate.episode),
            is_alternate_parent=is_alternate_parent,
        )

    def _is_ready(self) -> bool:
        return bool(getattr(self._imdb_client, "is_ready", lambda: False)())

    def _lookup_episode_imdb_id(self, show_imdb_id: str, season: int, episode: int) -> str:
        return str(getattr(self._imdb_client, "lookup_episode_imdb_id", lambda *_args: "")(show_imdb_id, season, episode) or "")

    def _lookup_episode_imdb_id_by_title(self, show_imdb_id: str, title: str) -> str:
        return str(getattr(self._imdb_client, "lookup_episode_imdb_id_by_title", lambda *_args: "")(show_imdb_id, title) or "")

    def _lookup_overflow_episode_imdb_id(self, show_imdb_id: str, season: int, episode: int) -> str:
        return str(getattr(self._imdb_client, "lookup_overflow_episode_imdb_id", lambda *_args: "")(show_imdb_id, season, episode) or "")

    def _lookup_episode_metadata(self, imdb_id: str) -> dict[str, Any] | None:
        lookup = getattr(self._imdb_client, "lookup_episode_metadata", None)
        if lookup is None:
            return None
        metadata = lookup(imdb_id)
        return metadata if isinstance(metadata, dict) else None

    @staticmethod
    def _as_float(value) -> float | None:
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _as_int(value) -> int | None:
        if value is None:
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None


def normalize_episode_title(title: str | None) -> str:
    normalized = unicodedata.normalize("NFKC", str(title or ""))
    normalized = normalized.translate(_QUOTE_TRANSLATION)
    return " ".join(normalized.casefold().strip().split())


def is_generic_episode_title(title: str | None) -> bool:
    normalized = normalize_episode_title(title)
    return bool(_GENERIC_EPISODE_TITLE_RE.fullmatch(normalized))


def episode_titles_equivalent(left: str | None, right: str | None) -> bool:
    normalized_left = normalize_episode_title(left)
    normalized_right = normalize_episode_title(right)
    if not normalized_left or not normalized_right:
        return False
    if normalized_left == normalized_right:
        return True
    identity_left = " ".join(
        "".join(character if character.isalnum() else " " for character in normalized_left).split()
    )
    identity_right = " ".join(
        "".join(character if character.isalnum() else " " for character in normalized_right).split()
    )
    return bool(identity_left and identity_left == identity_right)
