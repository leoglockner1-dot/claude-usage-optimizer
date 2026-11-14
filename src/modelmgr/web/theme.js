"use strict";
// runs in <head> so the page never flashes the wrong theme; dark is the default
(() => {
  let t = "dark";
  try { t = localStorage.getItem("uo-theme") || "dark"; } catch { /* storage blocked: keep the default */ }
  document.documentElement.dataset.theme = t === "light" ? "light" : "dark";
})();
