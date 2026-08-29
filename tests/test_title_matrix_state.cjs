const assert = require("node:assert/strict");
const test = require("node:test");

const {readTitleMatrixLayoutState} = require("../trakt_tracker/web/static/ui_core.js");

const fragment = (available) => ({
    getAttribute(name) {
        return name === "data-imdb-layout-available" ? available : null;
    },
});

test("TMDb matrix without a season toggle keeps the IMDb grid visible", () => {
    assert.equal(readTitleMatrixLayoutState(fragment("1"), null), true);
    assert.equal(readTitleMatrixLayoutState(fragment("0"), null), false);
});

test("matrix season toggle still controls layouts when it exists", () => {
    const toggle = (checked) => ({checked, hasAttribute: () => false});
    assert.equal(readTitleMatrixLayoutState(fragment("1"), toggle(true)), true);
    assert.equal(readTitleMatrixLayoutState(fragment("1"), toggle(false)), false);
});
