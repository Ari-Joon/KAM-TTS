// The whole script sits in one function so it can be injected more than once.
// The worker now injects it into tabs that were open before an install or a
// reload, and those tabs may also get the manifest copy, or may still hold the
// copy from before a reload. Top-level let and const would then throw "already
// declared", and two live copies would draw two header bars. The body is not
// re-indented, since that would touch every line for no change in behaviour.
(() => {
// A copy stays in charge only while its own runtime is alive. After a reload
// the old copy's runtime is gone, its listeners hear nothing, and it is left
// behind as an orphan, so it has to give way rather than block the new one. I
// keep the runtime object from load time, since the global could be replaced.
const _kamRt = (typeof chrome !== "undefined" && chrome.runtime) || null;
const _kamPrev = globalThis.__kamTtsContent;
if (_kamPrev && typeof _kamPrev.alive === "function" && _kamPrev.alive()) return;
globalThis.__kamTtsContent = {
  alive: () => { try { return !!(_kamRt && _kamRt.id); } catch (e) { return false; } },
};

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
// markers stripped and so on), so I compare normalised forms, meaning
// lowercased, accents folded, only [a-z0-9 ] and with the whitespace collapsed.
// I find the chunk inside a normalised concatenation of the page's text nodes,
// then map the hit back to the real DOM offsets to build the Range.

const _HL_SUPPORTED =
  typeof Highlight !== "undefined" &&
  CSS && CSS.highlights &&
  typeof Range !== "undefined";

const _HL_NAME = "kam-tts-reading";
let _hlStyleInjected = false;
let _hlColour = "#f5c518";

// Cache of the page's text-node map. Each entry: {node, start, norm, idxMap}
// where start is the offset of this node's normalised text within
// `_hlNormJoined`. It is built once and kept for the read. It used to be
// rebuilt for every chunk, with a getComputedStyle for every text node on the
// page each time, which on a long article is thousands of style reads every
// few seconds for a page that had not changed. A MutationObserver marks it
// stale when the content does change, and it is rebuilt anyway after
// _HL_MAP_MAX_AGE, which covers what the observer cannot see (a stylesheet
// hiding or showing text without touching the nodes).
let _hlNodeMap = null;
let _hlNormJoined = "";
let _hlMapRoot = null;
let _hlMapAt = 0;
let _hlMapStale = true;
let _hlObserver = null;
const _HL_MAP_MAX_AGE = 120000;

// Where the last chunk was found, kept as a place in the DOM rather than an
// offset into the joined text so that it survives a rebuild of the map.
// _hlAfter is where the next search starts, _hlFrom is where the last one
// started (for painting the same chunk again), _hlNo is that chunk's number.
let _hlAfter = null;
let _hlFrom = null;
let _hlNo = 0;

// Letters NFKD leaves alone, since they are letters in their own right rather
// than a base letter plus a mark. The same table textnorm.js uses.
const _HL_LETTERS = { "ß": "ss", "æ": "ae", "œ": "oe", "ø": "o", "đ": "d", "ł": "l",
                      "þ": "th", "ð": "d", "ı": "i" };

// One character, folded the way the spoken chunk was. A letter or digit comes
// back as its plain ASCII form ("é" as "e", "ﬁ" as "fi"), a combining mark as
// "" (it belongs to the letter before it, so it is not a break in the word),
// and anything else as " ", a separator.
//
// The chunk I am matching has already been through kamSpeakable and the ASCII
// strip, so "café" arrives here as "cafe". A content script cannot
// importScripts textnorm.js, so this is the part of it that matters for
// matching: the accent fold. Symbols it turns into words ("£" into "pounds")
// are not copied; a chunk with one of those falls to the fuzzy anchors below.
function _hlFold(ch) {
  let c = ch.toLowerCase();
  if (c.length === 1 && c < "\u0080") {
    return ((c >= "a" && c <= "z") || (c >= "0" && c <= "9")) ? c : " ";
  }
  if (c >= "̀" && c <= "ͯ") return "";
  if (_HL_LETTERS[c]) return _HL_LETTERS[c];
  try { c = c.normalize("NFKD"); } catch (e) { return " "; }
  let out = "";
  for (let i = 0; i < c.length; i++) {
    const k = c[i];
    if ((k >= "a" && k <= "z") || (k >= "0" && k <= "9")) out += k;
  }
  return out || " ";
}

// Normalise a string and keep, for every character of the result, the index of
// the character it came from. Runs of separators collapse to one space.
// prevSpace says whether the text this follows already ended in a space.
//
// This is the ONE normalisation, used for the page text and for the spoken
// chunk alike. There used to be two: the chunk was reduced to [a-z0-9 ] while
// the page map kept - – — / = + _ ^ * < >, so "well-known" on the page stayed
// "well-known" and the chunk became "well known". Any chunk with a hyphen, a
// dash or a slash in it could not match exactly and fell to a fuzzy guess.
function _hlNormalise(orig, prevSpace) {
  const idxMap = [];           // idxMap[normPos] = origPos
  let norm = "";
  for (let i = 0; i < orig.length; i++) {
    const f = _hlFold(orig[i]);
    if (f === "") continue;
    if (f === " ") {
      if (!prevSpace) { idxMap.push(i); norm += " "; prevSpace = true; }
      continue;
    }
    for (let k = 0; k < f.length; k++) idxMap.push(i);
    norm += f;
    prevSpace = false;
  }
  return { norm, idxMap };
}

function _normaliseForMatch(s) {
  return _hlNormalise(s || "", true).norm.trim();
}

// Find needle at or after `from`, and only if it is not there, anywhere.
function _hlFindFrom(hay, needle, from) {
  const p = from > 0 ? hay.indexOf(needle, from) : -1;
  return p !== -1 ? p : hay.indexOf(needle);
}

// Where a normalised chunk sits in the normalised page text. Returns
// {start, end, next} or null: the span to paint, and where the search for the
// following chunk should begin.
//
// Every search used to start at the top of the page, so a phrase that occurs
// twice (a heading repeated in the body, a refrain, "See also") was painted at
// its first occurrence however far down the reading had got. Each search now
// looks forward from the end of the previous match first and falls back to
// the whole page only when nothing lies ahead.
function _hlLocate(joined, target, from) {
  const find = needle => _hlFindFrom(joined, needle, from);

  const pos = find(target);
  if (pos !== -1) return { start: pos, end: pos + target.length, next: pos + target.length };

  // If the exact normalised chunk isn't found (server-side cleaning changed the
  // spoken text through pronunciation rules, LaTeX expansion and punctuation
  // fixes), I anchor the start with the first words and the end with the last,
  // and span between them. Highlighting only the head probe left most of the
  // chunk unpainted, which read as "highlight sometimes doesn't cover the
  // chunk".
  const words = target.split(" ").filter(Boolean);
  if (words.length < 3) return null;

  // START anchor: try the first 8 words, then shrink (6/4/3), then slide
  // forward up to 3 words, so an opening the server rewrote still anchors.
  let headPos = -1, headLen = 0, headStartWord = 0;
  outer:
  for (let skip = 0; skip <= Math.min(3, words.length - 3); skip++) {
    for (const n of [8, 6, 4, 3]) {
      if (skip + n > words.length) continue;
      const head = words.slice(skip, skip + n).join(" ");
      const p = find(head);
      if (p !== -1) { headPos = p; headLen = head.length; headStartWord = skip; break outer; }
    }
  }
  if (headPos !== -1) {
    // The end of the words that really matched. When the chunk's own end has
    // to be estimated, the next search starts here rather than at the
    // estimate, since an estimate that overshoots would start it past the
    // chunk that follows.
    const anchorEnd = headPos + headLen;
    if (headStartWord > 0) {
      // The head only matched after sliding past rewritten opening words, so I
      // pull the start back by their length and those words get painted too.
      const skippedLen = words.slice(0, headStartWord).join(" ").length + 1;
      headPos = Math.max(0, headPos - skippedLen);
      headLen = anchorEnd - headPos;
    }
    // END anchor: try tails of decreasing length, searched after the head
    // so we find THIS chunk's tail, not a later repetition.
    for (const n of [6, 5, 4, 3, 2]) {
      if (words.length - headStartWord <= n) continue;
      const tail = words.slice(-n).join(" ");
      const tp = joined.indexOf(tail, headPos + headLen);
      // Sanity bound: a bogus match must not paint half the page.
      if (tp !== -1 && tp + tail.length - headPos <= target.length * 2) {
        return { start: headPos, end: tp + tail.length, next: tp + tail.length };
      }
    }
    // No trustworthy tail (server rewrote the ending) → estimate the span
    // from the chunk's own length so the WHOLE chunk is painted, not just
    // the head words. Slight overshoot is clipped to the page text.
    return { start: headPos, end: Math.min(headPos + target.length, joined.length), next: anchorEnd };
  }

  // The head anchor failed, meaning the chunk's opening words were rewritten
  // by numbers, pronunciation or maths, or they straddle skipped elements.
  // So anchor from any 4-word window inside the chunk instead, and
  // back-calculate the chunk's start from that window's offset within the
  // chunk, so the WHOLE chunk paints, rather than just the part after wherever
  // a head would match.
  for (let w = 1; w + 4 <= words.length; w += 2) {
    const win = words.slice(w, w + 4).join(" ");
    const p = find(win);
    if (p !== -1) {
      const offsetInChunk = words.slice(0, w).join(" ").length + 1;
      const start = Math.max(0, p - offsetInChunk);   // approximate chunk start
      return { start, end: Math.min(start + target.length, joined.length), next: p + win.length };
    }
  }
  return null;
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

// True when the element is not rendered: itself or any ancestor is
// display:none, or it is visibility:hidden. checkVisibility does the walk up
// the tree natively; the fallback is for a Chrome too old to have it.
function _hlHidden(el) {
  if (typeof el.checkVisibility === "function") {
    try { return !el.checkVisibility({ checkVisibilityCSS: true, visibilityProperty: true }); }
    catch (_) { /* fall through to the walk */ }
  }
  // visibility is inherited, so the element's own computed value already
  // answers for its ancestors. display is not, so that one needs the walk.
  if (window.getComputedStyle(el).visibility === "hidden") return true;
  for (let e = el; e && e.nodeType === Node.ELEMENT_NODE; e = e.parentElement) {
    if (window.getComputedStyle(e).display === "none") return true;
  }
  return false;
}

// Elements whose text is never part of what is read, wherever in them it sits.
const _HL_SKIP_INSIDE = "script,style,noscript,nav,iframe,svg,button,select,textarea";

// Whether text directly inside `el` is left out of the map.
//
// This used to look at the direct parent only: its tag and its own computed
// style. So a link inside a <nav>, or a paragraph inside a display:none panel,
// was mapped (the <a> and the <p> are neither nav nor hidden themselves) and a
// chunk could be painted in the menu or matched against text nobody can see.
// Ancestors are checked now, with closest() and checkVisibility() so it stays
// one native call each rather than a walk in script.
//
// <form> stays a direct-parent test on purpose. Some sites wrap the whole page
// in one form, and skipping everything under it would end highlighting there.
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
  if (tag === "form" || tag === "input") return true;
  if (el.closest(_HL_SKIP_INSIDE)) return true;
  // Never highlight inside our own overlay.
  if (el.closest("#tts-overlay")) return true;
  return _hlHidden(el);
}

// Build the normalised text map for the content root. We keep, for each text
// node, the mapping between positions in its ORIGINAL text and positions in
// the normalised joined string, so a normalised match can be translated back
// to an exact DOM Range.
function _buildHlMap(root) {
  _hlMapRoot = root || null;
  _hlMapAt = Date.now();
  _hlMapStale = false;
  if (!root) { _hlNodeMap = []; _hlNormJoined = ""; return; }

  // Sibling text nodes share a parent, so each parent is judged once.
  const skipped = new Map();
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
    acceptNode(n) {
      if (!n.textContent || !n.textContent.trim()) return NodeFilter.FILTER_REJECT;
      const p = n.parentElement;
      let skip = skipped.get(p);
      if (skip === undefined) { skip = _hlSkip(p); skipped.set(p, skip); }
      return skip ? NodeFilter.FILTER_REJECT : NodeFilter.FILTER_ACCEPT;
    },
  });

  const map = [];
  let joined = "";
  let node;
  while ((node = walker.nextNode())) {
    const { norm, idxMap } = _hlNormalise(node.textContent, joined.endsWith(" ") || joined === "");
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

// My own header bar going into or out of the page is not a change to what is
// being read, so it must not cost a rebuild.
function _hlOwnMutation(rec) {
  if (rec.type !== "childList") return false;
  const mine = n => n && n.id === "tts-overlay";
  const nodes = [...rec.addedNodes, ...rec.removedNodes];
  return nodes.length > 0 && nodes.every(mine);
}

function _hlWatch(root) {
  if (typeof MutationObserver === "undefined" || !root) return;
  if (!_hlObserver) {
    _hlObserver = new MutationObserver(recs => {
      if (recs.every(_hlOwnMutation)) return;
      _hlMapStale = true;
      // Nothing more to learn until the next rebuild, which watches again.
      _hlObserver.disconnect();
    });
  } else {
    _hlObserver.disconnect();
  }
  try {
    _hlObserver.observe(root, {
      childList: true, subtree: true, characterData: true,
      // The attributes that hide and show text without moving a node.
      attributes: true, attributeFilter: ["class", "style", "hidden", "open"],
    });
  } catch (_) { /* a root that cannot be observed; the age limit still applies */ }
}

// Make sure the map describes the page as it is now.
function _hlFreshMap() {
  const root = _hlContentRoot();
  if (_hlNodeMap && !_hlMapStale && root === _hlMapRoot &&
      Date.now() - _hlMapAt < _HL_MAP_MAX_AGE) return;
  _buildHlMap(root);
  _hlWatch(root);
}

// Forget everything kept for a read: the map, the watcher, and where the last
// chunk was. Called when a read starts and when one is cleared, so nothing is
// left observing a page that is no longer being read.
function _hlEndRead() {
  if (_hlObserver) _hlObserver.disconnect();
  _hlNodeMap = null;
  _hlNormJoined = "";
  _hlMapRoot = null;
  _hlMapStale = true;
  _hlAfter = null;
  _hlFrom = null;
  _hlNo = 0;
}

// An offset in the joined text, as a place in the DOM.
function _hlDomAt(normPos) {
  if (!_hlNodeMap || !_hlNodeMap.length) return null;
  for (const e of _hlNodeMap) {
    if (normPos < e.start + e.norm.length) {
      const local = Math.max(0, normPos - e.start);
      return { node: e.node, off: e.idxMap[local] };
    }
  }
  const last = _hlNodeMap[_hlNodeMap.length - 1];
  return { node: last.node, off: last.node.textContent.length };
}

// And back: a place in the DOM, as an offset in the joined text as it is now.
// A node that has left the map since (the page replaced it) gives 0, which is
// a search of the whole page, as every search used to be.
function _hlNormAt(pos) {
  if (!pos || !_hlNodeMap) return 0;
  for (const e of _hlNodeMap) {
    if (e.node !== pos.node) continue;
    let k = 0;
    while (k < e.idxMap.length && e.idxMap[k] < pos.off) k++;
    return e.start + k;
  }
  return 0;
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

// `no` is the chunk's number in the read, which tells a repeat of the same
// chunk and a jump backwards apart from ordinary progress.
function highlightChunkInPage(rawText, no) {
  if (!_HL_SUPPORTED || !_hlEnabled) return;
  const target = _normaliseForMatch(_stripMarkers(rawText));
  // Match failures below deliberately KEEP the previous highlight rather than
  // clearing: a bar that lags one chunk is far less jarring than one that
  // blinks out whenever server-side rewriting defeats the matcher. Explicit
  // stop (clearOverlay) and the toggle are the only things that clear.
  if (target.length < 4) return;

  _hlFreshMap();
  if (!_hlNormJoined) return;

  // Where to search from. The same chunk again (the highlight toggled back
  // on) starts where it started last time, or it would look past itself. A
  // jump backwards starts from the top, since "forward from the last match"
  // would then find a later repeat instead of the earlier text jumped to.
  let from;
  if (no && no === _hlNo) from = _hlFrom;
  else {
    if (no && no < _hlNo) _hlAfter = null;
    from = _hlAfter;
  }

  const hit = _hlLocate(_hlNormJoined, target, _hlNormAt(from));
  if (!hit) return;   // no anchor found — keep the previous highlight (see note above)
  const range = _rangeFromNormSpan(hit.start, hit.end);
  if (!range) return;

  _hlFrom = from;
  if (no) _hlNo = no;
  _hlAfter = _hlDomAt(hit.next);
  _paintRange(range);
  _hlAutoScroll(range);
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

// --- Following the highlight down the page ---
// There was no scrolling at all, so on a long article the highlight walked off
// the bottom of the screen within a minute and the reader had to chase it by
// hand. When the painted chunk is not wholly on screen it is now brought to
// about a quarter of the way down, smoothly. Only a chunk that was actually
// found is scrolled to, and not for five seconds after the reader scrolls
// themselves, so looking back up the page is not fought.
let _hlQuietUntil = 0;
let _hlBarDrag = false;
let _hlScrollWired = false;
const _HL_SCROLL_KEYS = ["ArrowUp", "ArrowDown", "PageUp", "PageDown", "Home", "End", " "];

function _wireScrollWatch() {
  if (_hlScrollWired) return;
  _hlScrollWired = true;
  const quiet = () => { _hlQuietUntil = Date.now() + 5000; };
  const opts = { capture: true, passive: true };
  // The scroll event itself is no use here: my own smooth scroll fires it too,
  // and so does any carousel on the page. These are the reader's hands.
  window.addEventListener("wheel", quiet, opts);
  window.addEventListener("touchmove", quiet, opts);
  window.addEventListener("keydown", e => { if (_HL_SCROLL_KEYS.includes(e.key)) quiet(); }, opts);
  // A drag on the page's scrollbar, which is none of the above. The bar lies
  // outside the root element's client width.
  window.addEventListener("mousedown", e => {
    if (e.clientX >= document.documentElement.clientWidth) { _hlBarDrag = true; quiet(); }
  }, opts);
  window.addEventListener("mouseup", () => { if (_hlBarDrag) { _hlBarDrag = false; quiet(); } }, opts);
}

// How far to scroll so a range spanning [top, bottom] sits about a quarter of
// the way down the visible band [viewTop, viewBottom]. 0 when it is already
// wholly on screen, or near enough where it would be put that moving it would
// only be a twitch. A range too tall for that goes to the top of the band.
function _hlScrollDelta(top, bottom, viewTop, viewBottom) {
  const view = viewBottom - viewTop;
  if (view <= 0) return 0;
  if (top >= viewTop && bottom <= viewBottom) return 0;
  const tall = bottom - top > view * 0.7;
  const want = viewTop + (tall ? 8 : view / 4);
  const d = Math.round(top - want);
  return Math.abs(d) < 4 ? 0 : d;
}

// The element that scrolls the text at `node`, or null for the document. Most
// pages scroll the document; an app shell scrolls a pane inside it.
function _hlScrollerOf(node) {
  const body = document.body, root = document.documentElement;
  for (let el = node && node.parentElement; el && el !== body && el !== root; el = el.parentElement) {
    if (el.scrollHeight <= el.clientHeight + 1) continue;   // cannot scroll; no style read needed
    const oy = window.getComputedStyle(el).overflowY;
    if (oy === "auto" || oy === "scroll") return el;
  }
  // A shell whose body is the pane: the document cannot scroll but the body can.
  const se = document.scrollingElement || root;
  if (body && se !== body && se.scrollHeight <= se.clientHeight + 1 &&
      body.scrollHeight > body.clientHeight + 1) return body;
  return null;
}

function _hlAutoScroll(range) {
  _wireScrollWatch();
  if (_hlBarDrag || Date.now() < _hlQuietUntil) return;
  let r;
  try { r = range.getBoundingClientRect(); } catch (_) { return; }
  if (!r || (!r.width && !r.height)) return;
  // The header bar covers the top of the window, so the band starts below it.
  let viewTop = (_bar && !_bar.hidden && _isTopFrame()) ? _bar.host.offsetHeight : 0;
  let viewBottom = window.innerHeight;
  const pane = _hlScrollerOf(range.startContainer);
  if (pane) {
    const b = pane.getBoundingClientRect();
    viewTop = Math.max(viewTop, b.top);
    viewBottom = Math.min(viewBottom, b.bottom);
  }
  const d = _hlScrollDelta(r.top, r.bottom, viewTop, viewBottom);
  if (!d) return;
  let calm = false;
  try { calm = window.matchMedia("(prefers-reduced-motion: reduce)").matches; } catch (_) {}
  try { (pane || window).scrollBy({ top: d, behavior: calm ? "auto" : "smooth" }); } catch (_) {}
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

// The header bar while it is on the page: its host element and the parts of it
// that change. The bar lives in a closed shadow root (see _buildBar), so these
// references are the only way to reach its buttons; nothing can be looked up
// by id from the document any more.
let _bar = null;
// What the bar is showing besides a chunk: "" for the chunk being spoken,
// "preparing" before the first one is ready, "error" after the server failed.
let _barState = "";
// The chunk last spoken, for painting it again when the highlight is turned
// back on. Kept apart from _lastOverlay, whose text may be a status message.
let _lastChunk = null;

// --- Where the last right-click landed ---
// The menu's "Read from here" has to know where "here" is, and the worker is
// only told which frame was clicked, not which element. So each right-click
// notes the start of the block under the pointer, and the worker asks for it
// when that menu entry is chosen. Nothing leaves the page unless it is.
let _menuAnchor = "";
function _blockTextAt(el) {
  // Nearest ancestor that is a block of prose, so a click on a link or a bold
  // word inside a paragraph still means that paragraph.
  let node = el && el.nodeType === 1 ? el : (el && el.parentElement);
  const block = node && node.closest
    ? node.closest("p, li, h1, h2, h3, h4, h5, h6, blockquote, td, th, dd, dt, figcaption, pre, article, section, div")
    : null;
  const text = ((block || node || {}).innerText || "").replace(/\s+/g, " ").trim();
  return text.slice(0, 240);
}
try {
  document.addEventListener("contextmenu", e => {
    try { _menuAnchor = _blockTextAt(e.target); } catch (_) { _menuAnchor = ""; }
  }, true);
} catch (e) { /* no document events here; "read from here" starts at the top */ }

chrome.runtime.onMessage.addListener((request, sender, sendResponse) => {
  if (request.action === "updateOverlay") {
    _lastChunk = { text: request.text, current: request.current };
    showOverlay(request.text, request.current, request.total);
    highlightChunkInPage(request.text, request.current);   // paint the spoken chunk in the page
    sendResponse({ status: "ok" });
    return false;
  }
  // A read has started here and its first chunk is still being made. The bar
  // goes up now, saying so, with nothing highlighted yet.
  if (request.action === "overlayPreparing") {
    _lastChunk = null;
    _hlEndRead();
    clearPageHighlight();
    showOverlay(request.text || "Preparing the first chunk…", request.current, request.total, "preparing");
    sendResponse({ status: "ok" });
    return false;
  }
  // The server stopped answering. The bar stays and says what happened, since
  // with the popup closed it is the only place the reader would see it, and
  // its play button becomes the Resume the message asks for.
  if (request.action === "playbackError") {
    showOverlay(request.message || "Playback stopped.", (request.index || 0) + 1, request.total, "error");
    sendResponse({ status: "ok" });
    return false;
  }
  // Something to tell the reader that is not a read: the right-click menu was
  // used and the page could not be read, or the server is off. There is no
  // popup open to say it in, so the bar says it for a few seconds and goes,
  // unless a read has taken the bar over in the meantime.
  if (request.action === "overlayNotice") {
    _lastChunk = null;
    _hlEndRead();
    clearPageHighlight();
    showOverlay(request.text || "", null, null, "preparing");
    const shown = _lastOverlay;
    setTimeout(() => {
      if (_lastOverlay !== shown) return;
      removeOverlay();
      _lastOverlay = null;
    }, request.ms || 7000);
    sendResponse({ status: "ok" });
    return false;
  }
  // "Read from here": where the last right-click landed, as the text of the
  // block that was clicked. The worker finds the chunk that text begins in.
  if (request.action === "getMenuAnchor") {
    sendResponse({ text: _menuAnchor });
    return false;
  }
  if (request.action === "clearOverlay") {
    removeOverlay();
    clearPageHighlight();
    _hlEndRead();
    _lastOverlay = null;
    _lastChunk = null;
    sendResponse({ status: "ok" });
    return false;
  }
  if (request.action === "pausedPlaying") {
    ttsIsPaused = true;
    _syncPauseBtn();
    sendResponse({ status: "ok" });
    return false;
  }
  if (request.action === "resumedPlaying") {
    ttsIsPaused = false;
    _syncPauseBtn();
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
      if (_lastChunk) highlightChunkInPage(_lastChunk.text, _lastChunk.current);
    } else {
      clearPageHighlight();
    }
  }

  // Header bar on/off toggled (from the popup button).
  if (changes.overlayEnabled) {
    _overlayEnabled = changes.overlayEnabled.newValue !== false;
    if (_overlayEnabled) {
      // Re-show the bar with the latest chunk if we have one.
      if (_lastOverlay) showOverlay(_lastOverlay.text, _lastOverlay.current, _lastOverlay.total, _lastOverlay.state);
    } else {
      _removeOverlayBarOnly();   // hide bar, keep highlight intact
    }
  }

  if (changes.overlayLineColour) _setHlColour(changes.overlayLineColour.newValue);

  if (!(changes.overlayLineColour || changes.overlayBarColour || changes.overlayTextSize)) return;
  if (!_barLive()) return;
  _getOverlayCfg(cfg => _applyOverlayStyle(cfg));
});

// Toggle the in-page highlight on/off and persist it so the popup button and
// the overlay button always agree. Single source of truth = storage; both
// entry points just flip this value and react via the listener above.
function toggleHighlight() {
  chrome.storage.local.set({ highlightEnabled: !_hlEnabled });
}

// Reflect current highlight state on the overlay toggle button, if present.
function _syncHlToggleButton() {
  const btn = _bar && _bar.btnHl;
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

// The pause button's face. It shows play while paused, and also after a
// failure, when pressing it resumes the read rather than unpausing it.
function _syncPauseBtn() {
  const btn = _bar && _bar.btnPause;
  if (!btn) return;
  const go = ttsIsPaused || _barState === "error";
  btn.textContent = go ? "▶" : "⏸";
  btn.style.background = go ? "#22c55e" : _bar.accent;
  btn.style.color = go ? "white" : "#000";
  btn.title = _barState === "error" ? "Resume" : "";
}

// Strip any semantic markers that arrive in the text before displaying.
// This is a local defence. background.js should have stripped the markers
// already, but content.js protects itself in case of a version mismatch or
// something cached.
const _MARKERS = {
  'H1':1,'/H1':1,'H2':1,'/H2':1,'H3':1,'/H3':1,
  'BOLD':1,'/BOLD':1,'ITALIC':1,'/ITALIC':1,
  'CODE':1,'/CODE':1,'CALLOUT':1,'/CALLOUT':1,
  'CAPTION':1,'/CAPTION':1,'LIST':1,'/LIST':1,'BREAK':1
};
function _stripMarkers(text) {
  return text.split('|')
    .filter(p => p.trim().length > 0 && !_MARKERS[p.trim()])
    .join('')
    .replace(/ {2,}/g, ' ')
    .trim();
}

function _applyOverlayStyle(cfg) {
  // Keep the in-page highlight colour in sync with the overlay accent.
  _setHlColour(cfg.overlayLineColour);
  if (!_bar) return;
  const overlay = _bar.overlay;
  _bar.accent = cfg.overlayLineColour;

  // Bar background per theme/custom colour. Choose readable text colours based
  // on the background's luminance so a light bar (e.g. Light theme white) shows
  // dark text and a dark bar shows light text.
  const barBg = cfg.overlayBarColour || "#0f0f0f";
  const dark  = _isDarkColour(barBg);
  overlay.style.background = _hexToRgba(barBg, 0.97);
  overlay.style.color = dark ? "#f0f0f0" : "#1a1a1a";

  // Border and progress accent pick up the highlight colour.
  overlay.style.borderBottom = "2px solid " + cfg.overlayLineColour;

  _bar.textRow.style.fontSize = cfg.overlayTextSize + "px";
  _bar.textRow.style.color = dark ? "#aaa" : "#444";   // dimmed, readable either way

  _bar.progress.style.color = cfg.overlayLineColour;

  // Pause button uses the line colour when active.
  _syncPauseBtn();

  // Re-seat the page margin in case bar height changed (larger text → taller bar).
  _seatBody();
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

// True while my bar is on the page. A page that rebuilds its own body takes
// the bar out with it, and the references I hold would then point at nothing.
function _barLive() {
  return !!(_bar && _bar.host && _bar.host.isConnected !== false);
}

// Put the latest text, count and state into the bar that is already up.
function _fillBar() {
  const o = _lastOverlay;
  if (!_bar || !o) return;
  _bar.textRow.textContent = o.text;
  // A notice (see overlayNotice) belongs to no chunk, so it has no count.
  _bar.progress.textContent = (o.current != null && o.total != null) ? `${o.current} / ${o.total}` : "";
  if (_barState !== o.state) {
    _barState = o.state;
    _syncPauseBtn();
  }
}

// `state` is "" for a chunk being spoken, "preparing" or "error" for the two
// status messages the bar also carries (see the message listener).
function showOverlay(text, current, total, state) {
  text = _stripMarkers(text);

  // Remember the latest chunk so the bar can be re-shown if the user toggles
  // it back on mid-playback.
  _lastOverlay = { text, current, total, state: state || "" };

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
  // highlighting still runs. Only the bar goes: this used to clear the
  // highlight as well, which the caller then painted again for a chunk but
  // not for a status message.
  if (!_overlayEnabled) { _removeBar(); _overlayCreating = false; return; }

  // Update in place to avoid flicker.
  if (_barLive()) { _fillBar(); return; }

  // Guard against a race: _getOverlayCfg is async, so two fast chunks could both
  // pass the check above and each create a header. A synchronous flag ensures
  // only the first proceeds to build one.
  if (_overlayCreating) return;
  _overlayCreating = true;

  ttsIsPaused = false;

  _getOverlayCfg(cfg => {
    // Re-check inside the async callback: if the overlay was built meanwhile,
    // abort so we never end up with two headers.
    if (_barLive()) { _overlayCreating = false; _fillBar(); return; }
    // And if the read was cleared meanwhile there is nothing to show. A stop
    // that landed inside this wait used to be followed by a bar for a read
    // that was already over.
    if (!_lastOverlay) { _overlayCreating = false; return; }
    _buildBar(cfg);
    _overlayCreating = false;
  });
}

// Build the bar and put it on the page.
//
// It used to be plain elements in the page's own DOM, so the site's stylesheet
// applied to it: a site rule like `button { border-radius: 0; padding: 0 }` or
// `div { box-sizing: ... }` restyled the bar, differently on every site. The
// bar now sits in a closed shadow root, where no page rule can reach, and the
// host element resets everything it would otherwise inherit (font, colour,
// line height) with all:initial. Each declaration on the host is !important,
// because inline style on its own still loses to a page rule that is.
// The look is unchanged: the same elements with the same styles, one level in.
function _buildBar(cfg) {
  const host = document.createElement("div");
  host.id = "tts-overlay";
  host.style.cssText =
    "all:initial !important;display:block !important;position:fixed !important;" +
    "top:0 !important;left:0 !important;right:0 !important;" +
    "z-index:2147483647 !important";
  // A note of what I did to the page's body margin, for a later copy of this
  // script to read if this one is orphaned. Empty means not pushed.
  host.setAttribute("data-kam-margin", "");
  const root = host.attachShadow ? host.attachShadow({ mode: "closed" }) : host;

  const overlay = document.createElement("div");
  overlay.style.cssText =
    "background:rgba(15,15,15,0.97);color:#f0f0f0;" +
    "font-family:'Segoe UI',sans-serif;" +
    "box-shadow:0 4px 20px rgba(0,0,0,0.5)";

  const row = document.createElement("div");
  row.style.cssText = "display:flex;align-items:center;justify-content:center;gap:12px;padding:8px 20px";

  const btnPause = _makeBtn("⏸", () => {
    // After a failure this button is the Resume the message asks for. The
    // worker answers by sending the Preparing state, which puts it back.
    if (_barState === "error") {
      chrome.runtime.sendMessage({ action: "resumeRead" }, () => { void chrome.runtime.lastError; });
      return;
    }
    chrome.runtime.sendMessage({ action: "pauseResume" }, (res) => {
      if (chrome.runtime.lastError) return;
      if (res) {
        ttsIsPaused = res.isPaused;
        _syncPauseBtn();
      }
    });
  }, cfg.overlayLineColour);

  const btnStop = _makeBtn("⏹", () => {
    chrome.runtime.sendMessage({ action: "stop" }).catch(() => {});
    removeOverlay();
  }, "#ef4444");

  const progress = document.createElement("span");
  progress.style.cssText =
    "font-size:12px;font-weight:600;min-width:55px;text-align:center;" +
    "color:" + cfg.overlayLineColour;

  // The highlight on/off toggle. One click, instant, and the state is shared
  // with the popup.
  const btnHl = document.createElement("button");
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
  textRow.style.cssText =
    "padding:2px 20px 8px;color:#999;text-align:center;" +
    "white-space:nowrap;overflow:hidden;text-overflow:ellipsis;" +
    "font-size:" + cfg.overlayTextSize + "px";

  overlay.appendChild(row);
  overlay.appendChild(textRow);
  root.appendChild(overlay);

  // Whether to push the page down is decided once, before the bar has changed
  // anything, and kept for the life of the bar so the page does not jump.
  _bar = { host, overlay, textRow, progress, btnPause, btnHl,
           accent: cfg.overlayLineColour, hidden: false, shell: _isAppShell() };
  _barState = "";
  _fillBar();

  document.body.appendChild(host);
  // Belt-and-braces: if any duplicate slipped in, keep only this one.
  document.querySelectorAll("#tts-overlay").forEach(el => { if (el !== host) el.remove(); });
  _applyOverlayStyle(cfg);   // colours, border, and the body margin
  _hlEnabled = cfg.highlightEnabled !== false;
  _syncHlToggleButton();
  _wireFullscreenHide();   // header must not clip over fullscreen video
}

// --- Making room for the bar ---
// The bar is fixed to the top of the window, so an ordinary document is pushed
// down by the bar's height with a margin on its body, and nothing is covered.
//
// A full-height app shell is different. A mail client, an editor or a chat
// sizes itself to the window and scrolls inside its own panes, and a margin on
// its body does not move its content out from under the bar: it pushes the
// whole shell down so its bottom edge (often the text box) leaves the screen,
// or it steals the height the shell had measured for itself. So there the bar
// simply lies over the top of the page and the body is left alone. I do not
// try to move a site's own fixed or sticky header either way: the margin moves
// the document, and a header pinned to the window stays where the site put it.
//
// A page counts as a shell when html or body clips its overflow, or when the
// body is exactly the height of the window, the document itself cannot
// scroll, and something inside it at the middle of the screen does the
// scrolling. The last part is what tells a shell from a short ordinary page
// that happens to use height:100%, which should still be pushed.
function _isAppShell() {
  try {
    const de = document.documentElement, b = document.body;
    if (!de || !b) return false;
    const clipped = v => v === "hidden" || v === "clip";
    if (clipped(window.getComputedStyle(de).overflowY) ||
        clipped(window.getComputedStyle(b).overflowY)) return true;

    const vh = window.innerHeight;
    if (Math.abs(b.getBoundingClientRect().height - vh) > 2) return false;
    const se = document.scrollingElement || de;
    if (se.scrollHeight > se.clientHeight + 2) return false;
    const under = document.elementsFromPoint(window.innerWidth / 2, vh / 2);
    for (const el of under) {
      if (el === b || el === de) continue;
      if (el.scrollHeight <= el.clientHeight + 2) continue;
      const oy = window.getComputedStyle(el).overflowY;
      if (oy === "auto" || oy === "scroll") return true;
    }
  } catch (_) { /* a page I cannot measure is treated as an ordinary one */ }
  return false;
}

// The page's own inline margin-top on body, held while mine is in its place:
// { value, priority }. Null whenever I am not pushing.
//
// Taking the bar away used to set the inline margin to "", which also threw
// away a margin the page had put there itself. And it did so on every clear,
// bar or no bar, in every frame. The original is now remembered at the first
// push and put back, and a page I never pushed is not touched at all.
let _bodyPush = null;

function _seatBody() {
  if (!_bar || !document.body) return;
  if (_bar.shell || _bar.hidden) return;
  const st = document.body.style;
  if (!_bodyPush) {
    _bodyPush = { value: st.marginTop || "",
                  priority: (st.getPropertyPriority && st.getPropertyPriority("margin-top")) || "" };
    try { _bar.host.setAttribute("data-kam-margin", JSON.stringify(_bodyPush)); } catch (_) {}
  }
  st.marginTop = (_bar.host.offsetHeight + 4) + "px";
}

function _restoreBody(saved) {
  _bodyPush = null;
  if (!saved || !document.body) return;
  const st = document.body.style;
  if (saved.priority && st.setProperty) st.setProperty("margin-top", saved.value, saved.priority);
  else st.marginTop = saved.value;
}

// Give the body its own margin back while the bar stays (fullscreen).
function _releaseBody() {
  _restoreBody(_bodyPush);
  if (_bar) { try { _bar.host.setAttribute("data-kam-margin", ""); } catch (_) {} }
}

// What a bar on the page says it did to the body margin. undefined for a bar
// with no note at all, which is one from before notes were kept; that code
// always pushed and never remembered, so its margin is cleared as it used to
// be. null for a bar that did not push.
function _marginNote(el) {
  let raw = null;
  try { raw = el && el.getAttribute ? el.getAttribute("data-kam-margin") : null; } catch (_) {}
  if (raw === null || raw === undefined) return undefined;
  if (!raw) return null;
  try {
    const o = JSON.parse(raw);
    return { value: String(o.value || ""), priority: o.priority === "important" ? "important" : "" };
  } catch (_) { return null; }
}

// When any element enters fullscreen (typically a page video), hide the reader
// header + release the body margin so nothing overlaps the fullscreen content.
// Restore both on exit. It's idempotent since the listener attaches only once.
let _fsWired = false;
function _wireFullscreenHide() {
  if (_fsWired) return;
  _fsWired = true;
  const onFs = () => {
    const fs = document.fullscreenElement || document.webkitFullscreenElement;
    if (!_barLive()) return;
    _bar.hidden = !!fs;
    // The host's display is !important, so it has to be changed the same way.
    _bar.host.style.setProperty("display", fs ? "none" : "block", "important");
    if (fs) _releaseBody();        // let fullscreen use full height
    else _seatBody();
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

// Take every bar off the page and give the body its margin back. A bar left
// by an orphaned copy of this script is included, and its note says what that
// copy had done to the margin.
function _removeBar() {
  let saved = _bodyPush;
  document.querySelectorAll("#tts-overlay").forEach(el => {
    if (!saved) {
      const note = _marginNote(el);
      saved = note === undefined ? { value: "", priority: "" } : note;
    }
    el.remove();
  });
  _bar = null;
  _barState = "";
  _restoreBody(saved);
}

function removeOverlay() {
  _removeBar();
  _overlayCreating = false;
  clearPageHighlight();
}

// Remove just the header bar WITHOUT clearing the in-page highlight. Used when
// the bar gets toggled off mid-playback, since the highlight should keep
// following the audio whether or not the bar is visible.
function _removeOverlayBarOnly() {
  _removeBar();
}

// Taking over from an orphan: its header bar is still on the page, but its
// buttons call a runtime that no longer exists, so they are dead. Updating that
// bar in place would keep them dead, so it goes, along with its highlight.
if (_kamPrev && document.getElementById("tts-overlay")) removeOverlay();
})();
