/* In-page links on the thread page (#1035).
 *
 * A "replied to" link, an outline row or a Patches-index link names the
 * thread page that holds its message, plus the message's #m<id> anchor. When that message is
 * already in this document (HTMX appended the page that holds it), follow
 * the anchor instead of loading the page again. Otherwise the link works
 * as a plain link, which is also all it does without JS.
 *
 * It pushes the link's whole URL rather than setting location.hash: the
 * message can sit on an appended page, and a hash alone would keep page
 * 1's path, so the address bar (copied, reloaded) would name a page that
 * does not hold it. pushState fires no hashchange, so it sends one for
 * thread-tabs.js to open the tab that holds the message; that also
 * covers a second click on the same link, which a hash would not.
 * Delegated on the document, because appended slices arrive after this
 * runs. Modified clicks pass through, so a new tab still opens the page.
 * In /static/ because the CSP blocks inline scripts.
 */
(function () {
  document.addEventListener("click", function (e) {
    if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    var a = e.target.closest(".replied-to a, .thread-outline a, .patch-index a");
    if (!a) return;
    var url = new URL(a.href, location.href);
    var el = /^#m\d+$/.test(url.hash) && document.getElementById(url.hash.slice(1));
    if (!el) return;
    e.preventDefault();
    if (url.href !== location.href) history.pushState(null, "", url.href);
    /* The tab first: a message in a hidden tab cannot be scrolled to. */
    window.dispatchEvent(new HashChangeEvent("hashchange"));
    el.scrollIntoView();
  });
})();
