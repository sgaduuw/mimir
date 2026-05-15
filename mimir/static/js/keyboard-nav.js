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
    // The active thread-list <li> is the source of truth for "current
    // position" on the message page, thread-fold.js keeps it in sync
    // with the rendered #msg via htmx:afterSwap.
    return document.querySelector(".thread-list li.is-active");
  }

  function navigateThread(direction) {
    var active = focusedThreadItem();
    if (!active) return false;
    // Walk siblings until we hit a thread-list <li> with a clickable
    // anchor. The off-list-ancestor row has no anchor; skip past it.
    var next = direction === "next"
      ? active.nextElementSibling
      : active.previousElementSibling;
    while (next) {
      var anchor = next.querySelector("a[href]");
      if (anchor) {
        anchor.click();
        return true;
      }
      next = direction === "next"
        ? next.nextElementSibling
        : next.previousElementSibling;
    }
    return false;
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
