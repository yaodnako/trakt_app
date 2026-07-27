(() => {
    if (!window.fetch) return;
    const watchOverlay = document.getElementById("history-watch-overlay");
    const watchBody = document.getElementById("history-watch-body");
    const watchTitle = document.getElementById("history-watch-title");
    const dateOverlay = document.getElementById("history-watch-date-overlay");
    const customInput = dateOverlay?.querySelector("[data-history-watch-custom-input]");
    let activeTrigger = null;
    let activePanelUrl = "";
    let pendingAction = null;
    let watchRefreshToken = 0;
    let watchLoadController = null;
    let watchRefreshController = null;
    let watchRefreshTimer = 0;
    let mappingRefreshAttempts = 0;

    function cancelWatchPanelRequests() {
        watchLoadController?.abort();
        watchRefreshController?.abort();
        watchLoadController = null;
        watchRefreshController = null;
        if (watchRefreshTimer) window.clearTimeout(watchRefreshTimer);
        watchRefreshTimer = 0;
    }

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

    function scheduleMappingRefresh(token) {
        if (watchRefreshTimer) window.clearTimeout(watchRefreshTimer);
        const mappingPending = watchBody?.querySelector("[data-search-watch-panel]")?.dataset.imdbMappingPending === "1";
        if (!mappingPending || mappingRefreshAttempts >= 8) return;
        watchRefreshTimer = window.setTimeout(() => {
            if (token !== watchRefreshToken || watchOverlay?.hidden) return;
            mappingRefreshAttempts += 1;
            loadWatchPanel("", {preserve: true});
        }, 900);
    }

    async function refreshWatchPanel(requestUrl, token, {state = null, focusDefault = false} = {}) {
        if (token !== watchRefreshToken || watchOverlay?.hidden) return;
        if (watchRefreshTimer) window.clearTimeout(watchRefreshTimer);
        watchRefreshTimer = 0;
        if (!window.traktShowWatchPanel?.needsRefresh(watchBody)) {
            scheduleMappingRefresh(token);
            return;
        }
        watchRefreshController?.abort();
        const controller = new AbortController();
        watchRefreshController = controller;
        const panelWasPending = Boolean(watchBody?.querySelector("[data-watch-panel-pending='1']"));
        const refreshUrl = new URL(requestUrl, window.location.href);
        refreshUrl.searchParams.set("refresh", "1");
        if (!panelWasPending) refreshUrl.searchParams.set("artwork_patch", "1");
        let needsArtworkFollowup = false;
        try {
            const response = await fetch(refreshUrl.toString(), {
                headers: {"Accept": "text/html"}, cache: "no-store", signal: controller.signal,
            });
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
            if (watchRefreshController === controller) watchRefreshController = null;
            if (token === watchRefreshToken && !watchOverlay?.hidden) {
                if (needsArtworkFollowup) {
                    void refreshWatchPanel(requestUrl, token, {state, focusDefault});
                } else {
                    scheduleMappingRefresh(token);
                }
            }
        }
    }

    async function loadWatchPanel(panelUrl = "", {preserve = false, focusDefault = false} = {}) {
        const requestUrl = panelUrl || activePanelUrl;
        if (!requestUrl || !watchBody) return;
        if (watchRefreshTimer) window.clearTimeout(watchRefreshTimer);
        watchRefreshTimer = 0;
        const token = watchRefreshToken;
        const state = preserve ? window.traktShowWatchPanel?.captureState(watchBody) : null;
        if (!preserve) watchBody.innerHTML = '<div class="title-matrix-loading-shell"><div class="title-matrix-loading-bar is-wide"></div></div>';
        watchLoadController?.abort();
        watchLoadController = new AbortController();
        try {
            const response = await fetch(requestUrl, {
                headers: {"Accept": "text/html"}, cache: "no-store", signal: watchLoadController.signal,
            });
            if (!response.ok) throw new Error("Could not load episodes.");
            if (token !== watchRefreshToken) return;
            watchBody.innerHTML = await response.text();
            if (state) window.traktShowWatchPanel?.restoreState(watchBody, state);
            else if (focusDefault) window.traktShowWatchPanel?.focusDefaultEpisode(watchBody);
            window.traktShowWatchPanel?.configureScopeActions(watchOverlay, activeTrigger, watchBody);
            await refreshWatchPanel(requestUrl, token, {state, focusDefault});
        } catch (error) {
            if (token === watchRefreshToken && error.name !== "AbortError") {
                const errorNode = document.createElement("div");
                errorNode.className = "title-matrix-empty-state";
                const message = document.createElement("p");
                message.textContent = error.message || "Could not load episodes.";
                errorNode.append(message);
                watchBody.replaceChildren(errorNode);
            }
        }
    }

    function openWatchPanel(trigger) {
        activeTrigger = trigger;
        activePanelUrl = trigger.dataset.watchPanelUrl || "";
        watchRefreshToken += 1;
        mappingRefreshAttempts = 0;
        cancelWatchPanelRequests();
        if (watchTitle) watchTitle.textContent = trigger.dataset.title || "Episodes";
        window.traktShowWatchPanel?.configureTitleRatings(watchOverlay, null);
        window.traktShowWatchPanel?.configurePlayAction(watchOverlay, trigger);
        setOverlayOpen(watchOverlay, true);
        loadWatchPanel("", {focusDefault: true});
    }

    function actionFromElement(target) {
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

    async function submitWatch(dateMode) {
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

    document.addEventListener("click", (event) => {
        const trigger = event.target.closest("[data-history-show-watch-trigger]");
        if (trigger) {
            event.preventDefault();
            openWatchPanel(trigger);
            return;
        }
        const unwatchAction = event.target.closest("[data-search-unwatch-action]");
        if (unwatchAction) {
            event.preventDefault();
            let removedEntry = null;
            let removedParent = null;
            let removedNext = null;
            let hiddenTitleCard = null;
            window.traktShowWatchPanel?.unwatch(unwatchAction, {
                onRemoved: async () => {
                    if (watchOverlay?.contains(unwatchAction)) {
                        await loadWatchPanel("", {preserve: true});
                        return;
                    }
                    removedEntry = unwatchAction.closest("[data-history-entry-card]")
                        || unwatchAction.closest(".history-title-mode-card");
                    removedParent = removedEntry?.parentElement || null;
                    removedNext = removedEntry?.nextSibling || null;
                    hiddenTitleCard = removedEntry?.closest("[data-history-title-key]") || null;
                    removedEntry?.remove();
                    if (hiddenTitleCard && !hiddenTitleCard.querySelector("[data-history-entry-card]")) hiddenTitleCard.hidden = true;
                },
                onRestored: async () => {
                    if (watchOverlay && !watchOverlay.hidden) {
                        await loadWatchPanel("", {preserve: true});
                    } else if (removedEntry && removedParent) {
                        hiddenTitleCard && (hiddenTitleCard.hidden = false);
                        removedParent.insertBefore(removedEntry, removedNext && removedNext.parentElement === removedParent ? removedNext : null);
                    }
                },
            });
            return;
        }
        const action = event.target.closest("[data-search-watch-action]");
        if (action) {
            event.preventDefault();
            if (!action.disabled) {
                pendingAction = actionFromElement(action);
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
        if (event.target.closest("[data-history-watch-close]")) {
            event.preventDefault();
            watchRefreshToken += 1;
            cancelWatchPanelRequests();
            setOverlayOpen(watchOverlay, false);
            if (activeTrigger && document.contains(activeTrigger)) activeTrigger.focus();
            activeTrigger = null;
            return;
        }
        if (event.target.closest("[data-history-watch-date-close]")) {
            event.preventDefault();
            pendingAction = null;
            setOverlayOpen(dateOverlay, false);
            return;
        }
        const dateMode = event.target.closest("[data-history-watch-date-mode]");
        if (dateMode) {
            event.preventDefault();
            submitWatch(dateMode.dataset.historyWatchDateMode || "none");
        }
    });

    document.addEventListener("trakt:imdb-seasons-preference-changed", () => {
        if (!watchBody?.querySelector("[data-search-watch-panel]") || watchOverlay?.hidden) return;
        mappingRefreshAttempts = 0;
        cancelWatchPanelRequests();
        loadWatchPanel("", {preserve: true});
    });

    window.addEventListener("keydown", (event) => {
        if (event.key !== "Escape") return;
        if (dateOverlay && !dateOverlay.hidden) {
            pendingAction = null;
            setOverlayOpen(dateOverlay, false);
        } else if (watchOverlay && !watchOverlay.hidden) {
            watchRefreshToken += 1;
            cancelWatchPanelRequests();
            setOverlayOpen(watchOverlay, false);
        }
    });
})();
