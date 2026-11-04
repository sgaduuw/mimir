const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { test } = require("node:test");
const vm = require("node:vm");

const source = readFileSync("mimir/static/js/thread-fold.js", "utf8");

function firstPaint(context, pin, blocked = false) {
  const attrs = { "data-thread-root-id": "7", "data-thread-context": context };
  const writes = [];
  const buttons = ["closed", "partial", "expanded"].map(mode => ({
    attrs: { "data-fold-set": mode },
    getAttribute(name) { return this.attrs[name]; },
    setAttribute(name, value) { this.attrs[name] = value; },
    addEventListener() {},
  }));
  const section = {
    getAttribute: name => attrs[name],
    querySelector: () => null,
    querySelectorAll: () => buttons,
  };
  vm.runInNewContext(source, {
    localStorage: {
      getItem(key) {
        assert.equal(key, "mimir.fold.7");
        if (blocked) throw Error("storage blocked");
        return pin;
      },
      setItem: (...args) => writes.push(args),
    },
    document: {
      readyState: "complete",
      body: { addEventListener() {}, removeEventListener() {} },
      querySelector: () => section,
      documentElement: {
        getAttribute: name => attrs[name],
        setAttribute: (name, value) => attrs[name] = value,
      },
      addEventListener() {},
    },
  });
  for (const button of buttons) {
    assert.equal(button.attrs["aria-pressed"], String(button.attrs["data-fold-set"] === attrs["data-thread-fold"]));
  }
  return { mode: attrs["data-thread-fold"], writes };
}

test("fresh root and reply views show the compact tree without writing a preference", () => {
  for (const context of ["root", "deep"]) {
    for (const blocked of [false, true]) {
      assert.deepEqual(firstPaint(context, null, blocked), { mode: "partial", writes: [] });
    }
  }
});

test("each saved fold choice still wins without a passive write", () => {
  for (const context of ["root", "deep"]) {
    for (const pin of ["closed", "partial", "expanded"]) {
      assert.deepEqual(firstPaint(context, pin), { mode: pin, writes: [] });
    }
  }
});
