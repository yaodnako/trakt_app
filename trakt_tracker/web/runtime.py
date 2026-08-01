from __future__ import annotations

from threading import Lock, RLock, Thread, current_thread
from typing import Callable

from trakt_tracker.application.operations import OperationLog
from trakt_tracker.application.services import ServiceContainer, build_services
from trakt_tracker.config import ConfigStore, get_app_data_dir, trakt_cache_provider
from trakt_tracker.persistence.database import Database
from trakt_tracker.profiles import mark_setup_complete, prepare_active_profile, register_active_profile


class ProfileOperationCoordinator:
    """Serialize profile-writing workflows while retaining explicit operation logs."""

    _WRITE_PREFIXES = (
        "initial_setup",
        "history_sync",
        "progress_sync",
        "progress_",
        "release_",
        "watchlist_",
        "settings_",
        "imdb_",
        "search_enrichment",
        "tray_notification_poll",
        "trakt_outbox",
    )

    def __init__(self) -> None:
        self._running: set[str] = set()
        self._pending: list[tuple[str, str, OperationLog, Callable[[], None]]] = []
        self._threads: set[Thread] = set()
        self._closed = False
        self._lock = Lock()

    def start(self, key: str, *, source: str, operations, fn: Callable[[], None]) -> bool:
        with self._lock:
            if self._closed:
                operations.publish(source, f"{source}: coordinator is shutting down.")
                return False
            if key in self._running or any(pending[0] == key for pending in self._pending):
                operations.publish(source, f"{source}: already running or queued.")
                return False
            entry = (key, source, operations, fn)
            if self._is_profile_write(key) and self._has_running_profile_write_locked():
                self._pending.append(entry)
                operations.publish(source, f"{source}: queued behind the active profile workflow.")
                return True
            self._running.add(key)
        self._launch(entry)
        return True

    def start_coalesced(self, key: str, *, source: str, operations, fn: Callable[[], None]) -> bool:
        entry = (key, source, operations, fn)
        with self._lock:
            if self._closed:
                operations.publish(source, f"{source}: coordinator is shutting down.")
                return False
            for index, pending in enumerate(self._pending):
                if pending[0] != key:
                    continue
                self._pending[index] = entry
                operations.publish(source, f"{source}: updated queued work.")
                return True
            if key in self._running:
                self._pending.append(entry)
                operations.publish(source, f"{source}: queued one coalesced rerun.")
                return True
            if self._is_profile_write(key) and self._has_running_profile_write_locked():
                self._pending.append(entry)
                operations.publish(source, f"{source}: queued behind the active profile workflow.")
                return True
            self._running.add(key)
        self._launch(entry)
        return True

    def is_running(self, key: str) -> bool:
        with self._lock:
            return key in self._running or any(pending[0] == key for pending in self._pending)

    def is_active(self, key: str) -> bool:
        with self._lock:
            return key in self._running

    def is_queued(self, key: str) -> bool:
        with self._lock:
            return any(pending[0] == key for pending in self._pending)

    def has_running_prefix(self, *prefixes: str) -> bool:
        with self._lock:
            return any(any(item.startswith(prefix) for prefix in prefixes) for item in self._running)

    def has_running_profile_write(self) -> bool:
        with self._lock:
            return self._has_running_profile_write_locked()

    def any_running(self) -> bool:
        with self._lock:
            return bool(self._running or self._pending)

    def _launch(self, entry: tuple[str, str, OperationLog, Callable[[], None]]) -> None:
        key, source, operations, fn = entry

        def runner() -> None:
            operations.publish(source, f"{source}: started.")
            try:
                fn()
            except Exception as exc:
                operations.publish(source, f"{source}: failed: {exc}")
            else:
                operations.publish(source, f"{source}: completed.")
            finally:
                next_entry = None
                with self._lock:
                    self._running.discard(key)
                    self._threads.discard(current_thread())
                    if not self._closed:
                        for index, candidate in enumerate(self._pending):
                            if not self._is_profile_write(candidate[0]) or not self._has_running_profile_write_locked():
                                next_entry = self._pending.pop(index)
                                self._running.add(next_entry[0])
                                break
                if next_entry is not None:
                    self._launch(next_entry)

        worker = Thread(target=runner, name=f"trakt-{key[:36]}", daemon=True)
        with self._lock:
            if self._closed:
                self._running.discard(key)
                return
            self._threads.add(worker)
        worker.start()

    def close(self, timeout: float = 5.0) -> bool:
        """Stop accepting work and wait briefly before profile resources close."""
        with self._lock:
            self._closed = True
            self._pending.clear()
            workers = list(self._threads)
        for worker in workers:
            worker.join(timeout=max(0.0, float(timeout)))
        return all(not worker.is_alive() for worker in workers)

    def _has_running_profile_write_locked(self) -> bool:
        return any(self._is_profile_write(key) for key in self._running)

    @classmethod
    def _is_profile_write(cls, key: str) -> bool:
        return key.startswith(cls._WRITE_PREFIXES)


class PortalRuntime:
    """Owns the active profile services without invalidating in-flight requests."""

    def __init__(self, config_store: ConfigStore | None = None) -> None:
        self.config_store = config_store or ConfigStore()
        self._lock = RLock()
        self._retired_databases: list[Database] = []
        self._retired_services: list[ServiceContainer] = []
        self.background_tasks = ProfileOperationCoordinator()
        self._active_slug = ""
        self._database: Database
        self._services: ServiceContainer
        self._open_active_profile()

    @property
    def active_slug(self) -> str:
        with self._lock:
            return self._active_slug

    @property
    def database(self) -> Database:
        with self._lock:
            return self._database

    @property
    def services(self) -> ServiceContainer:
        with self._lock:
            return self._services

    def activate_profile(self, slug: str) -> ServiceContainer:
        with self._lock:
            if self.background_tasks.any_running() or self._services.enrich_queue.is_running():
                raise RuntimeError("Profile switch is waiting for active profile operations to finish")
            register_active_profile(self.config_store, slug)
            return self._replace_active_profile()

    def refresh_active_profile(self) -> bool:
        with self._lock:
            configured_slug = self.config_store.load().active_slug
            if configured_slug == self._active_slug:
                return False
            if self.background_tasks.any_running() or self._services.enrich_queue.is_running():
                raise RuntimeError("Profile switch is waiting for active profile operations to finish")
            self._replace_active_profile()
            return True

    def close(self) -> None:
        self.background_tasks.close()
        with self._lock:
            databases = [self._database, *self._retired_databases]
            services = [self._services, *self._retired_services]
            self._retired_databases = []
            self._retired_services = []
        seen_services: set[int] = set()
        for service in services:
            if id(service) in seen_services:
                continue
            seen_services.add(id(service))
            service.close()
        seen: set[int] = set()
        for database in databases:
            if id(database) in seen:
                continue
            seen.add(id(database))
            database.close()

    def _replace_active_profile(self) -> ServiceContainer:
        migration = prepare_active_profile(self.config_store)
        database = Database(migration.database_path)
        try:
            config = self.config_store.load()
            trakt_cache = get_app_data_dir() / "cache" / trakt_cache_provider(config.active_slug)
            database.create_schema(pre_migration_cache=trakt_cache)
            if migration.copied_legacy_database:
                mark_setup_complete(database, "Existing profile migrated and ready.")
            services = build_services(self.config_store, database)
            def wake_outbox(active_services=services) -> None:
                def drain_due() -> None:
                    for _attempt in range(5):
                        result = active_services.trakt_sync.drain(limit=20)
                        if result.processed == 0 or result.delivered == 0:
                            break

                self.background_tasks.start_coalesced(
                    "trakt_outbox_sync",
                    source="Trakt sync queue",
                    operations=active_services.operations,
                    fn=drain_due,
                )

            services.trakt_sync.set_wake_callback(wake_outbox)
        except Exception:
            database.close()
            raise
        with self._lock:
            old_database = getattr(self, "_database", None)
            old_services = getattr(self, "_services", None)
            self._database = database
            self._services = services
            self._active_slug = migration.slug
            if old_database is not None:
                self._retired_databases.append(old_database)
            if old_services is not None and old_services is not services:
                self._retired_services.append(old_services)
        return services

    def _open_active_profile(self) -> None:
        self._replace_active_profile()
