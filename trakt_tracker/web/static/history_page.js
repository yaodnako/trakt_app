(() => {
    if (!window.fetch) {
        return;
    }
    let activeController = null;
    let filterRequest = null;
    let titleFilterTimer = null;

    function filterUrl(form) {
        const target = new URL(form.action || "/history", window.location.href);
        target.search = new URLSearchParams(new FormData(form)).toString();
        target.searchParams.set("page", "1");
        return target;
    }

    async function navigateHistory(
        target,
        {push = true, restoreTitleFocus = false, scrollToPageStart = false} = {},
    ) {
        filterRequest?.abort();
        const controller = new AbortController();
        filterRequest = controller;
        const currentRegion = document.getElementById("history-page-region");
        currentRegion?.setAttribute("aria-busy", "true");
        const activeInput = document.querySelector('.history-filter-form input[name="title"]');
        const selectionStart = activeInput instanceof HTMLInputElement ? activeInput.selectionStart : null;
        const selectionEnd = activeInput instanceof HTMLInputElement ? activeInput.selectionEnd : null;
        try {
            const response = await fetch(target.toString(), {
                headers: {"Accept": "text/html"},
                cache: "no-store",
                signal: controller.signal,
            });
            if (!response.ok) throw new Error(`History request failed: ${response.status}`);
            const html = await response.text();
            const parsed = new DOMParser().parseFromString(html, "text/html");
            const incoming = parsed.getElementById("history-page-region");
            const current = document.getElementById("history-page-region");
            if (!incoming || !current) throw new Error("History response is incomplete");
            activeController?.dispose();
            current.replaceWith(incoming);
            if (push) history.pushState(null, "", target.toString());
            else history.replaceState(null, "", target.toString());
            document.title = parsed.title || document.title;
            bindFilterControls();
            startHistoryController();
            if (scrollToPageStart) {
                const pageRoot = document.getElementById("history-page-root");
                pageRoot?.scrollIntoView({block: "start", behavior: "auto"});
            }
            if (restoreTitleFocus) {
                const nextInput = document.querySelector('.history-filter-form input[name="title"]');
                if (nextInput instanceof HTMLInputElement) {
                    nextInput.focus({preventScroll: true});
                    if (selectionStart !== null && selectionEnd !== null) {
                        nextInput.setSelectionRange(
                            Math.min(selectionStart, nextInput.value.length),
                            Math.min(selectionEnd, nextInput.value.length),
                        );
                    }
                }
            }
        } catch (error) {
            if (error?.name !== "AbortError") {
                window.pushFlashToast?.("Could not update History.", 3200);
            }
        } finally {
            if (filterRequest === controller) filterRequest = null;
            document.getElementById("history-page-region")?.removeAttribute("aria-busy");
        }
    }

    function bindFilterControls() {
        const form = document.querySelector(".history-filter-form");
        if (!(form instanceof HTMLFormElement)) return;
        const historyTypeSelect = form.querySelector('select[name="type"]');
        const titleFilterInput = form.querySelector('input[name="title"]');
        const ratedOnlyToggle = form.querySelector('input[name="rated_only"]');
        const sortSelect = form.querySelector('select[name="sort"]');
        const sortDirectionInput = form.querySelector('input[name="sort_dir"]');
        const sortDirectionButton = form.querySelector("[data-history-sort-direction]");
        const applyImmediately = () => navigateHistory(filterUrl(form));
        historyTypeSelect?.addEventListener("change", applyImmediately);
        ratedOnlyToggle?.addEventListener("change", applyImmediately);
        sortSelect?.addEventListener("change", applyImmediately);
        sortDirectionButton?.addEventListener("click", () => {
            if (!(sortDirectionInput instanceof HTMLInputElement)) return;
            sortDirectionInput.value = sortDirectionInput.value === "asc" ? "desc" : "asc";
            applyImmediately();
        });
        if (titleFilterInput instanceof HTMLInputElement) {
            let lastSubmittedTitle = titleFilterInput.value;
            const applyTitle = () => {
                if (titleFilterInput.value === lastSubmittedTitle) return;
                lastSubmittedTitle = titleFilterInput.value;
                navigateHistory(filterUrl(form), {push: false, restoreTitleFocus: true});
            };
            titleFilterInput.addEventListener("input", () => {
                if (titleFilterTimer !== null) window.clearTimeout(titleFilterTimer);
                titleFilterTimer = window.setTimeout(() => {
                    titleFilterTimer = null;
                    applyTitle();
                }, 600);
            });
            titleFilterInput.addEventListener("keydown", (event) => {
                if (event.key !== "Enter") return;
                event.preventDefault();
                if (titleFilterTimer !== null) window.clearTimeout(titleFilterTimer);
                titleFilterTimer = null;
                applyTitle();
            });
        }
    }

    class HistoryRefreshController {
        constructor(root) {
            this.root = root;
            this.page = Number(root.dataset.historyPage || "1") || 1;
            this.historyType = root.dataset.historyType || "all";
            this.titleFilter = root.dataset.historyTitleFilter || "";
            this.ratedOnly = root.dataset.historyRatedOnly || "0";
            this.historyView = root.dataset.historyView || "episodes";
            this.historySort = root.dataset.historySort || "last_watched";
            this.historySortDirection = root.dataset.historySortDirection || "desc";
            this.shouldAutoSync = this.page === 1;
            this.idleRefreshIntervalMs = 300000;
            this.viewportRefreshDebounceMs = 180;
            this.scrollIdleDelayMs = 280;
            this.scrollActiveUntil = 0;
            this.historySyncRunning = root.dataset.historySyncRunning === "1";
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
            this.viewportTitleKeys = new Set();
            this.nearbyTitleKeys = new Set();
            this.viewportObserver = null;
            this.nearbyObserver = null;
            this.syncStatusAfter = 0;
            this.syncToast = null;
            this.syncToastTimer = null;
            this.onScrollActivity = () => {
                this.scrollActiveUntil = Date.now() + this.scrollIdleDelayMs;
            };
            window.addEventListener("wheel", this.onScrollActivity, {passive: true});
            window.addEventListener("touchmove", this.onScrollActivity, {passive: true});
            window.addEventListener("scroll", this.onScrollActivity, {passive: true});
            this.setupObservers();
        }

        isScrollActive() {
            return Date.now() < this.scrollActiveUntil;
        }

        async waitForScrollIdle() {
            while (this.isScrollActive()) {
                const waitMs = Math.max(16, this.scrollActiveUntil - Date.now() + 16);
                await new Promise((resolve) => window.setTimeout(resolve, waitMs));
            }
        }

        async yieldToBrowser() {
            await new Promise((resolve) => window.requestAnimationFrame(() => resolve()));
        }

        pageTitleKeys() {
            return Array.from(this.root.querySelectorAll("[data-history-title-key]"))
                .map((node) => node.dataset.historyTitleKey || "")
                .filter((value) => value);
        }

        viewportKeys() {
            const pageKeys = this.pageTitleKeys();
            if (!this.viewportObserver) {
                return pageKeys;
            }
            return pageKeys.filter((key) => this.viewportTitleKeys.has(key));
        }

        nearbyKeys() {
            const pageKeys = this.pageTitleKeys();
            if (!this.nearbyObserver) {
                return [];
            }
            return pageKeys.filter((key) => this.nearbyTitleKeys.has(key) && !this.viewportTitleKeys.has(key));
        }

        hasRunningJobs() {
            return Boolean(this.historySyncRunning || this.queueRunning);
        }

        setupObservers() {
            if (!("IntersectionObserver" in window)) {
                return;
            }
            this.viewportObserver = new IntersectionObserver(
                (entries) => {
                    for (const entry of entries) {
                        const titleKey = entry.target.dataset.historyTitleKey || "";
                        if (!titleKey) {
                            continue;
                        }
                        const wasVisible = this.viewportTitleKeys.has(titleKey);
                        if (entry.isIntersecting) {
                            this.viewportTitleKeys.add(titleKey);
                            if (!wasVisible) {
                                this.requestVisibleRefresh();
                            }
                        } else {
                            this.viewportTitleKeys.delete(titleKey);
                        }
                    }
                },
                {threshold: 0.01}
            );
            this.nearbyObserver = new IntersectionObserver(
                (entries) => {
                    for (const entry of entries) {
                        const titleKey = entry.target.dataset.historyTitleKey || "";
                        if (!titleKey) {
                            continue;
                        }
                        if (entry.isIntersecting) {
                            this.nearbyTitleKeys.add(titleKey);
                        } else {
                            this.nearbyTitleKeys.delete(titleKey);
                        }
                    }
                },
                {threshold: 0, rootMargin: "600px 0px"}
            );
            this.observeTitleCards();
        }

        observeTitleCards() {
            if (!this.viewportObserver || !this.nearbyObserver) {
                return;
            }
            const currentKeys = new Set(
                Array.from(this.root.querySelectorAll("[data-history-title-key]"))
                    .map((node) => node.dataset.historyTitleKey || "")
                    .filter((value) => value)
            );
            this.viewportTitleKeys = new Set(
                Array.from(this.viewportTitleKeys).filter((key) => currentKeys.has(key))
            );
            this.nearbyTitleKeys = new Set(
                Array.from(this.nearbyTitleKeys).filter((key) => currentKeys.has(key))
            );
            this.viewportObserver.disconnect();
            this.nearbyObserver.disconnect();
            for (const card of this.root.querySelectorAll("[data-history-title-key]")) {
                this.viewportObserver.observe(card);
                this.nearbyObserver.observe(card);
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

        dispose() {
            this.stopPolling();
            if (this.idlePollTimer !== null) window.clearTimeout(this.idlePollTimer);
            if (this.viewportRefreshTimer !== null) window.clearTimeout(this.viewportRefreshTimer);
            if (this.syncToastTimer !== null) window.clearTimeout(this.syncToastTimer);
            this.viewportObserver?.disconnect();
            this.nearbyObserver?.disconnect();
            window.removeEventListener("wheel", this.onScrollActivity);
            window.removeEventListener("touchmove", this.onScrollActivity);
            window.removeEventListener("scroll", this.onScrollActivity);
            this.idlePollTimer = null;
            this.viewportRefreshTimer = null;
            this.syncToastTimer = null;
        }

        scheduleIdlePoll() {
            if (this.pollTimer !== null || this.idlePollTimer !== null) {
                return;
            }
            this.idlePollTimer = window.setTimeout(() => {
                this.idlePollTimer = null;
                this.autoSync().finally(() => {
                    this.forceVisibleRefresh = true;
                    this.startPolling();
                });
            }, this.idleRefreshIntervalMs);
        }

        requestVisibleRefresh() {
            if (this.page !== 1) {
                return;
            }
            if (this.viewportRefreshTimer !== null) {
                window.clearTimeout(this.viewportRefreshTimer);
            }
            this.viewportRefreshTimer = window.setTimeout(() => {
                this.viewportRefreshTimer = null;
                if (this.isScrollActive()) {
                    this.requestVisibleRefresh();
                    return;
                }
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

        updateDayCards() {
            const dayCards = Array.from(document.querySelectorAll("[data-history-day-card]"));
            for (const dayCard of dayCards) {
                const titleCards = Array.from(dayCard.querySelectorAll("[data-history-title-key]"));
                if (!titleCards.length) {
                    dayCard.remove();
                    continue;
                }
                const totalEntries = titleCards.reduce((sum, card) => {
                    const parsed = Number(card.dataset.historyEntryCount || "0");
                    return sum + (Number.isFinite(parsed) ? parsed : 0);
                }, 0);
                const countNode = dayCard.querySelector(".history-day-count");
                if (countNode) {
                    countNode.textContent = `${totalEntries} watch${totalEntries === 1 ? "" : "es"}`;
                }
            }
            const emptyState = document.querySelector("[data-history-empty-state]");
            if (emptyState) {
                emptyState.hidden = document.querySelectorAll("[data-history-title-key]").length > 0;
            }
            this.observeTitleCards();
        }

        async applyRefresh(payload) {
            const renderedGroups = Array.isArray(payload.title_groups) ? payload.title_groups : [];
            let domChanged = false;
            for (const item of renderedGroups) {
                await this.waitForScrollIdle();
                const titleKey = String(item && item.title_key ? item.title_key : "");
                const html = String(item && item.html ? item.html : "");
                if (!titleKey || !html) {
                    continue;
                }
                const existing = document.querySelector(`[data-history-title-key="${CSS.escape(titleKey)}"]`);
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
                domChanged = true;
                await this.yieldToBrowser();
            }

            const missingGroups = Array.isArray(payload.missing_title_keys) ? payload.missing_title_keys : [];
            for (const titleKey of missingGroups) {
                await this.waitForScrollIdle();
                const existing = document.querySelector(`[data-history-title-key="${CSS.escape(String(titleKey))}"]`);
                if (existing) {
                    existing.remove();
                    domChanged = true;
                    await this.yieldToBrowser();
                }
            }

            if (domChanged) {
                await this.waitForScrollIdle();
                this.updateDayCards();
            }

            if (payload && payload.page_changed && !this.pageChangeNotified && window.pushFlashToast) {
                window.pushFlashToast("History updated; refresh page to show new items/order.", 4200);
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
                await this.pollSyncStatus();
                const forceVisibleRefresh = this.forceVisibleRefresh;
                this.forceVisibleRefresh = false;
                const response = await fetch("/history/refresh", {
                    method: "POST",
                    headers: {
                        "Accept": "application/json",
                        "Content-Type": "application/json",
                    },
                    cache: "no-store",
                    body: JSON.stringify({
                        type: this.historyType,
                        view: this.historyView,
                        title_filter: this.titleFilter,
                        rated_only: this.ratedOnly,
                        sort: this.historySort,
                        sort_dir: this.historySortDirection,
                        page: this.page,
                        viewport_title_keys: this.viewportKeys(),
                        nearby_title_keys: this.nearbyKeys(),
                        page_title_keys: this.pageTitleKeys(),
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
                await this.applyRefresh(payload);
                this.historySyncRunning = Boolean(payload && payload.history_sync_running);
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

        applySyncStatus(payload) {
            if (!payload) {
                return;
            }
            const running = Boolean(payload.running);
            this.historySyncRunning = running;
            const events = Array.isArray(payload.events) ? payload.events : [];
            let latestMessage = "";
            for (const event of events) {
                const message = String(event && event.message ? event.message : "");
                if (message) {
                    latestMessage = message;
                }
                if (event && Number.isFinite(event.seq)) {
                    this.syncStatusAfter = Math.max(this.syncStatusAfter, Number(event.seq));
                }
            }
            const progressMessage = String(payload.progress_message || "");
            if (window.traktDebugMode && running) {
                this.showSyncToast(progressMessage || latestMessage || "Running…");
            } else if (window.traktDebugMode && this.syncToast) {
                this.showSyncToast(latestMessage || "Complete.", 4200);
            }
        }

        showSyncToast(message, timeoutMs = 0) {
            const stack = document.getElementById("web-flash-stack");
            if (!stack) {
                return;
            }
            if (!this.syncToast) {
                this.syncToast = document.createElement("div");
                this.syncToast.className = "web-flash";
                this.syncToast.setAttribute("role", "status");
                stack.appendChild(this.syncToast);
            }
            this.syncToast.classList.remove("is-leaving");
            this.syncToast.textContent = `History sync: ${message}`;
            if (this.syncToastTimer !== null) {
                window.clearTimeout(this.syncToastTimer);
                this.syncToastTimer = null;
            }
            if (timeoutMs > 0) {
                const toast = this.syncToast;
                this.syncToastTimer = window.setTimeout(() => {
                    toast.classList.add("is-leaving");
                    window.setTimeout(() => toast.remove(), 320);
                    if (this.syncToast === toast) {
                        this.syncToast = null;
                    }
                    this.syncToastTimer = null;
                }, timeoutMs);
            }
        }

        async pollSyncStatus() {
            try {
                const response = await fetch(`/history/sync-status?after=${this.syncStatusAfter}`, {
                    headers: {"Accept": "application/json"},
                    cache: "no-store",
                });
                if (!response.ok) {
                    return;
                }
                const payload = await response.json();
                this.applySyncStatus(payload);
            } catch (_error) {
            }
        }

        async autoSync() {
            if (!this.shouldAutoSync) {
                return;
            }
            if (window.traktDebugMode && window.pushFlashToast) {
                window.pushFlashToast("History auto-sync: checking…", 2400);
            }
            try {
                const response = await fetch("/history/auto-sync", {
                    headers: {"Accept": "application/json"},
                    cache: "no-store",
                });
                const payload = response.ok ? await response.json() : null;
                if (window.traktDebugMode && window.pushFlashToast) {
                    if (payload && payload.error) {
                        window.pushFlashToast(`History auto-sync failed: ${payload.error}`, 4200);
                    } else if (payload && payload.started) {
                        window.pushFlashToast("History auto-sync started.", 2400);
                    } else {
                        window.pushFlashToast("History auto-sync: no changes.", 2400);
                    }
                }
                if (payload && (payload.changed || payload.started)) {
                    this.startPolling();
                }
            } catch (_error) {
            }
        }
    }

    function startHistoryController() {
        const root = document.getElementById("history-page-root");
        if (!root) return;
        activeController = new HistoryRefreshController(root);
        const needsPolling = (
            (activeController.page === 1 && activeController.pageTitleKeys().length)
            || activeController.hasRunningJobs()
        );
        if (needsPolling) {
            activeController.startPolling();
        } else {
            activeController.pollSyncStatus();
        }
        activeController.scheduleIdlePoll();
        activeController.autoSync();
    }

    document.addEventListener("click", (event) => {
        const link = event.target.closest("#history-page-region a[href^='/history']");
        if (!link || link.target || event.defaultPrevented) return;
        const scrollToPageStart = Boolean(link.closest(".history-pager"));
        event.preventDefault();
        navigateHistory(new URL(link.href, window.location.href), {scrollToPageStart});
    });
    window.addEventListener("popstate", () => {
        if (window.location.pathname === "/history") {
            navigateHistory(new URL(window.location.href), {push: false});
        }
    });
    bindFilterControls();
    startHistoryController();
})();
