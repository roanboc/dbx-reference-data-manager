/* Drag the right edge of the sidebar to resize it; double-click the edge to reset.
   The width is remembered per browser (localStorage). Mantine's AppShell sizes both the
   navbar and the main area from the --app-shell-navbar-width variable on its root element. */
(function () {
  var KEY = "rdm.navbarWidth", MIN = 240, MAX = 640, DEFAULT = 330;
  function root() { return document.querySelector(".mantine-AppShell-root"); }
  function narrow() { return window.matchMedia("(max-width: 48em)").matches; }  /* AppShell breakpoint "sm" */
  function apply(w) {
    var r = root();
    if (!r) { return; }
    if (narrow()) {  /* the navbar is collapsed on small screens: leave Mantine's own variables alone */
      r.style.removeProperty("--app-shell-navbar-width"); r.style.removeProperty("--app-shell-navbar-offset"); return;
    }
    /* the main area is padded by the offset variable, the navbar sized by the width variable */
    r.style.setProperty("--app-shell-navbar-width", w + "px");
    r.style.setProperty("--app-shell-navbar-offset", w + "px");
  }
  function clamp(w) { return Math.min(MAX, Math.max(MIN, w)); }
  function stored() {
    try { var v = parseInt(localStorage.getItem(KEY), 10); return v >= MIN && v <= MAX ? v : null; } catch (e) { return null; }
  }
  function init() {
    var handle = document.getElementById("nav-resizer");
    if (!handle || handle.dataset.ready) { return; }
    handle.dataset.ready = "1";
    var saved = stored();
    if (saved) { apply(saved); }
    var dragging = false;
    handle.addEventListener("mousedown", function (e) {
      dragging = true; document.body.style.cursor = "col-resize"; document.body.style.userSelect = "none"; e.preventDefault();
    });
    window.addEventListener("mousemove", function (e) { if (dragging) { apply(clamp(e.clientX)); } });
    window.addEventListener("mouseup", function (e) {
      if (!dragging) { return; }
      dragging = false; document.body.style.cursor = ""; document.body.style.userSelect = "";
      var w = clamp(e.clientX); apply(w);
      try { localStorage.setItem(KEY, String(w)); } catch (err) { /* private mode */ }
    });
    handle.addEventListener("dblclick", function () {
      apply(DEFAULT);
      try { localStorage.removeItem(KEY); } catch (err) { /* ignore */ }
    });
    window.addEventListener("resize", function () { apply(stored() || DEFAULT); });
  }
  new MutationObserver(init).observe(document.documentElement, { childList: true, subtree: true });
  document.addEventListener("DOMContentLoaded", init);
})();
