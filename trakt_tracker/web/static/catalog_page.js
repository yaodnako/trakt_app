(() => {
    const runtimeIcon = (name) => document.querySelector(`meta[name="${name}"]`)?.content || "";
    if (!window.fetch) {
        return;
    }
    const watchOverlay = document.getElementById("search-watch-overlay");
    const watchBody = document.getElementById("search-watch-body");
    const watchTitle = document.getElementById("search-watch-title");
    const dateOverlay = document.getElementById("search-watch-date-overlay");
    const customInput = dateOverlay ? dateOverlay.querySelector("[data-search-watch-custom-input]") : null;
    let activeTrigger = null;
    let activePanelUrl = "";
    let pendingAction = null;
    let watchRefreshToken = 0;

    function openOverlay(node) {
        if (!node) {
            return;
        }
        if (window.TraktDialogs) {
            window.TraktDialogs.open(node, {
                onEscape: () => {
                    closeOverlay(node);
                    if (node === dateOverlay) pendingAction = null;
                },
            });
            return;
        }
        node.hidden = false;
        node.classList.add("is-open");
        node.setAttribute("aria-hidden", "false");
        if (window.traktSyncOverlayBodyLock) {
            window.traktSyncOverlayBodyLock();
        } else {
            document.body.classList.add("has-title-matrix-overlay");
        }
    }

    function closeOverlay(node) {
        if (!node) {
            return;
        }
        if (window.TraktDialogs) {
            window.TraktDialogs.close(node);
            return;
        }
        node.hidden = true;
        node.classList.remove("is-open");
        node.setAttribute("aria-hidden", "true");
        if (window.traktSyncOverlayBodyLock) {
            window.traktSyncOverlayBodyLock();
        } else if ((!watchOverlay || watchOverlay.hidden) && (!dateOverlay || dateOverlay.hidden)) {
            document.body.classList.remove("has-title-matrix-overlay");
        }
    }

    function renderWatchLoading() {
        if (!watchBody) {
            return;
        }
        watchBody.innerHTML = `
            <div class="title-matrix-loading-shell">
                <div class="title-matrix-loading-bar is-wide"></div>
                <div class="title-matrix-loading-grid">
                    <span></span><span></span><span></span><span></span>
                    <span></span><span></span><span></span><span></span>
                </div>
            </div>
        `;
    }

    async function refreshWatchPanel(requestUrl, token) {
        const selectedSeason = watchBody?.querySelector(".search-watch-episode-grid:not(.is-hidden)");
        if (!selectedSeason?.querySelector(".search-watch-still[data-still-pending='1']")) {
            return;
        }
        const refreshUrl = new URL(requestUrl, window.location.href);
        refreshUrl.searchParams.set("refresh", "1");
        try {
            const response = await fetch(refreshUrl.toString(), {
                headers: {"Accept": "text/html"},
                cache: "no-store",
            });
            if (!response.ok || token !== watchRefreshToken) {
                return;
            }
            window.traktShowWatchPanel?.patchArtwork(watchBody, await response.text());
        } catch (_error) {
            // Keep the already rendered episode panel visible when artwork refresh fails.
        }
    }

    async function loadWatchPanel(panelUrl, {preserve = false, focusDefault = false} = {}) {
        const requestUrl = panelUrl || activePanelUrl;
        if (!requestUrl || !watchBody) {
            return;
        }
        const token = watchRefreshToken;
        const state = preserve ? window.traktShowWatchPanel?.captureState(watchBody) : null;
        if (!preserve) renderWatchLoading();
        try {
            const response = await fetch(requestUrl, {
                headers: {"Accept": "text/html"},
                cache: "no-store",
            });
            if (token !== watchRefreshToken) {
                return;
            }
            watchBody.innerHTML = await response.text();
            if (state) window.traktShowWatchPanel?.restoreState(watchBody, state);
            else if (focusDefault) window.traktShowWatchPanel?.focusDefaultEpisode(watchBody);
            window.traktShowWatchPanel?.configureScopeActions(watchOverlay, activeTrigger, watchBody);
            refreshWatchPanel(requestUrl, token);
        } catch (_error) {
            if (token === watchRefreshToken) {
                watchBody.innerHTML = `<div class="title-matrix-empty-state"><p>Could not load episodes.</p></div>`;
            }
        }
    }

    async function openWatchPanel(trigger) {
        activeTrigger = trigger;
        activePanelUrl = trigger.dataset.watchPanelUrl || "";
        watchRefreshToken += 1;
        if (watchTitle) {
            watchTitle.textContent = trigger.dataset.title || "Episodes";
        }
        window.traktShowWatchPanel?.configurePlayAction(watchOverlay, trigger);
        openOverlay(watchOverlay);
        await loadWatchPanel("", {focusDefault: trigger.dataset.unwatchFocus !== "true"});
        if (trigger.dataset.unwatchFocus === "true") {
            window.traktShowWatchPanel?.focusUnwatchScope(watchBody, watchOverlay);
        }
    }

    function catalogCardForAction(action) {
        if (!action) return null;
        if (action.closest?.(".search-result-card")) return action.closest(".search-result-card");
        const traktId = action.dataset.traktId || "";
        return Array.from(document.querySelectorAll(".search-result-card")).find((card) => (
            card.querySelector(".search-watch-title-button")?.dataset.traktId === traktId
        )) || null;
    }

    function setCatalogCardWatched(card, watched) {
        if (!card) return;
        const overlay = card.querySelector(".catalog-seen-overlay");
        if (overlay) overlay.hidden = !watched;
        const button = card.querySelector(".search-watch-title-button");
        if (!button) return;
        const titleType = button.dataset.titleType || "movie";
        const title = button.dataset.title || "";
        const traktId = button.dataset.traktId || "";
        button.removeAttribute("data-search-watch-action");
        button.removeAttribute("data-search-unwatch-action");
        button.removeAttribute("data-search-show-watch-trigger");
        button.removeAttribute("data-unwatch-focus");
        button.removeAttribute("data-scope");
        if (watched && titleType === "show") {
            button.dataset.searchShowWatchTrigger = "";
            button.dataset.watchPanelUrl = `/search/show/${traktId}/watch-panel`;
            button.dataset.unwatchFocus = "true";
            button.title = "Choose watched history to remove";
            button.setAttribute("aria-label", `Choose watched history to remove for ${title}`);
        } else if (watched) {
            button.dataset.searchUnwatchAction = "";
            button.dataset.scope = "title";
            button.title = "Remove from watched history";
            button.setAttribute("aria-label", `Remove ${title} from watched history`);
        } else {
            button.dataset.searchWatchAction = "";
            button.dataset.scope = "title";
            button.title = "Mark watched";
            button.setAttribute("aria-label", `Mark ${title} watched`);
        }
        if (watched) {
            const glyphs = document.createElement("span");
            glyphs.className = "search-watch-action-glyphs";
            glyphs.setAttribute("aria-hidden", "true");
            const seen = document.createElement("img");
            seen.className = "icon-glyph icon-glyph-seen";
            seen.src = runtimeIcon("trakt-icon-seen");
            seen.alt = "";
            const cancel = document.createElement("img");
            cancel.className = "icon-glyph icon-glyph-cancel";
            cancel.src = runtimeIcon("trakt-icon-cancel");
            cancel.alt = "";
            glyphs.append(seen, cancel);
            button.replaceChildren(glyphs);
        } else {
            const check = document.createElement("img");
            check.className = "icon-glyph icon-glyph-check";
            check.src = runtimeIcon("trakt-icon-watched");
            check.alt = "";
            check.setAttribute("aria-hidden", "true");
            button.replaceChildren(check);
        }
    }

    function actionFromElement(target) {
        return {
            title_type: target.dataset.titleType || "movie",
            trakt_id: Number(target.dataset.traktId || "0"),
            title: target.dataset.title || "",
            scope: target.dataset.scope || "title",
            season: target.dataset.season || "",
            episode: target.dataset.episode || "",
            remove_from_watchlist: target.dataset.removeFromWatchlist === "true",
        };
    }

    function openDatePrompt(action) {
        pendingAction = action;
        if (customInput) {
            customInput.value = "";
        }
        openOverlay(dateOverlay);
    }

    async function submitWatch(dateMode) {
        if (!pendingAction) {
            return;
        }
        const payload = {...pendingAction, date_mode: dateMode};
        if (dateMode === "custom") {
            payload.watched_at = customInput ? customInput.value : "";
        }
        try {
            const response = await fetch("/search/watch", {
                method: "POST",
                headers: {
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                },
                cache: "no-store",
                body: JSON.stringify(payload),
            });
            const result = await response.json();
            if (!response.ok || !result.ok) {
                if (window.pushFlashToast) {
                    window.pushFlashToast(result.message || "Watch action failed.", 5200);
                }
                return;
            }
            if (window.pushFlashToast) {
                window.pushFlashToast(result.message || "Marked watched.", 3600);
            }
            const completedAction = pendingAction;
            if (result.removed_from_watchlist && completedAction) {
                const watchedButton = document.querySelector(`[data-search-watch-action][data-trakt-id="${completedAction.trakt_id}"]`);
                watchedButton?.closest(".search-result-card")?.remove();
            }
            closeOverlay(dateOverlay);
            pendingAction = null;
            if (watchOverlay && !watchOverlay.hidden && activePanelUrl) {
                await loadWatchPanel("", {preserve: true});
            }
            if (
                completedAction
                && window.traktOpenRatingModal
                && (
                    (completedAction.title_type === "movie" && completedAction.scope === "title")
                    || (completedAction.title_type === "show" && completedAction.scope === "episode")
                )
            ) {
                window.traktOpenRatingModal({
                    titleType: completedAction.title_type,
                    traktId: completedAction.trakt_id,
                    title: completedAction.title,
                    season: completedAction.season,
                    episode: completedAction.episode,
                });
            }
        } catch (_error) {
            if (window.pushFlashToast) {
                window.pushFlashToast("Watch action failed.", 5200);
            }
        }
    }

    async function toggleWatchlist(button) {
        const nextState = button.dataset.watchlisted !== "true";
        button.disabled = true;
        try {
            const response = await fetch("/watchlist/toggle", {
                method: "POST",
                headers: {"Accept": "application/json", "Content-Type": "application/json"},
                cache: "no-store",
                body: JSON.stringify({
                    title_type: button.dataset.titleType || "",
                    trakt_id: Number(button.dataset.traktId || "0"),
                    watchlisted: nextState,
                }),
            });
            const result = await response.json();
            if (!response.ok || !result.ok) {
                throw new Error(result.message || "Watchlist action failed.");
            }
            button.dataset.watchlisted = result.watchlisted ? "true" : "false";
            button.classList.toggle("is-active", Boolean(result.watchlisted));
            button.setAttribute("aria-pressed", result.watchlisted ? "true" : "false");
            button.title = result.watchlisted ? "Remove from watchlist" : "Add to watchlist";
            button.setAttribute("aria-label", result.watchlisted ? "Remove from watchlist" : "Add to watchlist");
            const icon = button.querySelector(".icon-glyph-bookmark");
            if (icon) {
                icon.src = result.watchlisted ? icon.dataset.filledSrc : icon.dataset.unfilledSrc;
                icon.classList.toggle("is-filled", Boolean(result.watchlisted));
                icon.classList.toggle("is-unfilled", !result.watchlisted);
            }
            if (!result.watchlisted && document.querySelector("#catalog-results-region")?.dataset.catalogPageKind === "watchlist") {
                button.closest(".search-result-card")?.remove();
            }
            if (window.pushFlashToast) {
                window.pushFlashToast(result.message, 3600);
            }
        } catch (error) {
            if (window.pushFlashToast) {
                window.pushFlashToast(error.message || "Watchlist action failed.", 5200);
            }
        } finally {
            button.disabled = false;
        }
    }

    async function toggleReleaseTracking(button) {
        const nextState = button.dataset.tracked !== "true";
        button.disabled = true;
        try {
            const response = await fetch("/release-tracking/toggle", {
                method: "POST",
                headers: {"Accept": "application/json", "Content-Type": "application/json"},
                cache: "no-store",
                body: JSON.stringify({
                    title_type: button.dataset.titleType || "",
                    trakt_id: Number(button.dataset.traktId || "0"),
                    tracked: nextState,
                    list_count: Number(button.dataset.listCount || "0") || null,
                }),
            });
            const result = await response.json();
            if (!response.ok || !result.ok) throw new Error(result.message || "Release tracking action failed.");
            button.dataset.tracked = result.tracked ? "true" : "false";
            button.classList.toggle("is-active", Boolean(result.tracked));
            button.setAttribute("aria-pressed", result.tracked ? "true" : "false");
            button.title = result.tracked ? "Stop release tracking" : "Track release";
            const icon = button.querySelector("img");
            if (icon) icon.src = result.tracked ? icon.dataset.onSrc : icon.dataset.offSrc;
            window.pushFlashToast?.(result.message, 3600);
        } catch (error) {
            window.pushFlashToast?.(error.message || "Release tracking action failed.", 5200);
        } finally {
            button.disabled = false;
        }
    }

    document.addEventListener("click", (event) => {
        const showTrigger = event.target.closest("[data-search-show-watch-trigger]");
        if (showTrigger) {
            event.preventDefault();
            openWatchPanel(showTrigger);
            return;
        }
        const watchlistTarget = event.target.closest("[data-watchlist-toggle]");
        if (watchlistTarget) {
            event.preventDefault();
            if (!watchlistTarget.disabled) {
                toggleWatchlist(watchlistTarget);
            }
            return;
        }
        const releaseTarget = event.target.closest("[data-release-toggle]");
        if (releaseTarget) {
            event.preventDefault();
            if (!releaseTarget.disabled) toggleReleaseTracking(releaseTarget);
            return;
        }
        const unwatchTarget = event.target.closest("[data-search-unwatch-action]");
        if (unwatchTarget) {
            event.preventDefault();
            let changedCard = null;
            window.traktShowWatchPanel?.unwatch(unwatchTarget, {
                onRemoved: async (_restore, target, result) => {
                    if (!result.still_watched) {
                        changedCard = catalogCardForAction(target);
                        setCatalogCardWatched(changedCard, false);
                    }
                    if (watchOverlay && !watchOverlay.hidden) {
                        await loadWatchPanel("", {preserve: true});
                    }
                },
                onRestored: async () => {
                    setCatalogCardWatched(changedCard, true);
                    if (watchOverlay && !watchOverlay.hidden) {
                        await loadWatchPanel("", {preserve: true});
                    }
                },
            });
            return;
        }
        const actionTarget = event.target.closest("[data-search-watch-action]");
        if (actionTarget) {
            event.preventDefault();
            if (actionTarget.disabled) {
                return;
            }
            openDatePrompt(actionFromElement(actionTarget));
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
        if (event.target.closest("[data-search-watch-close]")) {
            event.preventDefault();
            watchRefreshToken += 1;
            closeOverlay(watchOverlay);
            if (activeTrigger && document.contains(activeTrigger)) {
                activeTrigger.focus();
            }
            activeTrigger = null;
            return;
        }
        if (event.target.closest("[data-search-watch-date-close]")) {
            event.preventDefault();
            closeOverlay(dateOverlay);
            pendingAction = null;
            return;
        }
        const dateModeTarget = event.target.closest("[data-search-watch-date-mode]");
        if (dateModeTarget) {
            event.preventDefault();
            submitWatch(dateModeTarget.dataset.searchWatchDateMode || "none");
        }
    });

    window.addEventListener("keydown", (event) => {
        if (event.key !== "Escape") {
            return;
        }
        if (dateOverlay && !dateOverlay.hidden) {
            closeOverlay(dateOverlay);
            pendingAction = null;
            return;
        }
        if (watchOverlay && !watchOverlay.hidden) {
            closeOverlay(watchOverlay);
        }
    });
})();

(() => {
    const catalogPaths = new Set(["/search", "/explore", "/watchlist"]);
    let navigationRequest = null;

    function catalogUrl(url) {
        const target = new URL(url, window.location.href);
        if (target.origin !== window.location.origin || !catalogPaths.has(target.pathname)) return null;
        target.searchParams.delete("catalog_shell");
        return target;
    }

    function setActiveNavigation(pathname) {
        document.querySelectorAll(".nav a[href]").forEach((link) => {
            const active = new URL(link.href, window.location.href).pathname === pathname;
            link.classList.toggle("active", active);
            if (active) link.setAttribute("aria-current", "page");
            else link.removeAttribute("aria-current");
        });
    }

    async function navigateCatalog(target, {push = true} = {}) {
        navigationRequest?.abort();
        const controller = new AbortController();
        navigationRequest = controller;
        const region = document.querySelector("#catalog-page-region");
        region?.setAttribute("aria-busy", "true");
        setActiveNavigation(target.pathname);
        try {
            const response = await fetch(target.toString(), {
                headers: {"Accept": "text/html"},
                cache: "no-store",
                signal: controller.signal,
            });
            if (!response.ok) throw new Error(`Catalog request failed: ${response.status}`);
            const html = await response.text();
            const parsed = new DOMParser().parseFromString(html, "text/html");
            const incoming = parsed.querySelector("#catalog-page-region");
            const current = document.querySelector("#catalog-page-region");
            const incomingNav = parsed.querySelector(".nav");
            const currentNav = document.querySelector(".nav");
            if (!incoming || !current) throw new Error("Catalog response is incomplete");
            current.replaceWith(incoming);
            if (incomingNav && currentNav) currentNav.replaceWith(incomingNav);
            if (push) history.pushState(null, "", target.toString());
            document.title = parsed.title || document.title;
        } catch (error) {
            if (error?.name === "AbortError") return;
            setActiveNavigation(window.location.pathname);
            const current = document.querySelector("#catalog-results-region");
            if (current) current.innerHTML = '<section class="banner error">Could not load catalog results.</section>';
        } finally {
            if (navigationRequest === controller) {
                document.querySelector("#catalog-page-region")?.removeAttribute("aria-busy");
                navigationRequest = null;
            }
        }
    }

    document.addEventListener("submit", (event) => {
        const form = event.target;
        if (!(form instanceof HTMLFormElement)) return;
        const target = catalogUrl(form.action || window.location.href);
        if (!target) return;
        event.preventDefault();
        target.search = "";
        for (const [key, value] of new FormData(form).entries()) target.searchParams.append(key, String(value));
        navigateCatalog(target);
    });
    document.addEventListener("click", (event) => {
        const link = event.target.closest("a[href]");
        if (!link || link.target || event.defaultPrevented) return;
        const target = catalogUrl(link.href);
        if (!target) return;
        event.preventDefault();
        navigateCatalog(target);
    });
    const catalogRegion = document.querySelector("#catalog-results-region");
    if (catalogRegion?.dataset.catalogLoading === "1") {
        const cleanUrl = catalogUrl(window.location.href);
        history.replaceState(null, "", cleanUrl.toString());
        navigateCatalog(cleanUrl, {push: false});
    }
    window.addEventListener("popstate", () => {
        const target = catalogUrl(window.location.href);
        if (target) navigateCatalog(target, {push: false});
    });
})();

(() => {
    let activeIndex = -1;

    function recentMenuFor(input) {
        return input.closest("[data-search-query-field]")?.querySelector("[data-search-recent-menu]") || null;
    }

    function visibleOptions(menu) {
        return Array.from(menu.querySelectorAll("[data-search-recent-query]")).filter((option) => !option.hidden);
    }

    function setActiveOption(input, options, index) {
        activeIndex = index;
        options.forEach((option, optionIndex) => option.classList.toggle("is-active", optionIndex === index));
        const active = options[index];
        if (active) {
            input.setAttribute("aria-activedescendant", active.id);
            active.scrollIntoView({block: "nearest"});
        } else {
            input.removeAttribute("aria-activedescendant");
        }
    }

    function closeRecentMenu(input) {
        const menu = recentMenuFor(input);
        if (!menu) return;
        menu.hidden = true;
        input.setAttribute("aria-expanded", "false");
        setActiveOption(input, visibleOptions(menu), -1);
    }

    function openRecentMenu(input, {filter = false} = {}) {
        const menu = recentMenuFor(input);
        if (!menu) return;
        const query = filter ? input.value.trim().toLocaleLowerCase() : "";
        const allOptions = Array.from(menu.querySelectorAll("[data-search-recent-query]"));
        allOptions.forEach((option) => {
            option.hidden = Boolean(query) && !option.textContent.toLocaleLowerCase().includes(query);
        });
        const options = visibleOptions(menu);
        menu.hidden = options.length === 0;
        input.setAttribute("aria-expanded", options.length ? "true" : "false");
        setActiveOption(input, options, -1);
    }

    function chooseRecentQuery(input, option) {
        input.value = option.dataset.searchRecentQuery || option.textContent.trim();
        closeRecentMenu(input);
        input.form?.requestSubmit();
    }

    document.addEventListener("focusin", (event) => {
        const input = event.target.closest?.("[data-search-query-input]");
        if (input) openRecentMenu(input);
    });

    document.addEventListener("input", (event) => {
        const input = event.target.closest?.("[data-search-query-input]");
        if (input) openRecentMenu(input, {filter: true});
    });

    document.addEventListener("keydown", (event) => {
        const input = event.target.closest?.("[data-search-query-input]");
        if (!input) return;
        const menu = recentMenuFor(input);
        if (!menu) return;
        if (event.key === "Escape") {
            closeRecentMenu(input);
            return;
        }
        if (event.key !== "ArrowDown" && event.key !== "ArrowUp" && event.key !== "Enter") return;
        if (menu.hidden && event.key !== "Enter") openRecentMenu(input);
        const options = visibleOptions(menu);
        if (!options.length) return;
        if (event.key === "Enter") {
            if (activeIndex < 0) return;
            event.preventDefault();
            chooseRecentQuery(input, options[activeIndex]);
            return;
        }
        event.preventDefault();
        const offset = event.key === "ArrowDown" ? 1 : -1;
        const nextIndex = activeIndex < 0
            ? (offset > 0 ? 0 : options.length - 1)
            : (activeIndex + offset + options.length) % options.length;
        setActiveOption(input, options, nextIndex);
    });

    document.addEventListener("click", (event) => {
        const option = event.target.closest?.("[data-search-recent-query]");
        if (option) {
            const input = option.closest("[data-search-query-field]")?.querySelector("[data-search-query-input]");
            if (input) chooseRecentQuery(input, option);
            return;
        }
        const input = event.target.closest?.("[data-search-query-input]");
        if (input) {
            openRecentMenu(input);
            return;
        }
        document.querySelectorAll("[data-search-query-input][aria-expanded='true']").forEach(closeRecentMenu);
    });
})();

document.addEventListener("click", (event) => {
    const button = event.target.closest("[data-watchlist-direction-toggle]");
    if (!button || !(button.form instanceof HTMLFormElement)) return;
    const input = button.form.querySelector("[data-watchlist-direction-input]");
    if (!(input instanceof HTMLInputElement)) return;
    input.value = input.value === "desc" ? "asc" : "desc";
    button.form.requestSubmit();
});

document.addEventListener("change", (event) => {
    const control = event.target.closest("[data-catalog-auto-submit]");
    if (!control || !(control.form instanceof HTMLFormElement)) return;
    control.form.requestSubmit();
});
