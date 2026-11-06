const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { test } = require("node:test");
const vm = require("node:vm");

test("HTTP errors cannot change the document, history or out-of-band navigation", () => {
  const listeners = {};
  vm.runInNewContext(readFileSync("mimir/static/js/htmx-navigation.js", "utf8"), {
    document: { addEventListener: (name, handler) => listeners[name] = handler },
  });
  for (const status of [200, 204, 304, 404, 500]) {
    let cancelled = false;
    listeners["htmx:before:swap"]({
      detail: { ctx: { response: { status } } },
      preventDefault() { cancelled = true; },
    });
    assert.equal(cancelled, status >= 400, `status ${status}`);
  }
});

test("history restores current metadata and server context while keeping preferences", () => {
  const listeners = {};
  const attrs = new Map([["data-thread-root-id", "old"], ["data-thread-context", "root"], ["data-theme", "dark"], ["data-thread-fold", "expanded"]]);
  let current = ["old canonical", "old JSON-LD"];
  const response = {
    head: { querySelectorAll: () => ["new canonical", "new JSON-LD"].map(value => ({ cloneNode: () => value })) },
    documentElement: { getAttribute: name => ({ "data-nav-up": "/beta/", "data-thread-root-id": "new", "data-thread-context": "deep" })[name] ?? null },
  };
  const document = {
    addEventListener: (name, handler) => listeners[name] = handler,
    head: {
      querySelectorAll: selector => {
        assert.match(selector, /application\/ld\+json/);
        return current.map(value => ({ remove() { current = current.filter(item => item !== value); } }));
      },
      appendChild: node => current.push(node),
    },
    documentElement: { setAttribute: (name, value) => attrs.set(name, value), removeAttribute: name => attrs.delete(name) },
  };
  vm.runInNewContext(readFileSync("mimir/static/js/htmx-navigation.js", "utf8"), {
    document,
    DOMParser: class { parseFromString(text, mime) { assert.equal(text, "restored response"); assert.equal(mime, "text/html"); return response; } },
  });
  const event = { detail: { ctx: { text: "restored response", request: { headers: { "HX-History-Restore-Request": "true" } } } } };
  listeners["htmx:after:swap"](event);
  assert.deepEqual(current, ["new canonical", "new JSON-LD"]);
  assert.equal(attrs.get("data-nav-up"), "/beta/");
  assert.equal(attrs.get("data-thread-root-id"), "new");
  assert.equal(attrs.get("data-thread-context"), "deep");
  assert.equal(attrs.get("data-theme"), "dark");
  assert.equal(attrs.get("data-thread-fold"), "expanded");
  response.documentElement.getAttribute = () => null;
  listeners["htmx:after:swap"](event);
  assert.deepEqual(current, ["new canonical", "new JSON-LD"]);
  assert.equal(attrs.has("data-thread-root-id"), false);
  assert.equal(attrs.has("data-thread-context"), false);
  assert.equal(attrs.has("data-nav-up"), false);
  assert.equal(attrs.get("data-theme"), "dark");
});
