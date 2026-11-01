/* In-page links on the thread page (#1035).
 *
 * A "replied to" link, an outline row or a Patches-index link names the
 * thread page that holds its message, plus the message's #m<id> anchor. When that message is
 * already in this document (HTMX appended the page that holds it), follow
 * the anchor instead of loading the page again. Otherwise the link works
 * as a plain link, which is also all it does without JS.
 *
 * It sets location.hash rather than only scrolling, so the URL names the
 * message and thread-tabs.js (on hashchange) opens the tab that holds it.
 * Delegated on the document, because appended slices arrive after this
 * runs. Modified clicks pass through, so a new tab still opens the page.
 * In /static/ because the CSP blocks inline scripts.
 */
(function () {
  document.addEventListener("click", function (e) {
    if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    var a = e.target.closest(".replied-to a, .thread-outline a, .patch-index a");
    if (!a) return;
    var hash = new URL(a.href, location.href).hash;
    var el = /^#m\d+$/.test(hash) && document.getElementById(hash.slice(1));
    if (!el) return;
    e.preventDefault();
    /* Setting the hash it already has neither scrolls nor fires hashchange. */
    if (location.hash === hash) el.scrollIntoView();
    else location.hash = hash;
  });
})();
