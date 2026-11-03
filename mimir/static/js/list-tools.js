/* Filter a list by typing (/merge-window, #1000).
 *
 * `<input data-filter-input="LIST_ID">` filters the `[data-filter-item]`
 * rows inside that list by their `data-filter-text` (already lowercase).
 * A row that holds other rows (the "smaller subsystems" group) shows when
 * any of them matches, and opens its <details> so the match is visible.
 * Clearing the box puts every <details> back as the server rendered it.
 *
 * The controls ship `hidden` inside `[data-filter-controls]` and are shown
 * here, so a browser without JS never sees a box that does nothing. Nothing
 * is stored: a remembered filter would be one more entry on /privacy.
 * Lives in /static/ because the CSP blocks inline scripts.
 */
(function () {
  function all(root, selector) {
    return Array.prototype.slice.call(root.querySelectorAll(selector));
  }

  all(document, "[data-filter-controls]").forEach(function (box) {
    box.hidden = false;
  });

  all(document, "[data-filter-input]").forEach(function (input) {
    var list = document.getElementById(input.getAttribute("data-filter-input"));
    if (!list) return;
    var items = all(list, "[data-filter-item]");
    var rendered = items.map(function (item) {
      var d = item.querySelector("details");
      return d ? d.open : false;
    });

    function apply() {
      var query = input.value.trim().toLowerCase();
      var shown = [];
      // Last row first: a nested row is decided before the row holding it.
      for (var i = items.length - 1; i >= 0; i--) {
        var item = items[i];
        var nested = all(item, "[data-filter-item]");
        var show;
        if (!query) {
          show = true;
        } else if (nested.length) {
          show = nested.some(function (n) { return shown.indexOf(n) !== -1; });
        } else {
          show = item.getAttribute("data-filter-text").indexOf(query) !== -1;
        }
        if (show) shown.push(item);
        item.hidden = !show;
        var d = item.querySelector("details");
        if (d) d.open = query ? (nested.length ? show : rendered[i]) : rendered[i];
      }
    }

    input.addEventListener("input", apply);
  });
})();
