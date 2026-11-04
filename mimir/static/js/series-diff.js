/* Full-response HTMX navigation keeps the document and history metadata aligned. */
(function () {
  "use strict";

  function enhanceSelector() {
    var select = document.getElementById("series-diff-position");
    if (!select) return;
    // The change handler navigates; keep the submit button as the no-JS fallback.
    var button = select.form.querySelector('button[type="submit"]');
    if (button) button.hidden = true;
  }

  function syncDocument() {
    var page = document.getElementById("series-diff");
    if (!page) return;
    enhanceSelector();
    var data = page.dataset;
    document.title = data.pageTitle;
    document.querySelector('link[rel="canonical"]').href = data.canonical;
    ['meta[name="description"]', 'meta[property="og:description"]', 'meta[name="twitter:description"]'].forEach(function (selector) {
      document.querySelector(selector).content = data.pageDescription;
    });
    ['meta[property="og:title"]', 'meta[name="twitter:title"]'].forEach(function (selector) {
      document.querySelector(selector).content = data.pageTitle;
    });
    document.querySelector('meta[property="og:url"]').content = data.canonical;
    var feed = document.querySelector('link[type="application/atom+xml"]');
    feed.href = "/" + data.inbox + "/feed.atom";
    feed.title = data.inbox + " feed";
    document.documentElement.setAttribute("data-nav-up", "/" + data.inbox + "/");
  }

  function resetSelector() {
    var page = document.getElementById("series-diff");
    if (page) document.getElementById("series-diff-position").value = page.dataset.position;
  }

  document.addEventListener("change", function (event) {
    var select = event.target;
    if (!select || !select.hasAttribute("data-series-diff-select")) return;
    var option = select.options[select.selectedIndex];
    var url = option && option.getAttribute("data-url");
    // Keep the control describing the readable body if the request fails.
    resetSelector();
    var links = document.querySelectorAll("#series-diff .series-diff-overview a");
    for (var i = 0; i < links.length; i++) {
      if (links[i].getAttribute("href") === url) {
        links[i].click();
        return;
      }
    }
  });
  enhanceSelector();
  document.addEventListener("htmx:beforeRequest", resetSelector);
  document.addEventListener("htmx:afterSwap", function (event) {
    if (event.detail.elt.tagName !== "MAIN") return;
    syncDocument();
    var page = document.getElementById("series-diff");
    // History swaps lack request target details and retain their own focus.
    if (page && event.detail.target) page.focus({ preventScroll: true });
  });
  document.addEventListener("htmx:historyRestore", syncDocument);
})();
