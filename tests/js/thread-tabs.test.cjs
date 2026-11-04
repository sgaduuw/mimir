const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { test } = require("node:test");
const vm = require("node:vm");

const source = readFileSync("mimir/static/js/thread-tabs.js", "utf8");

function element(attrs = {}) {
  return {
    attrs, children: [], events: {},
    getAttribute(name) { return this.attrs[name] ?? null; },
    hasAttribute(name) { return name in this.attrs; },
    setAttribute(name, value) { this.attrs[name] = value; },
    addEventListener(name, fn) { this.events[name] = fn; },
    appendChild(child) { this.children.push(child); },
    set textContent(value) { this.text = value; this.children = []; },
    get textContent() { return this.text || ""; },
    focus() {},
  };
}

function reader(hash, kinds, next) {
  const tabs = ["discussion", "patches", "all"].map(name => element({ "data-tab": name }));
  const tablist = element();
  tablist.querySelectorAll = () => tabs;
  tablist.querySelector = () => ({ textContent: "4" });
  const box = element();
  let cards, boundary;
  function page(kinds, next) {
    cards = kinds.map(kind => element({ "data-kind": kind }));
    boundary = next ? element(next) : null;
  }
  page(kinds, next);
  box.querySelectorAll = () => cards;
  box.querySelector = selector => {
    assert.equal(selector, ".thread-more");
    return boundary;
  };
  box.insertBefore = child => box.children.unshift(child);
  const body = element();
  const window = element();
  const location = { hash };
  vm.runInNewContext(source, {
    location, window,
    history: { replaceState(_state, _title, hash) { location.hash = hash; } },
    document: {
      body,
      querySelector: () => tablist,
      getElementById: id => id === "thread-messages" ? box : null,
      createElement: () => element(),
    },
  });
  return { box, tabs, location, empty: box.children[0], append(kinds, next) {
    page(kinds, next);
    body.events["htmx:afterSwap"]();
  } };
}

test("empty tab follows the latest append boundary and clears stale onward links", () => {
  const r = reader("#patches", ["reply"], { "data-next-patches": "/t/3#patches" });
  assert.equal(r.box.getAttribute("data-tab"), "patches");
  assert.equal(r.empty.hidden, false);
  assert.equal(r.empty.children[0].href, "/t/3#patches");
  r.append(["reply", "reply"], { "data-next-patches": "/t/4#patches" });
  assert.equal(r.empty.children[0].href, "/t/4#patches");
  r.append(["reply", "patch"], null);
  assert.equal(r.empty.hidden, true);
  assert.equal(r.empty.children.length, 0);
});

test("discussion links to later visible content, and an exhausted tab has no link", () => {
  const r = reader("#discussion", ["patch"], { "data-next-discussion": "/t/4#discussion" });
  assert.equal(r.empty.children[0].href, "/t/4#discussion");
  r.append(["patch"], null);
  assert.equal(r.empty.hidden, false);
  assert.equal(r.empty.children.length, 0);
  r.append(["patch", "cover"], null);
  assert.equal(r.empty.hidden, true);
  r.tabs[2].events.click();
  assert.equal(r.location.hash, "#all");
  assert.equal(r.empty.hidden, true);
});
