(() => {
    const watchOverlay = document.getElementById("release-watch-overlay");
    const watchBody = document.getElementById("release-watch-body");
    const watchTitle = document.getElementById("release-watch-title");
    const dateOverlay = document.getElementById("release-watch-date-overlay");
    const customInput = dateOverlay?.querySelector("[data-release-watch-custom-input]");
    let activeTrigger = null;
    let activePanelUrl = "";
    let pendingAction = null;
    let watchRefreshToken = 0;
    let mappingRefreshTimer = 0;
    let mappingRefreshAttempts = 0;

    function setOverlayOpen(node, open) {
        if (!node) return;
        if (window.TraktDialogs) {
            if (open) {
                window.TraktDialogs.open(node, {
                    onEscape: () => {
                        setOverlayOpen(node, false);
                        if (node === dateOverlay) pendingAction = null;
                    },
                });
            } else {
                window.TraktDialogs.close(node);
            }
            return;
        }
        node.hidden = !open;
        node.classList.toggle("is-open", open);
        node.setAttribute("aria-hidden", open ? "false" : "true");
        if (window.traktSyncOverlayBodyLock) window.traktSyncOverlayBodyLock();
        else document.body.classList.toggle(
            "has-title-matrix-overlay",
            (watchOverlay && !watchOverlay.hidden) || (dateOverlay && !dateOverlay.hidden),
        );
    }

    async function refreshWatchPanel(requestUrl, token, {state = null, focusDefault = false} = {}) {
        if (mappingRefreshTimer) window.clearTimeout(mappingRefreshTimer);
        mappingRefreshTimer = 0;
        if (!window.traktShowWatchPanel?.needsRefresh(watchBody)) {
            scheduleMappingRefresh(token);
            return;
        }
        const panelWasPending = Boolean(watchBody?.querySelector("[data-watch-panel-pending='1']"));
        const refreshUrl = new URL(requestUrl, window.location.href);
        refreshUrl.searchParams.set("refresh", "1");
        if (!panelWasPending) refreshUrl.searchParams.set("artwork_patch", "1");
        let needsArtworkFollowup = false;
        try {
            const response = await fetch(refreshUrl.toString(), {headers: {"Accept": "text/html"}, cache: "no-store"});
            const html = await response.text();
            if (token !== watchRefreshToken) return;
            if (!response.ok && !panelWasPending) return;
            const replacedPanel = window.traktShowWatchPanel?.applyRefresh(watchBody, html);
            if (replacedPanel) {
                if (state) window.traktShowWatchPanel?.restoreState(watchBody, state);
                else if (focusDefault) window.traktShowWatchPanel?.focusDefaultEpisode(watchBody);
                window.traktShowWatchPanel?.configureScopeActions(watchOverlay, activeTrigger, watchBody);
            }
            needsArtworkFollowup = Boolean(
                replacedPanel && window.traktShowWatchPanel?.needsRefresh(watchBody)
            );
        } catch (_error) {
            // Keep the already rendered episode panel visible when artwork refresh fails.
        } finally {
            if (token === watchRefreshToken && !watchOverlay?.hidden) {
                if (needsArtworkFollowup) {
                    void refreshWatchPanel(requestUrl, token, {state, focusDefault});
                } else {
                    scheduleMappingRefresh(token);
                }
            }
        }
    }

    function scheduleMappingRefresh(token) {
        if (mappingRefreshTimer) window.clearTimeout(mappingRefreshTimer);
        mappingRefreshTimer = 0;
        const panel = watchBody?.querySelector("[data-search-watch-panel]");
        if (panel?.dataset.imdbMappingPending !== "1" || mappingRefreshAttempts >= 8) return;
        mappingRefreshTimer = window.setTimeout(() => {
            mappingRefreshTimer = 0;
            if (token !== watchRefreshToken || watchOverlay?.hidden) return;
            mappingRefreshAttempts += 1;
            loadWatchPanel("", {preserve: true});
        }, 900);
    }

    async function loadWatchPanel(panelUrl = "", {preserve = false, focusDefault = false} = {}) {
        const requestUrl = panelUrl || activePanelUrl;
        if (!requestUrl || !watchBody) return;
        if (mappingRefreshTimer) window.clearTimeout(mappingRefreshTimer);
        mappingRefreshTimer = 0;
        const token = watchRefreshToken;
        const state = preserve ? window.traktShowWatchPanel?.captureState(watchBody) : null;
        if (!preserve) watchBody.innerHTML = '<div class="title-matrix-loading-shell"><div class="title-matrix-loading-bar is-wide"></div></div>';
        try {
            const response = await fetch(requestUrl, {headers: {"Accept": "text/html"}, cache: "no-store"});
            if (token !== watchRefreshToken) return;
            watchBody.innerHTML = await response.text();
            if (state) window.traktShowWatchPanel?.restoreState(watchBody, state);
            else if (focusDefault) window.traktShowWatchPanel?.focusDefaultEpisode(watchBody);
            window.traktShowWatchPanel?.configureScopeActions(watchOverlay, activeTrigger, watchBody);
            await refreshWatchPanel(requestUrl, token, {state, focusDefault});
        } catch (_error) {
            if (token === watchRefreshToken) {
                watchBody.innerHTML = '<div class="title-matrix-empty-state"><p>Could not load episodes.</p></div>';
            }
        }
    }

    function openWatchPanel(trigger) {
        activeTrigger = trigger;
        activePanelUrl = trigger.dataset.watchPanelUrl || "";
        watchRefreshToken += 1;
        mappingRefreshAttempts = 0;
        if (mappingRefreshTimer) window.clearTimeout(mappingRefreshTimer);
        if (watchTitle) watchTitle.textContent = trigger.dataset.title || "Episodes";
        window.traktShowWatchPanel?.configureTitleRatings(watchOverlay, null);
        window.traktShowWatchPanel?.configurePlayAction(watchOverlay, trigger);
        setOverlayOpen(watchOverlay, true);
        loadWatchPanel("", {focusDefault: true});
    }

    function watchAction(target) {
        return {
            title_type: target.dataset.titleType || "show",
            trakt_id: Number(target.dataset.traktId || "0"),
            title: target.dataset.title || "",
            scope: target.dataset.scope || "episode",
            season: target.dataset.season || "",
            episode: target.dataset.episode || "",
            season_layout: target.dataset.seasonLayout || "trakt",
            remove_from_watchlist: target.dataset.removeFromWatchlist === "true",
        };
    }

    async function submitEpisodeWatch(dateMode) {
        if (!pendingAction) return;
        const payload = {...pendingAction, date_mode: dateMode};
        if (dateMode === "custom") payload.watched_at = customInput?.value || "";
        try {
            const response = await fetch("/search/watch", {
                method: "POST",
                headers: {"Accept": "application/json", "Content-Type": "application/json"},
                cache: "no-store",
                body: JSON.stringify(payload),
            });
            const result = await response.json();
            if (!response.ok || !result.ok) throw new Error(result.message || "Watch action failed.");
            const completedAction = pendingAction;
            pendingAction = null;
            setOverlayOpen(dateOverlay, false);
            window.pushFlashToast?.(result.message || "Marked watched.", 3600);
            await loadWatchPanel("", {preserve: true});
            if (completedAction.scope === "episode" && window.traktOpenRatingModal) {
                window.traktOpenRatingModal({
                    titleType: completedAction.title_type,
                    traktId: completedAction.trakt_id,
                    title: completedAction.title,
                    season: completedAction.season,
                    episode: completedAction.episode,
                });
            }
        } catch (error) {
            window.pushFlashToast?.(error.message || "Watch action failed.", 5200);
        }
    }

    async function post(url, payload) {
        const response = await fetch(url, {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(payload)});
        const result = await response.json();
        if (!response.ok || !result.ok) throw new Error(result.message || "Action failed");
        return result;
    }
    document.addEventListener("click", async (event) => {
        const showTrigger = event.target.closest("[data-release-show-watch-trigger]");
        if (showTrigger) {
            event.preventDefault();
            openWatchPanel(showTrigger);
            return;
        }
        const unwatchAction = event.target.closest("[data-search-unwatch-action]");
        if (unwatchAction) {
            event.preventDefault();
            window.traktShowWatchPanel?.unwatch(unwatchAction, {
                onRemoved: () => loadWatchPanel("", {preserve: true}),
                onRestored: () => loadWatchPanel("", {preserve: true}),
            });
            return;
        }
        const episodeAction = event.target.closest("[data-search-watch-action]");
        if (episodeAction) {
            event.preventDefault();
            if (!episodeAction.disabled) {
                pendingAction = watchAction(episodeAction);
                if (customInput) customInput.value = "";
                setOverlayOpen(dateOverlay, true);
            }
            return;
        }
        const seasonTab = event.target.closest("[data-search-watch-season-tab]");
        if (seasonTab && watchBody) {
            event.preventDefault();
            const season = seasonTab.dataset.searchWatchSeasonTab || "";
            window.traktShowWatchPanel?.selectSeason(watchBody, season);
            const selectedPanel = watchBody.querySelector(`[data-search-watch-season-panel="${season}"]`);
            if (selectedPanel?.querySelector(".search-watch-still[data-still-pending='1']") && activePanelUrl) {
                const separator = activePanelUrl.includes("?") ? "&" : "?";
                refreshWatchPanel(`${activePanelUrl}${separator}season=${encodeURIComponent(season)}`, watchRefreshToken);
            }
            return;
        }
        if (event.target.closest("[data-release-watch-close]")) {
            event.preventDefault();
            watchRefreshToken += 1;
            setOverlayOpen(watchOverlay, false);
            activeTrigger?.focus();
            activeTrigger = null;
            return;
        }
        if (event.target.closest("[data-release-watch-date-close]")) {
            event.preventDefault();
            pendingAction = null;
            setOverlayOpen(dateOverlay, false);
            return;
        }
        const dateMode = event.target.closest("[data-release-watch-date-mode]");
        if (dateMode) {
            event.preventDefault();
            submitEpisodeWatch(dateMode.dataset.releaseWatchDateMode || "none");
            return;
        }
        const toggle = event.target.closest("[data-release-toggle]");
        const acknowledge = event.target.closest("[data-release-acknowledge]");
        const watched = event.target.closest("[data-release-watched]");
        const button = toggle || acknowledge || watched;
        if (!button || button.disabled) return;
        button.disabled = true;
        try {
            if (toggle) {
                await post("/release-tracking/toggle", {title_type: button.dataset.titleType, trakt_id: Number(button.dataset.traktId), tracked: false});
                button.closest("[data-release-card]")?.remove();
            } else if (acknowledge) {
                const next = button.dataset.acknowledged !== "true";
                const result = await post("/release-tracking/acknowledge", {title_type: button.dataset.titleType, trakt_id: Number(button.dataset.traktId), acknowledged: next});
                button.dataset.acknowledged = result.acknowledged ? "true" : "false";
                button.classList.toggle("is-active", result.acknowledged);
                button.closest("[data-release-card]")?.classList.toggle("is-unacknowledged", !result.acknowledged);
                button.title = result.acknowledged ? "Resume notifications" : "Acknowledge release";
            } else {
                await post("/search/watch", {title_type: button.dataset.titleType, trakt_id: Number(button.dataset.traktId), title: button.dataset.title, scope: "title"});
                button.closest("[data-release-card]")?.remove();
            }
            window.pushFlashToast?.("Releases updated.", 2200);
        } catch (error) {
            window.pushFlashToast?.(error.message || "Action failed", 3200);
        } finally {
            button.disabled = false;
        }
    });
    document.addEventListener("trakt:imdb-seasons-preference-changed", () => {
        if (!watchBody?.querySelector("[data-search-watch-panel]") || watchOverlay?.hidden) return;
        mappingRefreshAttempts = 0;
        if (mappingRefreshTimer) window.clearTimeout(mappingRefreshTimer);
        mappingRefreshTimer = 0;
        loadWatchPanel("", {preserve: true});
    });
    window.addEventListener("keydown", (event) => {
        if (event.key !== "Escape") return;
        if (dateOverlay && !dateOverlay.hidden) {
            pendingAction = null;
            setOverlayOpen(dateOverlay, false);
        } else if (watchOverlay && !watchOverlay.hidden) {
            setOverlayOpen(watchOverlay, false);
        }
    });
})();
