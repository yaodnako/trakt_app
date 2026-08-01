from __future__ import annotations


def schedule_watch_follow_up(
    app,
    *,
    title_type: str,
    trakt_id: int,
    remove_from_watchlist: bool = False,
    remove_from_release_tracking: bool = False,
) -> bool:
    services = app.state.services
    operations = services.operations
    bg_tasks = getattr(app.state, "bg_tasks", None)
    normalized_type = "show" if title_type == "show" else "movie"
    title_trakt_id = int(trakt_id)
    trakt_sync = getattr(services, "trakt_sync", None)
    if trakt_sync is not None:
        dependency_key = trakt_sync.pending_history_dependency(
            title_type=normalized_type,
            trakt_id=title_trakt_id,
        )
        watchlist_key = (normalized_type, title_trakt_id)
        should_remove_from_watchlist = bool(remove_from_watchlist)
        if normalized_type == "show":
            try:
                should_remove_from_watchlist = (
                    not services.catalog.has_watchlist_snapshot()
                    or watchlist_key in services.catalog.watchlist_keys(title_type="show")
                )
            except Exception as exc:
                operations.publish("Watchlist cleanup after watch", f"Snapshot check failed: {exc}")
                should_remove_from_watchlist = True
        if should_remove_from_watchlist:
            services.catalog.set_watchlisted(
                normalized_type,
                title_trakt_id,
                watchlisted=False,
                dependency_key=dependency_key,
                origin="history",
            )
        release_tracking = getattr(services, "release_tracking", None)
        if remove_from_release_tracking and release_tracking is not None:
            try:
                if watchlist_key in release_tracking.local_keys():
                    release_tracking.set_tracked(
                        normalized_type,
                        title_trakt_id,
                        tracked=False,
                        dependency_key=dependency_key,
                        origin="history",
                    )
            except Exception as exc:
                operations.publish("Release cleanup after watch", f"Local cleanup failed: {exc}")
        return should_remove_from_watchlist

    if bg_tasks is None:
        operations.publish("Watch follow-up", "Background task coordinator is unavailable.")
        return False

    watchlist_key = (normalized_type, title_trakt_id)
    should_remove_from_watchlist = bool(remove_from_watchlist)
    if normalized_type == "show":
        has_snapshot = getattr(services.catalog, "has_watchlist_snapshot", None)
        snapshot_available = True
        try:
            if callable(has_snapshot):
                snapshot_available = bool(has_snapshot())
            should_remove_from_watchlist = (
                not snapshot_available
                or watchlist_key
                in services.catalog.watchlist_keys(title_type="show")
            )
        except Exception as exc:
            operations.publish("Watchlist cleanup after watch", f"Snapshot check failed: {exc}")
            should_remove_from_watchlist = True
    removal_scheduled = False
    if should_remove_from_watchlist:
        task_key = f"watchlist_remove_after_watch_{normalized_type}_{title_trakt_id}"

        def remove_watchlisted_title() -> None:
            if normalized_type == "show":
                has_current_snapshot = getattr(
                    services.catalog,
                    "has_watchlist_snapshot",
                    None,
                )
                if callable(has_current_snapshot) and not has_current_snapshot():
                    services.catalog.refresh_watchlist_snapshot()
                if watchlist_key not in services.catalog.watchlist_keys(
                    title_type="show"
                ):
                    return
            services.catalog.set_watchlisted(
                normalized_type,
                title_trakt_id,
                watchlisted=False,
            )

        removal_scheduled = _start_coalesced(
            bg_tasks,
            task_key,
            source="Watchlist cleanup after watch",
            operations=operations,
            fn=remove_watchlisted_title,
        )

    release_tracking = getattr(services, "release_tracking", None)
    if remove_from_release_tracking and release_tracking is not None:
        try:
            is_release_tracked = watchlist_key in release_tracking.local_keys()
        except Exception as exc:
            operations.publish(
                "Release cleanup after watch",
                f"Snapshot check failed: {exc}",
            )
            is_release_tracked = False
        if is_release_tracked:
            release_task_key = (
                f"release_remove_after_watch_{normalized_type}_{title_trakt_id}"
            )

            def remove_release_tracking() -> None:
                if watchlist_key not in release_tracking.local_keys():
                    return
                release_tracking.set_tracked(
                    normalized_type,
                    title_trakt_id,
                    tracked=False,
                )

            _start_coalesced(
                bg_tasks,
                release_task_key,
                source="Release cleanup after watch",
                operations=operations,
                fn=remove_release_tracking,
            )

    progress = getattr(services, "progress", None)
    if progress is not None and normalized_type == "show":
        _start_coalesced(
            bg_tasks,
            f"progress_refresh_after_watch_{title_trakt_id}",
            source="Progress refresh after watch",
            operations=operations,
            fn=lambda: progress.refresh_show_progress(
                title_trakt_id,
                fresh=True,
                enrich_assets=False,
            ),
        )

    return removal_scheduled


def _start_coalesced(bg_tasks, key: str, *, source: str, operations, fn) -> bool:
    starter = getattr(bg_tasks, "start_coalesced", None)
    if not callable(starter):
        starter = bg_tasks.start
    try:
        started = bool(
            starter(
                key,
                source=source,
                operations=operations,
                fn=fn,
            )
        )
    except Exception as exc:
        operations.publish(source, f"{source}: could not be scheduled: {exc}")
        return False
    if started:
        return True
    is_running = getattr(bg_tasks, "is_running", None)
    return bool(callable(is_running) and is_running(key))
