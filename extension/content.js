let ttsIsPaused = false;

// ---
// IN-PAGE LIVE HIGHLIGHT
// ---
// As each chunk is spoken, highlight the matching sentence in the actual page
// (the same page Read Page pulled its text from) to guide the reader's eyes.
//
// Uses the CSS Custom Highlight API (Highlight + ::highlight()), which paints
// over Ranges without mutating the page DOM, so it can never break the host
// site's layout or its scripts. If the API isn't available then highlighting is
// skipped (the overlay bar still works).
//
// Matching is fuzzy: the spoken text has been cleaned (numbers expanded,
// markers stripped and so on, so I compare normalised forms, meaning lowercased,
// only [a-z0-9 ] and with the whitespace collapsed. I find the chunk inside a
// normalised concatenation of the page's text nodes, then map the hit back to
// the real DOM
// offsets to build the Range.

const _HL_SUPPORTED =
  typeof Highlight !== "undefined" &&
  CSS && CSS.highlights &&
  typeof Range !== "undefined";

const _HL_NAME = "kam-tts-reading";
let _hlStyleInjected = false;
let _hlColour = "#f5c518";

// Cache of the page's text-node map, rebuilt lazily (and when the DOM changes
// enough that offsets would be stale). Each entry: {node, start, len} where
// start is the offset of this node's normalised text within `_hlNormJoined`.
let _hlNodeMap = null;
let _hlNormJoined = "";

function _normaliseForMatch(s) {
  return (s || "")
    .toLowerCase()
    .replace(/[^a-z0-9 ]+/g, " ")
    .replace(/\s+/g, " ")
    .trim();
}

function _hlContentRoot() {
  // Mirror Read Page's root selection so we search the same subtree it spoke.
  return (
    document.querySelector("article") ||
    document.querySelector("main") ||
    document.querySelector("[role='main']") ||
    document.querySelector(".content") ||
    document.querySelector("#content") ||
    document.body
  );
}

// True if this element is (or sits inside) a rendered equation: MathML,
// MathJax (SVG or CHTML output) or KaTeX. Equations must stay highlightable,
// so these are never skipped even though they contain svg/other markup.
function _isMathEl(el) {
  if (!el || !el.closest) return false;
  return !!el.closest(
    "math, mjx-container, .MathJax, .MathJax_SVG, .MathJax_CHTML, " +
    ".katex, .katex-display, [data-mathml], annotation"
  );
}

function _hlSkip(el) {
  if (!el || el.nodeType !== Node.ELEMENT_NODE) return false;
  const tag = el.tagName.toLowerCase();
  // Equations first: never skip math, even though MathJax renders into <svg>.
  if (_isMathEl(el)) {
    // The hidden MathML and LaTeX source duplicates the visible glyphs, so I
    // skip just the source nodes and the equation doesn't get read or
    // highlighted twice.
    if (["annotation", "annotation-xml"].includes(tag)) return true;
    if (el.closest("mjx-assistive-mml, .katex-mathml")) return true;
    return false;
  }
  if (["script","style","noscript","nav","iframe","svg","button",
       "input","select","textarea","form"].includes(tag)) return true;
  // Never highlight inside our own overlay.
  if (el.id === "tts-overlay" || el.closest && el.closest("#tts-overlay")) return true;
  const st = window.getComputedStyle(el);
  if (st.display === "none" || st.visibility === "hidden") return true;
  return false;
}

// Build (or rebuild) the normalised text map for the content root. We keep, for
// each text node, the mapping between positions in its ORIGINAL text and
// positions in the normalised joined string, so a normalised match can be
// translated back to an exact DOM Range.
function _buildHlMap() {
  const root = _hlContentRoot();
  if (!root) { _hlNodeMap = []; _hlNormJoined = ""; return; }

  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
    acceptNode(n) {
      if (!n.textContent || !n.textContent.trim()) return NodeFilter.FILTER_REJECT;
      if (_hlSkip(n.parentElement)) return NodeFilter.FILTER_REJECT;
      return NodeFilter.FILTER_ACCEPT;
    },
  });

  const map = [];
  let joined = "";
  let node;
  while ((node = walker.nextNode())) {
    // Build a per-node mapping from normalised-char index -> original index.
    const orig = node.textContent;
    const idxMap = [];           // idxMap[normPos] = origPos
    let norm = "";
    let prevSpace = joined.endsWith(" ") || joined === "";
    // Characters carrying mathematical meaning have to survive normalisation,
    // since collapsing them to spaces made equations like X_t and t+1
    // impossible to match.
    const MATH_CHARS = "_^+-=<>/*×÷−–—≤≥≠≈∑∏√∫";
    for (let i = 0; i < orig.length; i++) {
      const c = orig[i].toLowerCase();
      const isAlnum = (c >= "a" && c <= "z") || (c >= "0" && c <= "9");
      const isMath  = MATH_CHARS.indexOf(c) !== -1;
      if (isAlnum || isMath) {
        idxMap.push(i);
        norm += c;
        prevSpace = false;
      } else {
        // collapse runs of other non-alnum to a single space
        if (!prevSpace) {
          idxMap.push(i);
          norm += " ";
          prevSpace = true;
        }
      }
    }
    if (!norm) continue;
    // Ensure a separating space between nodes in the joined string.
    if (joined && !joined.endsWith(" ") && !norm.startsWith(" ")) {
      joined += " ";
    }
    map.push({ node, start: joined.length, norm, idxMap });
    joined += norm;
  }
  _hlNodeMap = map;
  _hlNormJoined = joined;
}

// Translate a [normStart, normEnd) span in _hlNormJoined into a DOM Range.
function _rangeFromNormSpan(normStart, normEnd) {
  if (!_hlNodeMap || !_hlNodeMap.length) return null;
  let startNode = null, startOff = 0, endNode = null, endOff = 0;

  for (const entry of _hlNodeMap) {
    const eStart = entry.start;
    const eEnd = entry.start + entry.norm.length;
    if (endNode && startNode) break;
    // Does this node contain the normStart?
    if (!startNode && normStart >= eStart && normStart < eEnd) {
      const local = normStart - eStart;
      startNode = entry.node;
      startOff = entry.idxMap[local] != null ? entry.idxMap[local] : 0;
    }
    // Does this node contain the (inclusive) end char?
    const lastChar = normEnd - 1;
    if (lastChar >= eStart && lastChar < eEnd) {
      const local = lastChar - eStart;
      endNode = entry.node;
      // +1 so the range includes the final character
      const oi = entry.idxMap[local] != null ? entry.idxMap[local] : entry.node.textContent.length - 1;
      endOff = oi + 1;
    }
  }
  if (!startNode || !endNode) return null;
  try {
    const r = document.createRange();
    r.setStart(startNode, startOff);
    r.setEnd(endNode, endOff);
    return r;
  } catch (_) {
    return null;
  }
}

function _ensureHlStyle() {
  if (_hlStyleInjected) return;
  const style = document.createElement("style");
  style.id = "kam-tts-hl-style";
  style.textContent =
    `::highlight(${_HL_NAME}){background:${_hlColour};color:#000;}`;
  document.head.appendChild(style);
  _hlStyleInjected = true;
}

function _setHlColour(colour) {
  _hlColour = colour || _hlColour;
  const style = document.getElementById("kam-tts-hl-style");
  if (style) {
    style.textContent =
      `::highlight(${_HL_NAME}){background:${_hlColour};color:#000;}`;
  }
}

function highlightChunkInPage(rawText) {
  if (!_HL_SUPPORTED || !_hlEnabled) return;
  const target = _normaliseForMatch(_stripMarkers(rawText));
  // Match failures below deliberately KEEP the previous highlight rather than
  // clearing: a bar that lags one chunk is far less jarring than one that
  // blinks out whenever server-side rewriting defeats the matcher. Explicit
  // stop (clearOverlay) and the toggle are the only things that clear.
  if (target.length < 4) return;

  // (Re)build the map if missing. Cheap enough to rebuild per chunk on most
  // study pages; rebuild guarantees offsets are valid even if the page mutated.
  _buildHlMap();
  if (!_hlNormJoined) return;

  let pos = _hlNormJoined.indexOf(target);

  // If the exact normalised chunk isn't found (server-side cleaning changed the
  // spoken text through pronunciation rules, LaTeX expansion and punctuation
  // fixes), I anchor the start with the first words and the end with the last,
  // and
  // span between them. Highlighting only the head probe left most of the chunk
  // unpainted, which read as "highlight sometimes doesn't cover the chunk".
  if (pos === -1) {
    const words = target.split(" ").filter(Boolean);
    if (words.length >= 3) {
      // START anchor: try the first 8 words, then shrink (6/4/3), then slide
      // forward up to 3 words, so an opening the server rewrote still anchors.
      let headPos = -1, headLen = 0, headStartWord = 0;
      outer:
      for (let skip = 0; skip <= Math.min(3, words.length - 3); skip++) {
        for (const n of [8, 6, 4, 3]) {
          if (skip + n > words.length) continue;
          const head = words.slice(skip, skip + n).join(" ");
          const p = _hlNormJoined.indexOf(head);
          if (p !== -1) { headPos = p; headLen = head.length; headStartWord = skip; break outer; }
        }
      }
      if (headPos !== -1 && headStartWord > 0) {
        // The head only matched after sliding past rewritten opening words, so I
        // pull the start back by their length and those words get painted too.
        const skippedLen = words.slice(0, headStartWord).join(" ").length + 1;
        headPos = Math.max(0, headPos - skippedLen);
        headLen += skippedLen;
      }
      if (headPos !== -1) {
        // END anchor: try tails of decreasing length, searched after the head
        // so we find THIS chunk's tail, not a later repetition.
        let endPos = -1;
        for (const n of [6, 5, 4, 3, 2]) {
          if (words.length - headStartWord <= n) continue;
          const tail = words.slice(-n).join(" ");
          const tp = _hlNormJoined.indexOf(tail, headPos + headLen);
          // Sanity bound: a bogus match must not paint half the page.
          if (tp !== -1 && tp + tail.length - headPos <= target.length * 2) {
            endPos = tp + tail.length;
            break;
          }
        }
        // No trustworthy tail (server rewrote the ending) → estimate the span
        // from the chunk's own length so the WHOLE chunk is painted, not just
        // the head words. Slight overshoot is clipped to the page text.
        if (endPos === -1) {
          endPos = Math.min(headPos + target.length, _hlNormJoined.length);
        }
        const range = _rangeFromNormSpan(headPos, endPos);
        _paintRange(range);
        return;
      }
      // The head anchor failed, meaning the chunk's opening words were rewritten
      // by numbers, pronunciation or maths, or they straddle skipped elements.
      // So anchor from any
      // 4-word window inside the chunk instead, and back-calculate the chunk's
      // start from that window's offset within the chunk, so the WHOLE chunk
      // paints, rather than just the part after wherever a head would match.
      for (let w = 1; w + 4 <= words.length; w += 2) {
        const win = words.slice(w, w + 4).join(" ");
        const p = _hlNormJoined.indexOf(win);
        if (p !== -1) {
          const offsetInChunk = words.slice(0, w).join(" ").length + 1;
          const start = Math.max(0, p - offsetInChunk);   // approximate chunk start
          const end = Math.min(start + target.length, _hlNormJoined.length);
          const range = _rangeFromNormSpan(start, end);
          _paintRange(range);
          return;
        }
      }
    }
    return;   // no anchor found — keep the previous highlight (see note above)
  }

  const range = _rangeFromNormSpan(pos, pos + target.length);
  _paintRange(range);
}

function _paintRange(range) {
  if (!range) return;   // keep the previous highlight rather than blinking out
  _ensureHlStyle();
  try {
    const hl = new Highlight(range);
    CSS.highlights.set(_HL_NAME, hl);
  } catch (_) { /* keep previous highlight */ }
}

function clearPageHighlight() {
  if (_HL_SUPPORTED && CSS.highlights) CSS.highlights.delete(_HL_NAME);
}

// --- Overlay appearance and live-configurable from the popup ---
// Keys are stored in chrome.storage.local so the popup can update them
// without any message passing. A storage listener below reapplies the style
// to the currently-visible overlay whenever the user drags a slider.
const OVERLAY_DEFAULTS = {
  overlayLineColour: '#f5c518',   // highlight + bar accent colour
  overlayBarColour:  '#0f0f0f',   // header bar background colour
  overlayTextSize:   12,           // px, for the chunk text line
  highlightEnabled:  true,         // in-page chunk highlighting on/off
  overlayEnabled:    true,         // the header bar itself on/off
};

// Live mirror of the highlight-enabled flag so the paint path can check it
// synchronously. Kept in sync via the storage listener below.
let _hlEnabled = true;
let _overlayEnabled = true;        // header bar shown during playback?
let _overlayCreating = false;      // sync guard against duplicate header creation
let _lastOverlay = null;           // {text,current,total} of latest chunk, for re-show

// True only in the page's top frame. This script runs in every frame so the
// highlighting can reach text inside iframes, but anything that draws chrome on
// the page has to be top-frame only or you get one copy per frame. Accessing
// window.top across origins can throw, and a throw means we are definitely in a
// cross-origin sub-frame, so that case answers false.
function _isTopFrame() {
  try { return window.self === window.top; }
  catch (_) { return false; }
}
try {
  chrome.storage.local.get({ highlightEnabled: true, overlayEnabled: true }, c => {
    _hlEnabled = c.highlightEnabled !== false;
    _overlayEnabled = c.overlayEnabled !== false;
  });
} catch (_) { /* storage unavailable on some pages — default on */ }

function _getOverlayCfg(cb) {
  // chrome.storage.local.get takes an object of defaults, so any key that's
  // missing falls back to the default given here.
  chrome.storage.local.get(OVERLAY_DEFAULTS, cb);
}

chrome.runtime.onMessage.addListener((request, sender, sendResponse) => {
  if (request.action === "updateOverlay") {
    showOverlay(request.text, request.current, request.total);
    highlightChunkInPage(request.text);   // paint the spoken chunk in the page
    sendResponse({ status: "ok" });
    return false;
  }
  if (request.action === "clearOverlay") {
    removeOverlay();
    clearPageHighlight();
    _lastOverlay = null;
    sendResponse({ status: "ok" });
    return false;
  }
  if (request.action === "pausedPlaying") {
    ttsIsPaused = true;
    const btn = document.getElementById("tts-pause-btn");
    if (btn) { btn.textContent = "▶"; btn.style.background = "#22c55e"; btn.style.color = "white"; }
    sendResponse({ status: "ok" });
    return false;
  }
  if (request.action === "resumedPlaying") {
    ttsIsPaused = false;
    _getOverlayCfg(cfg => {
      const btn = document.getElementById("tts-pause-btn");
      if (btn) { btn.textContent = "⏸"; btn.style.background = cfg.overlayLineColour; btn.style.color = "#000"; }
    });
    sendResponse({ status: "ok" });
    return false;
  }
  return false;
});

// Live reapply, so whenever the popup changes overlayLineColour or
// overlayTextSize the overlay on screen updates in place with no refresh.
chrome.storage.onChanged.addListener((changes, area) => {
  if (area !== 'local') return;

  // Highlight on/off toggled (from the overlay button OR the popup button).
  if (changes.highlightEnabled) {
    _hlEnabled = changes.highlightEnabled.newValue !== false;
    _syncHlToggleButton();
    if (_hlEnabled) {
      const t = document.getElementById("tts-text-row");
      const src = (t && t.textContent) || (_lastOverlay && _lastOverlay.text);
      if (src) highlightChunkInPage(src);
    } else {
      clearPageHighlight();
    }
  }

  // Header bar on/off toggled (from the popup button).
  if (changes.overlayEnabled) {
    _overlayEnabled = changes.overlayEnabled.newValue !== false;
    if (_overlayEnabled) {
      // Re-show the bar with the latest chunk if we have one.
      if (_lastOverlay) showOverlay(_lastOverlay.text, _lastOverlay.current, _lastOverlay.total);
    } else {
      _removeOverlayBarOnly();   // hide bar, keep highlight intact
    }
  }

  if (changes.overlayLineColour) _setHlColour(changes.overlayLineColour.newValue);

  if (!(changes.overlayLineColour || changes.overlayBarColour || changes.overlayTextSize)) return;
  const overlay = document.getElementById("tts-overlay");
  if (!overlay) return;
  _getOverlayCfg(cfg => _applyOverlayStyle(overlay, cfg));
});

// Toggle the in-page highlight on/off and persist it so the popup button and
// the overlay button always agree. Single source of truth = storage; both
// entry points just flip this value and react via the listener above.
function toggleHighlight() {
  chrome.storage.local.set({ highlightEnabled: !_hlEnabled });
}

// Reflect current highlight state on the overlay toggle button, if present.
function _syncHlToggleButton() {
  const btn = document.getElementById("tts-hl-btn");
  if (!btn) return;
  if (_hlEnabled) {
    btn.style.background = _hlColour;
    btn.style.color = "#000";
    btn.style.opacity = "1";
    btn.title = "Highlight ON — click to turn off";
  } else {
    btn.style.background = "#2a2a2a";
    btn.style.color = "#bbb";
    btn.style.opacity = "0.85";
    btn.title = "Highlight OFF — click to turn on";
  }
}

// Strip any semantic markers that arrive in the text before displaying.
// This is a local defence. background.js should have stripped the markers
// already, but content.js protects itself in case of a version mismatch or
// something cached.
const _MARKERS = {
  'H1':1,'/H1':1,'H2':1,'/H2':1,'H3':1,'/H3':1,
  'BOLD':1,'/BOLD':1,'ITALIC':1,'/ITALIC':1,
  'CODE':1,'/CODE':1,'CALLOUT':1,'/CALLOUT':1,
  'CAPTION':1,'/CAPTION':1,'BREAK':1
};
function _stripMarkers(text) {
  return text.split('|')
    .filter(p => p.trim().length > 0 && !_MARKERS[p.trim()])
    .join('')
    .replace(/ {2,}/g, ' ')
    .trim();
}

function _applyOverlayStyle(overlay, cfg) {
  // Keep the in-page highlight colour in sync with the overlay accent.
  _setHlColour(cfg.overlayLineColour);

  // Bar background per theme/custom colour. Choose readable text colours based
  // on the background's luminance so a light bar (e.g. Light theme white) shows
  // dark text and a dark bar shows light text.
  const barBg = cfg.overlayBarColour || "#0f0f0f";
  const dark  = _isDarkColour(barBg);
  overlay.style.background = _hexToRgba(barBg, 0.97);
  overlay.style.color = dark ? "#f0f0f0" : "#1a1a1a";

  // Border and progress accent pick up the highlight colour.
  overlay.style.borderBottom = "2px solid " + cfg.overlayLineColour;

  const textRow = overlay.querySelector("#tts-text-row");
  if (textRow) {
    textRow.style.fontSize = cfg.overlayTextSize + "px";
    textRow.style.color = dark ? "#aaa" : "#444";   // dimmed, readable either way
  }

  const progress = overlay.querySelector("#tts-progress");
  if (progress) progress.style.color = cfg.overlayLineColour;

  // Pause button uses the line colour when active.
  const btnPause = overlay.querySelector("#tts-pause-btn");
  if (btnPause && !ttsIsPaused) {
    btnPause.style.background = cfg.overlayLineColour;
    btnPause.style.color = "#000";
  }

  // Re-seat the page margin in case bar height changed (larger text → taller bar).
  document.body.style.marginTop = (overlay.offsetHeight + 4) + "px";
}

// Luminance test so we can pick contrasting text. Returns true if the colour is
// dark enough to need light text on top.
function _isDarkColour(hex) {
  const c = hex.replace("#", "");
  if (c.length < 6) return true;
  const r = parseInt(c.slice(0,2),16), g = parseInt(c.slice(2,4),16), b = parseInt(c.slice(4,6),16);
  // Rec. 601 luma
  return (0.299*r + 0.587*g + 0.114*b) < 140;
}

function _hexToRgba(hex, a) {
  const c = hex.replace("#", "");
  if (c.length < 6) return hex;
  const r = parseInt(c.slice(0,2),16), g = parseInt(c.slice(2,4),16), b = parseInt(c.slice(4,6),16);
  return `rgba(${r},${g},${b},${a})`;
}

function showOverlay(text, current, total) {
  text = _stripMarkers(text);

  // Remember the latest chunk so the bar can be re-shown if the user toggles
  // it back on mid-playback.
  _lastOverlay = { text, current, total };

  // Only the top frame gets a header bar.
  //
  // The manifest runs this script with all_frames:true, which it has to,
  // because plenty of sites keep the real article text inside an iframe and the
  // highlighting needs to reach it. But every frame was then building its own
  // bar, so an embedded video or widget got a second control bar floating
  // inside it, overlapping the content. Highlighting still runs in sub-frames
  // and is unaffected, since it's decoupled from the bar.
  if (!_isTopFrame()) return;

  // The header bar is disabled so I don't create or update it, though the
  // highlighting still runs.
  if (!_overlayEnabled) { removeOverlay(); return; }

  // Update in place to avoid flicker.
  const existing = document.getElementById("tts-overlay");
  if (existing) {
    const t = document.getElementById("tts-text-row");
    const p = document.getElementById("tts-progress");
    if (t) t.textContent = text;
    if (p) p.textContent = `${current} / ${total}`;
    return;
  }

  // Guard against a race: _getOverlayCfg is async, so two fast chunks could both
  // pass the check above and each create a header. A synchronous flag ensures
  // only the first proceeds to build one.
  if (_overlayCreating) return;
  _overlayCreating = true;

  ttsIsPaused = false;

  _getOverlayCfg(cfg => {
    // Re-check inside the async callback: if the overlay was built meanwhile,
    // abort so we never end up with two headers.
    if (document.getElementById("tts-overlay")) { _overlayCreating = false; return; }
    const overlay = document.createElement("div");
    overlay.id = "tts-overlay";
    overlay.style.cssText =
      "position:fixed;top:0;left:0;right:0;" +
      "background:rgba(15,15,15,0.97);color:#f0f0f0;" +
      "font-family:'Segoe UI',sans-serif;z-index:2147483647;" +
      "box-shadow:0 4px 20px rgba(0,0,0,0.5)";

    const row = document.createElement("div");
    row.style.cssText = "display:flex;align-items:center;justify-content:center;gap:12px;padding:8px 20px";

    const btnPause = _makeBtn("⏸", () => {
      chrome.runtime.sendMessage({ action: "pauseResume" }, (res) => {
        if (chrome.runtime.lastError) return;
        if (res) {
          ttsIsPaused = res.isPaused;
          btnPause.textContent = res.isPaused ? "▶" : "⏸";
          if (res.isPaused) {
            btnPause.style.background = "#22c55e";
            btnPause.style.color = "white";
          } else {
            btnPause.style.background = cfg.overlayLineColour;
            btnPause.style.color = "#000";
          }
        }
      });
    }, cfg.overlayLineColour);
    btnPause.id = "tts-pause-btn";

    const btnStop = _makeBtn("⏹", () => {
      chrome.runtime.sendMessage({ action: "stop" }).catch(() => {});
      removeOverlay();
    }, "#ef4444");

    const progress = document.createElement("span");
    progress.id = "tts-progress";
    progress.style.cssText =
      "font-size:12px;font-weight:600;min-width:55px;text-align:center;" +
      "color:" + cfg.overlayLineColour;
    progress.textContent = `${current} / ${total}`;

    // The highlight on/off toggle. One click, instant, and the state is shared
    // with the popup.
    const btnHl = document.createElement("button");
    btnHl.id = "tts-hl-btn";
    btnHl.textContent = "✏️ Highlight";
    btnHl.style.cssText =
      "border:none;border-radius:8px;padding:7px 12px;font-size:12px;" +
      "font-weight:600;cursor:pointer;white-space:nowrap";
    btnHl.addEventListener("click", toggleHighlight);

    row.appendChild(btnPause);
    row.appendChild(btnStop);
    row.appendChild(progress);
    row.appendChild(btnHl);

    const textRow = document.createElement("div");
    textRow.id = "tts-text-row";
    textRow.style.cssText =
      "padding:2px 20px 8px;color:#999;text-align:center;" +
      "white-space:nowrap;overflow:hidden;text-overflow:ellipsis;" +
      "font-size:" + cfg.overlayTextSize + "px";
    textRow.textContent = text;

    overlay.appendChild(row);
    overlay.appendChild(textRow);
    _applyOverlayStyle(overlay, cfg);  // sets border-bottom using cfg
    document.body.appendChild(overlay);
    // Belt-and-braces: if any duplicate slipped in, keep only the last one.
    const _all = document.querySelectorAll("#tts-overlay");
    for (let i = 0; i < _all.length - 1; i++) _all[i].remove();
    document.body.style.marginTop = (overlay.offsetHeight + 4) + "px";
    _hlEnabled = cfg.highlightEnabled !== false;
    _syncHlToggleButton();
    _wireFullscreenHide();   // header must not clip over fullscreen video
    _overlayCreating = false;
  });
}

// When any element enters fullscreen (typically a page video), hide the reader
// header + release the body margin so nothing overlaps the fullscreen content.
// Restore both on exit. It's idempotent since the listener attaches only once.
let _fsWired = false;
let _fsSavedMargin = "";
function _wireFullscreenHide() {
  if (_fsWired) return;
  _fsWired = true;
  const onFs = () => {
    const fs = document.fullscreenElement || document.webkitFullscreenElement;
    const overlay = document.getElementById("tts-overlay");
    if (!overlay) return;
    if (fs) {
      _fsSavedMargin = document.body.style.marginTop;
      overlay.style.display = "none";
      document.body.style.marginTop = "";        // let fullscreen use full height
    } else {
      overlay.style.display = "";
      document.body.style.marginTop = _fsSavedMargin || "";
    }
  };
  document.addEventListener("fullscreenchange", onFs);
  document.addEventListener("webkitfullscreenchange", onFs);
}

function _makeBtn(label, onClick, bg) {
  const btn = document.createElement("button");
  btn.textContent = label;
  // Light yellow-ish buttons get black text for contrast; everything else white.
  const isLight = bg === "#f5c518";
  btn.style.cssText =
    "background:" + bg + ";" +
    "color:" + (isLight ? "#000" : "white") + ";" +
    "border:none;border-radius:8px;padding:7px 13px;font-size:15px;cursor:pointer";
  btn.addEventListener("click", onClick);
  return btn;
}

function removeOverlay() {
  document.querySelectorAll("#tts-overlay").forEach(el => el.remove());
  document.body.style.marginTop = "";
  _overlayCreating = false;
  clearPageHighlight();
}

// Remove just the header bar WITHOUT clearing the in-page highlight. Used when
// the bar gets toggled off mid-playback, since the highlight should keep
// following the audio whether or not the bar is visible.
function _removeOverlayBarOnly() {
  const el = document.getElementById("tts-overlay");
  if (el) el.remove();
  document.body.style.marginTop = "";
}
