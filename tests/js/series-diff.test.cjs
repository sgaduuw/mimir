const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { test } = require("node:test");
const vm = require("node:vm");

test("diff swap/history metadata and selection failure state follow the readable article", () => {
  const listeners = {};
  const head = new Map();
  const html = {};
  let clicked = 0;
  let focused = 0;
  const page = { dataset: { position: "1", inbox: "alpha", pageTitle: "Patch 1", pageDescription: "First patch", canonical: "https://test/first" }, focus() { focused++; } };
  let submit = { hidden: false };
  const select = { form: { querySelector: () => submit }, value: "2", selectedIndex: 1, hasAttribute: () => true, options: [{}, { getAttribute: () => "/second" }] };
  const link = { getAttribute: () => "/second", click() { clicked++; } };
  const document = {
    getElementById: id => id === "series-diff" ? page : select,
    querySelector(selector) { if (!head.has(selector)) head.set(selector, {}); return head.get(selector); },
    querySelectorAll: () => [link],
    documentElement: { setAttribute: (name, value) => html[name] = value },
    addEventListener: (name, handler) => listeners[name] = handler,
  };
  vm.runInNewContext(readFileSync("mimir/static/js/series-diff.js", "utf8"), { document });
  assert.equal(submit.hidden, true);
  listeners.change({ target: select });
  assert.equal(clicked, 1);
  assert.equal(select.value, "1"); // a failed request leaves the old selection intact
  page.dataset = { position: "2", inbox: "beta", pageTitle: "Patch 2", pageDescription: "Second patch", canonical: "https://test/second" };
  submit = { hidden: false }; // a newly swapped native form starts with Go visible
  listeners["htmx:afterSwap"]({ detail: { elt: { tagName: "MAIN" }, target: { tagName: "MAIN" } } });
  assert.equal(submit.hidden, true);
  assert.equal(document.title, "Patch 2");
  assert.equal(head.get('link[rel="canonical"]').href, "https://test/second");
  assert.equal(head.get('meta[name="description"]').content, "Second patch");
  assert.equal(head.get('meta[property="og:url"]').content, "https://test/second");
  assert.equal(head.get('link[type="application/atom+xml"]').href, "/beta/feed.atom");
  assert.equal(html["data-nav-up"], "/beta/");
  assert.equal(focused, 1);
  // HTMX history swaps supply the swapped elt, without request target details.
  page.dataset.pageTitle = "History swap";
  submit = { hidden: false };
  listeners["htmx:afterSwap"]({ detail: { elt: { tagName: "MAIN" } } });
  assert.equal(submit.hidden, true);
  assert.equal(document.title, "History swap");
  assert.equal(focused, 1);
  listeners["htmx:afterSwap"]({ detail: { elt: { tagName: "DIV" } } });
  assert.equal(focused, 1);
  page.dataset.inbox = "alpha";
  page.dataset.pageTitle = "Restored";
  submit = { hidden: false };
  listeners["htmx:historyRestore"]();
  assert.equal(submit.hidden, true);
  assert.equal(document.title, "Restored");
  assert.equal(html["data-nav-up"], "/alpha/");
  assert.equal(focused, 1); // history retains its own scroll/focus behavior
});
