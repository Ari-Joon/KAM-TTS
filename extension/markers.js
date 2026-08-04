// =============================================================================
// KAM TTS semantic marker grammar, kept in one place as the source of truth.
//
// The chunker in popup.js embeds structural markers like |H1|…|/H1| and |BREAK|
// into the chunk text so the code downstream knows the document structure.
// Anything that strips, splits on or inspects these markers has to use the
// definitions below. It gets loaded into:
//   background.js  via importScripts("markers.js")
//   popup.html / player.html  via <script src="markers.js">
//   content.js  via manifest content_scripts order
// =============================================================================
"use strict";

// All marker names the chunker may emit.
const KAM_MARKER_NAMES = ["H1", "H2", "H3", "BOLD", "ITALIC", "CODE", "CALLOUT", "CAPTION", "BREAK"];

// Matches any opening or closing marker, so |H1|, |/BOLD|, |BREAK| and so on.
// Where lastIndex matters I build a fresh regex per call, since this global
// constant is only for one-shot .replace() use.
const KAM_MARKER_RE = /\|\/?(?:H1|H2|H3|BOLD|ITALIC|CODE|CALLOUT|CAPTION|BREAK)\|/g;

// A chunk that begins with a heading marker came from an h1, h2 or h3.
const KAM_HEADING_START_RE = /^\s*\|H[123]\|/;

// A chunk ending in |BREAK| is closing a paragraph.
const KAM_PARAGRAPH_END_RE = /\|BREAK\|\s*$/;

// Strip every marker from text, collapsing the leftover whitespace.
function kamStripMarkers(text) {
  return String(text).replace(KAM_MARKER_RE, " ").replace(/\s+/g, " ").trim();
}