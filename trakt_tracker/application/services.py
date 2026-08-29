from __future__ import annotations

import logging
from collections import deque
from concurrent.futures import Future
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from secrets import token_urlsafe
from threading import Lock, RLock
from time import perf_counter
from typing import Any, Callable, cast

from trakt_tracker.application.catalog import CatalogService
from trakt_tracker.application.episode_ratings_matrix import EpisodeRatingsMatrixService
from trakt_tracker.application.episode_imdb_reconciliation import EpisodeIMDbReconciliationService
from trakt_tracker.application.enrich_queue import (
    EnrichQueueService,
    build_history_episode_task,
)
from trakt_tracker.application.history import HistoryService
from trakt_tracker.application.interactions import InteractionService
from trakt_tracker.application.operations import OperationLog
from trakt_tracker.application.episode_metadata import EpisodeMetadataService
from trakt_tracker.application.history_sync import HistorySyncWorkflow
from trakt_tracker.application.metadata_refresh_policy import (
    ASSET_KIND_EPISODE_RATINGS,
    ASSET_KIND_POSTER,
    ASSET_KIND_STILL,
    TRIGGER_MANUAL_REPAIR,
    TRIGGER_BACKGROUND_SWEEP,
    metadata_refresh_due,
)
from trakt_tracker.application.history_read_model import HistoryReadModelService
from trakt_tracker.application.notification_refresh import NotificationRefreshWorkflow
from trakt_tracker.application.progress_sync import ProgressSyncWorkflow
from trakt_tracker.application.release_tracking import ReleaseTrackingService
from trakt_tracker.application.search_watch import SearchWatchService
from trakt_tracker.application.title_aliases import TitleAliasService
from trakt_tracker.application.trakt_outbox import TraktOutboxService
from trakt_tracker.application.tmdb_catalog import TmdbCatalogService
from trakt_tracker.application.trakt_payload_cache import (
    load_cached_trakt_history_items,
    load_cached_trakt_rating_items,
)
from trakt_tracker.application.sync_policy import SyncPolicy
from trakt_tracker.config import (
    AppConfig,
    ConfigStore,
    normalize_kinopoisk_domain_options,
    normalize_kinopoisk_domain_tail,
    normalize_catalog_provider_mode,
    normalize_tmdb_proxy_url,
    resolved_tmdb_api_key,
    resolved_tmdb_read_access_token,
    resolved_trakt_client_id,
    resolved_trakt_client_secret,
    trakt_cache_provider,
)
from trakt_tracker.domain import (
    DashboardState,
    EpisodeSummary,
    ProgressSnapshot,
    ProgressSortMode,
    ProgressView,
)
from trakt_tracker.infrastructure.keyring_store import TokenBundle, TokenStore
from trakt_tracker.infrastructure.notifications import NotificationMessage, NotificationSender
from trakt_tracker.infrastructure.cache import BinaryCache, ProviderCache
from trakt_tracker.infrastructure.artwork_cache import has_cached_image, is_trusted_image_url, warm_image_urls
from trakt_tracker.infrastructure.artwork_queue import ArtworkQueue
from trakt_tracker.infrastructure.imdb_dataset import IMDbDatasetClient
from trakt_tracker.infrastructure.kinopoisk import KinopoiskClient
from trakt_tracker.infrastructure.tmdb import TMDbClient
from trakt_tracker.infrastructure.trakt.client import TraktClient
from trakt_tracker.infrastructure.trakt.oauth import (
    OAuthCallbackServer,
    OAuthCallbackUnavailable,
    build_authorization_url,
    open_authorization_url,
)
from trakt_tracker.persistence.database import Database
from trakt_tracker.persistence.repositories import (
    EpisodeRepository,
    HistoryRepository,
    NotificationRepository,
    ProgressRepository,
    ReleaseTrackingRepository,
    SyncStateRepository,
    TitleAliasRepository,
    TitleRepository,
    UserStateRepository,
)
from trakt_tracker.persistence.trakt_outbox import TraktOutboxRepository
from trakt_tracker.persistence.tmdb_preview import TmdbPreviewRepository


@dataclass(slots=True)
class ServiceContainer:
    database: Database
    auth: "AuthService"
    cache: "CacheService"
    catalog: "CatalogService"
    tmdb_catalog: TmdbCatalogService
    episode_ratings_matrix: "EpisodeRatingsMatrixService"
    enrich_queue: "EnrichQueueService"
    image_queue: ArtworkQueue
    history: "HistoryService"
    interactions: "InteractionService"
    play: "PlayService"
    progress: "ProgressService"
    release_tracking: "ReleaseTrackingService"
    search_watch: "SearchWatchService"
    title_aliases: "TitleAliasService"
    notifications: "NotificationService"
    sync: "SyncService"
    trakt_sync: "TraktOutboxService"
    operations: "OperationLog"
    closers: tuple[Callable[[], None], ...] = ()

    def close(self) -> None:
        """Stop profile-scoped workers and release their HTTP resources once."""
        self.enrich_queue.close()
        self.image_queue.close()
        for closer in self.closers:
            closer()



class CacheService:
    def __init__(self, profile_slug: str = "") -> None:
        self._providers = {
            "trakt": ProviderCache(trakt_cache_provider(profile_slug)),
            "tmdb": ProviderCache("tmdb"),
            "tmdb_catalog": ProviderCache("tmdb/catalog"),
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
        self._authorize_lock = Lock()
        self._authorization_attempt: Future[str] | None = None
        self._client_lock = RLock()
        self._client: TraktClient | None = None
        self._retired_clients: list[TraktClient] = []
        self._reauthorization_required_slugs: set[str] = set()

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
        kinopoisk_domain_tail: str | None = None,
        kinopoisk_domain_options: str | None = None,
        network_proxy_url: str | None = None,
    ) -> AppConfig:
        self._config.client_id = client_id.strip()
        if client_secret.strip():
            self._config.client_secret = client_secret.strip()
        self._config.redirect_uri = redirect_uri.strip()
        if tmdb_api_key is not None and tmdb_api_key.strip():
            self._config.tmdb_api_key = tmdb_api_key.strip()
        if tmdb_read_access_token is not None and tmdb_read_access_token.strip():
            self._config.tmdb_read_access_token = tmdb_read_access_token.strip()
        if kinopoisk_api_key is not None and kinopoisk_api_key.strip():
            self._config.kinopoisk_api_key = kinopoisk_api_key.strip()
        if kinopoisk_domain_options is not None:
            options = normalize_kinopoisk_domain_options(kinopoisk_domain_options)
            self._config.kinopoisk_domain_options = ",".join(options)
        if kinopoisk_domain_tail is not None:
            self._config.kinopoisk_domain_tail = normalize_kinopoisk_domain_tail(kinopoisk_domain_tail)
        if network_proxy_url is not None:
            self._config.network_proxy_url = normalize_tmdb_proxy_url(network_proxy_url)
        options = normalize_kinopoisk_domain_options(self._config.kinopoisk_domain_options)
        selected = normalize_kinopoisk_domain_tail(self._config.kinopoisk_domain_tail)
        if selected not in options:
            selected = options[0]
        self._config.kinopoisk_domain_options = ",".join(options)
        self._config.kinopoisk_domain_tail = selected
        self._config_store.save(self._config)
        self._invalidate_client()
        return self._config

    def get_client(self) -> TraktClient:
        if normalize_catalog_provider_mode(
            getattr(self._config, "catalog_provider_mode", "trakt")
        ) != "trakt":
            raise RuntimeError("Trakt client is disabled in TMDb mode.")
        with self._client_lock:
            if self._client is None:
                client = self._client_factory(self._config)
                if self._config.active_slug:
                    slug = self._config.active_slug
                    client.set_tokens(self._token_store.load(slug))
                    client.set_token_refresh_callback(
                        lambda bundle, account=slug, source=client: self._persist_refreshed_token(
                            account,
                            source,
                            bundle,
                        )
                    )
                    client.set_reauthorization_callback(
                        lambda account=slug, source=client: self._require_reauthorization(account, source)
                    )
                self._client = client
            return self._client

    def _persist_refreshed_token(self, slug: str, source: TraktClient, bundle: TokenBundle) -> None:
        with self._client_lock:
            if source is not self._client:
                return
            self._token_store.save(slug, bundle)

    def _require_reauthorization(self, slug: str, source: TraktClient) -> None:
        with self._client_lock:
            if source is not self._client:
                return
            self._reauthorization_required_slugs.add(slug)
            self._token_store.delete(slug)

    def close(self) -> None:
        with self._client_lock:
            clients = [self._client, *self._retired_clients]
            self._client = None
            self._retired_clients = []
        for client in clients:
            if client is not None:
                client.close()

    def _invalidate_client(self) -> None:
        """Keep an in-flight client usable; close retired pools at container shutdown."""
        with self._client_lock:
            if self._client is not None:
                self._retired_clients.append(self._client)
                self._client = None

    def is_configured(self) -> bool:
        return bool(resolved_trakt_client_id(self._config) and resolved_trakt_client_secret(self._config))

    def is_authorized(self) -> bool:
        if not self._config.active_slug:
            return False
        if self._config.active_slug in self._reauthorization_required_slugs:
            return False
        return self._token_store.load(self._config.active_slug) is not None

    def reauthorization_required(self) -> bool:
        return bool(
            self._config.active_slug
            and self._config.active_slug in self._reauthorization_required_slugs
        )

    def authorization_running(self) -> bool:
        with self._authorize_lock:
            return self._authorization_attempt is not None

    def authorize(self) -> str:
        with self._authorize_lock:
            attempt = self._authorization_attempt
            owns_attempt = attempt is None
            if attempt is None:
                attempt = Future[str]()
                self._authorization_attempt = attempt
        if not owns_attempt:
            return attempt.result()
        try:
            slug = self._authorize_once()
        except BaseException as exc:
            attempt.set_exception(exc)
            raise
        else:
            attempt.set_result(slug)
            return slug
        finally:
            with self._authorize_lock:
                if self._authorization_attempt is attempt:
                    self._authorization_attempt = None

    def _authorize_once(self) -> str:
        if not self.is_configured():
            raise RuntimeError("Trakt client_id and client_secret are not configured")
        state = token_urlsafe(32)
        server = OAuthCallbackServer(self._config.redirect_uri, expected_state=state)
        try:
            server.start()
        except OAuthCallbackUnavailable as exc:
            logging.getLogger(__name__).warning(
                "%s; using Trakt device authorization instead",
                exc,
            )
            client = self.get_client()
            authorization = client.start_device_authorization()
            open_authorization_url(authorization.activation_url)
            tokens = client.wait_for_device_authorization(authorization)
            return self._complete_authorization(client, tokens)
        try:
            open_authorization_url(
                build_authorization_url(resolved_trakt_client_id(self._config), self._config.redirect_uri, state=state)
            )
            result = server.wait_for_code()
        except Exception:
            server.close()
            raise
        client = self.get_client()
        tokens = client.exchange_code(result.code)
        return self._complete_authorization(client, tokens)

    def _complete_authorization(self, client: TraktClient, tokens) -> str:
        client.set_tokens(tokens.to_bundle())
        me = client.get_me()
        slug = me.get("user", {}).get("ids", {}).get("slug") or me.get("user", {}).get("username") or "default"
        self._token_store.save(slug, tokens.to_bundle())
        self._reauthorization_required_slugs.discard(slug)
        latest = self._config_store.load()
        if slug not in latest.known_profile_slugs:
            latest.known_profile_slugs.append(slug)
        self._config_store.save(latest)
        self._config = latest
        return slug

    def has_token(self, slug: str) -> bool:
        return self._token_store.load(str(slug or "").strip()) is not None

    def disconnect(self, slug: str) -> None:
        normalized = str(slug or "").strip()
        if normalized:
            self._token_store.delete(normalized)
            self._reauthorization_required_slugs.discard(normalized)
            if normalized == self._config.active_slug:
                self._invalidate_client()

    def clear_provider_overrides(self, provider: str) -> AppConfig:
        normalized = str(provider or "").strip().casefold()
        if normalized == "trakt":
            self._config.client_id = ""
            self._config.client_secret = ""
        elif normalized == "tmdb":
            self._config.tmdb_api_key = ""
            self._config.tmdb_read_access_token = ""
        else:
            raise ValueError("Unknown provider")
        self._config_store.save(self._config)
        self._invalidate_client()
        return self._config

    def refresh_tokens(self) -> None:
        if not self._config.active_slug:
            raise RuntimeError("No Trakt user has been authorized")
        client = self.get_client()
        client.refresh_access_token()

class ProgressService:
    def __init__(
        self,
        db: Database,
        auth_service: AuthService,
        history_repo: HistoryRepository,
        progress_repo: ProgressRepository,
        episode_repo: EpisodeRepository,
        titles: TitleRepository,
        user_states: UserStateRepository,
        sync_state: SyncStateRepository,
        tmdb_factory: Callable[[AppConfig], TMDbClient],
        imdb_client: IMDbDatasetClient,
        operations: OperationLog,
        episode_metadata: EpisodeMetadataService,
        catalog: CatalogService | None = None,
        enrich_queue: EnrichQueueService | None = None,
        notification_repo: NotificationRepository | None = None,
        trakt_outbox=None,
        tmdb_catalog: TmdbCatalogService | None = None,
    ) -> None:
        self._auth = auth_service
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
            history_repo=history_repo,
            catalog=catalog,
            notification_repo=notification_repo,
            trakt_outbox=trakt_outbox,
            episode_schedule_overlay=getattr(
                tmdb_catalog,
                "overlay_episode_row_air_dates",
                None,
            ),
        )
        self._tmdb_catalog = tmdb_catalog

    def refresh_show_progress(
        self,
        trakt_id: int,
        *,
        fresh: bool = False,
        enrich_assets: bool = True,
    ) -> ProgressSnapshot:
        auth_config = getattr(getattr(self, "_auth", None), "config", None)
        if normalize_catalog_provider_mode(
            getattr(auth_config, "catalog_provider_mode", "trakt")
        ) == "tmdb_preview":
            raise RuntimeError("Trakt progress refresh is disabled in TMDb mode.")
        result = self._workflow.refresh_show_progress(
            trakt_id,
            fresh=fresh,
            enrich_assets=enrich_assets,
        )
        if self._tmdb_catalog is not None:
            self._tmdb_catalog.overlay_episode_air_dates([result])
        return result

    def dashboard_progress(
        self,
        *,
        view: ProgressView | str = ProgressView.ACTIVE,
        sort_mode: ProgressSortMode | str = ProgressSortMode.EPISODE_RELEASE,
        descending: bool = True,
        dropped_only: bool | None = None,
        limit: int | None = 50,
    ) -> list[ProgressSnapshot]:
        auth_config = getattr(getattr(self, "_auth", None), "config", None)
        tmdb_local_mode = normalize_catalog_provider_mode(
            getattr(auth_config, "catalog_provider_mode", "trakt")
        ) == "tmdb_preview"
        if tmdb_local_mode:
            if self._tmdb_catalog is None:
                return []
            return self._tmdb_catalog.local_progress_items(
                view=view,
                sort_mode=sort_mode,
                descending=descending,
                limit=limit,
            )
        tmdb_episode_sort = bool(
            self._tmdb_catalog is not None
            and self._tmdb_catalog.episode_schedule_enabled()
            and ProgressRepository.normalize_sort_mode(sort_mode) is ProgressSortMode.EPISODE_RELEASE
        )
        result = self._workflow.dashboard_progress(
            view=view,
            sort_mode=sort_mode,
            descending=descending,
            dropped_only=dropped_only,
            limit=None if tmdb_episode_sort else limit,
        )
        if self._tmdb_catalog is not None:
            self._tmdb_catalog.overlay_episode_air_dates(result)
        if tmdb_episode_sort:
            dated = [item for item in result if item.next_episode and item.next_episode.first_aired]
            undated = [item for item in result if not item.next_episode or not item.next_episode.first_aired]

            def episode_air_date(item: ProgressSnapshot) -> datetime:
                assert item.next_episode is not None
                assert item.next_episode.first_aired is not None
                value = item.next_episode.first_aired
                return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)

            dated.sort(
                key=episode_air_date,
                reverse=bool(descending),
            )
            undated.sort(key=lambda item: (item.title.casefold(), int(item.trakt_id)))
            result = [*dated, *undated]
            if limit is not None:
                result = result[: max(0, int(limit))]
        return result

    def select_title_enrich_keys(
        self,
        items: list[ProgressSnapshot],
        *,
        trigger: str = "viewport",
        requested_parts=(),
        refresh_requests=None,
    ) -> list[tuple[int, str]]:
        return self._workflow.select_title_enrich_keys(
            items,
            trigger=trigger,
            requested_parts=requested_parts,
            refresh_requests=refresh_requests,
        )

    def select_episode_enrich_keys(
        self,
        items: list[ProgressSnapshot],
        *,
        trigger: str = "viewport",
        requested_parts=(),
        refresh_requests=None,
    ) -> list[tuple[int, int, int]]:
        return self._workflow.select_episode_enrich_keys(
            items,
            trigger=trigger,
            requested_parts=requested_parts,
            refresh_requests=refresh_requests,
        )

    def sync_progress(
        self,
        trakt_ids: list[int] | None = None,
        *,
        view: ProgressView | str = ProgressView.ACTIVE,
        sort_mode: ProgressSortMode | str = ProgressSortMode.EPISODE_RELEASE,
        descending: bool = True,
        dropped_only: bool | None = None,
        force_refresh: bool = False,
        force_full_assets: bool = False,
        defer_assets: bool = False,
    ) -> list[ProgressSnapshot]:
        auth_config = getattr(getattr(self, "_auth", None), "config", None)
        if normalize_catalog_provider_mode(
            getattr(auth_config, "catalog_provider_mode", "trakt")
        ) == "tmdb_preview":
            if self._tmdb_catalog is None:
                return []
            return self._tmdb_catalog.local_progress_items(
                view=view,
                sort_mode=sort_mode,
                descending=descending,
                limit=None,
            )
        result = self._workflow.sync_progress(
            trakt_ids,
            view=view,
            sort_mode=sort_mode,
            descending=descending,
            dropped_only=dropped_only,
            force_refresh=force_refresh,
            force_full_assets=force_full_assets,
            defer_assets=defer_assets,
        )
        if self._tmdb_catalog is not None:
            self._tmdb_catalog.overlay_episode_air_dates(result)
        return result

    def pause_show(self, trakt_id: int, *, progress: ProgressSnapshot | None = None) -> None:
        if normalize_catalog_provider_mode(
            getattr(getattr(getattr(self, "_auth", None), "config", None), "catalog_provider_mode", "trakt")
        ) == "tmdb_preview":
            return
        self._workflow.pause_show(trakt_id, progress=progress)

    def resume_show(self, trakt_id: int) -> None:
        if normalize_catalog_provider_mode(
            getattr(getattr(getattr(self, "_auth", None), "config", None), "catalog_provider_mode", "trakt")
        ) == "tmdb_preview":
            return
        self._workflow.resume_show(trakt_id)

    def drop_show(self, trakt_id: int) -> None:
        if normalize_catalog_provider_mode(
            getattr(getattr(getattr(self, "_auth", None), "config", None), "catalog_provider_mode", "trakt")
        ) == "tmdb_preview":
            return
        self._workflow.drop_show(trakt_id)

    def undrop_show(self, trakt_id: int) -> None:
        if normalize_catalog_provider_mode(
            getattr(getattr(getattr(self, "_auth", None), "config", None), "catalog_provider_mode", "trakt")
        ) == "tmdb_preview":
            return
        self._workflow.undrop_show(trakt_id)


class PlayService:
    def __init__(self, auth_service: AuthService) -> None:
        self._auth = auth_service

    def resolve_kinopoisk_url(self, title: str, domain: str | None = None) -> str | None:
        normalized_title = title.strip()
        if not normalized_title:
            return None
        resolved_domain = (domain or self._auth.config.kinopoisk_domain_tail or "net").strip()
        client = KinopoiskClient(api_key=self._auth.config.kinopoisk_api_key)
        return client.resolve_title_url(normalized_title, domain=resolved_domain)


class NotificationService:
    _ACTIVITY_SOURCES = frozenset({"progress", "release"})

    def __init__(
        self,
        db: Database,
        auth_service: AuthService,
        config_store: ConfigStore,
        notification_repo: NotificationRepository,
        episode_repo: EpisodeRepository,
        progress_repo: ProgressRepository,
        sender: NotificationSender,
        release_tracking: ReleaseTrackingService | None = None,
        progress_service: ProgressService | None = None,
        tmdb_catalog: TmdbCatalogService | None = None,
    ) -> None:
        self._db = db
        self._auth = auth_service
        self._config_store = config_store
        self._notification_repo = notification_repo
        self._sender = sender
        self._workflow = NotificationRefreshWorkflow(
            db,
            auth_service,
            config_store,
            notification_repo,
            episode_repo,
            progress_repo,
            sender,
            episode_schedule_overlay=getattr(tmdb_catalog, "overlay_episode_air_dates", None),
        )
        self._release_tracking = release_tracking
        self._progress_service = progress_service
        self._tmdb_catalog = tmdb_catalog
        self._activity_lock = Lock()
        self._activity_seq = 0
        self._activity_events: deque[dict] = deque(maxlen=32)
        self._pending_sources: set[str] = set()

    def _tmdb_preview_mode(self) -> bool:
        return normalize_catalog_provider_mode(
            getattr(getattr(self._auth, "config", None), "catalog_provider_mode", "trakt")
        ) == "tmdb_preview"

    def _tmdb_progress_items(self, view: str) -> list[ProgressSnapshot]:
        if self._tmdb_catalog is None:
            return []
        try:
            return list(
                self._tmdb_catalog.local_progress_items(
                    view=view,
                    sort_mode="episode_release",
                    descending=True,
                    limit=None,
                )
                or []
            )
        except Exception:
            return []

    def _notification_config(self):
        reader = getattr(self._config_store, "load", None)
        if callable(reader):
            try:
                return reader()
            except Exception:
                pass
        return getattr(self._auth, "config", None) or type("NotificationConfig", (), {})()

    @staticmethod
    def _tmdb_notification_show_id(show_trakt_id: int, episode: EpisodeSummary | None = None) -> int:
        supplied = int(show_trakt_id or 0)
        if supplied < 0:
            return supplied
        episode_id = int(getattr(episode, "trakt_id", 0) or 0) if episode is not None else 0
        if episode_id < 0:
            tmdb_id = abs(episode_id) // 1_000_000
            if tmdb_id > 0:
                return -tmdb_id
        return -supplied if supplied > 0 else 0

    def _tmdb_unseen_episode_ids(self) -> set[int]:
        raw_session_factory = getattr(self._db, "session", None)
        if not callable(raw_session_factory):
            return set()
        session_factory = cast(Callable[[], Any], raw_session_factory)
        with session_factory() as session:
            return {
                int(value)
                for value in self._notification_repo.unseen_episode_ids(session)
                if int(value) < 0
            }

    def _poll_tmdb_upcoming(self, *, send_native: bool) -> list[dict]:
        active_items = self._tmdb_progress_items("active")
        paused_items = self._tmdb_progress_items("paused")
        paused_show_ids = {
            self._tmdb_notification_show_id(int(getattr(item, "tmdb_id", 0) or 0), item.next_episode)
            for item in paused_items
            if getattr(item, "next_episode", None) is not None
        }
        progress_items = [*active_items, *paused_items]
        now = datetime.now(tz=UTC)
        config = self._notification_config()
        repeat_interval = timedelta(minutes=max(1, int(getattr(config, "notification_repeat_minutes", 5) or 5)))
        release_delay = timedelta(
            minutes=max(0, int(getattr(config, "notification_release_delay_minutes", 120) or 0))
        )
        sent: list[dict] = []
        raw_session_factory = getattr(self._db, "session", None)
        if not callable(raw_session_factory):
            return sent
        session_factory = cast(Callable[[], Any], raw_session_factory)
        with session_factory() as session:
            for item in progress_items:
                episode = getattr(item, "next_episode", None)
                tmdb_id = int(getattr(item, "tmdb_id", 0) or 0)
                episode_id = int(getattr(episode, "trakt_id", 0) or 0)
                if episode is None or tmdb_id <= 0 or episode_id >= 0:
                    continue
                first_aired = getattr(episode, "first_aired", None)
                if first_aired is None:
                    continue
                release_at = first_aired.replace(tzinfo=UTC) if first_aired.tzinfo is None else first_aired.astimezone(UTC)
                show_id = -tmdb_id
                if release_at > now:
                    continue
                message = f"S{int(episode.season):02d}E{int(episode.number):02d} {episode.title}"
                row = self._notification_repo.get_log(session, show_id, episode_id)
                if row is None:
                    self._notification_repo.track_released(
                        session,
                        show_trakt_id=show_id,
                        show_title=str(getattr(item, "title", "") or ""),
                        episode_trakt_id=episode_id,
                        season=int(episode.season),
                        episode=int(episode.number),
                        message=message,
                        released_at=release_at,
                    )
                    row = self._notification_repo.get_log(session, show_id, episode_id)
                if show_id in paused_show_ids:
                    self._notification_repo.mark_seen(
                        session,
                        show_trakt_id=show_id,
                        show_title=str(getattr(item, "title", "") or ""),
                        episode_trakt_id=episode_id,
                        season=int(episode.season),
                        episode=int(episode.number),
                        message=message,
                    )
                    continue
                if now < release_at + release_delay or row is None or row.seen_at is not None:
                    continue
                last_sent_at = row.last_sent_at
                if last_sent_at is not None:
                    last_sent_at = last_sent_at.replace(tzinfo=UTC) if last_sent_at.tzinfo is None else last_sent_at.astimezone(UTC)
                    if now - last_sent_at < repeat_interval:
                        continue
                if send_native:
                    self._sender.send(
                        NotificationMessage(title=str(getattr(item, "title", "") or ""), body=message)
                    )
                self._notification_repo.mark_sent(
                    session,
                    show_trakt_id=show_id,
                    show_title=str(getattr(item, "title", "") or ""),
                    episode_trakt_id=episode_id,
                    season=int(episode.season),
                    episode=int(episode.number),
                    message=message,
                )
                sent.append({"show_title": str(getattr(item, "title", "") or ""), "message": message, "source": "progress"})
        return sent

    def _tmdb_has_due_unseen_current_episode(self) -> bool:
        current_ids = {
            int(getattr(item.next_episode, "trakt_id", 0) or 0): item.next_episode
            for view in ("active", "paused")
            for item in self._tmdb_progress_items(view)
            if getattr(item, "next_episode", None) is not None
        }
        if not current_ids:
            return False
        config = self._notification_config()
        release_delay = timedelta(
            minutes=max(0, int(getattr(config, "notification_release_delay_minutes", 120) or 0))
        )
        now = datetime.now(tz=UTC)
        raw_session_factory = getattr(self._db, "session", None)
        if not callable(raw_session_factory):
            return False
        session_factory = cast(Callable[[], Any], raw_session_factory)
        with session_factory() as session:
            for row in self._notification_repo.list_unseen(session):
                episode = current_ids.get(int(row.episode_trakt_id))
                if episode is None:
                    continue
                first_aired = getattr(episode, "first_aired", None) or row.sent_at
                release_at = first_aired.replace(tzinfo=UTC) if first_aired.tzinfo is None else first_aired.astimezone(UTC)
                if now >= release_at + release_delay:
                    return True
        return False

    def poll_upcoming(
        self,
        *,
        send_native: bool = True,
        refresh_remote: bool = True,
    ) -> list[dict]:
        auth_config = getattr(self._auth, "config", None)
        preview_mode = normalize_catalog_provider_mode(
            getattr(auth_config, "catalog_provider_mode", "trakt")
        ) == "tmdb_preview"
        auth_status_reader = getattr(self._auth, "is_authorized", None)
        can_refresh_trakt = bool(
            not preview_mode
            and refresh_remote
            and (auth_status_reader() if callable(auth_status_reader) else True)
        )
        if can_refresh_trakt and self._progress_service is not None:
            try:
                self._progress_service.sync_progress(dropped_only=False)
            except Exception:
                can_refresh_trakt = False
        if preview_mode and refresh_remote and self._progress_service is not None and self._tmdb_catalog is not None:
            try:
                progress_items = self._progress_service.dashboard_progress(limit=None)
                self._tmdb_catalog.refresh_episode_air_dates(progress_items)
            except Exception:
                pass
        if preview_mode:
            items = self._poll_tmdb_upcoming(send_native=send_native)
        else:
            try:
                items = self._workflow.poll_upcoming(
                    send_native=send_native,
                    refresh_remote=can_refresh_trakt,
                )
            except Exception:
                # Preserve local episode notifications if an online refresh fails.
                items = self._workflow.poll_upcoming(send_native=send_native, refresh_remote=False)
        if self._release_tracking is not None and not preview_mode:
            try:
                items.extend(
                    self._release_tracking.poll(
                        send_native=send_native,
                        refresh_remote=can_refresh_trakt,
                    )
                )
            except Exception:
                items.extend(self._release_tracking.poll(send_native=send_native, refresh_remote=False))
        if self._tmdb_catalog is not None and preview_mode:
            try:
                items.extend(
                    self._tmdb_catalog.poll_releases(
                        send_native=send_native,
                        refresh_remote=refresh_remote,
                    )
                )
            except Exception:
                pass
        self.refresh_pending_sources()
        return items

    def mark_episode_seen(self, *, show_trakt_id: int, show_title: str, episode: EpisodeSummary) -> None:
        if self._tmdb_preview_mode():
            show_id = self._tmdb_notification_show_id(show_trakt_id, episode)
            episode_id = int(getattr(episode, "trakt_id", 0) or 0)
            if show_id == 0 or episode_id >= 0:
                return
            message = f"S{episode.season:02d}E{episode.number:02d} {episode.title}"
            with self._db.session() as session:
                self._notification_repo.mark_seen(
                    session,
                    show_trakt_id=show_id,
                    show_title=show_title,
                    episode_trakt_id=episode_id,
                    season=episode.season,
                    episode=episode.number,
                    message=message,
                )
        else:
            self._workflow.mark_episode_seen(show_trakt_id=show_trakt_id, show_title=show_title, episode=episode)
        self.refresh_pending_sources()

    def unseen_episode_ids(self) -> set[int]:
        if self._tmdb_preview_mode():
            return self._tmdb_unseen_episode_ids()
        return self._workflow.unseen_episode_ids()

    def upcoming_items(self) -> list[dict]:
        if self._tmdb_preview_mode():
            result: list[dict] = []
            for item in self._tmdb_progress_items("active"):
                episode = getattr(item, "next_episode", None)
                if episode is None:
                    continue
                result.append(
                    {
                        "show_title": str(getattr(item, "title", "") or ""),
                        "episode_trakt_id": int(getattr(episode, "trakt_id", 0) or 0),
                        "episode_title": str(getattr(episode, "title", "") or ""),
                        "season": int(episode.season),
                        "episode": int(episode.number),
                        "first_aired": getattr(episode, "first_aired", None),
                    }
                )
            return result
        return self._workflow.upcoming_items()

    def record_activity(self, items: list[dict]) -> int:
        sources = sorted(
            {
                str(item.get("source", "") or "")
                for item in items
                if isinstance(item, dict) and str(item.get("source", "") or "") in self._ACTIVITY_SOURCES
            }
        )
        with self._activity_lock:
            if sources:
                self._activity_seq += 1
                self._activity_events.append({"seq": self._activity_seq, "sources": sources})
            return self._activity_seq

    def activity_after(self, after: int = 0) -> list[dict]:
        with self._activity_lock:
            return [
                {"seq": int(event["seq"]), "sources": list(event["sources"])}
                for event in self._activity_events
                if int(event["seq"]) > max(0, int(after))
            ]

    def current_activity_seq(self) -> int:
        with self._activity_lock:
            return self._activity_seq

    def refresh_pending_sources(self) -> list[str]:
        pending: set[str] = set()
        if self._tmdb_preview_mode():
            if self._tmdb_has_due_unseen_current_episode():
                pending.add("progress")
        elif self._workflow.has_due_unseen_current_episode():
            pending.add("progress")
        auth_config = getattr(self._auth, "config", None)
        preview_mode = normalize_catalog_provider_mode(
            getattr(auth_config, "catalog_provider_mode", "trakt")
        ) == "tmdb_preview"
        if preview_mode:
            if self._tmdb_catalog is not None and self._tmdb_catalog.has_due_unacknowledged_release():
                pending.add("release")
        elif self._release_tracking is not None and self._release_tracking.has_due_unacknowledged_release():
            pending.add("release")
        with self._activity_lock:
            self._pending_sources = pending
            return sorted(self._pending_sources)

    def pending_sources(self) -> list[str]:
        with self._activity_lock:
            return sorted(self._pending_sources)


class SyncService:
    IMDB_AUTO_SYNC_KEY = "imdb_last_auto_sync_at"
    ARTWORK_WARM_CURSOR_KEY = "artwork_warm_cursor_v1"

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
        catalog: CatalogService | None = None,
        enrich_queue: EnrichQueueService | None = None,
        image_queue: ArtworkQueue | None = None,
        imdb_client: IMDbDatasetClient | None = None,
        progress_service: ProgressService | None = None,
        notification_service: NotificationService | None = None,
        trakt_outbox: TraktOutboxService | None = None,
        tmdb_catalog: TmdbCatalogService | None = None,
    ) -> None:
        self._db = db
        self._auth = auth_service
        self._sync_state = sync_state
        self._imdb_client = imdb_client or IMDbDatasetClient()
        self._titles = titles
        self._episode_repo = episode_repo
        self._catalog = catalog
        self._operations = operations
        self._episode_metadata = episode_metadata
        self._enrich_queue = enrich_queue
        self._image_queue = image_queue
        self._progress_service = progress_service
        self._notification_service = notification_service
        self._tmdb_catalog = tmdb_catalog
        self._image_cache = BinaryCache("images")
        self._image_failure_cache = ProviderCache("image_failures")
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
            catalog,
            trakt_outbox=trakt_outbox,
        )

    def initial_import(self, *, status_callback=None, defer_enrichment: bool = False) -> None:
        if not self._trakt_mode_enabled():
            return
        self._workflow.initial_import(
            status_callback=status_callback,
            defer_enrichment=defer_enrichment,
        )
        self._reconcile_tmdb_preview_mappings()

    def refresh_history(self, *, force_full_assets: bool = False, status_callback=None) -> None:
        if not self._trakt_mode_enabled():
            return
        self._workflow.refresh_history(force_full_assets=force_full_assets, status_callback=status_callback)
        self._reconcile_tmdb_preview_mappings()

    def sync_assets_full(self, *, status_callback=None) -> None:
        """Compatibility entry point for the normal Trakt data update."""
        self.sync_trakt_data(status_callback=status_callback)

    def sync_trakt_data(self, *, status_callback=None) -> None:
        if not self._trakt_mode_enabled():
            return
        def sync_all() -> None:
            self.refresh_history(
                force_full_assets=False,
                status_callback=status_callback,
            )
            if self._progress_service is not None:
                self._progress_service.sync_progress(
                    dropped_only=False,
                    force_refresh=True,
                    force_full_assets=False,
                )
            if self._notification_service is not None:
                self._notification_service.refresh_pending_sources()

        self._run_with_enrichment_barrier(
            "Trakt data update",
            sync_all,
        )

    def sync_assets_backfill(self, *, status_callback=None) -> None:
        if not self._trakt_mode_enabled():
            return
        self._run_with_enrichment_barrier(
            "Metadata backfill",
            lambda: self._sync_assets(
                mode_label="Metadata backfill",
                title_statuses=("unknown",),
                episode_statuses=("unknown",),
                include_missing_title_url=False,
                include_missing_still=False,
                include_binary_cache_gaps=False,
                batch_limit=80,
                status_callback=status_callback,
            ),
        )

    def sync_assets_timeout_only(self, *, status_callback=None) -> None:
        if not self._trakt_mode_enabled():
            return
        self._run_with_enrichment_barrier(
            "Metadata retry",
            lambda: self._sync_assets(
                mode_label="Metadata retry",
                title_statuses=("retryable_failure",),
                episode_statuses=("retryable_failure",),
                include_missing_title_url=False,
                include_missing_still=False,
                include_binary_cache_gaps=False,
                batch_limit=40,
                status_callback=status_callback,
            ),
        )

    def sync_assets_repair(self, *, status_callback=None) -> None:
        if not self._trakt_mode_enabled():
            return
        self._run_with_enrichment_barrier(
            "Metadata recheck",
            lambda: self._sync_assets(
                mode_label="Metadata recheck",
                title_statuses=("checked_no_data",),
                episode_statuses=("checked_no_data",),
                include_missing_title_url=False,
                include_missing_still=False,
                include_binary_cache_gaps=False,
                batch_limit=40,
                status_callback=status_callback,
            ),
        )

    def maybe_refresh_history(self) -> bool:
        if not self._trakt_mode_enabled():
            return False
        return self._workflow.maybe_refresh_history()

    def sync_updates(self) -> None:
        if not self._trakt_mode_enabled():
            return
        self._workflow.sync_updates()
        self._reconcile_tmdb_preview_mappings()

    def _trakt_mode_enabled(self) -> bool:
        config = getattr(getattr(self, "_auth", None), "config", None)
        return normalize_catalog_provider_mode(
            getattr(config, "catalog_provider_mode", "trakt")
        ) == "trakt"

    def _reconcile_tmdb_preview_mappings(self) -> None:
        tmdb_catalog = getattr(self, "_tmdb_catalog", None)
        if tmdb_catalog is None:
            return
        try:
            tmdb_catalog.reconcile_mapped_intents()
        except Exception:
            # A catalog mapping must never make an otherwise successful Trakt
            # import fail; the next catalog access or sync retries it.
            return

    def sync_imdb_dataset(self, force: bool = False, status_callback=None) -> bool:
        changed = self._imdb_client.sync(force=force, status_callback=status_callback)
        self._episode_metadata.backfill_episode_imdb_ids_from_payloads(
            load_cached_trakt_history_items(self._auth.config.active_slug)
            + load_cached_trakt_rating_items(self._auth.config.active_slug)
        )
        if changed:
            self._episode_metadata.repair_episode_imdb_ratings(refresh_known=True)
        else:
            self._episode_metadata.repair_episode_imdb_ratings()
        return changed

    def should_auto_sync_imdb_dataset(self, interval_minutes: int) -> bool:
        interval = max(1, int(interval_minutes or 1))
        with self._db.session() as session:
            raw = self._sync_state.get_value(session, self.IMDB_AUTO_SYNC_KEY, "")
        last_sync_at = SyncPolicy.parse_timestamp(raw)
        if last_sync_at is None:
            return True
        return datetime.now(tz=UTC) - last_sync_at >= timedelta(minutes=interval)

    def maybe_sync_imdb_dataset(self, interval_minutes: int, status_callback=None) -> bool:
        interval = max(1, int(interval_minutes or 1))
        now = datetime.now(tz=UTC)
        with self._db.session() as session:
            raw = self._sync_state.get_value(session, self.IMDB_AUTO_SYNC_KEY, "")
            last_sync_at = SyncPolicy.parse_timestamp(raw)
            if last_sync_at is not None and now - last_sync_at < timedelta(minutes=interval):
                return False
        # Auto-sync interval should mean a real scheduled refresh, not just a stale check.
        changed = self.sync_imdb_dataset(force=True, status_callback=status_callback)
        if changed or not self._imdb_client.is_stale():
            with self._db.session() as session:
                self._sync_state.set_value(session, self.IMDB_AUTO_SYNC_KEY, now.isoformat())
        return changed

    def clear_imdb_dataset(self) -> None:
        self._imdb_client.clear()

    def imdb_dataset_status(self) -> str:
        return self._imdb_client.last_updated_text()

    def repair_legacy_episode_history(self) -> bool:
        if not self._trakt_mode_enabled():
            return False
        return self._workflow.repair_legacy_episode_history()

    def refresh_show(self, trakt_id: int) -> ProgressSnapshot:
        if not self._trakt_mode_enabled():
            raise RuntimeError("Trakt progress refresh is disabled in TMDb mode.")
        return self._workflow.refresh_show(trakt_id)

    def dashboard_state(self) -> DashboardState:
        if not self._trakt_mode_enabled():
            return DashboardState()
        return self._workflow.dashboard_state()

    def enqueue_due_background_trakt_episode_ratings(self, *, limit: int = 200) -> int:
        if not self._trakt_mode_enabled() or self._enrich_queue is None or not self._auth.is_authorized():
            return 0
        batch_limit = max(1, int(limit or 1))
        with self._db.session() as session:
            candidates = self._episode_repo.list_episode_rating_refresh_candidates(session)
        tasks = []
        for row in candidates:
            if len(tasks) >= batch_limit:
                break
            due = metadata_refresh_due(
                ASSET_KIND_EPISODE_RATINGS,
                status=str(row.get("trakt_details_status") or ""),
                last_checked_at=row.get("trakt_details_refreshed_at"),
                has_value=(row.get("trakt_rating") is not None and row.get("trakt_votes") is not None),
                trigger=TRIGGER_BACKGROUND_SWEEP,
                first_aired=row.get("first_aired"),
            )
            if not due.should_refresh:
                continue
            show_trakt_id = int(row["show_trakt_id"])
            season = int(row["season"])
            episode = int(row["number"])
            tasks.append(
                build_history_episode_task(
                    title_key=f"bg:{show_trakt_id}:{season}:{episode}",
                    show_trakt_id=show_trakt_id,
                    season=season,
                    episode=episode,
                    priority=3,
                    trigger=TRIGGER_BACKGROUND_SWEEP,
                    requested_parts=(ASSET_KIND_EPISODE_RATINGS,),
                )
            )
        if not tasks:
            return 0
        self._enrich_queue.submit_history_refresh(viewport_tasks=[], nearby_tasks=[], page_tasks=tasks)
        return len(tasks)

    def warm_missing_artwork_cache(self, *, limit: int = 100, timeout: float = 8, max_workers: int = 4) -> dict:
        started = perf_counter()
        urls, scanned = self._missing_artwork_urls(limit=limit)
        if self._image_queue is not None:
            queued = self._image_queue.submit_many(urls, priority=4)
            warm_result = {
                "selected": len(urls),
                "warmed": 0,
                "failed": 0,
                "skipped": max(0, len(urls) - queued),
                "warmed_urls": [],
                "failed_urls": [],
                "queued": queued,
            }
        elif urls:
            warm_result = warm_image_urls(
                self._image_cache,
                urls,
                timeout=timeout,
                max_workers=max_workers,
                skip_cached=True,
                proxy_url=getattr(self._auth.config, "network_proxy_url", ""),
            )
        else:
            warm_result = {
                "selected": 0,
                "warmed": 0,
                "failed": 0,
                "skipped": 0,
                "warmed_urls": [],
                "failed_urls": [],
            }
        for failed_url in warm_result["failed_urls"]:
            self._image_failure_cache.set_json(str(failed_url), {"failed_at": datetime.now(tz=UTC).isoformat()})
        result = {
            **warm_result,
            "scanned": scanned,
            "duration_ms": round((perf_counter() - started) * 1000.0, 1),
        }
        if warm_result["selected"] or warm_result["failed"]:
            self._operations.publish(
                "Artwork cache",
                f"Artwork cache warm: scanned {scanned}, selected {warm_result['selected']}, "
                f"warmed {warm_result['warmed']}, failed {warm_result['failed']}, duration {result['duration_ms']:.1f} ms.",
            )
        return result

    def _run_with_enrichment_barrier(self, label: str, fn: Callable[[], None]) -> None:
        if self._enrich_queue is None:
            fn()
            return
        self._operations.publish(label, f"{label}: waiting for active metadata tasks.")
        with self._enrich_queue.exclusive_pause() as idle:
            if not idle:
                raise RuntimeError(f"{label}: metadata queue did not become idle")
            fn()

    def _missing_artwork_urls(self, *, limit: int) -> tuple[list[str], int]:
        batch_limit = max(1, int(limit or 1))
        urls: list[str] = []
        with self._db.session() as session:
            phase, after_id = self._artwork_warm_cursor(session)
            if phase == "title":
                rows = self._titles.list_artwork_batch(session, after_id=after_id, limit=batch_limit)
                next_phase = "title" if len(rows) >= batch_limit else "episode"
                next_id = int(rows[-1].id) if rows and next_phase == "title" else 0
                url_values = [str(row.poster_url or "") for row in rows]
            else:
                rows = self._episode_repo.list_artwork_batch(session, after_id=after_id, limit=batch_limit)
                next_phase = "episode" if len(rows) >= batch_limit else "title"
                next_id = int(rows[-1].id) if rows and next_phase == "episode" else 0
                url_values = [str(row.still_url or "") for row in rows]
            self._sync_state.set_value(session, self.ARTWORK_WARM_CURSOR_KEY, f"{next_phase}:{next_id}")

        for url in url_values:
            if not is_trusted_image_url(url):
                continue
            if not has_cached_image(self._image_cache, url) and not self._recent_image_failure(url):
                urls.append(url)
        return urls, len(url_values)

    def _artwork_warm_cursor(self, session) -> tuple[str, int]:
        raw = self._sync_state.get_value(session, self.ARTWORK_WARM_CURSOR_KEY, "title:0")
        phase, separator, raw_id = str(raw or "").partition(":")
        try:
            cursor_id = max(0, int(raw_id))
        except ValueError:
            cursor_id = 0
        return (phase if separator and phase in {"title", "episode"} else "title"), cursor_id

    def _recent_image_failure(self, url: str) -> bool:
        return self._image_failure_cache.get_json(url, ttl_hours=6) is not None

    def _sync_assets(
        self,
        *,
        mode_label: str,
        title_statuses: tuple[str, ...],
        episode_statuses: tuple[str, ...],
        include_missing_title_url: bool,
        include_missing_still: bool,
        include_binary_cache_gaps: bool = True,
        batch_limit: int | None = None,
        status_callback=None,
    ) -> None:
        def report(message: str) -> None:
            self._operations.publish(mode_label, message)
            if status_callback is not None:
                status_callback(message)

        with self._db.session() as session:
            title_targets = set(self._titles.list_title_targets(session, statuses=title_statuses))
            if include_missing_title_url:
                title_targets.update(self._titles.list_title_targets(session, include_missing_url=True))
            episode_targets = set(self._episode_repo.list_episode_keys(session, statuses=episode_statuses))
            if include_missing_still:
                episode_targets.update(self._episode_repo.list_episode_keys(session, include_missing_still=True))
            title_binary_targets: set[tuple[int, str]] = set()
            episode_binary_targets: set[tuple[int, int, int]] = set()
            if include_binary_cache_gaps:
                for title in self._titles.list_titles(session):
                    poster_url = str(title.poster_url or "")
                    if poster_url and not has_cached_image(self._image_cache, poster_url):
                        title_binary_targets.add((int(title.trakt_id), str(title.title_type)))
                for episode in self._episode_repo.list_all_episodes(session):
                    still_url = str(episode.still_url or "")
                    if still_url and not has_cached_image(self._image_cache, still_url):
                        episode_binary_targets.add((int(episode.show_trakt_id), int(episode.season), int(episode.number)))
            title_metadata_targets = set(title_targets)
            episode_metadata_targets = set(episode_targets)
            title_targets.update(title_binary_targets)
            episode_targets.update(episode_binary_targets)

        title_list = sorted(title_targets)
        episode_list = sorted(episode_targets)
        total_pending = len(title_list) + len(episode_list)
        if batch_limit is not None:
            remaining = max(1, int(batch_limit))
            selected_titles = title_list[:remaining]
            remaining -= len(selected_titles)
            title_list = selected_titles
            episode_list = episode_list[:remaining]
        total = len(title_list) + len(episode_list)
        completed = 0
        report(
            f"{mode_label}: selected {total} of {total_pending} pending item(s) "
            f"({len(title_list)} posters, {len(episode_list)} stills)."
        )
        if total <= 0:
            report(f"{mode_label}: nothing to sync.")
            return

        for trakt_id, title_type in title_list:
            try:
                if self._catalog is not None and (int(trakt_id), str(title_type)) in title_metadata_targets:
                    self._catalog.enrich_title_key(
                        int(trakt_id),
                        str(title_type),
                        trigger=TRIGGER_MANUAL_REPAIR,
                        requested_parts=(ASSET_KIND_POSTER,),
                    )
                self._warm_title_poster(int(trakt_id))
            except Exception as exc:
                report(f"{mode_label}: poster failed for {title_type}:{trakt_id}: {exc}")
            completed += 1
            report(f"{mode_label}: {completed}/{total} ({(completed * 100.0 / total):.1f}%)")

        for show_trakt_id, season, episode in episode_list:
            try:
                if (int(show_trakt_id), int(season), int(episode)) in episode_metadata_targets:
                    self._episode_metadata.enrich_episode_stills(
                        [(int(show_trakt_id), int(season), int(episode))],
                        trigger=TRIGGER_MANUAL_REPAIR,
                        requested_parts=(ASSET_KIND_STILL,),
                    )
                self._warm_episode_still(int(show_trakt_id), int(season), int(episode))
            except Exception as exc:
                report(f"{mode_label}: still failed for show:{show_trakt_id} S{season}E{episode}: {exc}")
            completed += 1
            report(f"{mode_label}: {completed}/{total} ({(completed * 100.0 / total):.1f}%)")

    def _warm_title_poster(self, trakt_id: int) -> None:
        with self._db.session() as session:
            row = self._titles.get_title(session, trakt_id)
            poster_url = str(row.poster_url or "") if row is not None else ""
        if poster_url:
            if self._image_queue is not None:
                self._image_queue.submit(poster_url, priority=3)
            else:
                warm_image_urls(
                    self._image_cache,
                    [poster_url],
                    timeout=8,
                    max_workers=1,
                    proxy_url=getattr(self._auth.config, "network_proxy_url", ""),
                )

    def _warm_episode_still(self, show_trakt_id: int, season: int, episode: int) -> None:
        with self._db.session() as session:
            row = self._episode_repo.find_episode(session, show_trakt_id, season, episode)
            still_url = str(row.still_url or "") if row is not None else ""
        if still_url:
            if self._image_queue is not None:
                self._image_queue.submit(still_url, priority=3)
            else:
                warm_image_urls(
                    self._image_cache,
                    [still_url],
                    timeout=8,
                    max_workers=1,
                    proxy_url=getattr(self._auth.config, "network_proxy_url", ""),
                )


class _ManagedTMDbClientFactory:
    """One TMDb connection pool per profile service container.

    Credentials may be edited while a request still owns the previous client.
    Such pools are retired and closed only with the container, rather than being
    closed under an in-flight request.
    """

    def __init__(self, *, cache_provider: str = "tmdb") -> None:
        self._lock = Lock()
        self._client: TMDbClient | None = None
        self._signature: tuple[str, str, int, str] | None = None
        self._retired_clients: list[TMDbClient] = []
        self._cache_provider = cache_provider

    def __call__(self, config: AppConfig) -> TMDbClient:
        signature = (
            resolved_tmdb_api_key(config),
            resolved_tmdb_read_access_token(config),
            int(config.cache_ttl_hours),
            normalize_tmdb_proxy_url(getattr(config, "network_proxy_url", "")),
        )
        with self._lock:
            if self._client is None or self._signature != signature:
                if self._client is not None:
                    self._retired_clients.append(self._client)
                self._client = TMDbClient(
                    api_key=signature[0],
                    read_access_token=signature[1],
                    cache_ttl_hours=signature[2],
                    cache_provider=self._cache_provider,
                    proxy_url=signature[3],
                )
                self._signature = signature
            return self._client

    def close(self) -> None:
        with self._lock:
            clients = [self._client, *self._retired_clients]
            self._client = None
            self._retired_clients = []
        for client in clients:
            if client is not None:
                client.close()


def build_services(config_store: ConfigStore, db: Database) -> ServiceContainer:
    tokens = TokenStore()
    titles = TitleRepository()
    user_states = UserStateRepository()
    history = HistoryRepository()
    progress = ProgressRepository()
    episode_repo = EpisodeRepository()
    sync_state = SyncStateRepository()
    notification_repo = NotificationRepository()
    release_tracking_repo = ReleaseTrackingRepository()
    title_alias_repo = TitleAliasRepository()
    trakt_outbox_repo = TraktOutboxRepository()
    tmdb_preview_repo = TmdbPreviewRepository()
    operations = OperationLog()

    def client_factory(config: AppConfig) -> TraktClient:
        client = TraktClient(
            resolved_trakt_client_id(config),
            resolved_trakt_client_secret(config),
            config.redirect_uri,
            cache_ttl_hours=config.cache_ttl_hours,
            cache_namespace=config.active_slug or "default",
            cache_provider=trakt_cache_provider(config.active_slug),
        )
        if config.active_slug:
            client.set_tokens(tokens.load(config.active_slug))
        return client

    tmdb_factory = _ManagedTMDbClientFactory()
    tmdb_catalog_factory = _ManagedTMDbClientFactory(cache_provider="tmdb/catalog")

    auth = AuthService(config_store, tokens, client_factory)

    trakt_outbox = TraktOutboxService(db, auth, trakt_outbox_repo, episode_repo, sync_state)
    cache = CacheService(auth.config.active_slug)
    image_queue = ArtworkQueue(
        BinaryCache("images"),
        max_workers=4,
        timeout=8,
        proxy_url_provider=lambda: getattr(auth.config, "network_proxy_url", ""),
    )
    imdb_client = IMDbDatasetClient(cache_ttl_hours=config_store.load().cache_ttl_hours)
    imdb_reconciliation = EpisodeIMDbReconciliationService(db, episode_repo, imdb_client)
    episode_metadata = EpisodeMetadataService(
        db,
        episode_repo,
        imdb_client,
        titles,
        auth,
        tmdb_factory,
        imdb_reconciliation=imdb_reconciliation,
    )
    history_read_model = HistoryReadModelService(db, history, user_states, titles, episode_repo, episode_metadata)
    catalog = CatalogService(
        db,
        auth,
        titles,
        user_states,
        sync_state,
        tmdb_factory,
        imdb_client,
        history,
        trakt_outbox=trakt_outbox,
    )
    episode_ratings_matrix = EpisodeRatingsMatrixService(
        db,
        auth,
        titles,
        history,
        episode_repo,
        imdb_client,
        imdb_reconciliation=imdb_reconciliation,
    )
    history_service = HistoryService(
        db,
        auth,
        titles,
        user_states,
        history,
        episode_repo,
        history_read_model,
        episode_metadata,
        trakt_outbox,
        progress,
    )
    search_watch = SearchWatchService(
        db,
        auth,
        titles,
        history,
        episode_repo,
        history_service,
        episode_metadata,
        tmdb_factory=tmdb_catalog_factory,
    )
    title_aliases = TitleAliasService(db, auth, title_alias_repo)
    enrich_queue = EnrichQueueService(
        {
            "history_title": lambda task: catalog.enrich_title_key(
                int(task.payload["trakt_id"]),
                str(task.payload["title_type"]),
                refresh_requests=task.payload.get("refresh_requests", []),
            ),
            "history_episode": lambda task: episode_metadata.enrich_episode_key(
                int(task.payload["show_trakt_id"]),
                int(task.payload["season"]),
                int(task.payload["episode"]),
                refresh_requests=task.payload.get("refresh_requests", []),
            ),
            "progress_title": lambda task: catalog.enrich_title_key(
                int(task.payload["trakt_id"]),
                str(task.payload["title_type"]),
                refresh_requests=task.payload.get("refresh_requests", []),
            ),
            "progress_episode": lambda task: episode_metadata.enrich_episode_key(
                int(task.payload["show_trakt_id"]),
                int(task.payload["season"]),
                int(task.payload["episode"]),
                refresh_requests=task.payload.get("refresh_requests", []),
            ),
        },
        max_workers=2,
    )
    play = PlayService(auth)
    tmdb_catalog = TmdbCatalogService(
        db,
        auth,
        tmdb_catalog_factory,
        titles,
        tmdb_preview_repo,
        trakt_outbox=trakt_outbox,
        imdb_client=imdb_client,
    )
    progress_service = ProgressService(
        db,
        auth,
        history,
        progress,
        episode_repo,
        titles,
        user_states,
        sync_state,
        tmdb_factory,
        imdb_client,
        operations,
        episode_metadata,
        catalog,
        notification_repo=notification_repo,
        trakt_outbox=trakt_outbox,
        tmdb_catalog=tmdb_catalog,
    )
    notification_sender = NotificationSender()
    release_tracking = ReleaseTrackingService(
        db,
        auth,
        config_store,
        release_tracking_repo,
        progress,
        notification_sender,
        titles=titles,
        trakt_outbox=trakt_outbox,
        episode_schedule_overlay=tmdb_catalog.overlay_episode_air_dates,
    )
    tmdb_catalog.set_legacy_services(
        catalog=catalog,
        release_tracking=release_tracking,
        search_watch=search_watch,
    )
    tmdb_catalog.set_notification_sender(notification_sender)
    trakt_outbox.set_delivery_callback(tmdb_catalog.on_trakt_outbox_delivered)
    notifications = NotificationService(
        db,
        auth,
        config_store,
        notification_repo,
        episode_repo,
        progress,
        notification_sender,
        release_tracking,
        progress_service,
        tmdb_catalog,
    )

    def refresh_notification_state() -> None:
        notifications.refresh_pending_sources()

    release_tracking.set_notification_state_callback(refresh_notification_state)
    notifications.refresh_pending_sources()
    interactions = InteractionService(history_service, notifications, progress_service)
    sync = SyncService(
        db,
        auth,
        titles,
        user_states,
        history,
        progress,
        episode_repo,
        sync_state,
        operations,
        episode_metadata,
        catalog,
        enrich_queue,
        image_queue=image_queue,
        imdb_client=imdb_client,
        progress_service=progress_service,
        notification_service=notifications,
        trakt_outbox=trakt_outbox,
        tmdb_catalog=tmdb_catalog,
    )
    def _refresh_delivered_history(trakt_id: int) -> None:
        if trakt_id > 0:
            progress_service.refresh_show_progress(
                trakt_id,
                fresh=True,
                enrich_assets=False,
            )

    trakt_outbox.set_history_delivered_callback(_refresh_delivered_history)
    return ServiceContainer(
        database=db,
        auth=auth,
        cache=cache,
        catalog=catalog,
        tmdb_catalog=tmdb_catalog,
        episode_ratings_matrix=episode_ratings_matrix,
        enrich_queue=enrich_queue,
        image_queue=image_queue,
        history=history_service,
        interactions=interactions,
        play=play,
        progress=progress_service,
        release_tracking=release_tracking,
        search_watch=search_watch,
        title_aliases=title_aliases,
        notifications=notifications,
        sync=sync,
        trakt_sync=trakt_outbox,
        operations=operations,
        closers=(auth.close, tmdb_factory.close, tmdb_catalog_factory.close, imdb_client.close),
    )
