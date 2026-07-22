    (() => {
        if (window.traktDebugMode && window.pushFlashToast) {
            document.querySelectorAll('form[action$="/watch"]').forEach((form) => {
                form.addEventListener("submit", () => window.pushFlashToast("Progress action: mark watched…", 2600));
            });
            document.querySelectorAll('form[action$="/seen"]').forEach((form) => {
                form.addEventListener("submit", () => window.pushFlashToast("Progress action: mark seen…", 2600));
            });
            document.querySelectorAll('form[action="/progress/rate"]').forEach((form) => {
                form.addEventListener("submit", () => window.pushFlashToast("Progress action: save rating…", 2600));
            });
            document.querySelectorAll(".js-play-link").forEach((link) => {
                link.addEventListener("click", () => window.pushFlashToast("Play: resolving target URL…", 2600));
            });
        }
    })();

(() => {
    document.addEventListener("change", (event) => {
        const root = document.getElementById("progress-page-root");
        if (!root) return;
        const toggle = event.target.closest("[data-progress-toggle]");
        if (toggle instanceof HTMLInputElement) {
            const params = new URLSearchParams({
                hide_upcoming: root.dataset.hideUpcoming || "0",
                show_dropped: root.dataset.showDropped || "0",
                use_year_filter: root.dataset.useYearFilter || "0",
                min_year: root.dataset.minYear || "",
            });
            params.set(toggle.dataset.progressToggle || "", toggle.checked ? "1" : "0");
            window.location.assign(`/progress?${params.toString()}`);
            return;
        }
        const yearToggle = event.target.closest("[data-progress-year-toggle]");
        if (yearToggle instanceof HTMLInputElement && yearToggle.form) {
            yearToggle.form.elements.use_year_filter.value = yearToggle.checked ? "1" : "0";
            yearToggle.form.requestSubmit();
        }
    });

    if (!window.fetch) {
        return;
    }

    class ProgressRefreshController {
        constructor(root) {
            this.root = root;
            this.hideUpcoming = root.dataset.hideUpcoming || "0";
            this.showDropped = root.dataset.showDropped || "0";
            this.minYear = root.dataset.minYear || "";
            this.useYearFilter = root.dataset.useYearFilter || "0";
            this.idleRefreshIntervalMs = 300000;
            this.viewportRefreshDebounceMs = 180;
            this.progressSyncRunning = root.dataset.progressSyncRunning === "1";
            this.queueRevision = 0;
            this.queueRunning = false;
            this.pollTimer = null;
            this.idlePollTimer = null;
            this.viewportRefreshTimer = null;
            this.pollInFlight = false;
            this.pollAttempts = 0;
            this.pageChangeNotified = false;
            this.forceVisibleRefresh = false;
            this.lastViewportRefreshKey = "";
            this.lastViewportRefreshAt = 0;
            this.viewportCardKeys = new Set();
            this.nearbyCardKeys = new Set();
            this.viewportObserver = null;
            this.nearbyObserver = null;
            this.setupObservers();
            this.bindInteractions();
        }

        pageCardKeys() {
            return Array.from(document.querySelectorAll("[data-progress-card-key]"))
                .map((node) => node.dataset.progressCardKey || "")
                .filter((value) => value);
        }

        viewportKeys() {
            const pageKeys = this.pageCardKeys();
            if (!this.viewportObserver) {
                return pageKeys;
            }
            return pageKeys.filter((key) => this.viewportCardKeys.has(key));
        }

        nearbyKeys() {
            const pageKeys = this.pageCardKeys();
            if (!this.nearbyObserver) {
                return [];
            }
            return pageKeys.filter((key) => this.nearbyCardKeys.has(key) && !this.viewportCardKeys.has(key));
        }

        hasRunningJobs() {
            return Boolean(this.progressSyncRunning || this.queueRunning);
        }

        setupObservers() {
            if (!("IntersectionObserver" in window)) {
                return;
            }
            this.viewportObserver = new IntersectionObserver((entries) => {
                for (const entry of entries) {
                    const cardKey = entry.target.dataset.progressCardKey || "";
                    if (!cardKey) {
                        continue;
                    }
                    const wasVisible = this.viewportCardKeys.has(cardKey);
                    if (entry.isIntersecting) {
                        this.viewportCardKeys.add(cardKey);
                        if (!wasVisible) {
                            this.requestVisibleRefresh();
                        }
                    } else {
                        this.viewportCardKeys.delete(cardKey);
                    }
                }
            }, {threshold: 0.01});
            this.nearbyObserver = new IntersectionObserver((entries) => {
                for (const entry of entries) {
                    const cardKey = entry.target.dataset.progressCardKey || "";
                    if (!cardKey) {
                        continue;
                    }
                    if (entry.isIntersecting) {
                        this.nearbyCardKeys.add(cardKey);
                    } else {
                        this.nearbyCardKeys.delete(cardKey);
                    }
                }
            }, {threshold: 0, rootMargin: "600px 0px"});
            this.observeCards();
        }

        observeCards() {
            if (!this.viewportObserver || !this.nearbyObserver) {
                return;
            }
            this.viewportObserver.disconnect();
            this.nearbyObserver.disconnect();
            this.viewportCardKeys.clear();
            this.nearbyCardKeys.clear();
            for (const card of document.querySelectorAll("[data-progress-card-key]")) {
                this.viewportObserver.observe(card);
                this.nearbyObserver.observe(card);
            }
        }

        bindInteractions() {
            if (window.traktBindPlayPromptLinks) {
                window.traktBindPlayPromptLinks();
            }
        }

        startPolling() {
            if (this.idlePollTimer !== null) {
                window.clearTimeout(this.idlePollTimer);
                this.idlePollTimer = null;
            }
            if (this.pollTimer !== null) {
                return;
            }
            this.pollOnce();
            this.pollTimer = window.setInterval(() => this.pollOnce(), 1200);
        }

        stopPolling() {
            if (this.pollTimer !== null) {
                window.clearInterval(this.pollTimer);
                this.pollTimer = null;
            }
        }

        scheduleIdlePoll() {
            if (this.pollTimer !== null || this.idlePollTimer !== null) {
                return;
            }
            this.idlePollTimer = window.setTimeout(() => {
                this.idlePollTimer = null;
                this.forceVisibleRefresh = true;
                this.startPolling();
            }, this.idleRefreshIntervalMs);
        }

        requestVisibleRefresh() {
            if (this.viewportRefreshTimer !== null) {
                window.clearTimeout(this.viewportRefreshTimer);
            }
            this.viewportRefreshTimer = window.setTimeout(() => {
                this.viewportRefreshTimer = null;
                const viewportKey = this.viewportKeys().slice().sort().join("|");
                const now = Date.now();
                if (viewportKey && viewportKey === this.lastViewportRefreshKey && (now - this.lastViewportRefreshAt) < 2500) {
                    return;
                }
                this.lastViewportRefreshKey = viewportKey;
                this.lastViewportRefreshAt = now;
                this.forceVisibleRefresh = true;
                if (this.pollTimer !== null) {
                    this.pollOnce();
                } else {
                    this.startPolling();
                }
            }, this.viewportRefreshDebounceMs);
        }

        refreshSections() {
            document.querySelectorAll("#progress-sections-root [data-progress-section]").forEach((section) => {
                const hasCards = section.querySelectorAll("[data-progress-card-key]").length > 0;
                section.hidden = !hasCards;
            });
            const emptyState = document.querySelector("#progress-sections-root [data-progress-empty-state]");
            if (emptyState) {
                emptyState.hidden = this.pageCardKeys().length > 0;
            }
            this.observeCards();
            this.bindInteractions();
        }

        replaceSections(html) {
            const root = document.getElementById("progress-sections-root");
            if (!root || !html) {
                return;
            }
            root.innerHTML = html;
            this.refreshSections();
        }

        applyRefresh(payload) {
            const renderedCards = Array.isArray(payload.cards) ? payload.cards : [];
            for (const item of renderedCards) {
                const cardKey = String(item && item.card_key ? item.card_key : "");
                const html = String(item && item.html ? item.html : "");
                if (!cardKey || !html) {
                    continue;
                }
                const existing = document.querySelector(`[data-progress-card-key="${CSS.escape(cardKey)}"]`);
                if (!existing) {
                    continue;
                }
                const template = document.createElement("template");
                template.innerHTML = html.trim();
                const replacement = template.content.firstElementChild;
                if (!replacement) {
                    continue;
                }
                existing.replaceWith(replacement);
            }

            const missingCardKeys = Array.isArray(payload.missing_card_keys) ? payload.missing_card_keys : [];
            for (const cardKey of missingCardKeys) {
                const existing = document.querySelector(`[data-progress-card-key="${CSS.escape(String(cardKey))}"]`);
                if (existing) {
                    existing.remove();
                }
            }

            if (payload && payload.sections_html) {
                this.replaceSections(String(payload.sections_html || ""));
            } else {
                this.refreshSections();
            }

            if (payload && payload.page_changed && !payload.sections_html && !this.pageChangeNotified && window.pushFlashToast) {
                window.pushFlashToast("Progress updated; refresh page to show new items/order.", 4200);
                this.pageChangeNotified = true;
            }
        }

        async pollOnce() {
            if (this.pollInFlight) {
                return;
            }
            this.pollInFlight = true;
            this.pollAttempts += 1;
            try {
                const forceVisibleRefresh = this.forceVisibleRefresh;
                this.forceVisibleRefresh = false;
                const response = await fetch("/progress/refresh", {
                    method: "POST",
                    headers: {
                        "Accept": "application/json",
                        "Content-Type": "application/json",
                    },
                    cache: "no-store",
                    body: JSON.stringify({
                        hide_upcoming: this.hideUpcoming,
                        show_dropped: this.showDropped,
                        min_year: this.minYear,
                        use_year_filter: this.useYearFilter,
                        viewport_card_keys: this.viewportKeys(),
                        nearby_card_keys: this.nearbyKeys(),
                        page_card_keys: this.pageCardKeys(),
                        force_visible_refresh: forceVisibleRefresh ? "1" : "0",
                        queue_after_revision: this.queueRevision,
                    }),
                });
                if (!response.ok) {
                    if (this.pollAttempts > 75) {
                        this.stopPolling();
                        this.scheduleIdlePoll();
                    }
                    return;
                }
                const payload = await response.json();
                this.applyRefresh(payload);
                this.progressSyncRunning = Boolean(payload && payload.progress_sync_running);
                const queue = payload && typeof payload.queue === "object" ? payload.queue : null;
                this.queueRevision = queue && Number.isFinite(queue.revision) ? Number(queue.revision) : this.queueRevision;
                this.queueRunning = Boolean(queue && queue.running);
                if (!this.hasRunningJobs()) {
                    this.stopPolling();
                    this.scheduleIdlePoll();
                }
            } catch (_error) {
                if (this.pollAttempts > 75) {
                    this.stopPolling();
                    this.scheduleIdlePoll();
                }
            } finally {
                this.pollInFlight = false;
            }
        }
    }

    const root = document.getElementById("progress-page-root");
    if (!root) {
        return;
    }
    const controller = new ProgressRefreshController(root);
    if (controller.pageCardKeys().length || controller.hasRunningJobs()) {
        controller.startPolling();
    }
    controller.scheduleIdlePoll();
    window.addEventListener("trakt-notifications-received", () => {
        controller.progressSyncRunning = true;
        controller.startPolling();
    });
})();
