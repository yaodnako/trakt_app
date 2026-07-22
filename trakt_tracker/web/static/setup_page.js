(() => {
    const root = document.querySelector("[data-setup-root]");
    if (!root || root.dataset.authorized !== "1") return;
    const stateNode = root?.querySelector("[data-setup-state]");
    const messageNode = root?.querySelector("[data-setup-message]");
    const errorNode = root?.querySelector("[data-setup-error]");
    const retryButton = root?.querySelector("[data-setup-retry]");
    let starting = false;

    async function startSync() {
        if (starting) return;
        starting = true;
        if (retryButton) retryButton.hidden = true;
        try {
            await fetch("/setup/sync", {method: "POST", headers: {"Accept": "application/json"}});
        } finally {
            starting = false;
            window.setTimeout(poll, 250);
        }
    }

    async function poll() {
        try {
            const response = await fetch("/setup/status", {headers: {"Accept": "application/json"}, cache: "no-store"});
            if (!response.ok) return;
            const status = await response.json();
            if (status.state === "complete") {
                window.location.replace("/progress");
                return;
            }
            if (stateNode) {
                stateNode.textContent = status.state.charAt(0).toUpperCase() + status.state.slice(1);
                stateNode.classList.toggle("is-running", Boolean(status.running));
            }
            if (messageNode) messageNode.textContent = status.message || "Preparing initial synchronization.";
            if (errorNode) {
                errorNode.textContent = status.error || "";
                errorNode.hidden = !status.error;
            }
            if (retryButton) {
                retryButton.hidden = Boolean(status.running);
                retryButton.textContent = status.state === "failed" ? "Retry" : "Start sync";
            }
            if (!status.running && status.state === "pending") {
                await startSync();
                return;
            }
        } catch (_error) {
        }
        window.setTimeout(poll, 1000);
    }

    retryButton?.addEventListener("click", startSync);
    poll();
})();
