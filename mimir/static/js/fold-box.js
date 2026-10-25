/* Per-box fold toggle for /merge-window (#881).
 *
 * Each .fold-box carries its own data-fold (closed / partial /
 * expanded), unlike thread-fold.js, which keeps one page-wide state on
 * <html> for the single thread tree. Deferred: the server renders every
 * box as partial, so there is no state to restore before paint. Lives
 * in /static/ because the CSP blocks inline scripts.
 */
document.querySelectorAll(".fold-box").forEach(function (box) {
  var buttons = box.querySelectorAll(".thread-toggle button");

  function setFold(mode) {
    box.setAttribute("data-fold", mode);
    buttons.forEach(function (b) {
      b.setAttribute("aria-pressed", String(b.getAttribute("data-fold-set") === mode));
    });
  }

  buttons.forEach(function (b) {
    b.addEventListener("click", function () {
      setFold(b.getAttribute("data-fold-set"));
    });
  });

  /* As on the thread view: a closed box opens to partial on a click
   * anywhere on its toolbar, which CSS marks with cursor: pointer. */
  box.querySelector(".thread-toolbar").addEventListener("click", function (e) {
    if (!e.target.closest("button") && box.getAttribute("data-fold") === "closed") {
      setFold("partial");
    }
  });
});
