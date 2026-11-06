/* Keep failed requests inert and refetched history's metadata current. */
(function () {
  "use strict";

  document.addEventListener("htmx:before:swap", function (event) {
    // noSwap alone still processes history and out-of-band content in HTMX 4.
    if (event.detail.ctx.response.status >= 400) event.preventDefault();
  });

  document.addEventListener("htmx:after:swap", function (event) {
    var ctx = event.detail.ctx;
    if (ctx.swap === "none" || ctx.request.headers["HX-History-Restore-Request"] !== "true") return;
    // History replaces the body, leaving head metadata and html attributes behind.
    // Copy only server-owned page metadata; scripts, styles and preferences survive.
    var restored = new DOMParser().parseFromString(ctx.text, "text/html");
    var metadata = 'title, meta[name="description"], meta[name="robots"], meta[property^="og:"], meta[name^="twitter:"], link[rel="canonical"], link[rel="alternate"], link[rel="next"], link[rel="prev"], script[type="application/ld+json"]';
    document.head.querySelectorAll(metadata).forEach(function (node) { node.remove(); });
    restored.head.querySelectorAll(metadata).forEach(function (node) { document.head.appendChild(node.cloneNode(true)); });
    ["data-nav-up", "data-thread-root-id", "data-thread-context"].forEach(function (name) {
      var value = restored.documentElement.getAttribute(name);
      if (value === null) document.documentElement.removeAttribute(name);
      else document.documentElement.setAttribute(name, value);
    });
  });
})();
