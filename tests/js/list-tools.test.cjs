const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { test } = require("node:test");
const vm = require("node:vm");

function leaf(text, open = false) {
  const details = { open };
  return { hidden: false, details, getAttribute: () => text, querySelectorAll: () => [], querySelector: () => details };
}

function group(children, open = false) {
  const details = { open };
  return { hidden: false, details, getAttribute: () => "smaller subsystems", querySelectorAll: () => children, querySelector: () => details };
}

function load(items, input) {
  const controls = { hidden: true };
  const listeners = {};
  input.addEventListener = (name, fn) => (listeners[name] = fn);
  const list = { querySelectorAll: () => items };
  const document = {
    getElementById: id => (id === "list" ? list : null),
    querySelectorAll: selector => (selector === "[data-filter-controls]" ? [controls] : [input]),
  };
  vm.runInNewContext(readFileSync("mimir/static/js/list-tools.js", "utf8"), { document });
  return { controls, type: value => { input.value = value; listeners.input(); } };
}

function fixture() {
  const mm = leaf("mm", true);
  const net = leaf("net");
  const small1 = leaf("scsi");
  const small2 = leaf("rtc");
  const smaller = group([small1, small2]);
  const input = { value: "", getAttribute: () => "list" };
  const ui = load([mm, net, smaller, small1, small2], input);
  return { mm, net, small1, small2, smaller, ...ui };
}

test("controls are shown only when the script runs", () => {
  assert.equal(fixture().controls.hidden, false);
});

test("typing hides non-matches, case-insensitively and trimmed", () => {
  const f = fixture();
  f.type("  NE ");
  assert.equal(f.net.hidden, false);
  assert.equal(f.mm.hidden, true);
  assert.equal(f.small1.hidden, true);
});

test("a match inside the group shows the group and opens it", () => {
  const f = fixture();
  f.type("rtc");
  assert.equal(f.small2.hidden, false);
  assert.equal(f.small1.hidden, true);
  assert.equal(f.smaller.hidden, false);
  assert.equal(f.smaller.details.open, true);
});

test("a query nothing matches hides the group too", () => {
  const f = fixture();
  f.type("zzz");
  assert.equal(f.smaller.hidden, true);
  assert.equal(f.mm.hidden, true);
});

test("clearing restores visibility and the rendered open state", () => {
  const f = fixture();
  f.type("rtc");
  f.type("");
  for (const item of [f.mm, f.net, f.small1, f.small2, f.smaller]) assert.equal(item.hidden, false);
  assert.equal(f.smaller.details.open, false);
  assert.equal(f.mm.details.open, true);
});
