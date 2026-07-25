(() => {
    function payloadFromElement(target) {
        return {
            title_type: target.dataset.titleType || "show",
            trakt_id: Number(target.dataset.traktId || "0"),
            title: target.dataset.title || "",
            scope: target.dataset.scope || "episode",
            season: target.dataset.season || "",
            episode: target.dataset.episode || "",
        };
    }

    async function postJson(url, payload) {
        const response = await fetch(url, {
            method: "POST",
            headers: {"Accept": "application/json", "Content-Type": "application/json"},
            cache: "no-store",
            body: JSON.stringify(payload),
        });
        const result = await response.json();
        if (!response.ok || !result.ok) throw new Error(result.message || "Action failed.");
        return result;
    }

    function showUndoToast(restore, onUndo) {
        const stack = document.getElementById("web-flash-stack");
        if (!stack) return;
        const toast = document.createElement("div");
        toast.className = "web-flash web-flash-action";
        const message = document.createElement("span");
        message.textContent = "Watch removed";
        const undo = document.createElement("button");
        undo.type = "button";
        undo.className = "web-flash-action-button";
        undo.textContent = "Undo";
        let closed = false;
        const close = () => {
            if (closed) return;
            closed = true;
            toast.classList.add("is-leaving");
            window.setTimeout(() => toast.remove(), 320);
        };
        undo.addEventListener("click", async () => {
            undo.disabled = true;
            try {
                await postJson("/search/restore-watch", {restore});
                close();
                window.pushFlashToast?.("Watch restored.", 3200);
                await onUndo?.();
            } catch (error) {
                undo.disabled = false;
                window.pushFlashToast?.(error.message || "Could not restore the watch.", 5200);
            }
        });
        toast.append(message, undo);
        stack.appendChild(toast);
        window.setTimeout(close, 8000);
    }

    async function confirmUnwatch(payload) {
        if (payload.scope === "episode") return true;
        const title = payload.title ? `“${payload.title}”` : "this title";
        if (payload.title_type === "movie") {
            return window.traktConfirm?.({
                title: "Remove movie watch?",
                message: `All watches for ${title} will be removed from History.`,
                confirmLabel: "Remove watch",
            }) || false;
        }
        if (payload.scope === "season") {
            return window.traktConfirm?.({
                title: `Remove watches from S${payload.season}?`,
                message: `All watches from this season of ${title} will be removed from History.`,
                confirmLabel: "Remove season",
            }) || false;
        }
        return window.traktConfirm?.({
            title: "Remove all series watches?",
            message: `All watches from every season of ${title} will be removed from History.`,
            confirmLabel: "Remove all",
        }) || false;
    }

    async function unwatch(target, callbacks = {}) {
        const payload = payloadFromElement(target);
        if (!(await confirmUnwatch(payload))) return null;
        target.disabled = true;
        try {
            const result = await postJson("/search/unwatch", payload);
            await callbacks.onRemoved?.(result.restore, target, result);
            showUndoToast(result.restore, callbacks.onRestored);
            return result;
        } catch (error) {
            target.disabled = false;
            window.pushFlashToast?.(error.message || "Could not remove the watch.", 5200);
            return null;
        }
    }

    function patchArtwork(body, html) {
        if (!body) return;
        const scrollTop = body.scrollTop;
        const scrollLeft = body.scrollLeft;
        const parsed = document.createElement("div");
        parsed.innerHTML = html;
        parsed.querySelectorAll(".search-watch-still[data-episode-key]").forEach((fresh) => {
            const key = fresh.dataset.episodeKey || "";
            const current = Array.from(body.querySelectorAll(".search-watch-still[data-episode-key]"))
                .find((node) => node.dataset.episodeKey === key);
            if (!current) return;
            current.replaceChildren(...Array.from(fresh.childNodes).map((node) => node.cloneNode(true)));
            if (fresh.dataset.stillPending === "1") current.dataset.stillPending = "1";
            else current.removeAttribute("data-still-pending");
        });
        body.scrollTop = scrollTop;
        body.scrollLeft = scrollLeft;
    }

    function captureState(body) {
        const active = body?.querySelector("[data-search-watch-season-tab].is-active");
        const focused = document.activeElement?.closest?.("[data-episode-key]");
        return {
            season: active?.dataset.searchWatchSeasonTab || "",
            scrollTop: body?.scrollTop || 0,
            focusKey: focused?.dataset.episodeKey || "",
        };
    }

    function restoreState(body, state) {
        if (!body || !state) return;
        if (state.season) selectSeason(body, state.season);
        body.scrollTop = state.scrollTop;
        if (state.focusKey) {
            const target = Array.from(body.querySelectorAll("button[data-episode-key], a[data-episode-key]"))
                .find((node) => node.dataset.episodeKey === state.focusKey);
            target?.focus?.({preventScroll: true});
        }
    }

    function selectSeason(body, season) {
        body.querySelectorAll("[data-search-watch-season-tab]").forEach((tab) => {
            const active = tab.dataset.searchWatchSeasonTab === String(season);
            tab.classList.toggle("is-active", active);
            tab.setAttribute("aria-selected", active ? "true" : "false");
            tab.tabIndex = active ? 0 : -1;
        });
        body.querySelectorAll("[data-search-watch-season-panel]").forEach((panel) => {
            const active = panel.dataset.searchWatchSeasonPanel === String(season);
            panel.classList.toggle("is-hidden", !active);
            panel.hidden = !active;
        });
        body.querySelectorAll("[data-search-watch-season-action]").forEach((button) => {
            button.classList.toggle("is-hidden", button.dataset.searchWatchSeasonAction !== String(season));
        });
    }

    function focusDefaultEpisode(body) {
        const panel = body?.querySelector("[data-search-watch-panel]");
        const key = panel?.dataset.defaultEpisodeKey || "";
        if (!key) return;
        const target = Array.from(body.querySelectorAll(".search-watch-episode-card[data-episode-key]"))
            .find((node) => node.dataset.episodeKey === key);
        if (!target) return;
        const bodyRect = body.getBoundingClientRect();
        const targetRect = target.getBoundingClientRect();
        body.scrollTop = Math.max(0, body.scrollTop + targetRect.top - bodyRect.top);
    }

    function focusUnwatchScope(body, overlay = null) {
        const activeSeason = body?.querySelector("[data-search-watch-season-tab].is-active")?.dataset.searchWatchSeasonTab || "";
        const seasonAction = Array.from(body?.querySelectorAll("[data-search-unwatch-action][data-scope='season']") || [])
            .find((node) => node.dataset.season === activeSeason && !node.classList.contains("is-hidden"));
        const target = seasonAction || overlay?.querySelector("[data-search-watch-header-unwatch]");
        target?.focus?.({preventScroll: true});
    }

    function configureTitleRatings(overlay, body) {
        const action = overlay?.querySelector?.("[data-search-watch-title-ratings]");
        if (!(action instanceof HTMLButtonElement)) return;
        const panel = body?.querySelector?.("[data-search-watch-panel]");
        const traktId = Number(panel?.dataset.searchWatchTraktId || "0");
        const title = panel?.dataset.searchWatchTitle || "";
        action.hidden = !traktId;
        if (!traktId) return;
        action.dataset.titleMatrixTitle = title;
        action.dataset.titleMatrixTraktId = String(traktId);
        action.dataset.titleMatrixUrl = `/titles/show/${traktId}/episode-ratings-matrix`;
        action.setAttribute("aria-label", `Open episode ratings matrix for ${title || "this series"}`);
        const traktRating = action.querySelector("[data-search-watch-trakt-rating]");
        const imdbRating = action.querySelector("[data-search-watch-imdb-rating]");
        if (traktRating) traktRating.textContent = panel.dataset.searchWatchTraktRating || "Loading";
        if (imdbRating) imdbRating.textContent = panel.dataset.searchWatchImdbRating || "Loading";
    }

    function configureScopeActions(overlay, trigger, body) {
        configureTitleRatings(overlay, body);
        const panel = body?.querySelector?.("[data-search-watch-panel]");
        const traktId = Number(panel?.dataset.searchWatchTraktId || trigger?.dataset?.traktId || "0");
        const title = panel?.dataset.searchWatchTitle || trigger?.dataset?.title || "";
        const watchedCount = Number(panel?.dataset.searchWatchWatchedCount || "0");
        const releasedCount = Number(panel?.dataset.searchWatchReleasedCount || "0");
        const releasedWatchedCount = Number(panel?.dataset.searchWatchReleasedWatchedCount || "0");
        const mark = overlay?.querySelector?.("[data-search-watch-header-mark]");
        const unwatch = overlay?.querySelector?.("[data-search-watch-header-unwatch]");
        [mark, unwatch].forEach((action) => {
            if (!(action instanceof HTMLButtonElement)) return;
            action.dataset.traktId = String(traktId);
            action.dataset.title = title;
        });
        if (mark instanceof HTMLButtonElement) {
            mark.dataset.removeFromWatchlist = trigger?.dataset?.removeFromWatchlist || "false";
            mark.hidden = !traktId || releasedCount <= releasedWatchedCount;
            mark.title = `Mark all released episodes of ${title || "this series"} watched`;
            mark.setAttribute("aria-label", mark.title);
        }
        if (unwatch instanceof HTMLButtonElement) {
            unwatch.hidden = !traktId || watchedCount <= 0;
            unwatch.title = `Remove all watched history for ${title || "this series"}`;
            unwatch.setAttribute("aria-label", unwatch.title);
        }
    }

    function configurePlayAction(overlay, trigger) {
        const action = overlay?.querySelector?.("[data-show-watch-play]");
        if (!(action instanceof HTMLAnchorElement)) return;
        const panelUrl = trigger?.dataset?.watchPanelUrl || "";
        const panelUrlMatch = panelUrl.match(/\/search\/show\/(\d+)\/watch-panel/);
        const traktId = Number(trigger?.dataset?.traktId || panelUrlMatch?.[1] || "0");
        const title = trigger?.dataset?.title || "";
        action.hidden = !traktId;
        if (!traktId) {
            action.removeAttribute("href");
            return;
        }
        action.href = `/search/show/${traktId}/play?title=${encodeURIComponent(title)}`;
        action.title = `Play ${title || "show"}`;
        action.setAttribute("aria-label", action.title);
    }

    document.addEventListener("keydown", (event) => {
        const tab = event.target.closest("[data-search-watch-season-tab]");
        if (!tab || !["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
        const tablist = tab.closest("[role='tablist']");
        const body = tab.closest("[data-search-watch-panel]")?.parentElement;
        const tabs = Array.from(tablist?.querySelectorAll("[data-search-watch-season-tab]") || []);
        if (!body || !tabs.length) return;
        event.preventDefault();
        const current = Math.max(0, tabs.indexOf(tab));
        const next = event.key === "Home"
            ? 0
            : event.key === "End"
                ? tabs.length - 1
                : (current + (event.key === "ArrowRight" ? 1 : -1) + tabs.length) % tabs.length;
        const target = tabs[next];
        selectSeason(body, target.dataset.searchWatchSeasonTab || "");
        target.focus();
    });

    window.traktShowWatchPanel = {
        captureState,
        configurePlayAction,
        configureScopeActions,
        configureTitleRatings,
        focusDefaultEpisode,
        focusUnwatchScope,
        patchArtwork,
        payloadFromElement,
        restoreState,
        selectSeason,
        unwatch,
    };
})();
