from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime

from trakt_tracker.application.enrich_state import (
    ENRICH_STATUS_CHECKED_NO_DATA,
    ENRICH_STATUS_READY,
    ENRICH_STATUS_RETRYABLE_FAILURE,
)
from trakt_tracker.application.episode_imdb_reconciliation import EpisodeIMDbReconciliationService
from trakt_tracker.application.metadata_refresh_policy import (
    ASSET_KIND_EPISODE_RATINGS,
    TRIGGER_VISIBLE_RATINGS_REFRESH,
    metadata_refresh_due,
)


LEGEND_BUCKETS = (
    ("Awesome", 9.0, "rgb(24, 106, 59)"),
    ("Great", 8.0, "rgb(40, 180, 99)"),
    ("Good", 7.0, "rgb(244, 208, 63)"),
    ("Regular", 6.0, "rgb(243, 156, 18)"),
    ("Bad", 5.0, "rgb(231, 76, 60)"),
    ("Garbage", None, "rgb(99, 57, 116)"),
)


@dataclass(slots=True)
class EpisodeMatrixLegendItem:
    label: str
    threshold_label: str
    color: str


@dataclass(slots=True)
class EpisodeMatrixCell:
    season: int
    episode: int
    exists: bool
    display_value: str
    imdb_season: int | None = None
    imdb_episode: int | None = None
    imdb_rating: float | None = None
    imdb_votes: int | None = None
    imdb_url: str = ""
    color: str = ""
    state: str = "empty"
    imdb_display_value: str = ""
    imdb_color: str = ""
    imdb_state: str = "empty"
    imdb_tooltip: str = ""
    trakt_rating: float | None = None
    trakt_votes: int | None = None
    trakt_display_value: str = "?"
    trakt_color: str = ""
    trakt_state: str = "unrated"
    trakt_tooltip: str = ""
    my_rating: float | None = None
    my_display_value: str = "?"
    my_color: str = ""
    my_state: str = "unrated"
    title: str = ""
    tooltip: str = ""


@dataclass(slots=True)
class EpisodeMatrixSeason:
    season: int
    label: str
    avg_display: str
    avg_rating: float | None = None
    avg_color: str = ""
    imdb_avg_display: str = "?"
    imdb_avg_rating: float | None = None
    imdb_avg_color: str = ""
    trakt_avg_display: str = "?"
    trakt_avg_rating: float | None = None
    trakt_avg_color: str = ""
    my_avg_display: str = "?"
    my_avg_rating: float | None = None
    my_avg_color: str = ""


@dataclass(slots=True)
class EpisodeMatrixRow:
    episode: int
    label: str
    cells: list[EpisodeMatrixCell] = field(default_factory=list)


@dataclass(slots=True)
class EpisodeRatingsMatrixViewModel:
    trakt_id: int
    title: str
    subtitle: str
    title_trakt_rating: float | None = None
    title_trakt_votes: int | None = None
    title_imdb_rating: float | None = None
    title_imdb_votes: int | None = None
    title_ratings_status: str = ""
    seasons: list[EpisodeMatrixSeason] = field(default_factory=list)
    rows: list[EpisodeMatrixRow] = field(default_factory=list)
    imdb_seasons: list[EpisodeMatrixSeason] = field(default_factory=list)
    imdb_rows: list[EpisodeMatrixRow] = field(default_factory=list)
    legend: list[EpisodeMatrixLegendItem] = field(default_factory=list)
    has_episodes: bool = False
    error_message: str = ""
    provider: str = "imdb"


def rating_bucket_color(rating: float | None) -> str:
    if rating is None:
        return ""
    for _label, threshold, color in LEGEND_BUCKETS:
        if threshold is None or rating >= threshold:
            return color
    return ""


class EpisodeRatingsMatrixService:
    def __init__(self, db, auth_service, titles_repo, history_repo, episode_repo, imdb_client, imdb_reconciliation=None) -> None:
        self._db = db
        self._auth = auth_service
        self._titles = titles_repo
        self._history = history_repo
        self._episode_repo = episode_repo
        self._imdb_client = imdb_client
        self._imdb_reconciliation = imdb_reconciliation or EpisodeIMDbReconciliationService(db, episode_repo, imdb_client)

    def load_show_matrix(
        self,
        trakt_id: int,
        *,
        force_refresh: bool = False,
        provider: str = "imdb",
        refresh_missing: bool = False,
        allow_network_refresh: bool = True,
    ) -> EpisodeRatingsMatrixViewModel:
        normalized_provider = self._normalize_provider(provider)
        title = self._load_title(trakt_id)
        error_message = ""
        with self._db.session() as session:
            episode_rows = self._episode_repo.list_show_episode_metadata(session, trakt_id)
            my_ratings = self._history.latest_show_episode_ratings(session, trakt_id)
        should_hydrate = force_refresh or not episode_rows
        if should_hydrate and allow_network_refresh:
            try:
                self._hydrate_show_episodes(trakt_id)
            except Exception as exc:
                error_message = str(exc)
            with self._db.session() as session:
                episode_rows = self._episode_repo.list_show_episode_metadata(session, trakt_id)
                my_ratings = self._history.latest_show_episode_ratings(session, trakt_id)
        if normalized_provider == "trakt" and (refresh_missing or force_refresh) and episode_rows and allow_network_refresh:
            try:
                self._refresh_due_trakt_ratings(
                    trakt_id,
                    episode_rows,
                    force_refresh=force_refresh,
                )
            except Exception as exc:
                error_message = str(exc)
            with self._db.session() as session:
                episode_rows = self._episode_repo.list_show_episode_metadata(session, trakt_id)
                my_ratings = self._history.latest_show_episode_ratings(session, trakt_id)
        if self._imdb_reconciliation.needs_reconciliation(
            show_imdb_id=title.get("imdb_id", ""),
            episode_rows=episode_rows,
            force=force_refresh,
        ):
            result = self._imdb_reconciliation.reconcile_show(
                trakt_id,
                show_imdb_id=title.get("imdb_id", ""),
                force=force_refresh,
            )
        else:
            result = None
        if result is not None and result.changed:
            with self._db.session() as session:
                episode_rows = self._episode_repo.list_show_episode_metadata(session, trakt_id)
                my_ratings = self._history.latest_show_episode_ratings(session, trakt_id)
        return self._build_matrix(
            trakt_id,
            title=title.get("title", f"Show {trakt_id}"),
            title_trakt_rating=title.get("trakt_rating"),
            title_trakt_votes=title.get("trakt_votes"),
            title_imdb_rating=title.get("imdb_rating"),
            title_imdb_votes=title.get("imdb_votes"),
            title_ratings_status=title.get("ratings_status", ""),
            episode_rows=episode_rows,
            my_ratings=my_ratings,
            error_message=error_message,
            provider=normalized_provider,
        )

    def select_trakt_rating_refresh_keys(self, trakt_id: int, *, force_refresh: bool = False) -> list[tuple[int, int]]:
        """Return local candidates so the HTTP layer can enqueue provider work."""
        with self._db.session() as session:
            episode_rows = self._episode_repo.list_show_episode_metadata(session, trakt_id)
        due_keys: list[tuple[int, int]] = []
        for row in episode_rows:
            if row.get("season") is None or row.get("number") is None:
                continue
            season = int(row["season"])
            episode = int(row["number"])
            if force_refresh:
                due_keys.append((season, episode))
                continue
            due = metadata_refresh_due(
                ASSET_KIND_EPISODE_RATINGS,
                status=str(row.get("trakt_details_status") or ""),
                last_checked_at=row.get("trakt_details_refreshed_at"),
                has_value=(row.get("trakt_rating") is not None and row.get("trakt_votes") is not None),
                trigger=TRIGGER_VISIBLE_RATINGS_REFRESH,
                first_aired=row.get("first_aired"),
            )
            if due.should_refresh:
                due_keys.append((season, episode))
        return due_keys

    @staticmethod
    def _normalize_provider(provider: str | None) -> str:
        normalized = str(provider or "").strip().lower()
        return "trakt" if normalized == "trakt" else "imdb"

    def _load_title(self, trakt_id: int) -> dict:
        with self._db.session() as session:
            title_row = self._titles.get_title(session, trakt_id)
            return {
                "title": title_row.title if title_row is not None and title_row.title else f"Show {trakt_id}",
                "imdb_id": title_row.imdb_id if title_row is not None else "",
                "trakt_rating": title_row.trakt_rating if title_row is not None else None,
                "trakt_votes": title_row.trakt_votes if title_row is not None else None,
                "imdb_rating": title_row.imdb_rating if title_row is not None else None,
                "imdb_votes": title_row.imdb_votes if title_row is not None else None,
                "ratings_status": title_row.ratings_status if title_row is not None else "",
            }

    def _hydrate_show_episodes(self, trakt_id: int) -> None:
        client = self._auth.get_client()
        episodes = client.get_show_episodes(trakt_id)
        with self._db.session() as session:
            self._episode_repo.replace_show_episodes(session, trakt_id, episodes)

    def _refresh_due_trakt_ratings(
        self,
        show_trakt_id: int,
        episode_rows: list[dict],
        *,
        force_refresh: bool,
    ) -> None:
        due_keys: list[tuple[int, int]] = []
        for row in episode_rows:
            if row.get("season") is None or row.get("number") is None:
                continue
            season = int(row["season"])
            episode = int(row["number"])
            if force_refresh:
                due_keys.append((season, episode))
                continue
            due = metadata_refresh_due(
                ASSET_KIND_EPISODE_RATINGS,
                status=str(row.get("trakt_details_status") or ""),
                last_checked_at=row.get("trakt_details_refreshed_at"),
                has_value=(row.get("trakt_rating") is not None and row.get("trakt_votes") is not None),
                trigger=TRIGGER_VISIBLE_RATINGS_REFRESH,
                first_aired=row.get("first_aired"),
            )
            if due.should_refresh:
                due_keys.append((season, episode))
        if not due_keys:
            return
        client = self._auth.get_client()
        first_error: Exception | None = None
        with self._db.session() as session:
            for season, episode in due_keys:
                try:
                    details = client.get_episode_details(show_trakt_id, season, episode, use_cache=False)
                except Exception as exc:
                    first_error = first_error or exc
                    self._episode_repo.update_trakt_details_enrich_state(
                        session,
                        show_trakt_id,
                        season,
                        episode,
                        status=ENRICH_STATUS_RETRYABLE_FAILURE,
                    )
                    continue
                if details is None:
                    self._episode_repo.update_trakt_details_enrich_state(
                        session,
                        show_trakt_id,
                        season,
                        episode,
                        status=ENRICH_STATUS_CHECKED_NO_DATA,
                    )
                    continue
                status = ENRICH_STATUS_READY if details.trakt_rating is not None and details.trakt_votes is not None else ENRICH_STATUS_CHECKED_NO_DATA
                self._episode_repo.update_trakt_details_enrich_state(
                    session,
                    show_trakt_id,
                    season,
                    episode,
                    status=status,
                    details=details,
                )
        if first_error is not None:
            raise first_error

    def _build_matrix(
        self,
        trakt_id: int,
        *,
        title: str,
        title_trakt_rating: float | None,
        title_trakt_votes: int | None,
        title_imdb_rating: float | None,
        title_imdb_votes: int | None,
        title_ratings_status: str,
        episode_rows: list[dict],
        my_ratings: dict[tuple[int, int], int],
        error_message: str,
        provider: str,
    ) -> EpisodeRatingsMatrixViewModel:
        now_utc = datetime.now(tz=UTC)
        if not episode_rows:
            return EpisodeRatingsMatrixViewModel(
                trakt_id=trakt_id,
                title=title,
                subtitle=self._provider_subtitle(provider),
                title_trakt_rating=title_trakt_rating,
                title_trakt_votes=title_trakt_votes,
                title_imdb_rating=title_imdb_rating,
                title_imdb_votes=title_imdb_votes,
                title_ratings_status=title_ratings_status,
                legend=self._legend_items(),
                has_episodes=False,
                error_message=error_message,
                provider=provider,
            )
        season_numbers = sorted({int(row["season"]) for row in episode_rows if row.get("season") is not None})
        max_episode_number = max(int(row["number"]) for row in episode_rows if row.get("number") is not None)
        rows_by_key = {
            (int(row["season"]), int(row["number"])): row
            for row in episode_rows
            if row.get("season") is not None and row.get("number") is not None
        }
        matrix_rows: list[EpisodeMatrixRow] = []
        season_averages: dict[int, float | None] = {}
        season_trakt_averages: dict[int, float | None] = {}
        season_my_averages: dict[int, float | None] = {}
        for season in season_numbers:
            ratings = [
                float(row["imdb_rating"])
                for row in episode_rows
                if int(row.get("season") or 0) == season and row.get("imdb_rating") is not None
            ]
            season_averages[season] = (sum(ratings) / len(ratings)) if ratings else None
            trakt_ratings = [
                visible_trakt_rating
                for row in episode_rows
                for visible_trakt_rating, _visible_votes in [self._visible_trakt_rating(row, now_utc)]
                if int(row.get("season") or 0) == season
                and visible_trakt_rating is not None
            ]
            season_trakt_averages[season] = (sum(trakt_ratings) / len(trakt_ratings)) if trakt_ratings else None
            my_values = [
                float(my_ratings[(season, int(row["number"]))])
                for row in episode_rows
                if int(row.get("season") or 0) == season
                and row.get("number") is not None
                and (season, int(row["number"])) in my_ratings
            ]
            season_my_averages[season] = (sum(my_values) / len(my_values)) if my_values else None
        overall_imdb_values = [
            float(row["imdb_rating"])
            for row in episode_rows
            if int(row.get("season") or 0) != 0 and row.get("imdb_rating") is not None
        ]
        overall_my_values = [
            float(value)
            for (season, _episode), value in my_ratings.items()
            if int(season) != 0
        ]
        overall_imdb_average = (sum(overall_imdb_values) / len(overall_imdb_values)) if overall_imdb_values else None
        overall_trakt_values = [
            visible_trakt_rating
            for row in episode_rows
            for visible_trakt_rating, _visible_votes in [self._visible_trakt_rating(row, now_utc)]
            if int(row.get("season") or 0) != 0
            and visible_trakt_rating is not None
        ]
        overall_trakt_average = (sum(overall_trakt_values) / len(overall_trakt_values)) if overall_trakt_values else None
        overall_my_average = (sum(overall_my_values) / len(overall_my_values)) if overall_my_values else None
        for episode_number in range(1, max_episode_number + 1):
            cells: list[EpisodeMatrixCell] = []
            for season in season_numbers:
                row = rows_by_key.get((season, episode_number))
                if row is None:
                    cells.append(
                        EpisodeMatrixCell(
                            season=season,
                            episode=episode_number,
                            exists=False,
                            display_value="",
                            state="empty",
                        )
                    )
                    continue
                imdb_rating = row.get("imdb_rating")
                imdb_votes = int(row["imdb_votes"]) if row.get("imdb_votes") is not None else None
                trakt_rating, trakt_votes = self._visible_trakt_rating(row, now_utc)
                imdb_id = str(row.get("imdb_id", "") or "")
                episode_title = str(row.get("title", "") or "")
                imdb_rating_value = float(imdb_rating) if imdb_rating is not None else None
                trakt_rating_value = float(trakt_rating) if trakt_rating is not None else None
                my_rating_value = float(my_ratings[(season, episode_number)]) if (season, episode_number) in my_ratings else None
                imdb_display_value = f"{imdb_rating_value:.1f}" if imdb_rating_value is not None else "?"
                trakt_display_value = f"{trakt_rating_value:.1f}" if trakt_rating_value is not None else "?"
                my_display_value = f"{my_rating_value:.0f}" if my_rating_value is not None else "?"
                imdb_color = rating_bucket_color(imdb_rating_value) if imdb_rating_value is not None else ""
                trakt_color = rating_bucket_color(trakt_rating_value) if trakt_rating_value is not None else ""
                my_color = rating_bucket_color(my_rating_value) if my_rating_value is not None else ""
                display_value = trakt_display_value if provider == "trakt" else imdb_display_value
                display_rating = trakt_rating_value if provider == "trakt" else imdb_rating_value
                display_color = trakt_color if provider == "trakt" else imdb_color
                display_votes = trakt_votes if provider == "trakt" else imdb_votes
                tooltip = self._build_cell_tooltip(
                    title=episode_title,
                    season=season,
                    episode=episode_number,
                    votes=display_votes,
                )
                cells.append(
                    EpisodeMatrixCell(
                        season=season,
                        episode=episode_number,
                        exists=True,
                        display_value=display_value,
                        imdb_season=(int(row["imdb_season"]) if row.get("imdb_season") is not None else None),
                        imdb_episode=(int(row["imdb_episode"]) if row.get("imdb_episode") is not None else None),
                        imdb_rating=imdb_rating_value,
                        imdb_votes=imdb_votes,
                        imdb_url=(f"https://www.imdb.com/title/{imdb_id}" if imdb_id else ""),
                        color=display_color,
                        state="rated" if display_rating is not None else "unrated",
                        imdb_display_value=imdb_display_value,
                        imdb_color=imdb_color,
                        imdb_state=("rated" if imdb_rating_value is not None else "unrated"),
                        imdb_tooltip=self._build_cell_tooltip(
                            title=episode_title,
                            season=season,
                            episode=episode_number,
                            votes=imdb_votes,
                        ),
                        trakt_rating=trakt_rating_value,
                        trakt_votes=trakt_votes,
                        trakt_display_value=trakt_display_value,
                        trakt_color=trakt_color,
                        trakt_state=("rated" if trakt_rating_value is not None else "unrated"),
                        trakt_tooltip=self._build_cell_tooltip(
                            title=episode_title,
                            season=season,
                            episode=episode_number,
                            votes=trakt_votes,
                        ),
                        my_rating=my_rating_value,
                        my_display_value=my_display_value,
                        my_color=my_color,
                        my_state=("rated" if my_rating_value is not None else "unrated"),
                        title=episode_title,
                        tooltip=tooltip,
                    )
                )
            label = f"E{episode_number}"
            matrix_rows.append(
                EpisodeMatrixRow(
                    episode=episode_number,
                    label=label,
                    cells=cells,
                )
            )
        seasons = [
            EpisodeMatrixSeason(
                season=season,
                label=f"S{season}",
                avg_display=(
                    f"{season_trakt_averages[season]:.1f}" if provider == "trakt" and season_trakt_averages[season] is not None
                    else f"{season_averages[season]:.1f}" if provider == "imdb" and season_averages[season] is not None
                    else "?"
                ),
                avg_rating=(season_trakt_averages[season] if provider == "trakt" else season_averages[season]),
                avg_color=rating_bucket_color(season_trakt_averages[season] if provider == "trakt" else season_averages[season]),
                imdb_avg_display=f"{season_averages[season]:.1f}" if season_averages[season] is not None else "?",
                imdb_avg_rating=season_averages[season],
                imdb_avg_color=rating_bucket_color(season_averages[season]),
                trakt_avg_display=f"{season_trakt_averages[season]:.1f}" if season_trakt_averages[season] is not None else "?",
                trakt_avg_rating=season_trakt_averages[season],
                trakt_avg_color=rating_bucket_color(season_trakt_averages[season]),
                my_avg_display=f"{season_my_averages[season]:.1f}" if season_my_averages[season] is not None else "?",
                my_avg_rating=season_my_averages[season],
                my_avg_color=rating_bucket_color(season_my_averages[season]),
            )
            for season in season_numbers
        ]
        seasons.append(
            EpisodeMatrixSeason(
                season=-1,
                label="ALL",
                avg_display=(
                    f"{overall_trakt_average:.1f}" if provider == "trakt" and overall_trakt_average is not None
                    else f"{overall_imdb_average:.1f}" if provider == "imdb" and overall_imdb_average is not None
                    else "?"
                ),
                avg_rating=(overall_trakt_average if provider == "trakt" else overall_imdb_average),
                avg_color=rating_bucket_color(overall_trakt_average if provider == "trakt" else overall_imdb_average),
                imdb_avg_display=f"{overall_imdb_average:.1f}" if overall_imdb_average is not None else "?",
                imdb_avg_rating=overall_imdb_average,
                imdb_avg_color=rating_bucket_color(overall_imdb_average),
                trakt_avg_display=f"{overall_trakt_average:.1f}" if overall_trakt_average is not None else "?",
                trakt_avg_rating=overall_trakt_average,
                trakt_avg_color=rating_bucket_color(overall_trakt_average),
                my_avg_display=f"{overall_my_average:.1f}" if overall_my_average is not None else "?",
                my_avg_rating=overall_my_average,
                my_avg_color=rating_bucket_color(overall_my_average),
            )
        )
        imdb_seasons, imdb_rows = self._build_imdb_season_layout(
            episode_rows=episode_rows,
            trakt_rows=matrix_rows,
            provider=provider,
        )
        return EpisodeRatingsMatrixViewModel(
            trakt_id=trakt_id,
            title=title,
            subtitle=self._provider_subtitle(provider),
            title_trakt_rating=title_trakt_rating,
            title_trakt_votes=title_trakt_votes,
            title_imdb_rating=title_imdb_rating,
            title_imdb_votes=title_imdb_votes,
            title_ratings_status=title_ratings_status,
            seasons=seasons,
            rows=matrix_rows,
            imdb_seasons=imdb_seasons,
            imdb_rows=imdb_rows,
            legend=self._legend_items(),
            has_episodes=True,
            error_message=error_message,
            provider=provider,
        )

    def _build_imdb_season_layout(
        self,
        *,
        episode_rows: list[dict],
        trakt_rows: list[EpisodeMatrixRow],
        provider: str,
    ) -> tuple[list[EpisodeMatrixSeason], list[EpisodeMatrixRow]]:
        cells_by_trakt_key = {
            (cell.season, cell.episode): cell
            for matrix_row in trakt_rows
            for cell in matrix_row.cells
            if cell.exists
        }
        cells_by_imdb_key: dict[tuple[int, int], EpisodeMatrixCell] = {}
        explicit_coordinates: dict[tuple[int, int], bool] = {}
        for row in episode_rows:
            if row.get("season") is None or row.get("number") is None:
                continue
            trakt_key = (int(row["season"]), int(row["number"]))
            cell = cells_by_trakt_key.get(trakt_key)
            if cell is None:
                continue
            has_imdb_coordinates = row.get("imdb_season") is not None and row.get("imdb_episode") is not None
            imdb_key = (
                (int(row["imdb_season"]), int(row["imdb_episode"]))
                if has_imdb_coordinates
                else trakt_key
            )
            if imdb_key in cells_by_imdb_key and not (has_imdb_coordinates and not explicit_coordinates[imdb_key]):
                continue
            imdb_season, imdb_episode = imdb_key
            cells_by_imdb_key[imdb_key] = replace(
                cell,
                season=imdb_season,
                episode=imdb_episode,
                tooltip=self._build_cell_tooltip(
                    title=cell.title,
                    season=imdb_season,
                    episode=imdb_episode,
                    votes=(cell.trakt_votes if provider == "trakt" else cell.imdb_votes),
                ),
                imdb_tooltip=self._build_cell_tooltip(
                    title=cell.title,
                    season=imdb_season,
                    episode=imdb_episode,
                    votes=cell.imdb_votes,
                ),
                trakt_tooltip=self._build_cell_tooltip(
                    title=cell.title,
                    season=imdb_season,
                    episode=imdb_episode,
                    votes=cell.trakt_votes,
                ),
            )
            explicit_coordinates[imdb_key] = has_imdb_coordinates

        if not cells_by_imdb_key:
            return [], []
        season_numbers = sorted({season for season, _episode in cells_by_imdb_key})
        max_episode_number = max(episode for _season, episode in cells_by_imdb_key)
        matrix_rows = [
            EpisodeMatrixRow(
                episode=episode_number,
                label=f"E{episode_number}",
                cells=[
                    cells_by_imdb_key.get(
                        (season, episode_number),
                        EpisodeMatrixCell(
                            season=season,
                            episode=episode_number,
                            exists=False,
                            display_value="",
                            state="empty",
                        ),
                    )
                    for season in season_numbers
                ],
            )
            for episode_number in range(1, max_episode_number + 1)
        ]
        seasons = [
            self._build_layout_season(
                season=season,
                label=f"S{season}",
                cells=[cell for (cell_season, _episode), cell in cells_by_imdb_key.items() if cell_season == season],
                provider=provider,
            )
            for season in season_numbers
        ]
        seasons.append(
            self._build_layout_season(
                season=-1,
                label="ALL",
                cells=[cell for (season, _episode), cell in cells_by_imdb_key.items() if season != 0],
                provider=provider,
            )
        )
        return seasons, matrix_rows

    @classmethod
    def _build_layout_season(
        cls,
        *,
        season: int,
        label: str,
        cells: list[EpisodeMatrixCell],
        provider: str,
    ) -> EpisodeMatrixSeason:
        imdb_average = cls._average(cell.imdb_rating for cell in cells)
        trakt_average = cls._average(cell.trakt_rating for cell in cells)
        my_average = cls._average(cell.my_rating for cell in cells)
        selected_average = trakt_average if provider == "trakt" else imdb_average
        return EpisodeMatrixSeason(
            season=season,
            label=label,
            avg_display=f"{selected_average:.1f}" if selected_average is not None else "?",
            avg_rating=selected_average,
            avg_color=rating_bucket_color(selected_average),
            imdb_avg_display=f"{imdb_average:.1f}" if imdb_average is not None else "?",
            imdb_avg_rating=imdb_average,
            imdb_avg_color=rating_bucket_color(imdb_average),
            trakt_avg_display=f"{trakt_average:.1f}" if trakt_average is not None else "?",
            trakt_avg_rating=trakt_average,
            trakt_avg_color=rating_bucket_color(trakt_average),
            my_avg_display=f"{my_average:.1f}" if my_average is not None else "?",
            my_avg_rating=my_average,
            my_avg_color=rating_bucket_color(my_average),
        )

    @staticmethod
    def _average(values) -> float | None:
        present_values = [float(value) for value in values if value is not None]
        return (sum(present_values) / len(present_values)) if present_values else None

    @staticmethod
    def _provider_subtitle(provider: str) -> str:
        return "Trakt episode ratings by season" if provider == "trakt" else "IMDb episode ratings by season"

    @staticmethod
    def _is_episode_released(row: dict, now_utc: datetime) -> bool:
        first_aired = row.get("first_aired")
        if first_aired is None:
            return True
        if first_aired.tzinfo is None:
            first_aired = first_aired.replace(tzinfo=UTC)
        else:
            first_aired = first_aired.astimezone(UTC)
        return first_aired <= now_utc

    @classmethod
    def _visible_trakt_rating(cls, row: dict, now_utc: datetime) -> tuple[float | None, int | None]:
        if not cls._is_episode_released(row, now_utc):
            return None, None
        raw_votes = row.get("trakt_votes")
        votes = int(raw_votes) if raw_votes is not None else None
        if votes is None or votes <= 0:
            return None, None
        raw_rating = row.get("trakt_rating")
        if raw_rating is None:
            return None, None
        return float(raw_rating), votes

    @staticmethod
    def _legend_items() -> list[EpisodeMatrixLegendItem]:
        return [
            EpisodeMatrixLegendItem(
                label=label,
                threshold_label=(f">= {threshold:.0f}" if threshold is not None else "< 5"),
                color=color,
            )
            for label, threshold, color in LEGEND_BUCKETS
        ]

    @staticmethod
    def _format_votes(votes: int | None) -> str:
        if votes is None:
            return "n/a votes"
        return f"{votes:,}".replace(",", " ") + " votes"

    @classmethod
    def _build_cell_tooltip(cls, *, title: str, season: int, episode: int, votes: int | None) -> str:
        base_title = title or "Episode"
        return f"{base_title}\nS{season:02d} E{episode:02d}\n{cls._format_votes(votes)}"
