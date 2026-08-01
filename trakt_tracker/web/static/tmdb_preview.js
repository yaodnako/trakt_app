(() => {
    const root = document.querySelector('[data-catalog-provider="tmdb_preview"]');
    const overlay = document.getElementById("search-watch-overlay") || document.getElementById("release-watch-overlay");
    const body = document.getElementById("search-watch-body") || document.getElementById("release-watch-body");
    const titleNode = document.getElementById("search-watch-title") || document.getElementById("release-watch-title");
    let activePanelCard = null;
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
            await post(watched ? "/tmdb-preview/watch" : "/tmdb-preview/unwatch", cardPayload(card));
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
            window.TraktDialogs.open(overlay);
            return;
        }
        overlay.hidden = false;
        overlay.classList.add("is-open");
        overlay.setAttribute("aria-hidden", "false");
    }

    function episodeCard(panel, episode) {
        const season = Number(episode.season ?? panel.selected_season ?? 0);
        const number = Number(episode.episode || 0);
        const label = `S${String(season).padStart(2, "0")}E${String(number).padStart(2, "0")}`;
        const episodeUrl = `https://www.themoviedb.org/tv/${Number(panel.tmdb_id || 0)}/season/${season}/episode/${number}`;
        const still = episode.still_url
            ? `<img src="${cachedImageUrl(episode.still_url)}" alt="${escapeHtml(episode.title || "Episode")} still" loading="lazy">`
            : "<span>No preview</span>";
        const watchedOverlay = episode.watched
            ? '<span class="search-watch-seen-overlay" aria-label="Watched"><svg viewBox="0 6 32 20" preserveAspectRatio="xMidYMid meet" width="60%" height="60%" fill="#fff" opacity="0.5" aria-hidden="true"><path d="M16 25.5a17.85 17.85 0 0 1-15.4-9 1 1 0 0 1 0-1A17.71 17.71 0 0 1 31.4 15.5a1 1 0 0 1 0 1 17.85 17.85 0 0 1-15.4 9ZM2.6 16a15.7 15.7 0 0 0 26.8 0 15.7 15.7 0 0 0-26.8 0Zm13.4 5.85A5.85 5.85 0 1 1 21.85 16 5.86 5.86 0 0 1 16 21.85Zm0-9.7A3.85 3.85 0 1 0 19.85 16 3.85 3.85 0 0 0 16 12.15Z"/></svg></span>'
            : "";
        const actionIcon = episode.watched
            ? '<span class="search-watch-action-glyphs" aria-hidden="true"><img class="icon-glyph icon-glyph-seen" src="/static/seen.svg" alt=""><img class="icon-glyph icon-glyph-cancel" src="/static/cancel.svg" alt=""></span>'
            : '<img class="icon-glyph icon-glyph-check" src="/static/watched_check.svg" alt="" aria-hidden="true">';
        return `
            <article class="search-watch-episode-card${episode.watched ? " is-watched" : ""}" data-episode-key="${season}-${number}">
                <div class="search-watch-still-shell">
                    <a class="search-watch-still" href="${episodeUrl}" target="_blank" rel="noreferrer" aria-label="Open ${escapeHtml(label)} on TMDb">
                        ${still}${watchedOverlay}<span class="search-watch-episode-label">${label}</span>
                    </a>
                </div>
                <div class="search-watch-episode-copy">
                    <h4>${escapeHtml(episode.title || "Episode")}</h4>
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

    async function openPanel(card, season = null) {
        try {
            const payload = cardPayload(card);
            activePanelCard = card;
            if (titleNode) titleNode.textContent = card?.querySelector(".search-result-title")?.textContent?.trim() || "Episodes";
            openOverlay();
            if (body) body.innerHTML = '<div class="title-matrix-loading-shell"><div class="title-matrix-loading-bar is-wide"></div></div>';
            const suffix = season === null ? "" : `?season=${encodeURIComponent(season)}`;
            const response = await fetch(`/tmdb-preview/show/${payload.tmdb_id}/watch-panel${suffix}`, {
                headers: {"Accept": "application/json"},
                cache: "no-store",
            });
            const data = await response.json();
            if (!response.ok || !data.ok) throw new Error(data.message || "Episode panel failed.");
            if (overlay) overlay.dataset.tmdbId = String(payload.tmdb_id);
            renderPanel(data);
        } catch (error) {
            if (body) body.innerHTML = '<div class="title-matrix-empty-state"><p>Could not load episodes.</p></div>';
            toast(error.message || "Episode panel failed.", true);
        }
    }

    document.addEventListener("click", (event) => {
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
        if (card && event.target.closest("[data-tmdb-watch-panel]")) {
            event.preventDefault();
            openPanel(card);
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
            };
            post(desired ? "/tmdb-preview/watch" : "/tmdb-preview/unwatch", payload).then(() => {
                toast(desired ? "Episode saved locally." : "Episode removed locally.");
                return openPanel(activePanelCard, season);
            }).catch((error) => toast(error.message || "Episode action failed.", true));
        }
    });
})();
