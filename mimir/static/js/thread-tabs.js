/* Display-only tabs on the thread page (#989).
 *
 * The server renders every message, each with its data-kind (cover,
 * patch, reply) and the root with data-root; this script only sets
 * data-tab on the message container, and CSS hides what that tab does
 * not show. So a slice HTMX appends needs no work here beyond the empty
 * notice. The root shows in every tab. The tab lives in the URL fragment
 * (#discussion, #patches, #all), which never reaches the server; a
 * message anchor (#m123) opens the tab that holds that message.
 * Deferred and in /static/ because the CSP blocks inline scripts.
 */
(function () {
  var tablist = document.querySelector(".thread-tabs");
  var box = document.getElementById("thread-messages");
  if (!tablist || !box) return;
  var tabs = Array.prototype.slice.call(tablist.querySelectorAll('[role="tab"]'));
  var names = tabs.map(function (t) { return t.getAttribute("data-tab"); });
  var empty = document.createElement("p");
  empty.className = "tab-empty";
  empty.hidden = true;
  box.insertBefore(empty, box.firstChild);

  /* A patch other than the root; the root shows in every tab. */
  function isPatch(el) {
    return el.getAttribute("data-kind") === "patch" && !el.hasAttribute("data-root");
  }

  /* Messages the tab shows besides the root, which shows everywhere. */
  function shown(name) {
    return Array.prototype.filter.call(box.querySelectorAll(".thread-message:not([data-root])"), function (el) {
      return isPatch(el) === (name === "patches");
    });
  }

  function updateEmpty() {
    var name = box.getAttribute("data-tab");
    var none = name !== "all" && shown(name).length === 0;
    empty.textContent = name === "patches" ? "No patches on this page." : "No replies on this page.";
    empty.hidden = !none;
  }

  function select(name, focus) {
    box.setAttribute("data-tab", name);
    tabs.forEach(function (t) {
      var on = t.getAttribute("data-tab") === name;
      t.setAttribute("aria-selected", String(on));
      t.tabIndex = on ? 0 : -1;
      if (on && focus) t.focus();
    });
    updateEmpty();
  }

  /* The tab a fragment asks for, with the message it names (or null),
   * or null when it names no tab. */
  function fromHash() {
    var hash = location.hash.slice(1);
    if (names.indexOf(hash) !== -1) return { name: hash, el: null };
    if (/^m\d+$/.test(hash)) {
      var el = document.getElementById(hash);
      if (el && box.contains(el)) return { name: isPatch(el) ? "patches" : "discussion", el: el };
    }
    return null;
  }

  function openFromHash() {
    var hit = fromHash();
    if (!hit) return false;
    select(hit.name, false);
    if (hit.el) hit.el.scrollIntoView();
    return true;
  }

  tabs.forEach(function (t) {
    t.addEventListener("click", function () {
      var name = t.getAttribute("data-tab");
      select(name, false);
      /* replaceState, not location.hash: no scroll, no hashchange loop. */
      history.replaceState(null, "", "#" + name);
    });
  });

  /* WAI-ARIA tabs: arrows move between tabs, Home and End to the ends. */
  tablist.addEventListener("keydown", function (e) {
    var i = names.indexOf(box.getAttribute("data-tab"));
    var next = { ArrowRight: i + 1, ArrowLeft: i - 1, Home: 0, End: names.length - 1 }[e.key];
    if (next === undefined) return;
    e.preventDefault();
    var name = names[(next + names.length) % names.length];
    select(name, true);
    history.replaceState(null, "", "#" + name);
  });

  window.addEventListener("hashchange", openFromHash);
  document.body.addEventListener("htmx:afterSwap", updateEmpty);

  tablist.hidden = false;
  if (!openFromHash()) {
    /* Nothing to discuss yet: open on everything, not on an empty column. */
    var replies = tablist.querySelector('[data-tab="discussion"] .tab-count');
    select(Number(replies.textContent) > 0 ? "discussion" : "all", false);
  }
})();
