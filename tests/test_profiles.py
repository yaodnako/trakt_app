from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from trakt_tracker.application.services import AuthService
from trakt_tracker.config import (
    AppConfig,
    ConfigStore,
    profile_database_path,
    resolved_tmdb_api_key,
    resolved_tmdb_read_access_token,
    resolved_trakt_client_id,
    resolved_trakt_client_secret,
    trakt_cache_provider,
)
from trakt_tracker.persistence.database import Database
from trakt_tracker.persistence.repositories import SyncStateRepository
from trakt_tracker.profiles import read_setup_state
from trakt_tracker.web.app import create_app
from trakt_tracker.web.runtime import PortalRuntime


class _Tokens:
    def load(self, _slug):
        return None

    def save(self, _slug, _bundle) -> None:
        return None

    def delete(self, _slug) -> None:
        return None


def _store(tmp_path: Path, config: AppConfig) -> ConfigStore:
    store = ConfigStore(tmp_path / "config.json")
    store.save(config)
    return store


def test_release_defaults_and_manual_overrides(monkeypatch) -> None:
    monkeypatch.setenv("TRAKT_TRACKER_TRAKT_CLIENT_ID", "default-client")
    monkeypatch.setenv("TRAKT_TRACKER_TRAKT_CLIENT_SECRET", "default-secret")
    monkeypatch.setenv("TRAKT_TRACKER_TMDB_API_KEY", "default-tmdb")
    monkeypatch.setenv("TRAKT_TRACKER_TMDB_READ_ACCESS_TOKEN", "default-read-token")
    config = AppConfig()

    assert resolved_trakt_client_id(config) == "default-client"
    assert resolved_trakt_client_secret(config) == "default-secret"
    assert resolved_tmdb_api_key(config) == "default-tmdb"
    assert resolved_tmdb_read_access_token(config) == "default-read-token"

    config.client_id = "manual-client"
    config.client_secret = "manual-secret"
    config.tmdb_api_key = "manual-tmdb"
    config.tmdb_read_access_token = "manual-read-token"
    assert resolved_trakt_client_id(config) == "manual-client"
    assert resolved_trakt_client_secret(config) == "manual-secret"
    assert resolved_tmdb_api_key(config) == "manual-tmdb"
    assert resolved_tmdb_read_access_token(config) == "manual-read-token"


def test_blank_secret_fields_preserve_existing_values(tmp_path: Path) -> None:
    store = _store(
        tmp_path,
        AppConfig(
            client_id="client",
            client_secret="trakt-secret",
            tmdb_api_key="tmdb-secret",
            tmdb_read_access_token="tmdb-token",
            kinopoisk_api_key="kinopoisk-secret",
        ),
    )
    auth = AuthService(store, _Tokens(), lambda _config: None)

    auth.update_config("client", "", "http://127.0.0.1:8765/callback", "", "", "")

    saved = store.load()
    assert saved.client_secret == "trakt-secret"
    assert saved.tmdb_api_key == "tmdb-secret"
    assert saved.tmdb_read_access_token == "tmdb-token"
    assert saved.kinopoisk_api_key == "kinopoisk-secret"


def test_settings_html_does_not_render_saved_secrets(tmp_path: Path) -> None:
    config = AppConfig(
        client_id="public-client-id",
        client_secret="private-trakt-secret",
        tmdb_api_key="private-tmdb-key",
        tmdb_read_access_token="private-tmdb-token",
        kinopoisk_api_key="private-kinopoisk-key",
        active_profile_slug="viewer",
        known_profile_slugs=["viewer"],
        legacy_profile_migrated_slug="viewer",
        database_path=str(tmp_path / "legacy.sqlite3"),
    )
    app = create_app(_store(tmp_path, config))
    app.state.services.auth.is_authorized = lambda: True
    try:
        response = TestClient(app, base_url="http://127.0.0.1").get("/settings")
    finally:
        app.state.runtime.close()

    assert response.status_code == 200
    assert "public-client-id" in response.text
    assert "private-trakt-secret" not in response.text
    assert "private-tmdb-key" not in response.text
    assert "private-tmdb-token" not in response.text
    assert "private-kinopoisk-key" not in response.text


def test_profile_paths_and_trakt_caches_are_separate(tmp_path: Path) -> None:
    config = AppConfig(database_path=str(tmp_path / "custom.sqlite3"))

    alpha = profile_database_path(config, "Alpha User")
    beta = profile_database_path(config, "Beta User")

    assert alpha != beta
    assert alpha.parent.parent == tmp_path / "TraktTrackerProfiles"
    assert alpha.name == "tracker.sqlite3"
    assert trakt_cache_provider("Alpha User") != trakt_cache_provider("Beta User")


def test_legacy_database_is_backed_up_once_and_source_is_preserved(tmp_path: Path) -> None:
    legacy_path = tmp_path / "legacy.sqlite3"
    legacy = Database(legacy_path)
    legacy.create_schema()
    repository = SyncStateRepository()
    with legacy.session() as session:
        repository.set_value(session, "legacy-sentinel", "copied")
    legacy.close()
    store = _store(
        tmp_path,
        AppConfig(
            database_path=str(legacy_path),
            active_profile_slug="legacy-user",
            known_profile_slugs=["legacy-user"],
        ),
    )

    runtime = PortalRuntime(store)
    target_path = profile_database_path(store.load(), "legacy-user")
    try:
        with runtime.database.session() as session:
            assert repository.get_value(session, "legacy-sentinel", "") == "copied"
        assert read_setup_state(runtime.database)["state"] == "complete"
    finally:
        runtime.close()

    assert legacy_path.exists()
    assert target_path.exists()
    source = Database(legacy_path)
    with source.session() as session:
        repository.set_value(session, "after-migration", "source-only")
    source.close()

    second_runtime = PortalRuntime(store)
    try:
        with second_runtime.database.session() as session:
            assert repository.get_value(session, "after-migration", "") == ""
    finally:
        second_runtime.close()
    assert store.load().legacy_profile_migrated_slug == "legacy-user"


def test_runtime_switches_profile_databases_without_mixing_state(tmp_path: Path) -> None:
    store = _store(
        tmp_path,
        AppConfig(
            database_path=str(tmp_path / "legacy.sqlite3"),
            active_profile_slug="alpha",
            known_profile_slugs=["alpha", "beta"],
            legacy_profile_migrated_slug="alpha",
        ),
    )
    runtime = PortalRuntime(store)
    repository = SyncStateRepository()
    alpha_path = runtime.database._engine.url.database
    with runtime.database.session() as session:
        repository.set_value(session, "profile-value", "alpha")

    runtime.activate_profile("beta")
    beta_path = runtime.database._engine.url.database
    with runtime.database.session() as session:
        assert repository.get_value(session, "profile-value", "") == ""
        repository.set_value(session, "profile-value", "beta")

    runtime.activate_profile("alpha")
    try:
        with runtime.database.session() as session:
            assert repository.get_value(session, "profile-value", "") == "alpha"
        assert alpha_path != beta_path
        assert store.load().active_slug == "alpha"
    finally:
        runtime.close()
