/* Colour scheme: System (default) / Light / Dark, chosen with the control in the header.
   Mantine paints from the data-mantine-color-scheme attribute (the MantineProvider owns it when
   a scheme is forced), AG Grid from data-ag-theme-mode. "System" follows the operating system
   and falls back to light when the browser cannot tell. The choice is remembered per browser by
   Dash (persistence on the control); a copy is kept here so the first frame is already right. */
(function () {
  var KEY = "rdm.colorScheme";
  var root = document.documentElement;
  var media = window.matchMedia ? window.matchMedia("(prefers-color-scheme: dark)") : null;
  var mode = "auto";

  function normalise(value) { return value === "light" || value === "dark" ? value : "auto"; }
  function resolve() { return mode === "auto" ? (media && media.matches ? "dark" : "light") : mode; }
  function paint() {
    var scheme = resolve();
    root.setAttribute("data-ag-theme-mode", scheme);
    if (mode === "auto") { root.setAttribute("data-mantine-color-scheme", scheme); }
  }

  try { mode = normalise(localStorage.getItem(KEY)); } catch (e) { /* storage unavailable */ }
  paint();
  if (media && media.addEventListener) { media.addEventListener("change", paint); }

  window.dash_clientside = Object.assign({}, window.dash_clientside, {
    rdm: {
      /* Clientside callback: control value -> MantineProvider.forceColorScheme (null = follow the system). */
      applyColorScheme: function (value) {
        mode = normalise(value);
        try { localStorage.setItem(KEY, mode); } catch (e) { /* ignore */ }
        paint();
        return mode === "auto" ? null : mode;
      }
    }
  });
})();
