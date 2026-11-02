/* Apply the saved preference before styles paint; no inline script is needed. */
(function () {
  var html = document.documentElement;
  var preference = "system";

  function apply(value) {
    preference = value === "light" || value === "dark" ? value : "system";
    if (preference === "system") html.removeAttribute("data-theme");
    else html.setAttribute("data-theme", preference);
  }

  function restorePreference() {
    try {
      apply(localStorage.getItem("mimir.theme"));
    } catch (e) {
      /* Keep the current page preference when storage is unavailable. */
    }
  }
  restorePreference();

  function init() {
    var select = document.getElementById("theme-select");
    if (!select) return;
    select.value = preference;
    document.getElementById("theme-control").hidden = false;
    select.onchange = function () {
      apply(select.value);
      try {
        if (preference === "system") localStorage.removeItem("mimir.theme");
        else localStorage.setItem("mimir.theme", preference);
      } catch (e) {
        /* Switching still works for this page when storage is blocked. */
      }
    };
  }

  document.addEventListener("DOMContentLoaded", init);
  document.addEventListener("htmx:historyRestore", init);
  window.addEventListener("pageshow", function (event) {
    if (event.persisted) {
      restorePreference();
      init();
    }
  });
})();
