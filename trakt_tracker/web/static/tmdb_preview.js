(() => {
    const root = document.querySelector('[data-catalog-provider="tmdb_preview"]') || document.querySelector("[data-tmdb-card]");
    const overlay = document.getElementById("search-watch-overlay")
        || document.getElementById("release-watch-overlay")
        || document.getElementById("history-watch-overlay");
    const body = document.getElementById("search-watch-body")
        || document.getElementById("release-watch-body")
        || document.getElementById("history-watch-body");
    const titleNode = document.getElementById("search-watch-title")
        || document.getElementById("release-watch-title")
        || document.getElementById("history-watch-title");
    let activePanelCard = null;
    let activePanelTrigger = null;
    let panelRequestToken = 0;
    if (!root) return;

    const toast = (message, error = false) => {
        if (window.pushFlashToast) window.pushFlashToast(message, error ? 5200 : 3600);
    };

    const cardPayload = (card) => ({
        tmdb_id: Number(card?.dataset.tmdbId || 0),
        title_type: card?.dataset.titleType || "movie",
    });

    async function post(url, payload) {
        const response = await fetch(url, {
            method: "POST",
            headers: {"Accept": "application/json", "Content-Type": "application/json"},
            cache: "no-store",
            body: JSON.stringify(payload),
        });
        const data = await response.json().catch(() => ({}));
        if (!response.ok || !data.ok) throw new Error(data.message || "TMDb preview action failed.");
        return data;
    }

    function escapeHtml(value) {
        return String(value || "").replace(/[&<>'"]/g, (character) => (
            {"&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;"}[character]
        ));
    }

    function cachedImageUrl(value) {
        return value ? `/cached-image?url=${encodeURIComponent(value)}&v=3` : "";
    }

    function updateWatchButton(card, watched) {
        const button = card?.querySelector("[data-tmdb-watch-toggle]");
        if (!button) return;
        button.dataset.tmdbWatched = watched ? "true" : "false";
        button.classList.toggle("is-active", watched);
        button.title = watched ? "Remove from watched history" : "Mark watched";
        const icon = button.querySelector("img");
        if (icon) icon.src = watched ? "/static/cancel.svg" : "/static/watched_check.svg";
        const seen = card.querySelector(".catalog-seen-overlay");
        if (watched && !seen) {
            card.querySelector(".search-result-poster")?.insertAdjacentHTML(
                "beforeend",
                '<span class="catalog-seen-overlay" aria-label="Seen"><img src="/static/seen.svg" alt=""></span>',
            );
        } else if (!watched && seen) {
            seen.remove();
        }
    }

    function updateMembershipButton(card, selector, active, onSrc, offSrc, dataKey) {
        const button = card?.querySelector(selector);
        if (!button) return;
        button.dataset[dataKey] = active ? "true" : "false";
        button.classList.toggle("is-active", active);
        button.setAttribute("aria-pressed", active ? "true" : "false");
        const icon = button.querySelector("img");
        if (icon) icon.src = active ? onSrc : offSrc;
    }

    async function toggleWatch(card) {
        const button = card.querySelector("[data-tmdb-watch-toggle]");
        const watched = button?.dataset.tmdbWatched !== "true";
        button.disabled = true;
        try {
            const result = await post(
                watched ? "/tmdb-preview/watch" : "/tmdb-preview/unwatch",
                {
                    ...cardPayload(card),
                    remove_from_release_tracking: watched && card.matches("[data-release-card]"),
                },
            );
            if (watched && result.removed_from_release_tracking && card.matches("[data-release-card]")) {
                card.remove();
                toast(result.message || "Watched state saved locally.");
                return;
            }
            updateWatchButton(card, watched);
            toast(watched ? "Watched state saved locally." : "Watched state removed locally.");
        } catch (error) {
            toast(error.message || "TMDb preview action failed.", true);
        } finally {
            button.disabled = false;
        }
    }

    async function toggleWatchlist(card) {
        const button = card.querySelector("[data-tmdb-watchlist-toggle]");
        const watchlisted = button?.dataset.tmdbWatchlisted !== "true";
        button.disabled = true;
        try {
            await post("/tmdb-preview/watchlist/toggle", {...cardPayload(card), watchlisted});
            updateMembershipButton(
                card,
                "[data-tmdb-watchlist-toggle]",
                watchlisted,
                "/static/bookmark.svg",
                "/static/bookmark_unfill.svg",
                "tmdbWatchlisted",
            );
            toast(watchlisted ? "Added to local watchlist." : "Removed from local watchlist.");
        } catch (error) {
            toast(error.message || "Watchlist action failed.", true);
        } finally {
            button.disabled = false;
        }
    }

    async function toggleRelease(card) {
        const button = card.querySelector("[data-tmdb-release-toggle]");
        const tracked = button?.dataset.tmdbTracked !== "true";
        button.disabled = true;
        try {
            await post("/tmdb-preview/release-tracking/toggle", {...cardPayload(card), tracked});
            updateMembershipButton(
                card,
                "[data-tmdb-release-toggle]",
                tracked,
                "/static/notification_on.svg",
                "/static/notification_off.svg",
                "tmdbTracked",
            );
            toast(tracked ? "Release tracking enabled." : "Release tracking disabled.");
        } catch (error) {
            toast(error.message || "Release tracking action failed.", true);
        } finally {
            button.disabled = false;
        }
    }

    async function toggleAcknowledged(card) {
        const button = card.querySelector("[data-tmdb-release-acknowledge]");
        const acknowledged = button?.dataset.tmdbAcknowledged !== "true";
        button.disabled = true;
        try {
            await post("/tmdb-preview/release-tracking/acknowledge", {...cardPayload(card), acknowledged});
            button.dataset.tmdbAcknowledged = acknowledged ? "true" : "false";
            button.classList.toggle("is-active", acknowledged);
            card.classList.toggle("is-unacknowledged", !acknowledged);
            card.classList.toggle(
                "is-notification-sent",
                !acknowledged && card.dataset.notificationSent === "true",
            );
            button.title = acknowledged ? "Resume notifications" : "Acknowledge release";
            toast(acknowledged ? "Release acknowledged." : "Release notifications resumed.");
        } catch (error) {
            toast(error.message || "Release acknowledgement failed.", true);
        } finally {
            button.disabled = false;
        }
    }

    function openOverlay() {
        if (!overlay) return;
        if (window.TraktDialogs) {
            window.TraktDialogs.open(overlay, {onEscape: closeTmdbPanel});
            return;
        }
        overlay.hidden = false;
        overlay.classList.add("is-open");
        overlay.setAttribute("aria-hidden", "false");
    }

    function closeTmdbPanel() {
        panelRequestToken += 1;
        activePanelCard = null;
        const trigger = activePanelTrigger;
        activePanelTrigger = null;
        if (overlay?.dataset.watchPanelOwner === "tmdb") delete overlay.dataset.watchPanelOwner;
        resetPanelHeader();
        window.TraktDialogs?.close(overlay);
        if (trigger && document.contains(trigger)) trigger.focus();
    }

    function removeActiveReleaseCard(result) {
        if (!result?.removed_from_release_tracking || !activePanelCard?.matches("[data-release-card]")) {
            return false;
        }
        const card = activePanelCard;
        card.remove();
        closeTmdbPanel();
        return true;
    }

    function openResultRating(result) {
        if (result?.rating_context && window.traktOpenRatingModal) {
            window.traktOpenRatingModal(result.rating_context);
        }
    }

    function formatVotes(value) {
        if (value === null || value === undefined || value === "") return "";
        const votes = Number(value);
        if (!Number.isFinite(votes)) return "";
        if (votes < 1000) return String(Math.round(votes));
        if (votes < 1000000) return `${(votes / 1000).toFixed(1).replace(/\.0$/, "")}k`;
        return `${(votes / 1000000).toFixed(2).replace(/0+$/, "").replace(/\.$/, "")}m`;
    }

    function formatRating(rating, votes, status = "") {
        if (rating === null || rating === undefined || rating === "") {
            return ["checked_no_data", "ready"].includes(String(status || "")) ? "n/a" : "Loading";
        }
        const value = Number(rating);
        if (Number.isFinite(value)) {
            const suffix = formatVotes(votes);
            return `${value.toFixed(1)}${suffix ? ` (${suffix})` : ""}`;
        }
        return ["checked_no_data", "ready"].includes(String(status || "")) ? "n/a" : "Loading";
    }

    function resetPanelHeader() {
        if (!overlay) return;
        overlay.removeAttribute("data-tmdb-id");
        const ratings = overlay.querySelector("[data-search-watch-title-ratings]");
        if (ratings) {
            ratings.hidden = true;
            delete ratings.dataset.titleMatrixTitle;
            delete ratings.dataset.titleMatrixTmdbId;
            delete ratings.dataset.titleMatrixTraktId;
            delete ratings.dataset.titleMatrixUrl;
            ratings.removeAttribute("aria-label");
        }
        overlay.querySelectorAll(
            "[data-search-watch-header-season-mark], [data-search-watch-header-mark], "
            + "[data-search-watch-header-season-unwatch], [data-search-watch-header-unwatch]",
        ).forEach((action) => {
            action.hidden = true;
            action.disabled = false;
            action.removeAttribute("data-tmdb-scope-action");
            action.removeAttribute("data-tmdb-scope-unwatch");
            delete action.dataset.tmdbId;
            delete action.dataset.season;
            delete action.dataset.title;
            action.removeAttribute("aria-label");
        });
        const play = overlay.querySelector("[data-show-watch-play]");
        if (play) {
            play.hidden = true;
            play.removeAttribute("href");
            play.removeAttribute("aria-label");
        }
    }

    function configureScopeAction(action, panel, scope, unwatch = false) {
        if (!action) return;
        const season = Number(panel.selected_season ?? 0);
        const visible = scope === "season"
            ? Boolean(unwatch ? panel.can_unwatch_season : panel.can_mark_season)
            : Boolean(unwatch ? panel.can_unwatch_title : panel.can_mark_title);
        action.hidden = !visible;
        action.dataset.tmdbId = String(Number(panel.tmdb_id || 0));
        action.dataset.title = panel.title || "";
        action.dataset.scope = scope;
        action.dataset.titleType = "show";
        if (scope === "season") action.dataset.season = String(season);
        else delete action.dataset.season;
        action.removeAttribute("data-search-watch-action");
        action.removeAttribute("data-search-unwatch-action");
        action.toggleAttribute(unwatch ? "data-tmdb-scope-unwatch" : "data-tmdb-scope-action", true);
        action.removeAttribute(unwatch ? "data-tmdb-scope-action" : "data-tmdb-scope-unwatch");
        const label = scope === "season" ? `S${season}` : "the entire series";
        action.title = unwatch
            ? `Remove watched history for ${label}`
            : `Mark all released episodes in ${label} watched`;
        action.setAttribute("aria-label", action.title);
        action.querySelectorAll("[data-search-watch-header-season-label]").forEach((node) => {
            node.textContent = `S${season}`;
        });
    }

    function configurePanelHeader(panel, card) {
        if (!overlay) return;
        const tmdbId = Number(panel.tmdb_id || card?.dataset.tmdbId || 0);
        const title = panel.title || card?.dataset.title || "";
        overlay.dataset.tmdbId = String(tmdbId);
        const ratings = overlay.querySelector("[data-search-watch-title-ratings]");
        if (ratings) {
            ratings.hidden = !tmdbId;
            ratings.dataset.titleMatrixTitle = title;
            ratings.dataset.titleMatrixTmdbId = String(tmdbId);
            delete ratings.dataset.titleMatrixTraktId;
            ratings.dataset.titleMatrixUrl = `/titles/tmdb/show/${tmdbId}/episode-ratings-matrix`;
            ratings.setAttribute("aria-label", `Open episode ratings matrix for ${title || "this series"}`);
            const primaryIcon = ratings.querySelector(".poster-chip-part:first-child img");
            if (primaryIcon) {
                primaryIcon.src = "/static/tmdb.png";
                primaryIcon.alt = "TMDb";
                primaryIcon.classList.add("poster-chip-icon-tmdb");
            }
            const primary = ratings.querySelector("[data-search-watch-trakt-rating]");
            const imdb = ratings.querySelector("[data-search-watch-imdb-rating]");
            if (primary) primary.textContent = formatRating(panel.tmdb_rating, panel.tmdb_votes, panel.ratings_status);
            if (imdb) imdb.textContent = formatRating(panel.imdb_rating, panel.imdb_votes, panel.ratings_status);
        }
        configureScopeAction(overlay.querySelector("[data-search-watch-header-season-mark]"), panel, "season");
        configureScopeAction(overlay.querySelector("[data-search-watch-header-mark]"), panel, "title");
        configureScopeAction(overlay.querySelector("[data-search-watch-header-season-unwatch]"), panel, "season", true);
        configureScopeAction(overlay.querySelector("[data-search-watch-header-unwatch]"), panel, "title", true);
        const play = overlay.querySelector("[data-show-watch-play]");
        if (play) {
            play.hidden = !tmdbId;
            play.href = `/search/show/${tmdbId}/play?title=${encodeURIComponent(title)}`;
            play.title = `Play ${title || "show"}`;
            play.setAttribute("aria-label", play.title);
        }
    }

    function episodeCard(panel, episode) {
        const season = Number(episode.season ?? panel.selected_season ?? 0);
        const number = Number(episode.episode || 0);
        const displaySeason = Number(episode.imdb_season ?? season);
        const displayEpisode = Number(episode.imdb_episode ?? number);
        const tmdbSeason = Number(episode.tmdb_season ?? season);
        const tmdbEpisode = Number(episode.tmdb_episode ?? number);
        const label = `S${String(displaySeason).padStart(2, "0")}E${String(displayEpisode).padStart(2, "0")}`;
        const episodeUrl = `https://www.themoviedb.org/tv/${Number(panel.tmdb_id || 0)}/season/${tmdbSeason}/episode/${tmdbEpisode}`;
        const still = episode.still_url
            ? `<img src="${cachedImageUrl(episode.still_url)}" alt="${escapeHtml(episode.title || "Episode")} still" loading="lazy">`
            : "<span>No preview</span>";
        const watchedOverlay = episode.watched
            ? '<span class="search-watch-seen-overlay" aria-label="Watched"><svg viewBox="0 6 32 20" preserveAspectRatio="xMidYMid meet" width="60%" height="60%" fill="#fff" opacity="0.5" aria-hidden="true"><path d="M16 25.5a17.85 17.85 0 0 1-15.4-9 1 1 0 0 1 0-1A17.71 17.71 0 0 1 31.4 15.5a1 1 0 0 1 0 1 17.85 17.85 0 0 1-15.4 9ZM2.6 16a15.7 15.7 0 0 0 26.8 0 15.7 15.7 0 0 0-26.8 0Zm13.4 5.85A5.85 5.85 0 1 1 21.85 16 5.86 5.86 0 0 1 16 21.85Zm0-9.7A3.85 3.85 0 1 0 19.85 16 3.85 3.85 0 0 0 16 12.15Z"/></svg></span>'
            : "";
        const actionIcon = episode.watched
            ? '<span class="search-watch-action-glyphs" aria-hidden="true"><img class="icon-glyph icon-glyph-seen" src="/static/seen.svg" alt=""><img class="icon-glyph icon-glyph-cancel" src="/static/cancel.svg" alt=""></span>'
            : '<img class="icon-glyph icon-glyph-check" src="/static/watched_check.svg" alt="" aria-hidden="true">';
        const userRating = Number(episode.user_rating || 0);
        const ratingButton = episode.watched
            ? `<button type="button" class="${userRating ? "history-rating-badge user-rating-badge episode-user-rating-badge" : "history-rate-chip"} search-watch-user-rating" data-rating-trigger data-rating-provider="tmdb" data-rating-title-type="show" data-rating-tmdb-id="${Number(panel.tmdb_id || 0)}" data-rating-title="${escapeHtml(panel.title || "")}" data-rating-season="${season}" data-rating-episode="${number}"${userRating ? ` data-user-rating="${userRating}"` : ""} aria-label="${userRating ? "Change rating for" : "Rate"} ${escapeHtml(panel.title || "show")} ${label}">${userRating ? `<span class="user-rating-value">${userRating}</span><span class="user-rating-star">&#9733;</span>` : "Rate"}</button>`
            : "";
        const episodeRatings = `<div class="poster-chip search-watch-episode-ratings">
            <span class="poster-chip-part"><img class="poster-chip-icon poster-chip-icon-tmdb" src="/static/tmdb.png" alt="tmdb"><span>${formatRating(episode.tmdb_rating, episode.tmdb_votes, "ready")}</span></span>
            <span class="poster-chip-part"><img class="poster-chip-icon" src="/static/imdb_icon.png" alt="imdb"><span>${formatRating(episode.imdb_rating, episode.imdb_votes, "ready")}</span></span>
        </div>`;
        return `
            <article class="search-watch-episode-card${episode.watched ? " is-watched" : ""}" data-episode-key="${season}-${number}">
                <div class="search-watch-still-shell">
                    <a class="search-watch-still" href="${episodeUrl}" target="_blank" rel="noreferrer" aria-label="Open ${escapeHtml(label)} on TMDb">
                        ${still}${watchedOverlay}<span class="search-watch-episode-label">${label}</span>
                    </a>
                    ${ratingButton}
                </div>
                <div class="search-watch-episode-copy">
                    <h4>${escapeHtml(episode.title || "Episode")}</h4>
                    ${episodeRatings}
                    <button type="button" class="icon-button search-watch-episode-action" data-tmdb-episode-toggle data-season="${season}" data-episode="${number}" data-watched="${episode.watched ? "true" : "false"}" title="${episode.watched ? "Remove episode from watched history" : "Mark episode watched"}">${actionIcon}</button>
                </div>
            </article>`;
    }

    function renderPanel(panel) {
        if (!body) return;
        if (titleNode) titleNode.textContent = panel.title || "Episodes";
        const selectedSeason = Number(panel.selected_season ?? 0);
        const seasons = Array.isArray(panel.seasons) ? panel.seasons : [];
        const episodes = Array.isArray(panel.episodes) ? panel.episodes : [];
        const tabs = seasons.map((season) => {
            const number = Number(season.season_number ?? season.season ?? 0);
            const active = number === selectedSeason;
            return `<button type="button" class="search-watch-season-tab${active ? " is-active" : ""}" data-tmdb-season-tab="${number}" role="tab" aria-selected="${active ? "true" : "false"}" tabindex="${active ? "0" : "-1"}">S${number}<span>${Number(season.episode_count || 0)}</span></button>`;
        }).join("");
        body.innerHTML = `
            <div class="search-watch-panel" data-tmdb-preview-watch-panel data-tmdb-id="${Number(panel.tmdb_id || 0)}">
                ${tabs ? `<div class="search-watch-seasons" role="tablist" aria-label="Seasons">${tabs}</div>` : ""}
                ${episodes.length ? `<section class="search-watch-episode-grid">${episodes.map((episode) => episodeCard(panel, episode)).join("")}</section>` : '<div class="title-matrix-empty-state"><p>No episodes found.</p></div>'}
            </div>`;
    }

    async function openPanel(card, season = null, trigger = null) {
        const requestToken = ++panelRequestToken;
        try {
            const payload = cardPayload(card);
            activePanelCard = card;
            if (trigger) activePanelTrigger = trigger;
            if (titleNode) titleNode.textContent = card?.querySelector(".search-result-title")?.textContent?.trim() || "Episodes";
            resetPanelHeader();
            if (overlay) overlay.dataset.watchPanelOwner = "tmdb";
            openOverlay();
            if (body) body.innerHTML = '<div class="title-matrix-loading-shell"><div class="title-matrix-loading-bar is-wide"></div></div>';
            const suffix = season === null ? "" : `?season=${encodeURIComponent(season)}`;
            const response = await fetch(`/tmdb-preview/show/${payload.tmdb_id}/watch-panel${suffix}`, {
                headers: {"Accept": "application/json"},
                cache: "no-store",
            });
            const data = await response.json();
            if (requestToken !== panelRequestToken || overlay?.dataset.watchPanelOwner !== "tmdb") return;
            if (!response.ok || !data.ok) throw new Error(data.message || "Episode panel failed.");
            renderPanel(data);
            configurePanelHeader(data, card);
        } catch (error) {
            if (requestToken !== panelRequestToken || overlay?.dataset.watchPanelOwner !== "tmdb") return;
            if (body) body.innerHTML = '<div class="title-matrix-empty-state"><p>Could not load episodes.</p></div>';
            toast(error.message || "Episode panel failed.", true);
        }
    }

    document.addEventListener("click", (event) => {
        const scopeAction = event.target.closest("[data-tmdb-scope-action], [data-tmdb-scope-unwatch]");
        if (scopeAction && overlay && activePanelCard) {
            event.preventDefault();
            event.stopImmediatePropagation();
            if (scopeAction.disabled) return;
            const unwatch = scopeAction.hasAttribute("data-tmdb-scope-unwatch");
            const scope = scopeAction.dataset.scope || "title";
            const payload = {
                tmdb_id: Number(scopeAction.dataset.tmdbId || overlay.dataset.tmdbId || 0),
                title_type: "show",
                scope,
                remove_from_release_tracking: activePanelCard.matches("[data-release-card]"),
            };
            if (scope === "season") payload.season = Number(scopeAction.dataset.season || 0);
            scopeAction.disabled = true;
            post(unwatch ? "/tmdb-preview/unwatch" : "/tmdb-preview/watch", payload).then((result) => {
                toast(result.message || (unwatch ? "Watched history removed." : "Episodes saved locally."));
                if (!unwatch && removeActiveReleaseCard(result)) return null;
                return openPanel(activePanelCard, scope === "season" ? payload.season : null);
            }).catch((error) => {
                scopeAction.disabled = false;
                toast(error.message || "Watched-history action failed.", true);
            });
            return;
        }
        if (
            overlay?.dataset.watchPanelOwner === "tmdb"
            && event.target.closest("[data-search-watch-close], [data-release-watch-close], [data-history-watch-close]")
        ) {
            closeTmdbPanel();
            return;
        }
        const historyUnwatch = event.target.closest("[data-tmdb-history-unwatch]");
        if (historyUnwatch) {
            event.preventDefault();
            const historyCard = historyUnwatch.closest("[data-tmdb-card]");
            const payload = {
                ...cardPayload(historyCard),
                season: Number(historyUnwatch.dataset.season || 0) || null,
                episode: Number(historyUnwatch.dataset.episode || 0) || null,
            };
            post("/tmdb-preview/unwatch", payload).then(() => {
                window.location.reload();
            }).catch((error) => toast(error.message || "Watched-history action failed.", true));
            return;
        }
        const card = event.target.closest("[data-tmdb-card]");
        if (card && event.target.closest("[data-tmdb-watch-toggle]")) {
            event.preventDefault();
            toggleWatch(card);
            return;
        }
        if (card && event.target.closest("[data-tmdb-watchlist-toggle]")) {
            event.preventDefault();
            toggleWatchlist(card);
            return;
        }
        if (card && event.target.closest("[data-tmdb-release-toggle]")) {
            event.preventDefault();
            toggleRelease(card);
            return;
        }
        if (card && event.target.closest("[data-tmdb-release-acknowledge]")) {
            event.preventDefault();
            toggleAcknowledged(card);
            return;
        }
        if (card && event.target.closest("[data-tmdb-watch-panel], [data-tmdb-progress-watch-panel]")) {
            event.preventDefault();
            openPanel(card, null, event.target.closest("[data-tmdb-watch-panel], [data-tmdb-progress-watch-panel]"));
            return;
        }
        const seasonTab = event.target.closest("[data-tmdb-season-tab]");
        if (seasonTab && activePanelCard) {
            event.preventDefault();
            openPanel(activePanelCard, Number(seasonTab.dataset.tmdbSeasonTab || 0));
            return;
        }
        const episodeButton = event.target.closest("[data-tmdb-episode-toggle]");
        if (episodeButton && overlay && activePanelCard) {
            event.preventDefault();
            const tmdbId = Number(overlay.dataset.tmdbId || 0);
            const desired = episodeButton.dataset.watched !== "true";
            const season = Number(episodeButton.dataset.season || 0);
            const payload = {
                tmdb_id: tmdbId,
                title_type: "show",
                season,
                episode: Number(episodeButton.dataset.episode || 0),
                remove_from_release_tracking: activePanelCard.matches("[data-release-card]"),
            };
            post(desired ? "/tmdb-preview/watch" : "/tmdb-preview/unwatch", payload).then(async (result) => {
                toast(desired ? "Episode saved locally." : "Episode removed locally.");
                if (desired && removeActiveReleaseCard(result)) {
                    openResultRating(result);
                    return;
                }
                await openPanel(activePanelCard, season);
                if (desired) openResultRating(result);
            }).catch((error) => toast(error.message || "Episode action failed.", true));
            return;
        }
    });
})();
