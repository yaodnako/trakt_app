from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Callable

from trakt_tracker.application.catalog import CatalogService
from trakt_tracker.application.operations import OperationLog
from trakt_tracker.application.episode_metadata import EpisodeMetadataService
from trakt_tracker.application.history_sync import HistorySyncWorkflow
from trakt_tracker.application.history_read_model import HistoryReadModelService
from trakt_tracker.application.notification_refresh import NotificationRefreshWorkflow
from trakt_tracker.application.progress_sync import ProgressSyncWorkflow
from trakt_tracker.application.trakt_payload_cache import (
    load_cached_trakt_history_items,
    load_cached_trakt_rating_items,
)
from trakt_tracker.config import AppConfig, ConfigStore
from trakt_tracker.domain import DashboardState, EpisodeSummary, HistoryItemInput, ProgressSnapshot, RatingInput, TitleSummary
from trakt_tracker.infrastructure.keyring_store import TokenStore
from trakt_tracker.infrastructure.notifications import NotificationSender
from trakt_tracker.infrastructure.cache import BinaryCache, ProviderCache
from trakt_tracker.infrastructure.imdb_dataset import IMDbDatasetClient
from trakt_tracker.infrastructure.kinopoisk import KinopoiskClient
from trakt_tracker.infrastructure.tmdb import TMDbClient
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
    catalog: "CatalogService"
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
        episode_repo: EpisodeRepository,
        history_read_model: HistoryReadModelService,
    ) -> None:
        self._db = db
        self._auth = auth_service
        self._titles = titles
        self._user_states = user_states
        self._history = history
        self._episode_repo = episode_repo
        self._history_read_model = history_read_model

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
        return self._history_read_model.history(
            title_type=title_type,
            limit=limit,
            offset=offset,
            title_filter=title_filter,
        )

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
        self._episode_metadata = episode_metadata
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
        self._episode_metadata.backfill_episode_imdb_ids_from_payloads(
            load_cached_trakt_history_items() + load_cached_trakt_rating_items()
        )
        self._episode_metadata.enrich_episode_imdb_ratings()
        return changed

    def clear_imdb_dataset(self) -> None:
        self._imdb_client.clear()

    def imdb_dataset_status(self) -> str:
        return self._imdb_client.last_updated_text()

    def repair_legacy_episode_history(self) -> bool:
        return self._workflow.repair_legacy_episode_history()

    def refresh_show(self, trakt_id: int) -> ProgressSnapshot:
        return self._workflow.refresh_show(trakt_id)

    def dashboard_state(self) -> DashboardState:
        return self._workflow.dashboard_state()


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
    history_read_model = HistoryReadModelService(db, history, user_states, episode_repo, episode_metadata)
    catalog = CatalogService(db, auth, titles, user_states, sync_state, tmdb_factory, imdb_client)
    library = LibraryService(db, auth, titles, user_states, history, episode_repo, history_read_model)
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
        catalog=catalog,
        library=library,
        play=play,
        progress=progress_service,
        notifications=notifications,
        sync=sync,
        operations=operations,
    )
