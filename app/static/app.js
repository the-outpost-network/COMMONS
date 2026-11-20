// Small progressive enhancements. Everything works without this file.

// Account menu: a <details>, so it opens and closes without JavaScript.
// With it, a click outside the menu or Escape closes it as well.
(function () {
  "use strict";
  function openMenus() {
    return document.querySelectorAll("details.acct-wrap[open]");
  }
  document.addEventListener("click", function (e) {
    openMenus().forEach(function (d) {
      if (!d.contains(e.target)) d.open = false;
    });
  });
  document.addEventListener("keydown", function (e) {
    if (e.key !== "Escape") return;
    openMenus().forEach(function (d) {
      var hadFocus = d.contains(document.activeElement);
      d.open = false;
      if (hadFocus) d.querySelector("summary").focus();
    });
  });
})();

// One-time links and codes: a Copy button. The buttons are hidden in the HTML,
// so without this file the link is still there to select and copy by hand.
(function () {
  "use strict";
  function selectText(el) {
    var range = document.createRange();
    range.selectNodeContents(el);
    var sel = window.getSelection();
    sel.removeAllRanges();
    sel.addRange(range);
  }
  document.querySelectorAll("button[data-copy]").forEach(function (btn) {
    var target = document.getElementById(btn.getAttribute("data-copy"));
    if (!target) return;
    var label = btn.textContent;
    btn.hidden = false;
    btn.addEventListener("click", function () {
      var text = target.textContent.trim();
      var done = function () {
        btn.textContent = btn.getAttribute("data-copied") || label;
        setTimeout(function () { btn.textContent = label; }, 2000);
      };
      if (navigator.clipboard && window.isSecureContext) {
        navigator.clipboard.writeText(text).then(done, function () { selectText(target); });
      } else {
        selectText(target);
      }
    });
  });
})();
