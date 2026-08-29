const assert = require("node:assert/strict");
const test = require("node:test");

const {
    configureSeasonScopeActions,
    focusDefaultEpisode,
    readSeasonScopeState,
    shouldFocusDefaultEpisode,
} = require("../trakt_tracker/web/static/show_watch_panel.js");

function makeActionButton() {
    const label = {textContent: ""};
    return {
        dataset: {},
        disabled: true,
        hidden: false,
        title: "",
        ariaLabel: "",
        label,
        querySelector(selector) {
            return selector === "[data-search-watch-header-season-label]" ? label : null;
        },
        removeAttribute(name) {
            if (name.startsWith("data-")) {
                const key = name.slice(5).replace(/-([a-z])/g, (_, letter) => letter.toUpperCase());
                delete this.dataset[key];
            }
        },
        setAttribute(name, value) {
            if (name === "aria-label") this.ariaLabel = value;
        },
        toggleAttribute(name, enabled) {
            const key = name.slice(5).replace(/-([a-z])/g, (_, letter) => letter.toUpperCase());
            if (enabled) this.dataset[key] = "";
            else delete this.dataset[key];
        },
    };
}

function makeSeasonHarness(tabData) {
    let activeTab = {dataset: {...tabData}};
    const panel = {
        dataset: {
            searchWatchTraktId: "42",
            searchWatchTitle: "Example Show",
            seasonLayout: "trakt",
        },
        querySelector(selector) {
            assert.equal(selector, "[data-search-watch-season-tab].is-active");
            return activeTab;
        },
    };
    const body = {
        querySelector(selector) {
            assert.equal(selector, "[data-search-watch-panel]");
            return panel;
        },
    };
    const mark = makeActionButton();
    const unwatch = makeActionButton();
    const overlay = {
        querySelector(selector) {
            if (selector === "[data-search-watch-header-season-mark]") return mark;
            if (selector === "[data-search-watch-header-season-unwatch]") return unwatch;
            return null;
        },
    };
    return {
        body,
        mark,
        overlay,
        panel,
        unwatch,
        select(tab, seasonLayout = "trakt") {
            activeTab = {dataset: {...tab}};
            panel.dataset.seasonLayout = seasonLayout;
        },
    };
}

test("a title with no watched episodes keeps the episode panel at the top", () => {
    const panel = {
        dataset: {
            searchWatchWatchedCount: "0",
            defaultEpisodeKey: "1-1",
        },
    };
    const body = {
        scrollTop: 160,
        querySelector(selector) {
            assert.equal(selector, "[data-search-watch-panel]");
            return panel;
        },
        querySelectorAll() {
            throw new Error("episode cards must not be inspected without watch history");
        },
    };

    assert.equal(shouldFocusDefaultEpisode(panel), false);
    focusDefaultEpisode(body);
    assert.equal(body.scrollTop, 0);
});

test("a title with watch history still moves to its default episode", () => {
    const panel = {
        dataset: {
            searchWatchWatchedCount: "3",
            defaultEpisodeKey: "2-4",
        },
    };
    const target = {
        dataset: {episodeKey: "2-4"},
        getBoundingClientRect() {
            return {top: 210};
        },
    };
    const body = {
        scrollTop: 30,
        querySelector(selector) {
            assert.equal(selector, "[data-search-watch-panel]");
            return panel;
        },
        querySelectorAll(selector) {
            assert.equal(selector, ".search-watch-episode-card[data-episode-key]");
            return [target];
        },
        getBoundingClientRect() {
            return {top: 50};
        },
    };

    assert.equal(shouldFocusDefaultEpisode(panel), true);
    focusDefaultEpisode(body);
    assert.equal(body.scrollTop, 190);
});

test("selected-season state drives both header actions without extra requests", () => {
    const harness = makeSeasonHarness({
        searchWatchSeasonTab: "1",
        searchWatchSeasonLabel: "S1",
        searchWatchSeasonCanMark: "1",
        searchWatchSeasonCanUnwatch: "0",
    });

    configureSeasonScopeActions(harness.overlay, harness.body);
    assert.equal(harness.mark.hidden, false);
    assert.equal(harness.unwatch.hidden, true);
    assert.equal(harness.mark.label.textContent, "S1");
    assert.equal(harness.mark.dataset.season, "1");
    assert.equal(harness.mark.dataset.seasonLayout, "trakt");

    harness.select({
        searchWatchSeasonTab: "2",
        searchWatchSeasonLabel: "S2",
        searchWatchSeasonCanMark: "1",
        searchWatchSeasonCanUnwatch: "1",
    }, "imdb");
    configureSeasonScopeActions(harness.overlay, harness.body);
    assert.equal(harness.mark.hidden, false);
    assert.equal(harness.unwatch.hidden, false);
    assert.equal(harness.mark.label.textContent, "S2");
    assert.equal(harness.unwatch.label.textContent, "S2");
    assert.equal(harness.mark.dataset.season, "2");
    assert.equal(harness.unwatch.dataset.seasonLayout, "imdb");
    assert.equal(harness.unwatch.ariaLabel, "Remove watched history for S2");

    harness.select({
        searchWatchSeasonTab: "3",
        searchWatchSeasonLabel: "S3",
        searchWatchSeasonCanMark: "0",
        searchWatchSeasonCanUnwatch: "1",
    });
    configureSeasonScopeActions(harness.overlay, harness.body);
    assert.equal(harness.mark.hidden, true);
    assert.equal(harness.unwatch.hidden, false);

    harness.select({
        searchWatchSeasonTab: "4",
        searchWatchSeasonLabel: "S4",
        searchWatchSeasonCanMark: "0",
        searchWatchSeasonCanUnwatch: "0",
    }, "imdb");
    const blocked = readSeasonScopeState(harness.panel, {
        dataset: {
            searchWatchSeasonTab: "4",
            searchWatchSeasonLabel: "S4",
            searchWatchSeasonCanMark: "0",
            searchWatchSeasonCanUnwatch: "0",
        },
    });
    assert.equal(blocked.canMark, false);
    assert.equal(blocked.canUnwatch, false);
    configureSeasonScopeActions(harness.overlay, harness.body);
    assert.equal(harness.mark.hidden, true);
    assert.equal(harness.unwatch.hidden, true);
});

test("loading a different title clears stale season actions", () => {
    const mark = makeActionButton();
    const unwatch = makeActionButton();
    const overlay = {
        querySelector(selector) {
            if (selector === "[data-search-watch-header-season-mark]") return mark;
            if (selector === "[data-search-watch-header-season-unwatch]") return unwatch;
            return null;
        },
    };
    const body = {querySelector: () => null};

    configureSeasonScopeActions(overlay, body);

    assert.equal(mark.hidden, true);
    assert.equal(unwatch.hidden, true);
});
