(() => {
    const runtimeValue = (name, fallback = "") => document.querySelector(`meta[name="${name}"]`)?.content ?? fallback;
    const runtimeBoolean = (name, fallback = false) => {
        const value = runtimeValue(name, fallback ? "true" : "false");
        return value === "true" || value === "1";
    };
    if (!window.fetch) {
        return;
    }

    const csrfToken = document.querySelector('meta[name="trakt-csrf-token"]')?.content || "";
    const nativeFetch = window.fetch.bind(window);
    window.fetch = (input, init = {}) => {
        const inputRequest = input instanceof Request ? input : null;
        const method = String(init.method || inputRequest?.method || "GET").toUpperCase();
        const target = new URL(inputRequest?.url || String(input), window.location.href);
        if (["POST", "PUT", "PATCH", "DELETE"].includes(method) && target.origin === window.location.origin) {
            const headers = new Headers(init.headers || inputRequest?.headers || undefined);
            headers.set("X-Trakt-CSRF", csrfToken);
            init = {...init, headers};
        }
        return nativeFetch(input, init);
    };
    document.addEventListener("submit", (event) => {
        const form = event.target;
        if (!(form instanceof HTMLFormElement)) return;
        const method = String(form.method || "GET").toUpperCase();
        if (!["POST", "PUT", "PATCH", "DELETE"].includes(method)) return;
        let field = form.querySelector('input[name="_csrf"]');
        if (!field) {
            field = document.createElement("input");
            field.type = "hidden";
            field.name = "_csrf";
            form.appendChild(field);
        }
        field.value = csrfToken;
    }, true);

    const DEBUG_MODE = runtimeBoolean("trakt-debug-mode");
    const NOTIFICATIONS_BROWSER_POLL_ENABLED = runtimeBoolean("trakt-notifications-browser-poll-enabled", true);
    const NOTIFICATION_SOUND_URL = runtimeValue("trakt-notification-sound-url");
    const NOTIFICATION_SOURCE_PATHS = {
        progress: "/progress",
        release: "/release-tracking",
    };
    const confirmOverlay = document.getElementById("trakt-confirm-overlay");
    const confirmTitle = document.getElementById("trakt-confirm-title");
    const confirmMessage = document.getElementById("trakt-confirm-message");
    const confirmAccept = confirmOverlay?.querySelector("[data-trakt-confirm-accept]");
    const PLAY_PROMPT_STORAGE_KEY = "trakt-progress-play-prompts";
    let lastDebugSeq = Number(runtimeValue("trakt-debug-initial-seq", "0")) || 0;
    let lastNotificationActivitySeq = Number(runtimeValue("trakt-notification-activity-initial-seq", "0")) || 0;
    let unreadNotificationSources = new Set(
        runtimeValue("trakt-notification-pending-sources")
            .split(",")
            .filter((source) => Object.hasOwn(NOTIFICATION_SOURCE_PATHS, source))
    );
    const notificationPulseTimers = new Map();
    let audioContext = null;
    let audioUnlocked = false;
    let alertAudio = null;

    function pushFlashToast(message, timeoutMs = 4200) {
        const stack = document.getElementById("web-flash-stack");
        if (!stack || !message) {
            return;
        }
        const toast = document.createElement("div");
        toast.className = "web-flash";
        toast.textContent = message;
        stack.appendChild(toast);
        window.setTimeout(() => {
            toast.classList.add("is-leaving");
            window.setTimeout(() => toast.remove(), 320);
        }, timeoutMs);
    }
    window.pushFlashToast = pushFlashToast;
    window.traktDebugMode = DEBUG_MODE;

    function syncNotificationNavState() {
        for (const source of Object.keys(NOTIFICATION_SOURCE_PATHS)) {
            const unread = unreadNotificationSources.has(source);
            document.body.classList.toggle(`has-notification-${source}`, unread);
            const link = document.querySelector(`.nav a[data-notification-source="${source}"]`);
            if (!link) {
                continue;
            }
            if (unread) {
                link.setAttribute("data-notification-unread", "true");
                link.setAttribute(
                    "aria-label",
                    `${link.dataset.notificationLabel || link.textContent.trim()}: notification waiting`
                );
            } else {
                link.removeAttribute("data-notification-unread");
                link.removeAttribute("aria-label");
            }
        }
    }

    function setPendingNotificationSources(sources) {
        unreadNotificationSources = new Set(
            sources.filter((source) => Object.hasOwn(NOTIFICATION_SOURCE_PATHS, source))
        );
        syncNotificationNavState();
    }

    function pulseNotificationSource(source) {
        if (!Object.hasOwn(NOTIFICATION_SOURCE_PATHS, source)) {
            return;
        }
        const className = `is-notifying-${source}`;
        const existingTimer = notificationPulseTimers.get(source);
        if (existingTimer) {
            window.clearTimeout(existingTimer);
        }
        document.body.classList.remove(className);
        void document.body.offsetWidth;
        document.body.classList.add(className);
        notificationPulseTimers.set(source, window.setTimeout(() => {
            document.body.classList.remove(className);
            notificationPulseTimers.delete(source);
        }, 3600));
    }

    function handleNotificationSources(sources) {
        for (const source of new Set(sources)) {
            if (!Object.hasOwn(NOTIFICATION_SOURCE_PATHS, source)) {
                continue;
            }
            pulseNotificationSource(source);
            unreadNotificationSources.add(source);
        }
        syncNotificationNavState();
    }

    function handleNotificationActivity(items, activitySeq = null) {
        const numericSeq = Number(activitySeq);
        if (Number.isFinite(numericSeq) && numericSeq > 0) {
            if (numericSeq <= lastNotificationActivitySeq) {
                return;
            }
            lastNotificationActivitySeq = numericSeq;
        }
        handleNotificationSources(
            items.map((item) => String(item && item.source ? item.source : "")).filter(Boolean)
        );
    }

    const dialogStack = [];
    const managedInertNodes = new Set();
    const focusableSelector = [
        "a[href]",
        "button:not([disabled])",
        "input:not([disabled])",
        "select:not([disabled])",
        "textarea:not([disabled])",
        "[tabindex]:not([tabindex='-1'])",
    ].join(",");

    function syncManagedDialogs() {
        const top = dialogStack.at(-1)?.overlay || null;
        for (const node of managedInertNodes) node.inert = false;
        managedInertNodes.clear();
        for (const state of dialogStack) {
            state.overlay.inert = state.overlay !== top;
        }
        let activeBranch = top;
        while (activeBranch?.parentElement) {
            for (const sibling of activeBranch.parentElement.children) {
                if (!(sibling instanceof HTMLElement) || sibling === activeBranch) continue;
                sibling.inert = true;
                managedInertNodes.add(sibling);
            }
            activeBranch = activeBranch.parentElement;
        }
        syncOverlayBodyLock();
    }

    function openManagedDialog(overlay, {initialFocus = null, onEscape = null} = {}) {
        if (!(overlay instanceof HTMLElement)) return;
        const previousIndex = dialogStack.findIndex((state) => state.overlay === overlay);
        if (previousIndex >= 0) dialogStack.splice(previousIndex, 1);
        dialogStack.push({
            overlay,
            previousFocus: document.activeElement instanceof HTMLElement ? document.activeElement : null,
            onEscape,
        });
        overlay.hidden = false;
        overlay.classList.add("is-open");
        overlay.setAttribute("aria-hidden", "false");
        syncManagedDialogs();
        window.setTimeout(() => {
            let target = initialFocus;
            if (typeof target === "string") target = overlay.querySelector(target);
            if (!(target instanceof HTMLElement)) target = overlay.querySelector("[role='dialog'], [role='alertdialog']");
            if (target instanceof HTMLElement) {
                if (!target.matches(focusableSelector) && !target.hasAttribute("tabindex")) target.tabIndex = -1;
                target.focus({preventScroll: true});
            }
        }, 0);
    }

    function closeManagedDialog(overlay, {restoreFocus = true} = {}) {
        if (!(overlay instanceof HTMLElement)) return;
        const index = dialogStack.findIndex((state) => state.overlay === overlay);
        const state = index >= 0 ? dialogStack.splice(index, 1)[0] : null;
        overlay.hidden = true;
        overlay.classList.remove("is-open");
        overlay.setAttribute("aria-hidden", "true");
        overlay.inert = false;
        syncManagedDialogs();
        if (restoreFocus && state?.previousFocus instanceof HTMLElement && document.contains(state.previousFocus)) {
            state.previousFocus.focus({preventScroll: true});
        }
    }

    document.addEventListener("keydown", (event) => {
        const state = dialogStack.at(-1);
        if (!state) return;
        if (event.key === "Escape") {
            event.preventDefault();
            event.stopImmediatePropagation();
            if (typeof state.onEscape === "function") state.onEscape();
            else closeManagedDialog(state.overlay);
            return;
        }
        if (event.key !== "Tab") return;
        const focusable = Array.from(state.overlay.querySelectorAll(focusableSelector))
            .filter((node) => node instanceof HTMLElement && !node.hidden && node.getClientRects().length);
        if (!focusable.length) {
            event.preventDefault();
            state.overlay.querySelector("[role='dialog'], [role='alertdialog']")?.focus();
            return;
        }
        const first = focusable[0];
        const last = focusable.at(-1);
        if (event.shiftKey && document.activeElement === first) {
            event.preventDefault();
            last.focus();
        } else if (!event.shiftKey && document.activeElement === last) {
            event.preventDefault();
            first.focus();
        }
    }, true);

    window.TraktDialogs = {
        open: openManagedDialog,
        close: closeManagedDialog,
        sync: syncManagedDialogs,
    };

    let confirmResolve = null;

    function closeConfirmModal(accepted) {
        if (!confirmOverlay || confirmOverlay.hidden) return;
        const resolve = confirmResolve;
        confirmResolve = null;
        closeManagedDialog(confirmOverlay);
        resolve?.(accepted);
    }

    function openConfirmModal({title = "Confirm action", message = "", confirmLabel = "Confirm", danger = true} = {}) {
        if (!confirmOverlay || !confirmTitle || !confirmMessage || !confirmAccept) {
            return Promise.resolve(false);
        }
        if (confirmResolve) closeConfirmModal(false);
        confirmTitle.textContent = title;
        confirmMessage.textContent = message;
        confirmAccept.textContent = confirmLabel;
        confirmAccept.classList.toggle("is-danger", Boolean(danger));
        openManagedDialog(confirmOverlay, {
            initialFocus: "[data-trakt-confirm-cancel]",
            onEscape: () => closeConfirmModal(false),
        });
        return new Promise((resolve) => {
            confirmResolve = resolve;
        });
    }
    window.traktConfirm = openConfirmModal;

    document.addEventListener("click", (event) => {
        if (event.target.closest("[data-trakt-confirm-cancel]")) {
            event.preventDefault();
            closeConfirmModal(false);
        } else if (event.target.closest("[data-trakt-confirm-accept]")) {
            event.preventDefault();
            closeConfirmModal(true);
        }
    });

    document.addEventListener("submit", async (event) => {
        const form = event.target;
        if (!(form instanceof HTMLFormElement) || !form.dataset.confirmMessage) return;
        if (form.dataset.confirmBypass === "true") {
            delete form.dataset.confirmBypass;
            return;
        }
        event.preventDefault();
        event.stopImmediatePropagation();
        const accepted = await openConfirmModal({
            title: form.dataset.confirmTitle || "Confirm action",
            message: form.dataset.confirmMessage,
            confirmLabel: form.dataset.confirmLabel || "Confirm",
            danger: form.dataset.confirmDanger !== "false",
        });
        if (!accepted) return;
        form.dataset.confirmBypass = "true";
        form.requestSubmit(event.submitter || undefined);
    }, true);

    document.addEventListener("load", (event) => {
        const image = event.target;
        if (!(image instanceof HTMLImageElement) || image.naturalWidth > 1) {
            return;
        }
        let current;
        try {
            current = new URL(image.currentSrc || image.src, window.location.href);
        } catch (_error) {
            return;
        }
        if (current.pathname !== "/cached-image") {
            return;
        }
        const attempts = Number(image.dataset.cachedImageRetry || "0");
        if (!Number.isFinite(attempts) || attempts >= 8) {
            return;
        }
        image.dataset.cachedImageRetry = String(attempts + 1);
        window.setTimeout(() => {
            if (!document.contains(image) || image.naturalWidth > 1) {
                return;
            }
            current.searchParams.set("cb", String(Date.now()));
            image.src = current.toString();
        }, Math.min(5000, 600 * (attempts + 1)));
    }, true);

    document.addEventListener("error", (event) => {
        const image = event.target;
        if (!(image instanceof HTMLImageElement)) return;
        let current;
        try {
            current = new URL(image.currentSrc || image.src, window.location.href);
        } catch (_error) {
            return;
        }
        if (current.pathname !== "/cached-image") return;
        const attempts = Number(image.dataset.cachedImageRetry || "0");
        if (!Number.isFinite(attempts) || attempts >= 8) return;
        image.dataset.cachedImageRetry = String(attempts + 1);
        window.setTimeout(() => {
            if (!document.contains(image)) return;
            current.searchParams.set("cb", String(Date.now()));
            image.src = current.toString();
        }, Math.min(5000, 600 * (attempts + 1)));
    }, true);

    function syncOverlayBodyLock() {
        const overlays = document.querySelectorAll(
            ".title-matrix-overlay, .trakt-rating-overlay, .trakt-confirm-overlay, .search-watch-overlay, .search-watch-date-overlay",
        );
        const hasOpenOverlay = Array.from(overlays).some((node) => !node.hidden);
        document.body.classList.toggle("has-title-matrix-overlay", hasOpenOverlay);
    }
    window.traktSyncOverlayBodyLock = syncOverlayBodyLock;

    function playFallbackTone() {
        try {
            audioContext = audioContext || new (window.AudioContext || window.webkitAudioContext)();
            if (audioContext.state === "suspended") {
                audioContext.resume().catch(() => {});
            }
            const oscillator = audioContext.createOscillator();
            const gain = audioContext.createGain();
            oscillator.type = "sine";
            oscillator.frequency.value = 880;
            gain.gain.setValueAtTime(0.0001, audioContext.currentTime);
            gain.gain.exponentialRampToValueAtTime(0.08, audioContext.currentTime + 0.01);
            gain.gain.exponentialRampToValueAtTime(0.0001, audioContext.currentTime + 0.22);
            oscillator.connect(gain);
            gain.connect(audioContext.destination);
            oscillator.start();
            oscillator.stop(audioContext.currentTime + 0.24);
        } catch (_error) {
        }
    }

    function playConfiguredNotificationSound() {
        if (!NOTIFICATION_SOUND_URL) {
            return false;
        }
        try {
            alertAudio = alertAudio || new Audio(NOTIFICATION_SOUND_URL);
            alertAudio.currentTime = 0;
            const playPromise = alertAudio.play();
            if (playPromise && typeof playPromise.catch === "function") {
                playPromise.catch(() => playFallbackTone());
            }
            return true;
        } catch (_error) {
            return false;
        }
    }

    function playAlertTone() {
        if (playConfiguredNotificationSound()) {
            return;
        }
        playFallbackTone();
    }
    window.traktPlayAlertTone = playAlertTone;

    function unlockAudio() {
        if (audioUnlocked) {
            return;
        }
        try {
            audioContext = audioContext || new (window.AudioContext || window.webkitAudioContext)();
            audioContext.resume().catch(() => {});
            if (NOTIFICATION_SOUND_URL) {
                alertAudio = alertAudio || new Audio(NOTIFICATION_SOUND_URL);
                alertAudio.load();
            }
            audioUnlocked = true;
        } catch (_error) {
        }
    }

    async function ensureNotificationPermission() {
        if (!window.Notification) {
            return "unsupported";
        }
        if (Notification.permission === "granted" || Notification.permission === "denied") {
            return Notification.permission;
        }
        try {
            return await Notification.requestPermission();
        } catch (_error) {
            return Notification.permission;
        }
    }

    function readPlayPrompts() {
        try {
            const raw = window.localStorage.getItem(PLAY_PROMPT_STORAGE_KEY);
            const parsed = raw ? JSON.parse(raw) : [];
            return Array.isArray(parsed) ? parsed : [];
        } catch (_error) {
            return [];
        }
    }

    function writePlayPrompts(items) {
        try {
            window.localStorage.setItem(PLAY_PROMPT_STORAGE_KEY, JSON.stringify(items));
        } catch (_error) {
        }
    }

    function upsertPlayPrompt(item) {
        const prompts = readPlayPrompts().filter((entry) => entry.key !== item.key);
        prompts.unshift(item);
        writePlayPrompts(prompts.slice(0, 8));
    }

    function removePlayPrompt(key) {
        const nextItems = readPlayPrompts().filter((entry) => entry.key !== key);
        writePlayPrompts(nextItems);
    }

    function submitPlayPromptWatch(prompt) {
        removePlayPrompt(prompt.key);
        renderPlayPrompts();
        const form = document.createElement("form");
        form.method = "post";
        form.action = `/progress/${prompt.traktId}/watch`;
        form.style.display = "none";
        [
            ["hide_upcoming", prompt.hideUpcoming || "0"],
            ["show_paused", prompt.showPaused || "0"],
            ["show_dropped", prompt.showDropped || "0"],
            ["sort", prompt.sort || "episode_release"],
            ["direction", prompt.direction || "desc"],
            ["min_year", prompt.minYear || ""],
            ["use_year_filter", prompt.useYearFilter || "0"],
        ].forEach(([name, value]) => {
            const input = document.createElement("input");
            input.type = "hidden";
            input.name = name;
            input.value = value;
            form.appendChild(input);
        });
        document.body.appendChild(form);
        form.requestSubmit();
    }

    function renderPlayPrompts() {
        const stack = document.getElementById("web-play-prompt-stack");
        if (!stack) {
            return;
        }
        stack.replaceChildren();
        for (const prompt of readPlayPrompts()) {
            const toast = document.createElement("section");
            toast.className = "web-play-prompt";

            const title = document.createElement("strong");
            title.textContent = prompt.title || "Ready to mark watched?";
            toast.appendChild(title);

            const body = document.createElement("span");
            body.textContent = `Finish watching? S${String(prompt.season).padStart(2, "0")}E${String(prompt.episode).padStart(2, "0")} ${prompt.episodeTitle || ""}`.trim();
            toast.appendChild(body);

            const actions = document.createElement("div");
            actions.className = "web-play-prompt-actions";

            const watchBtn = document.createElement("button");
            watchBtn.type = "button";
            watchBtn.className = "button";
            watchBtn.textContent = "Watched";
            watchBtn.addEventListener("click", () => submitPlayPromptWatch(prompt));
            actions.appendChild(watchBtn);

            const dismissBtn = document.createElement("button");
            dismissBtn.type = "button";
            dismissBtn.className = "button ghost";
            dismissBtn.textContent = "Dismiss";
            dismissBtn.addEventListener("click", () => {
                removePlayPrompt(prompt.key);
                renderPlayPrompts();
            });
            actions.appendChild(dismissBtn);

            toast.appendChild(actions);
            stack.appendChild(toast);
        }
    }

    function showInPageNotifications(items) {
        const stack = document.getElementById("web-notification-stack");
        if (!stack) {
            return;
        }
        for (const item of items) {
            const toast = document.createElement("div");
            toast.className = "web-notification";
            const title = document.createElement("strong");
            title.textContent = item.show_title || "New episode";
            const body = document.createElement("span");
            body.textContent = item.message || "";
            toast.appendChild(title);
            toast.appendChild(body);
            stack.appendChild(toast);
            window.setTimeout(() => {
                toast.classList.add("is-leaving");
                window.setTimeout(() => toast.remove(), 320);
            }, 12000);
        }
    }

    function setupFlashToast() {
        const toast = document.querySelector(".web-flash");
        if (!toast) {
            return;
        }
        window.setTimeout(() => {
            toast.classList.add("is-leaving");
            window.setTimeout(() => toast.remove(), 320);
        }, 4200);
    }

    function bindPlayPromptLinks() {
        document.querySelectorAll(".js-play-link").forEach((link) => {
            if (link.dataset.playPromptBound) {
                return;
            }
            link.dataset.playPromptBound = "1";
            link.addEventListener("click", () => {
                const key = link.dataset.playPromptKey || "";
                if (!key) {
                    return;
                }
                upsertPlayPrompt({
                    key,
                    traktId: link.dataset.playTraktId || "",
                    title: link.dataset.playTitle || "",
                    season: link.dataset.playSeason || "0",
                    episode: link.dataset.playEpisode || "0",
                    episodeTitle: link.dataset.playEpisodeTitle || "",
                    hideUpcoming: link.dataset.playHideUpcoming || "0",
                    showPaused: link.dataset.playShowPaused || "0",
                    showDropped: link.dataset.playShowDropped || "0",
                    sort: link.dataset.playSort || "episode_release",
                    direction: link.dataset.playDirection || "desc",
                    useYearFilter: link.dataset.playUseYearFilter || "0",
                    minYear: link.dataset.playMinYear || "",
                });
                renderPlayPrompts();
            });
        });
        document.querySelectorAll(".js-watch-form").forEach((form) => {
            if (form.dataset.playPromptBound) {
                return;
            }
            form.dataset.playPromptBound = "1";
            form.addEventListener("submit", () => {
                const key = form.dataset.playPromptKey || "";
                if (!key) {
                    return;
                }
                removePlayPrompt(key);
                renderPlayPrompts();
            });
        });
    }
    window.traktBindPlayPromptLinks = bindPlayPromptLinks;
    window.traktRenderPlayPrompts = renderPlayPrompts;

    const titleMatrixOverlay = document.getElementById("title-matrix-overlay");
    const titleMatrixBody = document.getElementById("title-matrix-overlay-body");
    const titleMatrixTitle = document.getElementById("title-matrix-title");
    const titleMatrixSubtitle = titleMatrixOverlay ? titleMatrixOverlay.querySelector(".title-matrix-subtitle") : null;
    const titleMatrixTitleRatings = titleMatrixOverlay
        ? titleMatrixOverlay.querySelector("[data-title-matrix-title-ratings]")
        : null;
    const titleMatrixTooltip = document.getElementById("title-matrix-tooltip");
    const ratingOverlay = document.getElementById("trakt-rating-overlay");
    const ratingForm = document.getElementById("trakt-rating-form");
    const ratingTitle = document.getElementById("trakt-rating-title");
    const ratingSubtitle = document.getElementById("trakt-rating-subtitle");
    const ratingValueNode = ratingOverlay ? ratingOverlay.querySelector("[data-rating-value]") : null;
    const ratingError = document.getElementById("trakt-rating-error");
    const ratingStars = ratingOverlay ? Array.from(ratingOverlay.querySelectorAll("[data-rating-star]")) : [];
    let titleMatrixTrigger = null;
    let titleMatrixUrl = "";
    let titleMatrixProvider = "imdb";
    let titleMatrixRequest = null;
    let titleMatrixRefreshTimer = 0;
    let titleMatrixRefreshAttempts = 0;
    const titleMatrixCache = new Map();
    let ratingTrigger = null;
    let ratingContext = null;
    let selectedRating = 0;
    let hoverRating = 0;
    let ratingSaving = false;

    function ratingLabel(context) {
        if (!context) {
            return "";
        }
        const season = context.season ? Number(context.season) : 0;
        const episode = context.episode ? Number(context.episode) : 0;
        if (season && episode) {
            return `S${String(season).padStart(2, "0")}E${String(episode).padStart(2, "0")}`;
        }
        return "";
    }

    function updateRatingStars(value) {
        const activeValue = Number(value || 0);
        for (const star of ratingStars) {
            const starValue = Number(star.dataset.ratingStar || "0");
            const active = starValue <= activeValue;
            star.classList.toggle("is-filled", active);
            star.setAttribute("aria-checked", selectedRating === starValue ? "true" : "false");
        }
        if (ratingValueNode) {
            ratingValueNode.textContent = String(activeValue || selectedRating || 0);
        }
    }

    function closeRatingModal({restoreFocus = true} = {}) {
        if (!ratingOverlay) {
            return;
        }
        closeManagedDialog(ratingOverlay, {restoreFocus});
        if (ratingError) {
            ratingError.hidden = true;
            ratingError.textContent = "";
        }
        ratingTrigger = null;
        ratingContext = null;
        selectedRating = 0;
        hoverRating = 0;
        ratingSaving = false;
        updateRatingStars(0);
    }

    function findRatingTrigger(context) {
        const matchesContext = (node) => (
            node.dataset.ratingTitleType === String(context.titleType)
            && node.dataset.ratingTraktId === String(context.traktId)
            && node.dataset.ratingSeason === String(context.season)
            && node.dataset.ratingEpisode === String(context.episode)
        );
        const openPanel = document.querySelector(".search-watch-overlay:not([hidden])");
        const panelMatch = openPanel
            ? Array.from(openPanel.querySelectorAll("[data-rating-trigger]")).find(matchesContext)
            : null;
        return panelMatch || Array.from(document.querySelectorAll("[data-rating-trigger]")).find(matchesContext) || null;
    }

    function openRatingModal(context, trigger = null) {
        if (!ratingOverlay || !context) {
            return;
        }
        ratingContext = {
            titleType: context.titleType || context.title_type || context.ratingTitleType || "movie",
            traktId: context.traktId || context.trakt_id || context.ratingTraktId || "",
            title: context.title || context.ratingTitle || "",
            season: context.season || context.ratingSeason || "",
            episode: context.episode || context.ratingEpisode || "",
        };
        ratingTrigger = trigger instanceof HTMLElement ? trigger : findRatingTrigger(ratingContext);
        selectedRating = 0;
        hoverRating = 0;
        ratingSaving = false;
        if (ratingTitle) {
            ratingTitle.textContent = ratingContext.title ? `Rate ${ratingContext.title}` : "Rate title";
        }
        if (ratingSubtitle) {
            const label = ratingLabel(ratingContext);
            ratingSubtitle.textContent = label ? `Choose a score for ${label}` : "Choose a score";
        }
        if (ratingError) {
            ratingError.hidden = true;
            ratingError.textContent = "";
        }
        updateRatingStars(0);
        const firstStar = ratingStars[0];
        openManagedDialog(ratingOverlay, {
            initialFocus: firstStar,
            onEscape: () => closeRatingModal(),
        });
    }
    window.traktOpenRatingModal = openRatingModal;

    function openRatingModalFromTrigger(trigger) {
        openRatingModal(
            {
                titleType: trigger.dataset.ratingTitleType || "movie",
                traktId: trigger.dataset.ratingTraktId || "",
                title: trigger.dataset.ratingTitle || "",
                season: trigger.dataset.ratingSeason || "",
                episode: trigger.dataset.ratingEpisode || "",
            },
            trigger
        );
    }

    function applySavedRatingToTrigger(rating) {
        if (!(ratingTrigger instanceof HTMLElement)) {
            return;
        }
        if (ratingTrigger.classList.contains("history-rate-chip")) {
            const badge = document.createElement("button");
            badge.type = "button";
            badge.className = "history-rating-badge search-watch-user-rating";
            badge.setAttribute("data-rating-trigger", "");
            badge.dataset.ratingTitleType = ratingContext.titleType;
            badge.dataset.ratingTraktId = ratingContext.traktId;
            badge.dataset.ratingTitle = ratingContext.title;
            badge.dataset.ratingSeason = ratingContext.season;
            badge.dataset.ratingEpisode = ratingContext.episode;
            badge.setAttribute("aria-label", `Change rating for ${ratingLabel(ratingContext) || ratingContext.title}`);
            badge.textContent = `${rating} \u2605`;
            ratingTrigger.replaceWith(badge);
            return;
        }
        if (ratingTrigger.classList.contains("history-rating-badge")) {
            ratingTrigger.textContent = `${rating} \u2605`;
        }
    }

    async function submitRating() {
        if (!ratingContext || selectedRating < 1 || ratingSaving) {
            return;
        }
        ratingSaving = true;
        if (ratingError) {
            ratingError.hidden = true;
            ratingError.textContent = "";
        }
        try {
            const response = await fetch("/ratings", {
                method: "POST",
                headers: {
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                },
                cache: "no-store",
                body: JSON.stringify({
                    title_type: ratingContext.titleType,
                    trakt_id: ratingContext.traktId,
                    title: ratingContext.title,
                    season: ratingContext.season,
                    episode: ratingContext.episode,
                    rating: selectedRating,
                }),
            });
            const payload = await response.json();
            if (!response.ok || !payload.ok) {
                throw new Error(payload && payload.message ? payload.message : "Rating failed.");
            }
            applySavedRatingToTrigger(selectedRating);
            closeRatingModal({restoreFocus: false});
            if (window.pushFlashToast) {
                window.pushFlashToast(payload.message || "Rating saved.", 3200);
            }
        } catch (error) {
            if (ratingError) {
                ratingError.textContent = error && error.message ? error.message : "Rating failed.";
                ratingError.hidden = false;
            }
            ratingSaving = false;
        }
    }

    function renderTitleMatrixLoading() {
        if (!titleMatrixBody) {
            return;
        }
        if (titleMatrixTitleRatings) {
            titleMatrixTitleRatings.hidden = true;
        }
        titleMatrixBody.innerHTML = `
            <div class="title-matrix-loading-shell">
                <div class="title-matrix-loading-bar is-wide"></div>
                <div class="title-matrix-loading-legend">
                    <span></span><span></span><span></span><span></span>
                </div>
                <div class="title-matrix-loading-grid">
                    <span></span><span></span><span></span><span></span>
                    <span></span><span></span><span></span><span></span>
                    <span></span><span></span><span></span><span></span>
                </div>
            </div>
        `;
    }

    function syncTitleMatrixTitleRatings() {
        if (!titleMatrixTitleRatings || !titleMatrixBody) {
            return;
        }
        const data = titleMatrixBody.querySelector("[data-title-matrix-title-rating-data]");
        if (!(data instanceof HTMLElement)) {
            titleMatrixTitleRatings.hidden = true;
            return;
        }
        const traktRating = titleMatrixTitleRatings.querySelector("[data-title-matrix-trakt-rating]");
        const imdbRating = titleMatrixTitleRatings.querySelector("[data-title-matrix-imdb-rating]");
        if (traktRating) {
            traktRating.textContent = data.dataset.titleTraktRating || "Loading";
        }
        if (imdbRating) {
            imdbRating.textContent = data.dataset.titleImdbRating || "Loading";
        }
        titleMatrixTitleRatings.hidden = false;
    }

    function closeTitleMatrixOverlay({restoreFocus = true} = {}) {
        if (!titleMatrixOverlay) {
            return;
        }
        if (titleMatrixRequest) {
            titleMatrixRequest.abort();
            titleMatrixRequest = null;
        }
        if (titleMatrixRefreshTimer) {
            window.clearTimeout(titleMatrixRefreshTimer);
            titleMatrixRefreshTimer = 0;
        }
        titleMatrixRefreshAttempts = 0;
        closeManagedDialog(titleMatrixOverlay, {restoreFocus});
        hideTitleMatrixTooltip();
        titleMatrixTrigger = null;
    }

    function applySeasonZeroVisibility(fragmentRoot, hideSeasonZero) {
        if (!fragmentRoot) {
            return;
        }
        fragmentRoot.classList.toggle("is-hide-season-zero", Boolean(hideSeasonZero));
        applyTitleMatrixRowVisibility(fragmentRoot, Boolean(hideSeasonZero));
        syncTitleMatrixHorizontalScroll(fragmentRoot);
    }

    function applyTitleMatrixRowVisibility(fragmentRoot, hideSeasonZero) {
        if (!fragmentRoot) {
            return;
        }
        const rows = fragmentRoot.querySelectorAll(".title-matrix-grid tbody tr:not(.title-matrix-avg-row)");
        for (const row of rows) {
            const seasonCells = row.querySelectorAll("td[data-matrix-season]");
            if (!seasonCells.length) {
                row.style.removeProperty("display");
                continue;
            }
            const visibleSeasonCells = Array.from(seasonCells).filter((cell) => {
                const season = Number(cell.getAttribute("data-matrix-season"));
                return !(hideSeasonZero && season === 0);
            });
            if (!visibleSeasonCells.length) {
                row.style.removeProperty("display");
                continue;
            }
            const hasAnyDataInVisibleSeasons = visibleSeasonCells.some((cell) => !cell.classList.contains("is-empty"));
            row.style.display = hasAnyDataInVisibleSeasons ? "" : "none";
        }
    }

    function normalizeTitleMatrixProvider(value) {
        return String(value || "").toLowerCase() === "trakt" ? "trakt" : "imdb";
    }

    function normalizeTitleMatrixMode(value) {
        const normalized = String(value || "").toLowerCase();
        if (normalized === "trakt" || normalized === "my") {
            return normalized;
        }
        return "imdb";
    }

    function titleMatrixCacheKey(provider) {
        return `${titleMatrixUrl}|provider=${normalizeTitleMatrixProvider(provider)}`;
    }

    function buildTitleMatrixRequestUrl({forceRefresh = false, provider = "imdb", refreshMissing = false} = {}) {
        const url = new URL(titleMatrixUrl, window.location.origin);
        url.searchParams.set("provider", normalizeTitleMatrixProvider(provider));
        if (forceRefresh) {
            url.searchParams.set("refresh", "1");
        }
        if (refreshMissing) {
            url.searchParams.set("refresh_missing", "1");
        }
        return `${url.pathname}${url.search}`;
    }

    function getSelectedTitleMatrixProvider(fragmentRoot) {
        const activeToggle = titleMatrixBody
            ? titleMatrixBody.querySelector("[data-title-matrix-provider-toggle][data-title-matrix-provider].is-active")
            : null;
        if (activeToggle) {
            return normalizeTitleMatrixProvider(activeToggle.getAttribute("data-title-matrix-provider"));
        }
        return normalizeTitleMatrixProvider(fragmentRoot ? fragmentRoot.getAttribute("data-matrix-provider") : titleMatrixProvider);
    }

    function getSelectedTitleMatrixMode(fragmentRoot) {
        const activeToggle = titleMatrixBody
            ? titleMatrixBody.querySelector("[data-title-matrix-rating-mode].is-active")
            : null;
        if (activeToggle) {
            return normalizeTitleMatrixMode(activeToggle.getAttribute("data-title-matrix-rating-mode"));
        }
        return normalizeTitleMatrixProvider(fragmentRoot ? fragmentRoot.getAttribute("data-matrix-provider") : titleMatrixProvider);
    }

    function syncTitleMatrixModeToggles(mode) {
        if (!titleMatrixBody) {
            return;
        }
        const selectedMode = normalizeTitleMatrixMode(mode);
        for (const toggle of titleMatrixBody.querySelectorAll("[data-title-matrix-rating-mode]")) {
            const toggleMode = normalizeTitleMatrixMode(toggle.getAttribute("data-title-matrix-rating-mode"));
            const isActive = toggleMode === selectedMode;
            toggle.classList.toggle("is-active", isActive);
            toggle.setAttribute("aria-pressed", isActive ? "true" : "false");
        }
    }

    function getTitleMatrixControlState() {
        if (!titleMatrixBody) {
            return {
                hideSeasonZero: true,
                imdbSeasons: true,
                ratingMode: "imdb",
            };
        }
        const hideToggle = titleMatrixBody.querySelector("[data-hide-season-zero-toggle]");
        const imdbSeasonsToggle = titleMatrixBody.querySelector("[data-imdb-seasons-toggle]");
        return {
            hideSeasonZero: hideToggle ? Boolean(hideToggle.checked) : true,
            imdbSeasons: imdbSeasonsToggle ? Boolean(imdbSeasonsToggle.checked) : true,
            ratingMode: getSelectedTitleMatrixMode(null),
        };
    }

    function restoreTitleMatrixControlState(state) {
        if (!titleMatrixBody || !state) {
            return;
        }
        const hideToggle = titleMatrixBody.querySelector("[data-hide-season-zero-toggle]");
        if (hideToggle) {
            hideToggle.checked = Boolean(state.hideSeasonZero);
        }
        const imdbSeasonsToggle = titleMatrixBody.querySelector("[data-imdb-seasons-toggle]");
        if (imdbSeasonsToggle) {
            imdbSeasonsToggle.checked = state.imdbSeasons !== false;
        }
    }

    function applyTitleMatrixMode(fragmentRoot, mode = null) {
        if (!fragmentRoot) {
            return;
        }
        const displayMode = normalizeTitleMatrixMode(mode || getSelectedTitleMatrixMode(fragmentRoot));
        const externalProvider = displayMode === "my" ? getSelectedTitleMatrixProvider(fragmentRoot) : normalizeTitleMatrixProvider(displayMode);
        if (displayMode !== "my") {
            titleMatrixProvider = externalProvider;
        }
        syncTitleMatrixModeToggles(displayMode);
        for (const cell of fragmentRoot.querySelectorAll(".title-matrix-cell[data-imdb-display]")) {
            const displayValue = cell.getAttribute(`data-${displayMode}-display`) || "?";
            const state = cell.getAttribute(`data-${displayMode}-state`) || "unrated";
            const color = cell.getAttribute(`data-${displayMode}-color`) || "";
            const tooltip = cell.getAttribute(`data-${displayMode}-tooltip`) || "";
            const valueNode = cell.querySelector("span");
            if (valueNode) {
                valueNode.textContent = displayValue;
            }
            cell.classList.remove("is-rated", "is-unrated");
            if (state === "rated") {
                cell.classList.add("is-rated");
            } else {
                cell.classList.add("is-unrated");
            }
            if (color) {
                cell.style.setProperty("--matrix-cell-color", color);
            } else {
                cell.style.removeProperty("--matrix-cell-color");
            }
            if (tooltip) {
                cell.setAttribute("data-matrix-tooltip", tooltip);
            } else if (cell.hasAttribute("data-matrix-tooltip")) {
                cell.removeAttribute("data-matrix-tooltip");
            }
            const originalImdbUrl = cell.getAttribute("data-imdb-url-original");
            if (!cell.hasAttribute("data-imdb-url-original")) {
                const imdbUrl = cell.getAttribute("data-imdb-url") || "";
                if (imdbUrl) {
                    cell.setAttribute("data-imdb-url-original", imdbUrl);
                }
            }
            if (displayMode !== "imdb") {
                cell.removeAttribute("data-imdb-url");
                cell.classList.remove("is-linkable");
            } else if (originalImdbUrl) {
                cell.setAttribute("data-imdb-url", originalImdbUrl);
                cell.classList.add("is-linkable");
            }
        }
        if (titleMatrixSubtitle) {
            const subtitle = displayMode === "my"
                ? (fragmentRoot.getAttribute("data-matrix-my-subtitle") || "My episode ratings by season")
                : (
                    displayMode === "trakt"
                        ? (fragmentRoot.getAttribute("data-matrix-trakt-subtitle") || "Trakt episode ratings by season")
                        : (fragmentRoot.getAttribute("data-matrix-imdb-subtitle") || "IMDb episode ratings by season")
                );
            titleMatrixSubtitle.textContent = subtitle;
        }
        syncTitleMatrixHorizontalScroll(fragmentRoot);
    }

    function applyTitleMatrixToggles(fragmentRoot) {
        if (!fragmentRoot || !titleMatrixBody) {
            return;
        }
        const hideToggle = titleMatrixBody.querySelector("[data-hide-season-zero-toggle]");
        const imdbSeasonsToggle = titleMatrixBody.querySelector("[data-imdb-seasons-toggle]");
        applyImdbSeasonLayout(fragmentRoot, Boolean(imdbSeasonsToggle && imdbSeasonsToggle.checked));
        applySeasonZeroVisibility(fragmentRoot, Boolean(hideToggle && hideToggle.checked));
        applyTitleMatrixMode(fragmentRoot, getSelectedTitleMatrixMode(fragmentRoot));
    }

    function applyImdbSeasonLayout(fragmentRoot, useImdbSeasons) {
        if (!fragmentRoot) {
            return;
        }
        const selectedLayout = useImdbSeasons ? "imdb" : "trakt";
        fragmentRoot.classList.toggle("is-imdb-seasons", Boolean(useImdbSeasons));
        for (const panel of fragmentRoot.querySelectorAll("[data-matrix-layout-panel]")) {
            panel.hidden = panel.getAttribute("data-matrix-layout-panel") !== selectedLayout;
        }
        applyTitleMatrixRowVisibility(fragmentRoot, fragmentRoot.classList.contains("is-hide-season-zero"));
        syncTitleMatrixHorizontalScroll(fragmentRoot);
    }

    function syncTitleMatrixHorizontalScroll(fragmentRoot) {
        if (!fragmentRoot) {
            return;
        }
        const wrap = fragmentRoot.querySelector(".title-matrix-grid-wrap:not([hidden])");
        if (!wrap) {
            return;
        }
        const hasOverflow = wrap.scrollWidth > (wrap.clientWidth + 1);
        wrap.style.overflowX = hasOverflow ? "auto" : "hidden";
    }

    function hideTitleMatrixTooltip() {
        if (!titleMatrixTooltip) {
            return;
        }
        titleMatrixTooltip.hidden = true;
        titleMatrixTooltip.textContent = "";
    }

    function showTitleMatrixTooltip(target) {
        if (!titleMatrixTooltip) {
            return;
        }
        const text = target.getAttribute("data-matrix-tooltip") || "";
        if (!text) {
            hideTitleMatrixTooltip();
            return;
        }
        titleMatrixTooltip.textContent = text;
        const rect = target.getBoundingClientRect();
        titleMatrixTooltip.hidden = false;
        const tooltipRect = titleMatrixTooltip.getBoundingClientRect();
        const left = Math.max(8, Math.min(window.innerWidth - tooltipRect.width - 8, rect.left + (rect.width / 2) - (tooltipRect.width / 2)));
        const top = Math.max(8, rect.top - tooltipRect.height - 8);
        titleMatrixTooltip.style.left = `${left}px`;
        titleMatrixTooltip.style.top = `${top}px`;
    }

    async function loadTitleMatrixOverlay({forceRefresh = false, provider = titleMatrixProvider, refreshMissing = false} = {}) {
        if (!titleMatrixBody || !titleMatrixUrl) {
            return;
        }
        const normalizedProvider = normalizeTitleMatrixProvider(provider);
        titleMatrixProvider = normalizedProvider;
        const cacheKey = titleMatrixCacheKey(normalizedProvider);
        const controlState = getTitleMatrixControlState();
        if (!forceRefresh && !refreshMissing && titleMatrixCache.has(cacheKey)) {
            titleMatrixBody.innerHTML = titleMatrixCache.get(cacheKey);
            syncTitleMatrixTitleRatings();
            const fragmentRoot = titleMatrixBody.querySelector(".title-matrix-fragment");
            restoreTitleMatrixControlState(controlState);
            applyImdbSeasonLayout(fragmentRoot, Boolean(controlState.imdbSeasons));
            applySeasonZeroVisibility(fragmentRoot, Boolean(controlState.hideSeasonZero));
            applyTitleMatrixMode(fragmentRoot, normalizedProvider);
            if (refreshMissing && normalizedProvider === "trakt" && titleMatrixRefreshAttempts < 6) {
                titleMatrixRefreshAttempts += 1;
                if (titleMatrixRefreshTimer) window.clearTimeout(titleMatrixRefreshTimer);
                titleMatrixRefreshTimer = window.setTimeout(() => {
                    titleMatrixCache.delete(cacheKey);
                    loadTitleMatrixOverlay({provider: normalizedProvider, refreshMissing: true});
                }, 1100);
            }
            return;
        }
        if (titleMatrixRequest) {
            titleMatrixRequest.abort();
        }
        titleMatrixRequest = new AbortController();
        renderTitleMatrixLoading();
        try {
            const resolvedUrl = buildTitleMatrixRequestUrl({
                forceRefresh,
                provider: normalizedProvider,
                refreshMissing,
            });
            const response = await fetch(resolvedUrl, {
                headers: {"Accept": "text/html"},
                cache: "no-store",
                signal: titleMatrixRequest.signal,
            });
            const html = await response.text();
            titleMatrixBody.innerHTML = html || `
                <div class="title-matrix-empty-state">
                    <p>Could not load the episode ratings matrix.</p>
                    <button type="button" class="button ghost" data-title-matrix-retry>Retry</button>
                </div>
            `;
            syncTitleMatrixTitleRatings();
            if (response.ok && html) {
                titleMatrixCache.set(cacheKey, html);
            }
            const fragmentRoot = titleMatrixBody.querySelector(".title-matrix-fragment");
            restoreTitleMatrixControlState(controlState);
            applyImdbSeasonLayout(fragmentRoot, Boolean(controlState.imdbSeasons));
            applySeasonZeroVisibility(fragmentRoot, Boolean(controlState.hideSeasonZero));
            applyTitleMatrixMode(fragmentRoot, normalizedProvider);
        } catch (error) {
            if (error && error.name === "AbortError") {
                return;
            }
            titleMatrixBody.innerHTML = `
                <div class="title-matrix-empty-state">
                    <p>Could not load the episode ratings matrix.</p>
                    <button type="button" class="button ghost" data-title-matrix-retry>Retry</button>
                </div>
            `;
            syncTitleMatrixTitleRatings();
        } finally {
            titleMatrixRequest = null;
        }
    }

    function openTitleMatrixOverlay(trigger) {
        if (!titleMatrixOverlay || !titleMatrixBody || !titleMatrixTitle) {
            return;
        }
        titleMatrixTrigger = trigger;
        titleMatrixUrl = trigger.dataset.titleMatrixUrl || "";
        titleMatrixProvider = "imdb";
        titleMatrixRefreshAttempts = 0;
        titleMatrixTitle.textContent = trigger.dataset.titleMatrixTitle || "Episode ratings";
        openManagedDialog(titleMatrixOverlay, {
            onEscape: () => closeTitleMatrixOverlay(),
        });
        renderTitleMatrixLoading();
        loadTitleMatrixOverlay({provider: titleMatrixProvider});
    }

    async function pollNotifications() {
        try {
            const response = await fetch("/notifications/poll", {
                headers: {"Accept": "application/json"},
                cache: "no-store",
            });
            if (!response.ok) {
                return;
            }
            const payload = await response.json();
            const items = Array.isArray(payload.items) ? payload.items : [];
            if (!items.length) {
                return;
            }
            handleNotificationActivity(items, payload.activity_seq);
            window.dispatchEvent(new CustomEvent("trakt-notifications-received", {detail: {items}}));
            playAlertTone();
            showInPageNotifications(items);
            const permission = await ensureNotificationPermission();
            if (permission !== "granted") {
                return;
            }
            for (const item of items) {
                const title = item.show_title || "New episode";
                const body = item.message || "";
                new Notification(title, {body});
            }
        } catch (_error) {
        }
    }

    async function pollNotificationActivity() {
        if (document.visibilityState === "hidden") {
            return;
        }
        try {
            const response = await fetch(`/notifications/activity?after=${lastNotificationActivitySeq}`, {
                headers: {"Accept": "application/json"},
                cache: "no-store",
            });
            if (!response.ok) {
                return;
            }
            const payload = await response.json();
            const events = Array.isArray(payload.events) ? payload.events : [];
            for (const event of events) {
                const seq = Number(event && event.seq);
                if (!Number.isFinite(seq) || seq <= lastNotificationActivitySeq) {
                    continue;
                }
                lastNotificationActivitySeq = seq;
                handleNotificationSources(Array.isArray(event.sources) ? event.sources : []);
            }
            setPendingNotificationSources(
                Array.isArray(payload.pending_sources) ? payload.pending_sources : []
            );
        } catch (_error) {
        }
    }

    async function pollDebugEvents() {
        if (!DEBUG_MODE) {
            return;
        }
        try {
            const response = await fetch(`/debug/events?after=${lastDebugSeq}`, {
                headers: {"Accept": "application/json"},
                cache: "no-store",
            });
            if (!response.ok) {
                return;
            }
            const payload = await response.json();
            const events = Array.isArray(payload.events) ? payload.events : [];
            for (const event of events) {
                if (typeof event.seq === "number") {
                    lastDebugSeq = Math.max(lastDebugSeq, event.seq);
                }
                const source = String(event && event.source ? event.source : "");
                if (source.startsWith("IMDb sync")) {
                    continue;
                }
                if (event && event.message) {
                    pushFlashToast(event.message, 3200);
                }
            }
        } catch (_error) {
        }
    }

    window.addEventListener("pointerdown", unlockAudio, {passive: true});
    window.addEventListener("keydown", unlockAudio, {passive: true});
    window.addEventListener("keydown", (event) => {
        const ratingKeyTarget = event.target.closest("[data-rating-trigger]");
        if (ratingKeyTarget && (event.key === "Enter" || event.key === " ")) {
            event.preventDefault();
            openRatingModalFromTrigger(ratingKeyTarget);
            return;
        }
        if (event.key !== "Escape") {
            return;
        }
        if (confirmOverlay && !confirmOverlay.hidden) {
            event.preventDefault();
            event.stopImmediatePropagation();
            closeConfirmModal(false);
            return;
        }
        if (ratingOverlay && !ratingOverlay.hidden) {
            event.preventDefault();
            event.stopImmediatePropagation();
            closeRatingModal();
            return;
        }
        if (titleMatrixOverlay && !titleMatrixOverlay.hidden) {
            event.preventDefault();
            event.stopImmediatePropagation();
            closeTitleMatrixOverlay();
        }
    });
    document.addEventListener("click", (event) => {
        const ratingTarget = event.target.closest("[data-rating-trigger]");
        if (ratingTarget) {
            event.preventDefault();
            openRatingModalFromTrigger(ratingTarget);
            return;
        }
        if (event.target.closest("[data-rating-close]")) {
            event.preventDefault();
            closeRatingModal();
            return;
        }
        const trigger = event.target.closest("[data-title-matrix-trigger]");
        if (trigger) {
            event.preventDefault();
            openTitleMatrixOverlay(trigger);
            return;
        }
        const closeTarget = event.target.closest("[data-title-matrix-close]");
        if (closeTarget) {
            event.preventDefault();
            closeTitleMatrixOverlay();
            return;
        }
        const retryTarget = event.target.closest("[data-title-matrix-retry]");
        if (retryTarget) {
            event.preventDefault();
            loadTitleMatrixOverlay({forceRefresh: true, provider: titleMatrixProvider});
            return;
        }
        const providerToggle = event.target.closest("[data-title-matrix-provider-toggle]");
        if (providerToggle && titleMatrixBody) {
            event.preventDefault();
            const mode = normalizeTitleMatrixMode(providerToggle.getAttribute("data-title-matrix-rating-mode"));
            if (mode === "my") {
                const fragmentRoot = titleMatrixBody.querySelector(".title-matrix-fragment");
                applyTitleMatrixMode(fragmentRoot, "my");
                return;
            }
            const provider = normalizeTitleMatrixProvider(providerToggle.getAttribute("data-title-matrix-provider"));
            titleMatrixProvider = provider;
            if (provider === "trakt") {
                loadTitleMatrixOverlay({provider, refreshMissing: true});
                return;
            }
            const cacheKey = titleMatrixCacheKey(provider);
            if (titleMatrixCache.has(cacheKey)) {
                const controlState = getTitleMatrixControlState();
                titleMatrixBody.innerHTML = titleMatrixCache.get(cacheKey);
                const fragmentRoot = titleMatrixBody.querySelector(".title-matrix-fragment");
                restoreTitleMatrixControlState(controlState);
                applyImdbSeasonLayout(fragmentRoot, Boolean(controlState.imdbSeasons));
                applySeasonZeroVisibility(fragmentRoot, Boolean(controlState.hideSeasonZero));
                applyTitleMatrixMode(fragmentRoot, provider);
            } else {
                loadTitleMatrixOverlay({provider, refreshMissing: provider === "trakt"});
            }
            return;
        }
        const imdbCell = event.target.closest(".title-matrix-cell[data-imdb-url]");
        if (imdbCell) {
            const imdbUrl = imdbCell.getAttribute("data-imdb-url") || "";
            if (imdbUrl) {
                window.open(imdbUrl, "_blank", "noopener,noreferrer");
            }
        }
    });
    document.addEventListener("change", (event) => {
        const toggle = event.target.closest("[data-hide-season-zero-toggle], [data-imdb-seasons-toggle]");
        if (!toggle || !titleMatrixBody) {
            return;
        }
        const fragmentRoot = titleMatrixBody.querySelector(".title-matrix-fragment");
        applyTitleMatrixToggles(fragmentRoot);
    });
    document.addEventListener("mouseover", (event) => {
        const cell = event.target.closest(".title-matrix-cell[data-matrix-tooltip]");
        if (!cell || !titleMatrixOverlay || titleMatrixOverlay.hidden) {
            return;
        }
        showTitleMatrixTooltip(cell);
    });
    document.addEventListener("mouseout", (event) => {
        const cell = event.target.closest(".title-matrix-cell[data-matrix-tooltip]");
        if (!cell) {
            return;
        }
        hideTitleMatrixTooltip();
    });
    window.addEventListener("resize", () => {
        if (!titleMatrixBody) {
            return;
        }
        const fragmentRoot = titleMatrixBody.querySelector(".title-matrix-fragment");
        syncTitleMatrixHorizontalScroll(fragmentRoot);
        hideTitleMatrixTooltip();
    });
    if (ratingForm) {
        ratingForm.addEventListener("submit", (event) => {
            event.preventDefault();
            submitRating();
        });
    }
    for (const star of ratingStars) {
        const value = Number(star.dataset.ratingStar || "0");
        star.addEventListener("mouseenter", () => {
            hoverRating = value;
            updateRatingStars(hoverRating);
        });
        star.addEventListener("focus", () => {
            hoverRating = value;
            updateRatingStars(hoverRating);
        });
        star.addEventListener("mouseleave", () => {
            hoverRating = 0;
            updateRatingStars(selectedRating);
        });
        star.addEventListener("click", () => {
            selectedRating = value;
            updateRatingStars(selectedRating);
            submitRating();
        });
    }

    setupFlashToast();
    bindPlayPromptLinks();
    renderPlayPrompts();
    syncNotificationNavState();
    const topbar = document.querySelector(".topbar");
    if (topbar) {
        new MutationObserver(() => syncNotificationNavState()).observe(topbar, {childList: true, subtree: true});
    }
    document.querySelectorAll("[data-rating-autopen]").forEach((node) => openRatingModalFromTrigger(node));
    if (NOTIFICATIONS_BROWSER_POLL_ENABLED) {
        pollNotifications();
        setInterval(pollNotifications, 60000);
    }
    pollNotificationActivity();
    setInterval(pollNotificationActivity, 5000);
    document.addEventListener("visibilitychange", () => {
        if (document.visibilityState === "visible") {
            pollNotificationActivity();
        }
    });
    pollDebugEvents();
    setInterval(pollDebugEvents, 2500);
})();
