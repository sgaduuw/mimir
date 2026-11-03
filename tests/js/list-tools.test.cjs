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

function sortFixture() {
  const make = (order, active) => {
    const attrs = { "data-order": String(order), "data-active": String(active) };
    return { attrs, getAttribute: name => attrs[name] };
  };
  // Default order is the server's (pinned first, then A to Z); the numbers
  // are 30-day counts. Two rows tie on 5 to prove the default order breaks it.
  const rows = [make(0, 1), make(1, 5), make(2, 9), make(3, 5)];
  const list = {
    children: [...rows],
    appendChild(item) {
      this.children = this.children.filter(c => c !== item).concat(item);
    },
  };
  const pressed = {};
  const button = mode => {
    const listeners = {};
    return {
      mode,
      getAttribute: name => ({ "data-sort-button": mode, "data-sort-list": "list" })[name],
      setAttribute: (name, value) => (pressed[mode] = value),
      addEventListener: (name, fn) => (listeners[name] = fn),
      click: () => listeners.click(),
    };
  };
  const buttons = [button("default"), button("active")];
  const document = {
    getElementById: id => (id === "list" ? list : null),
    querySelectorAll: selector => (selector === "[data-sort-button]" ? buttons : []),
  };
  vm.runInNewContext(readFileSync("mimir/static/js/list-tools.js", "utf8"), { document, Number, Array });
  return { rows, list, buttons, pressed, order: () => list.children.map(r => r.attrs["data-order"]) };
}

test("sorting by activity puts the busiest first and keeps the server order on ties", () => {
  const f = sortFixture();
  f.buttons[1].click();
  assert.deepEqual(f.order(), ["2", "1", "3", "0"]);
  assert.equal(f.pressed.active, "true");
  assert.equal(f.pressed.default, "false");
});

test("the default button restores the server order", () => {
  const f = sortFixture();
  f.buttons[1].click();
  f.buttons[0].click();
  assert.deepEqual(f.order(), ["0", "1", "2", "3"]);
  assert.equal(f.pressed.default, "true");
});

test("ties break by the server order even when the page is arranged otherwise", () => {
  const f = sortFixture();
  f.list.children = [...f.rows].reverse(); // as if shuffled by an earlier sort
  f.buttons[1].click();
  assert.deepEqual(f.order(), ["2", "1", "3", "0"]);
});
