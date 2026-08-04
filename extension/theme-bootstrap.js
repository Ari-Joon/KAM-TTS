// =============================================================================
// Theme bootstrap.
// This loads synchronously at the top of <body> so the data-theme attribute is
// set before anything visible renders, which gets rid of the theme flash you'd
// otherwise get waiting for dashboard.js at the end of the body to apply it.
// MV3 doesn't allow inline <script> blocks in extension HTML, so this has to be
// a separate file pulled in with <script src="..."></script>.
// =============================================================================

(function applyTheme() {
  // Default to arian, the KAM brand theme, until the storage lookup comes back.
  document.body.setAttribute('data-theme', 'arian');

  // Apply any per-theme colour overrides before paint. localStorage is
  // synchronous so this runs inline with no flash, and I only use the async
  // chrome.storage for the theme name. The override values themselves live in
  // localStorage keyed by theme, written by the customiser in dashboard.js.
  function applyOverrides(theme) {
    try {
      var ov = JSON.parse(localStorage.getItem('custColours:' + theme) || '{}');
      var allowed = ['--bg','--bg2','--bg3','--border','--text','--subtext','--dim','--indigo'];
      allowed.forEach(function (v) { document.body.style.removeProperty(v); });
      Object.keys(ov).forEach(function (k) {
        if (allowed.indexOf(k) !== -1 && ov[k]) document.body.style.setProperty(k, ov[k]);
      });
    } catch (e) { /* ignore */ }
  }
  applyOverrides('arian');

  try {
    chrome.storage.local.get({ playerTheme: 'arian' }, function (d) {
      var t = d.playerTheme || 'arian';
      document.body.setAttribute('data-theme', t);
      applyOverrides(t);
    });
  } catch (e) {
    // chrome.storage isn't there, which happens if it's opened as file://, so
    // I keep the default.
  }
})();