/* Thread-tree fold-state controller.
 *
 * Loaded from <head> with src="...thread-fold.js" so it runs synchronously
 * before <body> parses. The early block reads context (data-thread-root,
 * data-thread-context) off <html> -- set by Jinja blocks in base.html and
 * message.html -- plus any localStorage pin, and sets data-thread-fold on
 * <html>. CSS attribute selectors then resolve the right state (closed /
 * partial / expanded) on first paint, so a pin overriding the server's
 * default doesn't flash.
 *
 * The wire-handlers block defers until DOMContentLoaded since it needs the
 * actual .thread-context section to exist in the DOM.
 *
 * Lives in mimir/static/ rather than inline because the production CSP
 * (script-src 'self' https://unpkg.com) blocks inline <script> tags
 * without an explicit nonce or 'unsafe-inline'; external scripts under
 * 'self' are allowed.
 */
(function () {
  var html = document.documentElement;
  var rootId = html.getAttribute("data-thread-root") || null;
  var ctx = html.getAttribute("data-thread-context") || "deep";

  if (rootId || ctx === "root") {
    var pin = null;
    try {
      pin = rootId ? localStorage.getItem("mimir.fold." + rootId) : null;
    } catch (e) {
      /* private mode etc.; fall through to default. */
    }
    var mode = pin || (ctx === "root" ? "partial" : "closed");
    html.setAttribute("data-thread-fold", mode);
  }

  function init() {
    var section = document.querySelector(".thread-context");
    if (!section) return;
    var rootIdNow = section.getAttribute("data-thread-root") || rootId;

    function reflectToggle(modeNow) {
      section.querySelectorAll(".thread-toggle button").forEach(function (b) {
        b.setAttribute(
          "aria-pressed",
          b.getAttribute("data-fold-set") === modeNow ? "true" : "false"
        );
      });
    }

    function setFold(modeNow, persist) {
      html.setAttribute("data-thread-fold", modeNow);
      reflectToggle(modeNow);
      if (persist && rootIdNow) {
        try {
          localStorage.setItem("mimir.fold." + rootIdNow, modeNow);
        } catch (e) {
          /* ignore */
        }
      }
      if (modeNow === "partial") scrollActiveIntoView();
    }

    function scrollActiveIntoView() {
      var box = section.querySelector(".thread-box");
      var active = section.querySelector(".thread-list li.is-active");
      if (!box || !active) return;
      var liRect = active.getBoundingClientRect();
      var boxRect = box.getBoundingClientRect();
      var target =
        box.scrollTop +
        (liRect.top - boxRect.top) -
        boxRect.height / 2 +
        liRect.height / 2;
      box.scrollTop = Math.max(0, target);
    }

    reflectToggle(html.getAttribute("data-thread-fold") || "partial");
    if ((html.getAttribute("data-thread-fold") || "partial") === "partial") {
      scrollActiveIntoView();
    }

    section.querySelectorAll(".thread-toggle button").forEach(function (b) {
      b.addEventListener("click", function () {
        setFold(b.getAttribute("data-fold-set"), true);
      });
    });

    /* In `closed` mode, clicking anywhere on the toolbar (except the toggle
     * buttons themselves) opens the tree to `partial`. The CSS sets
     * cursor: pointer on the toolbar in closed mode to signal it. */
    var toolbar = section.querySelector(".thread-toolbar");
    if (toolbar) {
      toolbar.addEventListener("click", function (e) {
        if (e.target.closest("button")) return;
        if (html.getAttribute("data-thread-fold") === "closed") {
          setFold("partial", true);
        }
      });
    }

    /* After an HTMX swap, flip the active marker to whichever <li>
     * matches the new article's message-id. Class-toggling only; no DOM
     * rewrites, so hx-get on every anchor stays intact. */
    document.body.addEventListener("htmx:afterSwap", function () {
      var msg = document.getElementById("msg");
      if (!msg) return;
      var newId = msg.getAttribute("data-message-id");
      if (!newId) return;
      section.querySelectorAll(".thread-list li[data-message-id]").forEach(function (li) {
        li.classList.toggle("is-active", li.getAttribute("data-message-id") === newId);
      });
      if (html.getAttribute("data-thread-fold") === "partial") scrollActiveIntoView();
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
