(() => {
    const root = document.querySelector("[data-imdb-status]");
    if (!root || !window.fetch) {
        return;
    }
    const runningNode = root.querySelector("[data-imdb-running]");
    const statusNode = root.querySelector("[data-imdb-current-status]");
    const progressNode = root.querySelector("[data-imdb-progress]");
    const logNode = root.querySelector("[data-imdb-log]");
    const syncButton = document.querySelector("[data-imdb-sync-button]");
    const soundPathInput = document.querySelector('input[name="notification_sound_path"]');
    const soundPickerInput = document.querySelector("[data-notification-sound-picker]");
    const browseSoundButton = document.querySelector("[data-browse-notification-sound]");
    const testSoundButton = document.querySelector("[data-test-notification-sound]");
    let pendingSoundObjectUrl = "";
    let after = Number(root.dataset.after || "0");
    let keepVisibleUntil = root.classList.contains("is-hidden") ? 0 : Date.now() + 15000;
    let lastProgressMessage = "";
    let recentMessages = [];
    const refreshRoot = document.querySelector("[data-refresh-status]");
    const refreshRunningNode = document.querySelector("[data-refresh-running]");
    const operationSummaryNode = document.querySelector("[data-operation-summary]");
    const diagnosticsDetails = document.querySelector("[data-diagnostics-details]");
    const operationButtons = Array.from(document.querySelectorAll("[data-sync-task]"));
    const settingsForm = document.getElementById("settings-form");
    const saveBar = document.querySelector("[data-settings-save-bar]");
    const historyProgressNode = refreshRoot ? refreshRoot.querySelector("[data-history-progress]") : null;
    const historyLastNode = refreshRoot ? refreshRoot.querySelector("[data-history-last]") : null;
    const historyReconcileNode = refreshRoot ? refreshRoot.querySelector("[data-history-reconcile]") : null;
    const progressLastNode = refreshRoot ? refreshRoot.querySelector("[data-progress-last]") : null;
    const refreshLastNode = refreshRoot ? refreshRoot.querySelector("[data-refresh-last]") : null;
    const posterStatsNode = refreshRoot ? refreshRoot.querySelector("[data-poster-stats]") : null;
    const stillStatsNode = refreshRoot ? refreshRoot.querySelector("[data-still-stats]") : null;
    const providerHealthNode = refreshRoot ? refreshRoot.querySelector("[data-provider-health]") : null;
    const queueHealthNode = refreshRoot ? refreshRoot.querySelector("[data-queue-health]") : null;
    const artworkHealthNode = refreshRoot ? refreshRoot.querySelector("[data-artwork-health]") : null;
    const traktSyncRoot = document.querySelector("[data-trakt-sync-settings]");
    const traktSyncMode = traktSyncRoot?.querySelector("[data-trakt-sync-mode]");
    const traktSyncPending = traktSyncRoot?.querySelector("[data-trakt-sync-pending]");
    const traktSyncBlocked = traktSyncRoot?.querySelector("[data-trakt-sync-blocked]");
    const traktSyncSuccess = traktSyncRoot?.querySelector("[data-trakt-sync-success]");
    const traktSyncCache = traktSyncRoot?.querySelector("[data-trakt-sync-cache]");
    const traktSyncError = traktSyncRoot?.querySelector("[data-trakt-sync-error]");
    const traktSyncItems = traktSyncRoot?.querySelector("[data-trakt-sync-items]");
    const traktSyncRetry = traktSyncRoot?.querySelector("[data-trakt-sync-retry]");

    function renderTraktQueue(payload) {
        if (!traktSyncRoot || !payload) return;
        if (traktSyncMode) traktSyncMode.textContent = payload.mode || "local";
        if (traktSyncPending) traktSyncPending.textContent = String(payload.pending || 0);
        if (traktSyncBlocked) traktSyncBlocked.textContent = String(payload.blocked || 0);
        if (traktSyncSuccess) traktSyncSuccess.textContent = payload.last_success_at || "n/a";
        if (traktSyncCache) traktSyncCache.textContent = payload.last_cache_at || "n/a";
        if (traktSyncError) {
            traktSyncError.textContent = payload.last_error || "";
            traktSyncError.hidden = !payload.last_error;
        }
        if (!traktSyncItems) return;
        traktSyncItems.replaceChildren();
        const items = Array.isArray(payload.items) ? payload.items : [];
        if (!items.length) {
            const line = document.createElement("div");
            line.className = "settings-sync-log-line";
            line.textContent = "No queued Trakt changes.";
            traktSyncItems.appendChild(line);
            return;
        }
        for (const item of items) {
            const line = document.createElement("div");
            line.className = "settings-sync-log-line settings-trakt-queue-line";
            const text = document.createElement("span");
            text.textContent = `${item.operation_key} — ${item.status}${item.last_error ? `: ${item.last_error}` : ""}`;
            line.appendChild(text);
            if (item.status === "blocked") {
                const discard = document.createElement("button");
                discard.type = "button";
                discard.className = "button ghost";
                discard.dataset.traktSyncDiscard = String(item.id);
                discard.textContent = "Discard";
                line.appendChild(discard);
            }
            traktSyncItems.appendChild(line);
        }
    }

    async function refreshTraktQueue() {
        try {
            const response = await fetch("/trakt-sync/status", {headers: {"Accept": "application/json"}, cache: "no-store"});
            if (response.ok) renderTraktQueue(await response.json());
        } catch (_error) {
        }
    }

    function renderLog() {
        if (!logNode) {
            return;
        }
        logNode.replaceChildren();
        if (!recentMessages.length) {
            const line = document.createElement("div");
            line.className = "settings-sync-log-line";
            line.textContent = "Waiting for sync activity.";
            logNode.appendChild(line);
            return;
        }
        for (const message of recentMessages) {
            const line = document.createElement("div");
            line.className = "settings-sync-log-line";
            line.textContent = message;
            logNode.appendChild(line);
        }
    }

    function rememberMessage(message) {
        if (!message) {
            return;
        }
        recentMessages = [message, ...recentMessages.filter((item) => item !== message)].slice(0, 4);
        renderLog();
    }

    function setPanelVisible(visible) {
        root.classList.toggle("is-hidden", !visible);
    }

    async function pollStatus() {
        try {
            const response = await fetch(`/settings/imdb-sync-status?after=${after}`, {
                headers: {"Accept": "application/json"},
                cache: "no-store",
            });
            if (!response.ok) {
                return;
            }
            const payload = await response.json();
            if (statusNode && payload.status) {
                statusNode.textContent = payload.status;
            }
            if (runningNode) {
                runningNode.textContent = payload.running ? "Running" : "Idle";
                runningNode.classList.toggle("is-running", Boolean(payload.running));
            }
            if (syncButton) {
                syncButton.disabled = Boolean(payload.running);
                syncButton.textContent = payload.running ? "Running" : (syncButton.dataset.idleLabel || "Sync IMDb");
            }
            if (progressNode) {
                const message = payload.progress_message || "";
                progressNode.textContent = message;
                progressNode.classList.toggle("is-hidden", !message);
            }
            if (payload.running) {
                keepVisibleUntil = Date.now() + 15000;
            }
            if (Array.isArray(payload.events)) {
                for (const event of payload.events) {
                    if (typeof event.seq === "number") {
                        after = Math.max(after, event.seq);
                    }
                    rememberMessage(event.message || "");
                    keepVisibleUntil = Date.now() + 15000;
                }
            }
            if (payload.progress_message && payload.progress_message !== lastProgressMessage) {
                lastProgressMessage = payload.progress_message;
                keepVisibleUntil = Date.now() + 15000;
            }
            setPanelVisible(Boolean(payload.running) || Date.now() < keepVisibleUntil);
        } catch (_error) {
        }
    }

    if (syncButton) {
        syncButton.addEventListener("click", () => {
            keepVisibleUntil = Date.now() + 15000;
            setPanelVisible(true);
        });
    }

    function formatStats(label, stats) {
        if (!stats || typeof stats !== "object") {
            return `${label}: n/a`;
        }
        return `${label}: ready ${stats.ready}/${stats.total}, no_data ${stats.checked_no_data}, retry ${stats.retryable_failure}, unknown ${stats.unknown}`;
    }

    async function pollRefreshStatus() {
        if (!refreshRoot) {
            return;
        }
        try {
            const response = await fetch("/settings/refresh-status", {
                headers: {"Accept": "application/json"},
                cache: "no-store",
            });
            if (!response.ok) {
                return;
            }
            const payload = await response.json();
            const running = payload && payload.running ? payload.running : {};
            const queued = payload && payload.queued ? payload.queued : {};
            const activeSyncs = [
                running.full_sync && "full",
                running.backfill_sync && "backfill",
                running.timeout_sync && "timeout",
                running.repair_sync && "repair",
            ].filter(Boolean);
            const queuedSyncs = [
                queued.full_sync && "full",
                queued.backfill_sync && "backfill",
                queued.timeout_sync && "timeout",
                queued.repair_sync && "repair",
            ].filter(Boolean);
            const isRunning = Boolean(
                running.history_sync
                || running.progress_sync
                || running.enrich_queue
                || activeSyncs.length
                || queuedSyncs.length
            );
            const syncBusy = activeSyncs.length > 0 || queuedSyncs.length > 0;
            for (const button of operationButtons) {
                const task = button.dataset.syncTask || "";
                const active = Boolean(running[task]);
                const waiting = Boolean(queued[task]);
                button.textContent = active ? "Running" : waiting ? "Queued" : (button.dataset.idleLabel || button.textContent);
                button.disabled = syncBusy;
                button.setAttribute("aria-busy", active || waiting ? "true" : "false");
            }
            if (operationSummaryNode) {
                operationSummaryNode.textContent = activeSyncs.length
                    ? `Running: ${activeSyncs.join(", ")}`
                    : queuedSyncs.length
                        ? `Queued: ${queuedSyncs.join(", ")}`
                        : "Idle";
                operationSummaryNode.classList.toggle("is-running", syncBusy);
            }
            const shouldRevealDiagnostics = Boolean(
                activeSyncs.length
                || queuedSyncs.length
                || running.history_sync
                || running.progress_sync
                || Number(payload?.queue?.failed || 0) > 0
                || payload?.artwork?.error
            );
            if (diagnosticsDetails && shouldRevealDiagnostics) diagnosticsDetails.open = true;
            if (refreshRunningNode) {
                const activeText = `Running${running.history_sync ? " history" : ""}${running.progress_sync ? " progress" : ""}${running.enrich_queue ? " queue" : ""}${activeSyncs.length ? ` ${activeSyncs.join(", ")}` : ""}`;
                refreshRunningNode.textContent = activeSyncs.length || running.history_sync || running.progress_sync || running.enrich_queue
                    ? `${activeText}${queuedSyncs.length ? `; queued ${queuedSyncs.join(", ")}` : ""}`
                    : queuedSyncs.length ? `Queued ${queuedSyncs.join(", ")}` : "Idle";
                refreshRunningNode.classList.toggle("is-running", isRunning);
            }
            if (historyProgressNode) {
                const progressText = payload && payload.history && payload.history.progress_message
                    ? payload.history.progress_message
                    : "n/a";
                historyProgressNode.textContent = `History: ${progressText}`;
            }
            if (historyLastNode) {
                const message = payload && payload.history && payload.history.last_success_at ? payload.history.last_success_at : "n/a";
                historyLastNode.textContent = `Last history: ${message}`;
            }
            if (historyReconcileNode) {
                const message = payload && payload.history && payload.history.last_full_reconcile_at ? payload.history.last_full_reconcile_at : "n/a";
                historyReconcileNode.textContent = `Last full reconcile: ${message}`;
            }
            if (progressLastNode) {
                const message = payload && payload.progress && payload.progress.last_message ? payload.progress.last_message : "n/a";
                progressLastNode.textContent = `Last progress: ${message}`;
            }
            if (refreshLastNode) {
                const message = payload && payload.refresh && payload.refresh.last_message ? payload.refresh.last_message : "n/a";
                refreshLastNode.textContent = `Last refresh: ${message}`;
            }
            if (posterStatsNode) {
                posterStatsNode.textContent = formatStats("Posters", payload ? payload.posters : null);
            }
            if (stillStatsNode) {
                stillStatsNode.textContent = formatStats("Stills", payload ? payload.stills : null);
            }
            if (providerHealthNode) {
                const trakt = payload?.trakt?.authorized ? "authorized" : "not authorized";
                const tmdb = payload?.tmdb?.configured ? `configured, retry ${payload.tmdb.retryable_failure || 0}` : "not configured";
                providerHealthNode.textContent = `Providers: Trakt ${trakt}; TMDb ${tmdb}`;
            }
            if (queueHealthNode) {
                const queue = payload?.queue || {};
                queueHealthNode.textContent = `Queue: pending ${queue.pending || 0}, running ${queue.running || 0}, cooldown ${queue.cooldown || 0}, failed ${queue.failed || 0}`;
            }
            if (artworkHealthNode) {
                const artwork = payload?.artwork || {};
                artworkHealthNode.textContent = artwork.at
                    ? `Artwork: ${artwork.status || "n/a"} at ${artwork.at}, scanned ${artwork.scanned || 0}, selected ${artwork.selected || 0}, warmed ${artwork.warmed || 0}, failed ${artwork.failed || 0}, ${Math.round(artwork.duration_ms || 0)} ms${artwork.error ? `, ${artwork.error}` : ""}`
                    : "Artwork: n/a";
            }
        } catch (_error) {
        }
    }

    if (browseSoundButton && soundPickerInput && soundPathInput) {
        browseSoundButton.addEventListener("click", () => {
            soundPickerInput.click();
        });
        soundPickerInput.addEventListener("change", () => {
            const file = soundPickerInput.files && soundPickerInput.files[0];
            if (!file) {
                return;
            }
            soundPathInput.value = file.name;
            if (pendingSoundObjectUrl) {
                URL.revokeObjectURL(pendingSoundObjectUrl);
            }
            pendingSoundObjectUrl = URL.createObjectURL(file);
        });
    }

    if (testSoundButton) {
        testSoundButton.addEventListener("click", () => {
            if (pendingSoundObjectUrl) {
                const previewAudio = new Audio(pendingSoundObjectUrl);
                previewAudio.play().catch(() => {
                    if (window.traktPlayAlertTone) {
                        window.traktPlayAlertTone();
                    }
                });
                return;
            }
            if (window.traktPlayAlertTone) {
                window.traktPlayAlertTone();
            }
        });
    }

    if (settingsForm && saveBar) {
        const snapshot = () => {
            const data = new FormData(settingsForm);
            data.delete("notification_sound_file");
            return Array.from(data.entries())
                .map(([key, value]) => [key, String(value)])
                .sort(([leftKey, leftValue], [rightKey, rightValue]) => (
                    leftKey.localeCompare(rightKey) || leftValue.localeCompare(rightValue)
                ));
        };
        const initial = JSON.stringify(snapshot());
        const updateDirtyState = () => {
            saveBar.hidden = JSON.stringify(snapshot()) === initial;
        };
        settingsForm.addEventListener("input", updateDirtyState);
        settingsForm.addEventListener("change", updateDirtyState);
        settingsForm.addEventListener("reset", () => window.setTimeout(updateDirtyState, 0));
        settingsForm.addEventListener("submit", () => {
            saveBar.hidden = true;
        });
    }

    window.addEventListener("trakt-sync-status", (event) => renderTraktQueue(event.detail));
    traktSyncRetry?.addEventListener("click", async () => {
        traktSyncRetry.disabled = true;
        try {
            const response = await fetch("/trakt-sync/retry", {
                method: "POST",
                headers: {"Accept": "application/json", "Content-Type": "application/json"},
                body: "{}",
            });
            const payload = await response.json();
            if (!response.ok) throw new Error(payload.message || "Trakt retry failed.");
            renderTraktQueue(payload);
            window.refreshTraktSyncStatus?.();
        } catch (error) {
            window.pushFlashToast?.(error.message || "Trakt retry failed.", 4200);
        } finally {
            traktSyncRetry.disabled = false;
        }
    });
    traktSyncItems?.addEventListener("click", async (event) => {
        const button = event.target.closest("[data-trakt-sync-discard]");
        if (!button) return;
        const accepted = await window.traktConfirm?.({
            title: "Discard blocked Trakt change?",
            message: "The local state will stay as shown, but this queued Trakt change will no longer be synchronized.",
            confirmLabel: "Discard change",
            danger: true,
        });
        if (!accepted) return;
        button.disabled = true;
        try {
            const response = await fetch(`/trakt-sync/blocked/${button.dataset.traktSyncDiscard}/discard`, {
                method: "POST",
                headers: {"Accept": "application/json", "Content-Type": "application/json"},
                body: JSON.stringify({confirm: true}),
            });
            const payload = await response.json();
            if (!response.ok) throw new Error(payload.message || "Could not discard queue item.");
            renderTraktQueue(payload);
            window.refreshTraktSyncStatus?.();
        } catch (error) {
            button.disabled = false;
            window.pushFlashToast?.(error.message || "Could not discard queue item.", 4200);
        }
    });

    renderLog();
    refreshTraktQueue();
    pollStatus();
    pollRefreshStatus();
    window.setInterval(pollStatus, 1200);
    window.setInterval(pollRefreshStatus, 2000);
})();
