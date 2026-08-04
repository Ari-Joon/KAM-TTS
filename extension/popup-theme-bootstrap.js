// Applies the saved theme before the first paint so there's no theme flash.
// It has to be an external file since MV3's Content Security Policy blocks
// inline <script>.
try {
  document.body.setAttribute("data-theme", "arian"); // placeholder until callback
  chrome.storage.local.get({ popupTheme: "arian" }, d => {
    document.body.setAttribute("data-theme", d.popupTheme || "arian");
  });
} catch (e) {
  document.body.setAttribute("data-theme", "arian");
}