const assert = require("node:assert/strict");
const test = require("node:test");

const {
    readImdbSeasonsControlState,
    syncImdbSeasonsToggle,
} = require("../trakt_tracker/web/static/ui_core.js");

function makeToggle({checked = false, disabled = false, layoutLocked = false} = {}) {
    return {
        checked,
        disabled,
        hasAttribute(name) {
            return name === "data-layout-locked" && layoutLocked;
        },
    };
}

test("initial matrix load preserves the server IMDb-seasons preference", () => {
    assert.equal(readImdbSeasonsControlState(null), null);

    const serverToggle = makeToggle({checked: true});
    assert.equal(syncImdbSeasonsToggle(serverToggle, null), true);
    assert.equal(serverToggle.checked, true);

    const serverUncheckedToggle = makeToggle({checked: false});
    assert.equal(syncImdbSeasonsToggle(serverUncheckedToggle, null), false);
    assert.equal(serverUncheckedToggle.checked, false);
});

test("successful save keeps a temporarily disabled IMDb-seasons toggle checked", () => {
    const toggle = makeToggle({checked: true, disabled: true});

    assert.equal(syncImdbSeasonsToggle(toggle, true), true);
    assert.equal(toggle.checked, true);
});

test("permanently locked IMDb layout stays unchecked", () => {
    const toggle = makeToggle({checked: true, disabled: true, layoutLocked: true});

    assert.equal(syncImdbSeasonsToggle(toggle, true), false);
    assert.equal(toggle.checked, false);
});
