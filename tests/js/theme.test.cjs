const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { test } = require("node:test");
const vm = require("node:vm");

const source = readFileSync("mimir/static/js/theme.js", "utf8");

function page(saved, blocked = false) {
  const attributes = {};
  const values = new Map(saved === null ? [] : [["mimir.theme", saved]]);
  let control = { hidden: true };
  const selector = () => ({ value: "system", get change() { return this.onchange; } });
  let select = selector();
  const listeners = {};
  const storage = {
    getItem(key) { if (blocked) throw Error("blocked"); return values.get(key) ?? null; },
    setItem(key, value) { if (blocked) throw Error("blocked"); values.set(key, value); },
    removeItem(key) { if (blocked) throw Error("blocked"); values.delete(key); },
  };
  vm.runInNewContext(source, {
    window: { addEventListener: (event, handler) => listeners[event] = handler },
    localStorage: storage,
    document: {
      documentElement: {
        setAttribute: (key, value) => attributes[key] = value,
        removeAttribute: key => delete attributes[key],
      },
      addEventListener: (event, handler) => listeners[event] = handler,
      getElementById: id => id === "theme-select" ? select : control,
    },
  });
  return {
    attributes, values,
    get control() { return control; },
    get select() { return select; },
    ready: () => listeners.DOMContentLoaded(),
    pageshow: () => listeners.pageshow({ persisted: true }),
    restore() {
      select = selector();
      control = { hidden: true };
      listeners["htmx:historyRestore"]();
    },
  };
}

test("saved theme applies before DOM readiness; selection persists and System clears it", () => {
  const p = page("dark");
  assert.equal(p.attributes["data-theme"], "dark");
  p.ready();
  assert.equal(p.control.hidden, false);
  assert.equal(p.select.value, "dark");
  p.select.value = "light";
  p.select.change();
  assert.equal(p.attributes["data-theme"], "light");
  assert.equal(p.values.get("mimir.theme"), "light");
  const reloaded = page(p.values.get("mimir.theme"));
  assert.equal(reloaded.attributes["data-theme"], "light");
  p.select.value = "system";
  p.select.change();
  assert.equal(p.attributes["data-theme"], undefined);
  assert.equal(p.values.has("mimir.theme"), false);
});

test("system default and invalid saved values leave the OS in control", () => {
  for (const saved of [null, "system", "invalid"]) {
    const p = page(saved);
    p.ready();
    assert.equal(p.attributes["data-theme"], undefined);
    assert.equal(p.select.value, "system");
  }
});

test("blocked storage does not prevent switching themes", () => {
  const p = page(null, true);
  p.ready();
  p.select.value = "dark";
  assert.doesNotThrow(() => p.select.change());
  assert.equal(p.attributes["data-theme"], "dark");
  p.pageshow();
  assert.equal(p.attributes["data-theme"], "dark");
  p.select.value = "system";
  assert.doesNotThrow(() => p.select.change());
  assert.equal(p.attributes["data-theme"], undefined);
});

test("history-restored navigation keeps the control usable and reflects the current choice", () => {
  const p = page("light");
  p.ready();
  p.select.value = "dark";
  p.select.change();
  p.restore();
  assert.equal(p.control.hidden, false);
  assert.equal(p.select.value, "dark");
  p.select.value = "light";
  p.select.change();
  assert.equal(p.attributes["data-theme"], "light");
  assert.equal(p.values.get("mimir.theme"), "light");
});

test("Back restores the preference selected on a later page", () => {
  const p = page("light");
  p.ready();
  p.values.set("mimir.theme", "dark");
  p.pageshow();
  assert.equal(p.attributes["data-theme"], "dark");
  assert.equal(p.select.value, "dark");
  p.values.delete("mimir.theme");
  p.pageshow();
  assert.equal(p.attributes["data-theme"], undefined);
  assert.equal(p.select.value, "system");
});
