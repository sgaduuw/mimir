/* Filter and sort lists (/merge-window #1000, the front page #994).
 *
 * Filter a list by typing.
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
  Array.from(document.querySelectorAll("[data-filter-controls]")).forEach(function (box) {
    box.hidden = false;
  });

  Array.from(document.querySelectorAll("[data-filter-input]")).forEach(function (input) {
    var list = document.getElementById(input.getAttribute("data-filter-input"));
    if (!list) return;
    var items = Array.from(list.querySelectorAll("[data-filter-item]"));
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
        var nested = Array.from(item.querySelectorAll("[data-filter-item]"));
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

  // Sort a list by a number on each row (the front page's inbox list).
  // `data-order` is the server's order and breaks ties, so "A to Z" is just
  // that order again. No stored preference, for the reason above.
  Array.from(document.querySelectorAll("[data-sort-button]")).forEach(function (button) {
    var list = document.getElementById(button.getAttribute("data-sort-list"));
    if (!list) return;
    button.addEventListener("click", function () {
      var mode = button.getAttribute("data-sort-button");
      function number(row, name) {
        return Number(row.getAttribute(name));
      }
      Array.from(list.children)
        .sort(function (a, b) {
          var order = number(a, "data-order") - number(b, "data-order");
          return mode === "active" ? number(b, "data-active") - number(a, "data-active") || order : order;
        })
        .forEach(function (row) {
          list.appendChild(row);
        });
      Array.from(document.querySelectorAll("[data-sort-button]")).forEach(function (other) {
        other.setAttribute("aria-pressed", String(other === button));
      });
    });
  });
})();
