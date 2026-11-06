const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { test } = require("node:test");
const vm = require("node:vm");

function page(attributes = {}) {
  const listeners = {};
  let helpBindings = 0;
  const help = { open: false, showModal() { this.open = true; }, close() { this.open = false; }, set onclick(handler) { helpBindings++; this.click = handler; } };
  const window = { location: { href: "/current" } };
  let threadClicks = 0;
  const diffClicks = [];
  const anchor = { click() { threadClicks++; } };
  const sibling = { hasAttribute: () => true, querySelector: () => anchor, nextElementSibling: null, previousElementSibling: null };
  const active = { nextElementSibling: sibling, previousElementSibling: sibling, getAttribute: () => "2" };
  const document = {
    readyState: "complete",
    documentElement: { getAttribute: name => attributes[name] ?? null, setAttribute: (name, value) => attributes[name] = value },
    addEventListener: (name, handler) => listeners[name] = handler,
    getElementById: id => id === "keyboard-help" ? help : (id === "msg" && attributes.thread ? { getAttribute: () => "2" } : null),
    querySelector(selector) {
      if (selector.startsWith('#series-diff')) {
        const direction = selector.includes('"next"') ? "next" : "prev";
        const url = attributes[direction];
        return url ? { click() { diffClicks.push(url); } } : null;
      }
      if (attributes.thread && selector.startsWith(".thread-list")) return active;
      if (attributes.thread && selector === ".thread-context") return { getAttribute: () => "1" };
      return null;
    },
  };
  vm.runInNewContext(readFileSync("mimir/static/js/keyboard-nav.js", "utf8"), { window, document });
  return {
    help, window, listeners, diffClicks, get helpBindings() { return helpBindings; }, get threadClicks() { return threadClicks; },
    key(key, extra = {}) {
      let prevented = false;
      listeners.keydown({ key, target: { tagName: "BODY" }, preventDefault() { prevented = true; }, ...extra });
      return prevented;
    },
  };
}

test("j/k follow series position URLs and stop at missing boundaries", () => {
  const p = page({ next: "/next", prev: "/previous" });
  assert.equal(p.key("j"), true);
  assert.deepEqual(p.diffClicks, ["/next"]);
  assert.equal(p.key("k"), true);
  assert.deepEqual(p.diffClicks, ["/next", "/previous"]);
  const boundary = page();
  assert.equal(boundary.key("k"), false);
  assert.equal(boundary.window.location.href, "/current");
});

test("series shortcuts preserve typing, modifier and open-help guards", () => {
  const p = page({ next: "/next" });
  for (const tagName of ["INPUT", "TEXTAREA", "SELECT"]) assert.equal(p.key("j", { target: { tagName } }), false);
  assert.equal(p.key("j", { target: { isContentEditable: true } }), false);
  for (const modifier of ["ctrlKey", "metaKey", "altKey"]) assert.equal(p.key("j", { [modifier]: true }), false);
  p.key("?");
  assert.equal(p.help.open, true);
  assert.equal(p.key("j"), false);
  assert.equal(p.window.location.href, "/current");
  assert.equal(p.key("Escape"), true);
  assert.equal(p.help.open, false);
});

test("message j/k still click thread neighbors", () => {
  const p = page({ thread: true });
  assert.equal(p.key("j"), true);
  assert.equal(p.key("k"), true);
  assert.equal(p.threadClicks, 2);
});


test("history-restored help dialog gets its backdrop dismissal listener", () => {
  const p = page();
  assert.equal(p.helpBindings, 1);
  assert.equal(typeof p.listeners["htmx:after:swap"], "function");
  p.listeners["htmx:after:swap"]();
  assert.equal(p.helpBindings, 2);
});
