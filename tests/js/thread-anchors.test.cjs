const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { test } = require("node:test");
const vm = require("node:vm");

const source = readFileSync("mimir/static/js/thread-anchors.js", "utf8");

// Click a link of the given kind (the selector it must match) to `href`,
// with message ids `loaded` in the document. Returns what the script did.
function click(kind, href, loaded) {
  let handler;
  const calls = { prevented: false, pushed: null, scrolled: false, hashchange: false };
  const link = { href };
  vm.runInNewContext(source, {
    URL,
    HashChangeEvent: class {},
    location: { href: "http://h/a/t" },
    history: { state: { original: 7 }, pushState(state, _title, url) { calls.pushed = url; calls.state = state && { ...state }; } },
    window: { dispatchEvent: () => (calls.hashchange = true) },
    document: {
      addEventListener: (_event, fn) => (handler = fn),
      getElementById: id =>
        loaded.includes(id) ? { scrollIntoView: () => (calls.scrolled = true) } : null,
    },
  });
  handler({
    defaultPrevented: false,
    button: 0,
    target: { closest: selector => (selector.split(", ").includes(kind) ? link : null) },
    preventDefault: () => (calls.prevented = true),
  });
  return calls;
}

test("a revision link to a loaded message scrolls in place", () => {
  const calls = click(".revisions-fold a", "/a/t/2#m7", ["m7"]);
  assert.deepEqual(calls, {
    prevented: true,
    pushed: "http://h/a/t/2#m7",
    state: { original: 7, htmx: true },
    scrolled: true,
    hashchange: true,
  });
});

test("a revision link to a message page passes through", () => {
  const calls = click(".revisions-fold a", "/a/2024/01/7", ["m7"]);
  assert.equal(calls.prevented, false);
  assert.equal(calls.pushed, null);
});

test("a revision link to a message that is not loaded passes through", () => {
  assert.equal(click(".revisions-fold a", "/a/t/3#m9", ["m7"]).prevented, false);
});
