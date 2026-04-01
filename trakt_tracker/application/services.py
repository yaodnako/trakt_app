from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Callable

from trakt_tracker.application.operations import OperationLog
from trakt_tracker.application.episode_metadata import EpisodeMetadataService
from trakt_tracker.application.history_sync import HistorySyncWorkflow
from trakt_tracker.application.notification_refresh import NotificationRefreshWorkflow
from trakt_tracker.application.progress_sync import ProgressSyncWorkflow
from trakt_tracker.application.sync_policy import SyncPolicy
from trakt_tracker.config import AppConfig, ConfigStore, get_app_data_dir
from trakt_tracker.domain import DashboardState, EpisodeSummary, HistoryItemInput, ProgressSnapshot, RatingInput, TitleSummary
from trakt_tracker.infrastructure.keyring_store import TokenStore
from trakt_tracker.infrastructure.notifications import NotificationMessage, NotificationSender
from trakt_tracker.infrastructure.cache import BinaryCache, ProviderCache
from trakt_tracker.infrastructure.imdb_dataset import IMDbDatasetClient
from trakt_tracker.infrastructure.kinopoisk import KinopoiskClient
from trakt_tracker.infrastructure.tmdb import TMDbClient
from trakt_tracker.infrastructure.url_utils import normalize_external_url
from trakt_tracker.infrastructure.trakt.client import TraktClient
from trakt_tracker.infrastructure.trakt.oauth import OAuthCallbackServer, build_authorization_url, open_authorization_url
from trakt_tracker.persistence.database import Database
from trakt_tracker.persistence.repositories import (
    EpisodeRepository,
    HistoryRepository,
    NotificationRepository,
    ProgressRepository,
    SyncStateRepository,
    TitleRepository,
    UserStateRepository,
)


@dataclass(slots=True)
class ServiceContainer:
    auth: "AuthService"
    cache: "CacheService"
    library: "LibraryService"
    play: "PlayService"
    progress: "ProgressService"
    notifications: "NotificationService"
    sync: "SyncService"
    operations: "OperationLog"



class CacheService:
    def __init__(self) -> None:
        self._providers = {
            "trakt": ProviderCache("trakt"),
            "tmdb": ProviderCache("tmdb"),
            "images": BinaryCache("images"),
        }

    def clear_provider(self, provider: str) -> None:
        cache = self._providers.get(provider)
        if cache is not None:
            cache.clear()
        if provider == "tmdb":
            images = self._providers.get("images")
            if images is not None:
                images.clear()


class AuthService:
    def __init__(
        self,
        config_store: ConfigStore,
        token_store: TokenStore,
        client_factory: Callable[[AppConfig], TraktClient],
    ) -> None:
        self._config_store = config_store
        self._token_store = token_store
        self._client_factory = client_factory
        self._config = self._config_store.load()

    @property
    def config(self) -> AppConfig:
        return self._config

    def update_config(
        self,
        client_id: str,
        client_secret: str,
        redirect_uri: str,
        tmdb_api_key: str | None = None,
        tmdb_read_access_token: str | None = None,
        kinopoisk_api_key: str | None = None,
    ) -> AppConfig:
        self._config.client_id = client_id.strip()
        self._config.client_secret = client_secret.strip()
        self._config.redirect_uri = redirect_uri.strip()
        if tmdb_api_key is not None:
            self._config.tmdb_api_key = tmdb_api_key.strip()
        if tmdb_read_access_token is not None:
            self._config.tmdb_read_access_token = tmdb_read_access_token.strip()
        if kinopoisk_api_key is not None:
            self._config.kinopoisk_api_key = kinopoisk_api_key.strip()
        self._config_store.save(self._config)
        return self._config

    def get_client(self) -> TraktClient:
        client = self._client_factory(self._config)
        if self._config.last_user_slug:
            slug = self._config.last_user_slug
            client.set_tokens(self._token_store.load(slug))
            client.set_token_refresh_callback(lambda bundle, account=slug: self._token_store.save(account, bundle))
        return client

    def is_configured(self) -> bool:
        return bool(self._config.client_id and self._config.client_secret)

    def is_authorized(self) -> bool:
        if not self._config.last_user_slug:
            return False
        return self._token_store.load(self._config.last_user_slug) is not None

    def authorize(self) -> str:
        if not self.is_configured():
            raise RuntimeError("Trakt client_id and client_secret are not configured")
        server = OAuthCallbackServer(self._config.redirect_uri)
        open_authorization_url(build_authorization_url(self._config.client_id, self._config.redirect_uri))
        result = server.wait_for_code()
        client = self._client_factory(self._config)
        tokens = client.exchange_code(result.code)
        client.set_tokens(tokens.to_bundle())
        me = client.get_me()
        slug = me.get("user", {}).get("ids", {}).get("slug") or me.get("user", {}).get("username") or "default"
        self._token_store.save(slug, tokens.to_bundle())
        self._config.last_user_slug = slug
        self._config_store.save(self._config)
        return slug

    def refresh_tokens(self) -> None:
        if not self._config.last_user_slug:
            raise RuntimeError("No Trakt user has been authorized")
        client = self.get_client()
        refreshed = client.refresh_tokens()
        self._token_store.save(self._config.last_user_slug, refreshed.to_bundle())


class LibraryService:
    def __init__(
        self,
        db: Database,
        auth_service: AuthService,
        titles: TitleRepository,
        user_states: UserStateRepository,
        history: HistoryRepository,
        sync_state: SyncStateRepository,
        episode_repo: EpisodeRepository,
        tmdb_factory: Callable[[AppConfig], TMDbClient],
        imdb_client: IMDbDatasetClient,
    ) -> None:
        self._db = db
        self._auth = auth_service
        self._titles = titles
        self._user_states = user_states
        self._history = history
        self._sync_state = sync_state
        self._episode_repo = episode_repo
        self._tmdb_factory = tmdb_factory
        self._imdb_client = imdb_client
        self._episode_metadata = EpisodeMetadataService(db, episode_repo, imdb_client)

    def search_titles(self, query: str, title_type: str | None = None) -> list[TitleSummary]:
        self._remember_search_query(query)
        client = self._auth.get_client()
        results = client.search_titles(query, title_type)
        self.save_last_search_state(query, title_type, results)
        with self._db.session() as session:
            for title in results:
                self._titles.upsert_title(session, title)
        return results

    def enrich_title_with_tmdb(self, title: TitleSummary) -> TitleSummary:
        tmdb = self._tmdb_factory(self._auth.config)
        enriched = title
        if tmdb.is_configured():
            enriched = tmdb.enrich_title(enriched)
        enriched = self._imdb_client.enrich_title(enriched)
        with self._db.session() as session:
            self._titles.upsert_title(session, enriched)
        return enriched

    def imdb_dataset_status(self) -> str:
        return self._imdb_client.last_updated_text()

    def save_last_search_state(self, query: str, title_type: str | None, results: list[TitleSummary]) -> None:
        payload = {
            "query": query,
            "title_type": title_type or "all",
            "sort_mode": self.get_search_sort_mode(),
            "results": [asdict(item) for item in results],
        }
        with self._db.session() as session:
            self._sync_state.set_value(session, "last_search_state", json.dumps(payload, ensure_ascii=False))

    def load_last_search_state(self) -> dict | None:
        with self._db.session() as session:
            raw = self._sync_state.get_value(session, "last_search_state", "")
        if not raw:
            return None
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return None
        if not isinstance(payload, dict):
            return None
        results_raw = payload.get("results", [])
        if not isinstance(results_raw, list):
            results_raw = []
        results: list[TitleSummary] = []
        for item in results_raw:
            if not isinstance(item, dict):
                continue
            try:
                title = TitleSummary(**item)
            except TypeError:
                continue
            title.poster_url = normalize_external_url(title.poster_url)
            results.append(title)
        return {
            "query": str(payload.get("query", "") or ""),
            "title_type": str(payload.get("title_type", "all") or "all"),
            "sort_mode": str(payload.get("sort_mode", "IMDb votes") or "IMDb votes"),
            "results": results,
        }

    def search_history(self) -> list[str]:
        with self._db.session() as session:
            raw = self._sync_state.get_value(session, "search_history", "[]")
        try:
            items = json.loads(raw)
        except json.JSONDecodeError:
            return []
        if not isinstance(items, list):
            return []
        return [item for item in items if isinstance(item, str) and item.strip()]

    def get_title_details(self, trakt_id: int, title_type: str) -> TitleSummary:
        client = self._auth.get_client()
        title = client.get_title_details(trakt_id, title_type)
        tmdb = self._tmdb_factory(self._auth.config)
        if tmdb.is_configured():
            title = tmdb.enrich_title(title)
        with self._db.session() as session:
            model = self._titles.upsert_title(session, title)
            self._user_states.ensure_state(session, model.id)
        return title

    def add_history_item(self, item: HistoryItemInput) -> None:
        client = self._auth.get_client()
        with self._db.session() as session:
            existing_local = self._history.find_recent_local_watch(
                session,
                title_trakt_id=item.trakt_id,
                season=item.season,
                episode=item.episode,
                watched_at=item.watched_at,
            )
            remote_item = item
            if item.title_type == "show" and item.season is not None and item.episode is not None:
                episode_row = self._episode_repo.find_episode(session, item.trakt_id, item.season, item.episode)
                if episode_row is None:
                    episodes = client.get_show_episodes(item.trakt_id)
                    self._episode_repo.replace_show_episodes(session, item.trakt_id, episodes)
                    episode_row = self._episode_repo.find_episode(session, item.trakt_id, item.season, item.episode)
                if episode_row is None or not episode_row.episode_trakt_id:
                    raise RuntimeError("Episode metadata was not found for the selected season/episode")
                remote_item = HistoryItemInput(
                    title_type=item.title_type,
                    trakt_id=episode_row.episode_trakt_id,
                    watched_at=item.watched_at,
                    season=item.season,
                    episode=item.episode,
                    title=item.title,
                )
            if existing_local is None:
                client.add_history_item(remote_item)
            title = self._titles.get_title(session, item.trakt_id)
            if title is None:
                title = self._titles.upsert_title(
                    session,
                    TitleSummary(
                        trakt_id=item.trakt_id,
                        title_type=item.title_type,
                        title=item.title or f"{item.title_type.capitalize()} {item.trakt_id}",
                    ),
                )
            state = self._user_states.ensure_state(session, title.id)
            state.in_history = True
            state.tracked = item.title_type == "show"
            state.last_watched_at = item.watched_at
            self._history.add_event(
                session,
                trakt_history_id=None,
                title_trakt_id=item.trakt_id,
                title=title.title,
                title_type=item.title_type,
                action="watched",
                watched_at=item.watched_at,
                season=item.season,
                episode=item.episode,
                source="local",
            )

    def set_rating(self, item: RatingInput, title: str = "") -> None:
        client = self._auth.get_client()
        with self._db.session() as session:
            remote_item = item
            if item.title_type == "show" and item.season is not None and item.episode is not None:
                episode_row = self._episode_repo.find_episode(session, item.trakt_id, item.season, item.episode)
                if episode_row is None:
                    episodes = client.get_show_episodes(item.trakt_id)
                    self._episode_repo.replace_show_episodes(session, item.trakt_id, episodes)
                    episode_row = self._episode_repo.find_episode(session, item.trakt_id, item.season, item.episode)
                if episode_row is None or not episode_row.episode_trakt_id:
                    raise RuntimeError("Episode metadata was not found for the selected season/episode")
                remote_item = RatingInput(
                    title_type=item.title_type,
                    trakt_id=episode_row.episode_trakt_id,
                    rating=item.rating,
                    season=item.season,
                    episode=item.episode,
                )
            client.set_rating(remote_item)
            model = self._titles.get_title(session, item.trakt_id)
            if model is None:
                model = self._titles.upsert_title(
                    session,
                    TitleSummary(
                        trakt_id=item.trakt_id,
                        title_type=item.title_type,
                        title=title or f"{item.title_type.capitalize()} {item.trakt_id}",
                    ),
                )
            state = self._user_states.ensure_state(session, model.id)
            state.rating = item.rating
            self._history.add_event(
                session,
                trakt_history_id=None,
                title_trakt_id=item.trakt_id,
                title=model.title,
                title_type=item.title_type,
                action="rated",
                watched_at=datetime.now(tz=UTC),
                season=item.season,
                episode=item.episode,
                rating=item.rating,
                source="local",
            )
            self._history.apply_rating_to_latest_watch(
                session,
                title_trakt_id=item.trakt_id,
                title_type=item.title_type,
                season=item.season,
                episode=item.episode,
                rating=item.rating,
            )

    def history(
        self,
        title_type: str | None = None,
        limit: int | None = None,
        offset: int = 0,
        title_filter: str | None = None,
    ) -> list[dict]:
        with self._db.session() as session:
            rows = self._history.list_filtered(
                session,
                title_type=title_type,
                title_filter=title_filter,
                action="watched",
            )
            rows = self._dedupe_history_rows(rows)
            if offset:
                rows = rows[offset:]
            if limit is not None:
                rows = rows[:limit]
            ratings = self._user_states.ratings_by_trakt_ids(session, [row.title_trakt_id for row in rows])
            rated_map = self._history.latest_rated_map(
                session,
                title_type=title_type,
                title_filter=title_filter,
            )
            episode_metadata = self._episode_repo.metadata_by_episode_keys(
                session,
                [
                    (row.title_trakt_id, row.season, row.episode)
                    for row in rows
                    if row.title_type == "show" and row.season is not None and row.episode is not None
                ],
            )
            cached_episode_imdb = self._episode_metadata.load_cached_episode_imdb_metadata(
                [
                    (row.title_trakt_id, row.season, row.episode)
                    for row in rows
                    if row.title_type == "show" and row.season is not None and row.episode is not None
                ]
            )
            cached_title_ratings, cached_episode_ratings = self._episode_metadata.load_cached_trakt_rating_maps()
            return [
                {
                    "title_trakt_id": row.title_trakt_id,
                    "title": row.title,
                    "type": row.title_type,
                    "action": row.action,
                    "watched_at": row.watched_at,
                    "season": row.season,
                    "episode": row.episode,
                    "episode_title": (
                        (episode_metadata.get((row.title_trakt_id, row.season, row.episode)) or {}).get("title")
                        if row.season is not None and row.episode is not None
                        else None
                    ),
                    "episode_imdb_rating": (
                        (
                            (episode_metadata.get((row.title_trakt_id, row.season, row.episode)) or {}).get("imdb_rating")
                            or (cached_episode_imdb.get((row.title_trakt_id, row.season, row.episode)) or {}).get("imdb_rating")
                        )
                        if row.season is not None and row.episode is not None
                        else None
                    ),
                    "episode_imdb_votes": (
                        (
                            (episode_metadata.get((row.title_trakt_id, row.season, row.episode)) or {}).get("imdb_votes")
                            or (cached_episode_imdb.get((row.title_trakt_id, row.season, row.episode)) or {}).get("imdb_votes")
                        )
                        if row.season is not None and row.episode is not None
                        else None
                    ),
                    "event_rating": row.rating,
                    "title_rating": ratings.get(row.title_trakt_id),
                    "display_rating": (
                        row.rating
                        or rated_map.get((row.title_trakt_id, row.season, row.episode))
                        or cached_episode_ratings.get((row.title_trakt_id, row.season, row.episode))
                        if row.title_type == "show"
                        else (
                            row.rating
                            or rated_map.get((row.title_trakt_id, None, None), ratings.get(row.title_trakt_id))
                            or cached_title_ratings.get(row.title_trakt_id)
                        )
                    ),
                }
                for row in rows
            ]

    @staticmethod
    def _dedupe_history_rows(rows: list) -> list:
        seen: set[tuple[str, int, int | None, int | None]] = set()
        deduped: list = []
        for row in rows:
            key = (row.title_type, row.title_trakt_id, row.season, row.episode)
            if key in seen:
                continue
            seen.add(key)
            deduped.append(row)
        return deduped

    def history_titles(self, title_type: str | None = None) -> list[str]:
        with self._db.session() as session:
            return self._history.distinct_titles(session, title_type=title_type, action="watched")

    def displayed_history_rating(
        self,
        *,
        title_type: str,
        trakt_id: int,
        season: int | None = None,
        episode: int | None = None,
    ) -> int | None:
        rows = self.history(title_type=title_type)
        for row in rows:
            if row["title_trakt_id"] != trakt_id:
                continue
            if row["season"] != season or row["episode"] != episode:
                continue
            return row.get("display_rating")
        return None

    def _remember_search_query(self, query: str) -> None:
        query = query.strip()
        if not query:
            return
        with self._db.session() as session:
            raw = self._sync_state.get_value(session, "search_history", "[]")
            try:
                items = json.loads(raw)
            except json.JSONDecodeError:
                items = []
            if not isinstance(items, list):
                items = []
            deduped = [item for item in items if isinstance(item, str) and item.strip() and item != query]
            deduped.insert(0, query)
            self._sync_state.set_value(session, "search_history", json.dumps(deduped[:15], ensure_ascii=False))

    def get_search_sort_mode(self) -> str:
        with self._db.session() as session:
            return self._sync_state.get_value(session, "search_sort_mode", "IMDb votes")

    def set_search_sort_mode(self, mode: str) -> None:
        with self._db.session() as session:
            self._sync_state.set_value(session, "search_sort_mode", mode)


class ProgressService:
    def __init__(
        self,
        db: Database,
        auth_service: AuthService,
        progress_repo: ProgressRepository,
        episode_repo: EpisodeRepository,
        titles: TitleRepository,
        user_states: UserStateRepository,
        sync_state: SyncStateRepository,
        tmdb_factory: Callable[[AppConfig], TMDbClient],
        imdb_client: IMDbDatasetClient,
        operations: OperationLog,
        episode_metadata: EpisodeMetadataService,
    ) -> None:
        self._workflow = ProgressSyncWorkflow(
            db,
            auth_service,
            progress_repo,
            episode_repo,
            titles,
            user_states,
            sync_state,
            tmdb_factory,
            imdb_client,
            operations,
            episode_metadata,
        )

    def refresh_show_progress(self, trakt_id: int, *, fresh: bool = False) -> ProgressSnapshot:
        return self._workflow.refresh_show_progress(trakt_id, fresh=fresh)

    def dashboard_progress(self, *, dropped_only: bool = False) -> list[ProgressSnapshot]:
        return self._workflow.dashboard_progress(dropped_only=dropped_only)

    def sync_progress(self, trakt_ids: list[int] | None = None, *, dropped_only: bool = False) -> list[ProgressSnapshot]:
        return self._workflow.sync_progress(trakt_ids, dropped_only=dropped_only)

    def drop_show(self, trakt_id: int) -> None:
        self._workflow.drop_show(trakt_id)

    def undrop_show(self, trakt_id: int) -> None:
        self._workflow.undrop_show(trakt_id)


class PlayService:
    def __init__(self, auth_service: AuthService) -> None:
        self._auth = auth_service

    def resolve_kinopoisk_url(self, title: str, domain: str = "net") -> str | None:
        normalized_title = title.strip()
        if not normalized_title:
            return None
        client = KinopoiskClient(api_key=self._auth.config.kinopoisk_api_key)
        return client.resolve_title_url(normalized_title, domain=domain)


class NotificationService:
    def __init__(
        self,
        db: Database,
        auth_service: AuthService,
        config_store: ConfigStore,
        notification_repo: NotificationRepository,
        episode_repo: EpisodeRepository,
        progress_repo: ProgressRepository,
        sender: NotificationSender,
    ) -> None:
        self._workflow = NotificationRefreshWorkflow(
            db,
            auth_service,
            config_store,
            notification_repo,
            episode_repo,
            progress_repo,
            sender,
        )

    def poll_upcoming(self, *, send_native: bool = True) -> list[dict]:
        return self._workflow.poll_upcoming(send_native=send_native)

    def mark_episode_seen(self, *, show_trakt_id: int, show_title: str, episode: EpisodeSummary) -> None:
        self._workflow.mark_episode_seen(show_trakt_id=show_trakt_id, show_title=show_title, episode=episode)

    def unseen_episode_ids(self) -> set[int]:
        return self._workflow.unseen_episode_ids()

    def upcoming_items(self) -> list[dict]:
        return self._workflow.upcoming_items()


class SyncService:
    def __init__(
        self,
        db: Database,
        auth_service: AuthService,
        titles: TitleRepository,
        user_states: UserStateRepository,
        history: HistoryRepository,
        progress: ProgressRepository,
        episode_repo: EpisodeRepository,
        sync_state: SyncStateRepository,
        operations: OperationLog,
        episode_metadata: EpisodeMetadataService,
    ) -> None:
        self._imdb_client = IMDbDatasetClient()
        self._workflow = HistorySyncWorkflow(
            db,
            auth_service,
            titles,
            user_states,
            history,
            progress,
            episode_repo,
            sync_state,
            self._imdb_client,
            operations,
            episode_metadata,
        )

    def initial_import(self) -> None:
        self._workflow.initial_import()

    def refresh_history(self) -> None:
        self._workflow.refresh_history()

    def maybe_refresh_history(self) -> bool:
        return self._workflow.maybe_refresh_history()

    def sync_updates(self) -> None:
        self._workflow.sync_updates()

    def sync_imdb_dataset(self, force: bool = False, status_callback=None) -> bool:
        changed = self._imdb_client.sync(force=force, status_callback=status_callback)
        self._backfill_episode_imdb_ids_from_payloads(
            self._load_cached_trakt_history_items() + self._load_cached_trakt_rating_items()
        )
        self._enrich_episode_imdb_ratings()
        return changed

    def clear_imdb_dataset(self) -> None:
        self._imdb_client.clear()

    def imdb_dataset_status(self) -> str:
        return self._imdb_client.last_updated_text()

    def _sync_history_and_ratings(self, history_items: list[dict], ratings: list[dict]) -> None:
        show_ids: set[int] = set()
        with self._db.session() as session:
            for item in history_items:
                imported = self._import_history_item(session, item)
                if imported is not None and imported["title_type"] == "show":
                    show_ids.add(imported["trakt_id"])
            self._history.delete_trakt_rated(session)
            for item in ratings:
                self._import_rating_item(session, item)
            self._sync_state.set_value(session, "initial_import_at", datetime.now(tz=UTC).isoformat())
        for trakt_id in show_ids:
            self.refresh_show(trakt_id)
        self._backfill_episode_imdb_ids_from_payloads(history_items + ratings)
        self._enrich_episode_imdb_ratings()

    def repair_legacy_episode_history(self) -> bool:
        return self._workflow.repair_legacy_episode_history()

    def refresh_show(self, trakt_id: int) -> ProgressSnapshot:
        return self._workflow.refresh_show(trakt_id)

    def dashboard_state(self) -> DashboardState:
        return self._workflow.dashboard_state()

    def _import_history_item(self, session, item: dict) -> dict | None:
        raw_type = item.get("type")
        season = None
        episode_number = None
        if raw_type == "episode":
            episode_payload = item.get("episode", {}) or {}
            show_payload = item.get("show", {}) or {}
            ids = show_payload.get("ids", {}) if isinstance(show_payload, dict) else {}
            trakt_id = ids.get("trakt")
            if not trakt_id:
                return None
            title_type = "show"
            title = TitleSummary(
                trakt_id=trakt_id,
                title_type="show",
                title=show_payload.get("title", ""),
                year=show_payload.get("year"),
                overview=show_payload.get("overview", ""),
                status=show_payload.get("status", ""),
                slug=ids.get("slug", ""),
            )
            season = episode_payload.get("season")
            episode_number = episode_payload.get("number")
            episode_ids = episode_payload.get("ids", {}) if isinstance(episode_payload, dict) else {}
            episode_trakt_id = episode_ids.get("trakt", 0)
            if season is not None and episode_number is not None:
                self._episode_repo.upsert_episode(
                    session,
                    trakt_id,
                    EpisodeSummary(
                        trakt_id=episode_trakt_id,
                        season=season,
                        number=episode_number,
                        title=episode_payload.get("title", ""),
                        overview=episode_payload.get("overview", ""),
                        runtime=episode_payload.get("runtime"),
                        first_aired=(
                            datetime.fromisoformat(episode_payload["first_aired"].replace("Z", "+00:00"))
                            if episode_payload.get("first_aired")
                            else None
                        ),
                    ),
                )
        else:
            payload = item.get(raw_type, {})
            ids = payload.get("ids", {})
            trakt_id = ids.get("trakt")
            if not trakt_id:
                return None
            title_type = raw_type
            title = TitleSummary(
                trakt_id=trakt_id,
                title_type=title_type,
                title=payload.get("title", ""),
                year=payload.get("year"),
                overview=payload.get("overview", ""),
                status=payload.get("status", ""),
                slug=ids.get("slug", ""),
            )
        model = self._titles.upsert_title(session, title)
        state = self._user_states.ensure_state(session, model.id)
        state.in_history = True
        state.tracked = title_type == "show"
        watched_at_raw = item.get("watched_at")
        watched_at = datetime.fromisoformat(watched_at_raw.replace("Z", "+00:00")) if watched_at_raw else datetime.now(tz=UTC)
        state.last_watched_at = watched_at
        self._history.add_event(
            session,
            trakt_history_id=item.get("id"),
            title_trakt_id=trakt_id,
            title=title.title,
            title_type=title_type,
            action="watched",
            watched_at=watched_at,
            season=season,
            episode=episode_number,
            source="trakt",
        )
        return {"trakt_id": trakt_id, "title_type": title_type}

    def _import_rating_item(self, session, item: dict) -> None:
        raw_type = item.get("type")
        rating_value = item.get("rating")
        rated_at_raw = item.get("rated_at")
        rated_at = datetime.fromisoformat(rated_at_raw.replace("Z", "+00:00")) if rated_at_raw else datetime.now(tz=UTC)
        if raw_type == "episode":
            episode_payload = item.get("episode", {}) or {}
            show_payload = item.get("show", {}) or {}
            ids = show_payload.get("ids", {}) if isinstance(show_payload, dict) else {}
            trakt_id = ids.get("trakt")
            if not trakt_id:
                return
            title = TitleSummary(
                trakt_id=trakt_id,
                title_type="show",
                title=show_payload.get("title", ""),
                year=show_payload.get("year"),
                overview=show_payload.get("overview", ""),
                status=show_payload.get("status", ""),
                slug=ids.get("slug", ""),
            )
            self._titles.upsert_title(session, title)
            season = episode_payload.get("season")
            episode_number = episode_payload.get("number")
            episode_ids = episode_payload.get("ids", {}) if isinstance(episode_payload, dict) else {}
            episode_trakt_id = episode_ids.get("trakt", 0)
            if season is not None and episode_number is not None:
                self._episode_repo.upsert_episode(
                    session,
                    trakt_id,
                    EpisodeSummary(
                        trakt_id=episode_trakt_id,
                        season=season,
                        number=episode_number,
                        title=episode_payload.get("title", ""),
                        overview=episode_payload.get("overview", ""),
                        runtime=episode_payload.get("runtime"),
                        first_aired=(
                            datetime.fromisoformat(episode_payload["first_aired"].replace("Z", "+00:00"))
                            if episode_payload.get("first_aired")
                            else None
                        ),
                    ),
                )
            self._history.add_event(
                session,
                trakt_history_id=None,
                title_trakt_id=trakt_id,
                title=title.title,
                title_type="show",
                action="rated",
                watched_at=rated_at,
                season=season,
                episode=episode_number,
                rating=rating_value,
                source="trakt",
            )
            return
        payload = item.get(raw_type, {})
        ids = payload.get("ids", {})
        trakt_id = ids.get("trakt")
        if not trakt_id:
            return
        title = TitleSummary(
            trakt_id=trakt_id,
            title_type=raw_type,
            title=payload.get("title", ""),
            year=payload.get("year"),
            overview=payload.get("overview", ""),
            status=payload.get("status", ""),
            slug=ids.get("slug", ""),
        )
        model = self._titles.upsert_title(session, title)
        state = self._user_states.ensure_state(session, model.id)
        state.rating = rating_value
        self._history.add_event(
            session,
            trakt_history_id=None,
            title_trakt_id=trakt_id,
            title=title.title,
            title_type=raw_type,
            action="rated",
            watched_at=rated_at,
            rating=rating_value,
            source="trakt",
        )

    @staticmethod
    def _load_cached_trakt_history_items() -> list[dict]:
        cache_dir = get_app_data_dir() / "cache" / "trakt"
        if not cache_dir.exists():
            return []
        merged_payload: list[dict] = []
        seen_keys: set[tuple] = set()
        for path in cache_dir.glob("*.json"):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            value = payload.get("value")
            if not isinstance(value, list) or not value:
                continue
            first = value[0]
            if not isinstance(first, dict):
                continue
            if "watched_at" not in first or "type" not in first:
                continue
            for item in value:
                if not isinstance(item, dict):
                    continue
                key = (
                    item.get("id"),
                    item.get("watched_at"),
                    item.get("type"),
                    ((item.get("show") or {}).get("ids") or {}).get("trakt"),
                    ((item.get("episode") or {}).get("ids") or {}).get("trakt"),
                    ((item.get("movie") or {}).get("ids") or {}).get("trakt"),
                )
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                merged_payload.append(item)
        return merged_payload

    @staticmethod
    def _load_cached_trakt_rating_items() -> list[dict]:
        cache_dir = get_app_data_dir() / "cache" / "trakt"
        if not cache_dir.exists():
            return []
        merged_payload: list[dict] = []
        seen_keys: set[tuple] = set()
        for path in cache_dir.glob("*.json"):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            value = payload.get("value")
            if not isinstance(value, list) or not value:
                continue
            first = value[0]
            if not isinstance(first, dict):
                continue
            if "rated_at" not in first or "type" not in first or "rating" not in first:
                continue
            for item in value:
                if not isinstance(item, dict):
                    continue
                key = (
                    item.get("rated_at"),
                    item.get("type"),
                    item.get("rating"),
                    ((item.get("show") or {}).get("ids") or {}).get("trakt"),
                    ((item.get("episode") or {}).get("ids") or {}).get("trakt"),
                    ((item.get("movie") or {}).get("ids") or {}).get("trakt"),
                )
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                merged_payload.append(item)
        return merged_payload

    @staticmethod
    def _fetch_all_watch_history(client: TraktClient, page_size: int = 100) -> list[dict]:
        items: list[dict] = []
        page = 1
        while True:
            batch = client.get_watch_history(limit=page_size, page=page)
            if not batch:
                break
            items.extend(batch)
            if len(batch) < page_size:
                break
            page += 1
        return items

    @staticmethod
    def _fetch_all_ratings(client: TraktClient, page_size: int = 100) -> list[dict]:
        items: list[dict] = []
        seen_keys: set[tuple] = set()
        for title_type in ("episode", "show", "movie"):
            page = 1
            while True:
                batch = client.get_ratings(title_type=title_type, limit=page_size, page=page)
                if not batch:
                    break
                for item in batch:
                    if not isinstance(item, dict):
                        continue
                    key = (
                        item.get("rated_at"),
                        item.get("type"),
                        item.get("rating"),
                        ((item.get("show") or {}).get("ids") or {}).get("trakt"),
                        ((item.get("episode") or {}).get("ids") or {}).get("trakt"),
                        ((item.get("movie") or {}).get("ids") or {}).get("trakt"),
                    )
                    if key in seen_keys:
                        continue
                    seen_keys.add(key)
                    items.append(item)
                if len(batch) < page_size:
                    break
                page += 1
        return items

    def _fetch_recent_history_updates(self, client: TraktClient, page_size: int = 100) -> list[dict]:
        with self._db.session() as session:
            known_ids = self._history.known_trakt_history_ids(session)
        if not known_ids:
            return self._fetch_all_watch_history(client, page_size=page_size)
        items: list[dict] = []
        page = 1
        while True:
            batch = client.get_watch_history(limit=page_size, page=page)
            if not batch:
                break
            unseen = [item for item in batch if item.get("id") not in known_ids]
            if unseen:
                items.extend(unseen)
            if not unseen or len(batch) < page_size:
                break
            page += 1
        return items

    def _current_history_activity_signature(self) -> str:
        client = self._auth.get_client()
        payload = client.get_last_activities(use_cache=False)
        return self._policy.build_history_activity_signature(payload)

    def _enrich_episode_imdb_ratings(self) -> None:
        if not self._imdb_client.is_ready():
            return
        with self._db.session() as session:
            rows = self._episode_repo.list_all_with_imdb(session)
            for row in rows:
                if not row.imdb_id:
                    continue
                episode = EpisodeSummary(
                    trakt_id=row.episode_trakt_id,
                    season=row.season,
                    number=row.number,
                    title=row.title,
                    imdb_id=row.imdb_id,
                    imdb_rating=row.imdb_rating,
                    imdb_votes=row.imdb_votes,
                    first_aired=row.first_aired,
                    runtime=row.runtime,
                    overview=row.overview,
                )
                enriched = self._imdb_client.enrich_episode(episode)
                row.imdb_rating = enriched.imdb_rating
                row.imdb_votes = enriched.imdb_votes

    def _backfill_episode_imdb_ids_from_cached_payloads(self) -> None:
        payloads = []
        payloads.extend(self._load_cached_trakt_history_items())
        payloads.extend(self._load_cached_trakt_rating_items())
        self._backfill_episode_imdb_ids_from_payloads(payloads)

    def _backfill_episode_imdb_ids_from_payloads(self, payloads: list[dict]) -> None:
        if not payloads:
            return
        with self._db.session() as session:
            for item in payloads:
                if item.get("type") != "episode":
                    continue
                show_payload = item.get("show", {}) or {}
                episode_payload = item.get("episode", {}) or {}
                show_ids = show_payload.get("ids", {}) if isinstance(show_payload, dict) else {}
                episode_ids = episode_payload.get("ids", {}) if isinstance(episode_payload, dict) else {}
                show_trakt_id = show_ids.get("trakt")
                season = episode_payload.get("season")
                number = episode_payload.get("number")
                if not show_trakt_id or season is None or number is None:
                    continue
                row = self._episode_repo.find_episode(session, show_trakt_id, season, number)
                if row is None:
                    continue
                imdb_id = str(episode_ids.get("imdb", "") or "")
                if not imdb_id:
                    show_imdb_id = str(show_ids.get("imdb", "") or "")
                    if show_imdb_id:
                        imdb_id = self._imdb_client.lookup_episode_imdb_id(show_imdb_id, int(season), int(number))
                        if not imdb_id:
                            imdb_id = self._imdb_client.lookup_episode_imdb_id_by_title(
                                show_imdb_id,
                                str(episode_payload.get("title", "") or ""),
                            )
                if imdb_id and not row.imdb_id:
                    row.imdb_id = imdb_id


def build_services(config_store: ConfigStore, db: Database) -> ServiceContainer:
    tokens = TokenStore()
    titles = TitleRepository()
    user_states = UserStateRepository()
    history = HistoryRepository()
    progress = ProgressRepository()
    episode_repo = EpisodeRepository()
    sync_state = SyncStateRepository()
    notification_repo = NotificationRepository()
    operations = OperationLog()

    def client_factory(config: AppConfig) -> TraktClient:
        client = TraktClient(
            config.client_id,
            config.client_secret,
            config.redirect_uri,
            cache_ttl_hours=config.cache_ttl_hours,
            cache_namespace=config.last_user_slug or "default",
        )
        if config.last_user_slug:
            client.set_tokens(tokens.load(config.last_user_slug))
        return client

    def tmdb_factory(config: AppConfig) -> TMDbClient:
        return TMDbClient(
            api_key=config.tmdb_api_key,
            read_access_token=config.tmdb_read_access_token,
            cache_ttl_hours=config.cache_ttl_hours,
        )

    auth = AuthService(config_store, tokens, client_factory)
    cache = CacheService()
    imdb_client = IMDbDatasetClient(cache_ttl_hours=config_store.load().cache_ttl_hours)
    episode_metadata = EpisodeMetadataService(db, episode_repo, imdb_client)
    library = LibraryService(db, auth, titles, user_states, history, sync_state, episode_repo, tmdb_factory, imdb_client)
    play = PlayService(auth)
    progress_service = ProgressService(db, auth, progress, episode_repo, titles, user_states, sync_state, tmdb_factory, imdb_client, operations, episode_metadata)
    notifications = NotificationService(
        db,
        auth,
        config_store,
        notification_repo,
        episode_repo,
        progress,
        NotificationSender(),
    )
    sync = SyncService(db, auth, titles, user_states, history, progress, episode_repo, sync_state, operations, episode_metadata)
    return ServiceContainer(
        auth=auth,
        cache=cache,
        library=library,
        play=play,
        progress=progress_service,
        notifications=notifications,
        sync=sync,
        operations=operations,
    )
