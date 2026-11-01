/* Vim-style keyboard navigation controller.
 *
 * Bindings:
 *   h     left / back out, navigates to <html data-nav-up>'s URL
 *   j     down / next message in the current thread (message page only)
 *   k     up / previous message in the current thread (message page only)
 *   l     right / drill in, navigates to <html data-nav-down>'s URL
 *   Esc   close help / close thread-fold
 *   ?     toggle help overlay
 *
 * Lives in mimir/static/ rather than inline because the production CSP
 * (script-src 'self' https://unpkg.com) blocks inline <script>. Loaded
 * with `defer` so it runs after the DOM is parsed; nothing here needs
 * to run before paint.
 *
 * Modifier keys (Ctrl/Meta/Alt) bypass us so browser shortcuts pass
 * through unaltered. Input-shaped focus targets (INPUT, TEXTAREA,
 * SELECT, [contenteditable]) also bypass, so the search box and any
 * future admin forms keep their typing.
 *
 * j/k navigation drives the thread-list <a hx-get …> anchors via
 * .click(), so HTMX picks up the intra-thread swap naturally and the
 * is-active marker flip handled by thread-fold.js's htmx:afterSwap
 * hook still works without modification.
 */
(function () {
  "use strict";

  function isTypingTarget(el) {
    if (!el) return false;
    var tag = el.tagName;
    if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return true;
    if (el.isContentEditable) return true;
    return false;
  }

  function openHelp() {
    var dlg = document.getElementById("keyboard-help");
    if (dlg && typeof dlg.showModal === "function" && !dlg.open) {
      dlg.showModal();
    }
  }

  function closeHelp() {
    var dlg = document.getElementById("keyboard-help");
    if (dlg && dlg.open) {
      dlg.close();
      return true;
    }
    return false;
  }

  function toggleHelp() {
    var dlg = document.getElementById("keyboard-help");
    if (!dlg) return;
    if (dlg.open) dlg.close();
    else if (typeof dlg.showModal === "function") dlg.showModal();
  }

  function focusedThreadItem() {
    // Read #msg rather than the marker: this also works when our
    // afterSwap handler runs before thread-fold.js updates is-active.
    var msg = document.getElementById("msg");
    var id = msg && msg.getAttribute("data-article-id");
    return id ? document.querySelector('.thread-list li[data-article-id="' + id + '"]') : null;
  }

  function threadNeighbor(direction) {
    var active = focusedThreadItem();
    if (!active) return null;
    var tree = document.querySelector(".thread-context");
    if (direction === "prev" && tree &&
        active.getAttribute("data-article-id") === tree.getAttribute("data-thread-root-id")) return null;
    var next = direction === "next"
      ? active.nextElementSibling
      : active.previousElementSibling;
    while (next) {
      var anchor = next.hasAttribute("data-article-id")
        ? next.querySelector("a[href]") : null;
      if (anchor) return anchor;
      next = direction === "next" ? next.nextElementSibling : next.previousElementSibling;
    }
    return null;
  }

  function navigateThread(direction) {
    var anchor = threadNeighbor(direction);
    if (!anchor) return false;
    anchor.click();
    return true;
  }

  function syncMessagePager() {
    // An HTMX swap preserves the original window tree. Deriving the
    // controls from a freshly computed server window would disagree
    // with j/k after opening a message with different visible children.
    ["prev", "next"].forEach(function (direction) {
      var link = document.querySelector('#msg [data-message-nav="' + direction + '"]');
      if (!link) return;
      var neighbor = threadNeighbor(direction);
      if (neighbor) {
        var url = neighbor.getAttribute("href");
        link.setAttribute("href", url);
        link.setAttribute("hx-get", url);
        link.setAttribute("hx-target", "#msg");
        link.setAttribute("hx-swap", "outerHTML show:#msg:top");
        link.setAttribute("hx-push-url", "true");
        link.removeAttribute("aria-disabled");
        link.removeAttribute("tabindex");
      } else {
        ["href", "hx-get", "hx-target", "hx-swap", "hx-push-url"].forEach(function (name) { link.removeAttribute(name); });
        link.setAttribute("aria-disabled", "true");
        link.setAttribute("tabindex", "-1");
      }
      // htmx caches the verb/path when binding; changing hx-get alone
      // leaves a previously processed control targeting the old message.
      if (window.htmx) window.htmx.process(link);
    });
  }

  document.addEventListener("htmx:afterSwap", function (event) {
    if (event.detail.target && event.detail.target.id === "msg") syncMessagePager();
  });
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", syncMessagePager);
  } else {
    syncMessagePager();
  }

  function navigateTo(attr) {
    var url = document.documentElement.getAttribute(attr);
    if (!url) return false;
    window.location.href = url;
    return true;
  }

  function closeThreadFold() {
    var html = document.documentElement;
    if (html.getAttribute("data-thread-fold") === "expanded") {
      html.setAttribute("data-thread-fold", "partial");
      return true;
    }
    if (html.getAttribute("data-thread-fold") === "partial") {
      html.setAttribute("data-thread-fold", "closed");
      return true;
    }
    return false;
  }

  document.addEventListener("keydown", function (e) {
    if (e.ctrlKey || e.metaKey || e.altKey) return;
    if (isTypingTarget(e.target)) return;

    switch (e.key) {
      case "h":
        if (navigateTo("data-nav-up")) e.preventDefault();
        break;
      case "j":
        if (navigateThread("next")) e.preventDefault();
        break;
      case "k":
        if (navigateThread("prev")) e.preventDefault();
        break;
      case "l":
        if (navigateTo("data-nav-down")) e.preventDefault();
        break;
      case "Escape":
        if (closeHelp() || closeThreadFold()) e.preventDefault();
        break;
      case "?":
        // `?` is Shift+/ on most layouts; we already bypass on
        // typing targets so this is safe.
        toggleHelp();
        e.preventDefault();
        break;
      default:
        break;
    }
  });

  // Help dialog: ESC dismisses natively via <dialog>; clicking the
  // backdrop also closes it. Attach the backdrop-click handler once.
  function wireHelpDialog() {
    var dlg = document.getElementById("keyboard-help");
    if (!dlg) return;
    dlg.addEventListener("click", function (e) {
      // Click landed on the dialog element itself (i.e., the
      // backdrop area outside the content box). Inner clicks
      // bubble from <article> children and hit a different target.
      if (e.target === dlg) dlg.close();
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", wireHelpDialog);
  } else {
    wireHelpDialog();
  }
})();
