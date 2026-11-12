/* The reading code: taking text off a page and cutting it into chunks.

   This lived in popup.js, which was fine while the popup was the only way to
   start a read. The right-click menu starts one with no popup open, from the
   service worker, and the worker cannot load popup.js, since that file wires
   up the popup's own buttons as it loads. So the part that does the reading
   moved here, unchanged, and both load it: the popup with a script tag before
   popup.js, the worker with importScripts.

   What it expects from whoever loads it:
     kamFetch, SERVER            for the boundary corrections the server keeps
     kamSpeakable, kamMostlyNonLatin   from textnorm.js
     showError(msg)              somewhere to say why a page could not be read;
                                 the popup paints it, the worker shows it in
                                 the page's header bar

   getSelectedText and getPageText take the tab to read as an optional
   argument. The popup passes nothing and gets the active tab, as before; the
   worker passes the tab that was right-clicked. */

// The tab to read: the one handed in, or the active tab of the current window.
function _readerTabs(tab, cb) {
  if (tab && tab.id != null) { cb([tab]); return; }
  chrome.tabs.query({ active: true, currentWindow: true }, cb);
}


// What stopped an extraction, as a line for the user, or "" when the text is
// there to be read. Selected Text and Read Page share every one of these.
function extractionProblem(text) {
  if (text === "__KAM_NO_PAGE__") {
    return "Switch to a web page tab first — KAM can't read its own dashboard or chrome:// pages.";
  }
  if (text === "__KAM_TIMEOUT__") {
    return "The page did not respond in time. Reload the tab and try again — "
         + "some pages block extensions until they finish loading.";
  }
  if (text === "__KAM_PDF__") {
    return "PDFs cannot be read directly yet. Copy the text and paste it into Custom Text.";
  }
  return "";
}

// =============================================================================
// ADAPTIVE CHUNKING
//
// Every chunk should be a natural spoken unit: consistent in length, coherent,
// and never cut mid-phrase. Three things do that work.
//
//   1. One sentence per chunk. Text is split on block boundaries, then into
//      sentences, and each sentence is its own chunk, so nothing is packed
//      across a sentence end unless a piece is too short to stand alone.
//
//   2. The safety net. A sentence over the cap is split on clause boundaries,
//      and anything still too long is hard-split at a word. Undersized pieces are
//      glued to a neighbour in the same block rather than dropped or spoken
//      alone, and a block that is short all by itself is still spoken.
//
//   3. The cap. Nothing leaves here longer than MAX_CHARS as spoken, because
//      the server cuts a chunk off at its own limit and the tail is never heard.
//
// An earlier design had two more layers in front of these, a segment classifier
// and a content-aware target length, and this comment described all four. They
// were never called, so they and the regexes only they read are gone; the
// comment now describes the code that runs.
// =============================================================================

const TARGET_CHARS = 90;    // target chunk length — keeps XTTS inference fast
// The hard cap on a chunk as it is spoken. The server truncates at 190 and
// XTTS itself at 180, so this has to stay under 180 with room to spare for
// what the server expands on its side, such as numbers into words.
const MAX_CHARS    = 150;
const MIN_CHARS    = 6;     // anything shorter is glued to a neighbour

// ---------------------------------------------------------------------------
// Pre-compiled patterns, built once at module load and never rebuilt
// ---------------------------------------------------------------------------

const _RX_QUESTION    = /[?]/;
const _RX_EMPHASIS    = /[|](BOLD|ITALIC)[|]/;
const _RX_EXCLAIM_END = /[!]$/;
const _RX_WHITESPACE  = /\s+/;
// A closing tag that says what the whole chunk was. The worker reads it off
// the end of the chunk, so nothing may be glued on after it.
const _RX_STRUCT_END  = /\|\/(?:H[1-3]|LIST|CAPTION|CALLOUT)\|\s*$/;
// What can sit straight after a full stop and still belong to the sentence
// the stop ends: a closing quote or bracket.
const _RX_CLOSER      = /["'\u201D\u2019)\]]/;


// ---------------------------------------------------------------------------
// What a piece of text amounts to once it is spoken
// ---------------------------------------------------------------------------

// The text as the voice will get it: no layout markers, and accents and
// symbols turned into letters and words by the shared helper in textnorm.js.
// The worker does the same before it posts, so this is the form whose length
// and content matter. The helper is optional here because the tests lift this
// section on its own.
function spokenForm(s) {
  let t = String(s == null ? '' : s).replace(/\|\/?[A-Z0-9]+\|/g, ' ');
  if (typeof kamSpeakable === 'function') t = kamSpeakable(t);
  return t.replace(/\s+/g, ' ').trim();
}

function spokenLength(s) {
  return spokenForm(s).length;
}

// Is there anything here to say? One letter or one digit is enough. The old
// test wanted two ASCII letters, so "2020", "70" and "£5" were all thrown
// away as if they were noise, and counted a marker's own letters, so an empty
// |LIST| passed. Punctuation and symbols on their own still do not count.
function hasContent(s) {
  return /[A-Za-z0-9]/.test(spokenForm(s));
}


// ---------------------------------------------------------------------------
// The minimum length to stand alone, which is a context-aware floor
// ---------------------------------------------------------------------------

function minFor(text) {
  if (_RX_EMPHASIS.test(text)) return 20;
  if (_RX_QUESTION.test(text) || _RX_EXCLAIM_END.test(text)) return 15;
  return MIN_CHARS;
}


// ---------------------------------------------------------------------------
// Emphasis boundary guard
// ---------------------------------------------------------------------------

// Apply learned chunk-boundary corrections. The user reports (via the Report
// page) any fragment that was wrongly split off from its sentence; the server
// stores it. Here we fetch those fragments and re-merge: any chunk that begins
// with a known fragment is joined back onto the previous chunk, fixing the
// mis-split on every future read. It's guarded, so a failed fetch just leaves
// the chunks as they are.
async function applyBoundaryCorrections(chunks) {
  if (!Array.isArray(chunks) || chunks.length < 2) return chunks;
  let fragments = [];
  try {
    const res = await kamFetch(`${SERVER}/rules/boundary`, {});
    if (res.ok) {
      const data = await res.json();
      fragments = (data.fragments || []).map(f => String(f).trim()).filter(Boolean);
    }
  } catch (e) {
    return chunks;   // server offline or error: chunk as normal
  }
  return mergeBoundaryFragments(chunks, fragments);
}

// The merge itself, kept apart from the fetch so it can be tested. It obeys
// the same cap as the chunker, because it runs after it: a correction that
// joined two full chunks used to hand the server one it would cut short, so
// a join that would not fit is left as two chunks.
function mergeBoundaryFragments(chunks, fragments) {
  const norm = s => String(s).replace(/\s+/g, " ").trim().toLowerCase();
  const fragNorms = (fragments || []).map(norm).filter(Boolean);
  if (!fragNorms.length) return chunks;

  const merged = [];
  for (const chunk of chunks) {
    const c = norm(chunk);
    // Does this chunk START with a reported fragment? If so, it was wrongly
    // split, so glue it back onto the previous chunk.
    const hit  = fragNorms.some(f => c.startsWith(f));
    const prev = merged[merged.length - 1];
    if (hit && merged.length > 0 && spokenLength(prev + " " + chunk) <= MAX_CHARS) {
      merged[merged.length - 1] = (prev + " " + chunk).replace(/\s+/g, " ").trim();
    } else {
      merged.push(chunk);
    }
  }
  return merged;
}

// ---------------------------------------------------------------------------
// Main chunker
// ---------------------------------------------------------------------------

function splitIntoChunks(text) {
  const chunks = [];

  // Step 1: Split on |BREAK| markers, then heal conjunction-starting segments
  // back to their predecessor. A line starting with "or stored data." was
  // a new line in the source but belongs to the previous sentence.
  //
  // Only when the line before was left unfinished, though. This used to heal
  // onto a finished sentence too and strip its full stop to do it, so verse
  // where every line opens with "And" ran together into one long sentence.
  // "So", "yet" and "for" are gone from the list as well, since a line that
  // begins "For example" or "So far" is a sentence of its own. And the match
  // is on lower case only, because a line that opens with a capital "And" is
  // a line someone started on purpose, as every other line of a hymn is.
  const _HEAL_CONJ = /^\s*(and|but|or|nor)\b/;
  const _FINISHED  = /[.!?]["'\u201D\u2019)\]]*$/;
  const rawSegments = String(text == null ? '' : text).split('|BREAK|');
  const segments = [];
  for (const seg of rawSegments) {
    const s = seg.replace(/\s+/g, ' ').trim();
    if (!s) continue;
    const prev = segments.length ? segments[segments.length - 1] : '';
    if (prev && _HEAL_CONJ.test(s) && s.length <= 60
        && !_FINISHED.test(prev.replace(/(?:\s*\|\/?[A-Z0-9]+\|)+\s*$/, ''))
        && !_RX_STRUCT_END.test(prev)) {
      // Attach to the previous segment, since it's the same sentence just
      // broken across lines
      segments[segments.length - 1] = prev + ' ' + s;
    } else {
      segments.push(s);
    }
  }

  for (const seg of segments) {
    // A block with nothing to say: only punctuation, symbols or bare markers.
    if (!hasContent(seg)) continue;

    // Step 2: Split each segment into individual sentences.
    // Step 3: Each sentence becomes its own piece. If a sentence is too long,
    // split it on clauses and hard-split any clause still over the cap.
    const pieces = [];
    for (const raw of splitSentences(seg)) {
      const s = raw.trim();
      if (!s) continue;
      if (s.length <= MAX_CHARS) { pieces.push(s); continue; }
      splitOnClauses(s).forEach(c => {
        const cl = c.trim();
        if (!cl) return;
        if (cl.length <= MAX_CHARS) pieces.push(cl);
        else hardSplitAtWord(cl, MAX_CHARS).forEach(p => { const pt = p.trim(); if (pt) pieces.push(pt); });
      });
    }

    // Step 4: Glue the undersized pieces to a neighbour, then merge the
    // conjunction orphans. Both stay inside the block, so a heading, a table
    // cell or a line of verse is never run into the block beside it.
    mergeConjunctionStarts(glueShortPieces(pieces)).forEach(p => chunks.push(p));

    // If segment had no sentence-terminal punctuation (e.g. a heading or label),
    // splitSentences returns the whole segment as one item, which is correct.
  }

  // An empty list means there was nothing to read, and the caller says so.
  // This used to hand back the first 150 characters of the raw text instead,
  // which for text with no words in it was a chunk of punctuation.
  return capChunks(chunks);
}

// Merge a short chunk that starts with a bare conjunction into its
// predecessor. These are mostly orphan fragments left by clause splitting.
// They sound detached and get "swallowed" because they're too short to
// register as a complete thought.
//
// Four things were wrong with this. It had no length limit, so a run of short
// "And ..." sentences piled into one chunk of 200 characters or more, which
// the server then cut off. It stripped the full stop it merged across, so two
// sentences were read as one. It treated "For", "So", "However", "Therefore"
// and "Because" as conjunctions, which open perfectly good sentences. And it
// ran over the whole read, so the first line of a new paragraph could be
// pulled back into the paragraph before. So I merge only for and, but, or and
// nor, only inside one block, only when the result fits the cap, and the
// punctuation stays where it was.
function mergeConjunctionStarts(chunks) {
  const _CONJ_START = /^(and|but|or|nor)\b/i;
  const merged = [];
  for (const chunk of chunks) {
    const prev = merged[merged.length - 1];
    if (merged.length > 0 && _CONJ_START.test(chunk) && chunk.length <= 50
        && !_RX_STRUCT_END.test(prev)
        && spokenLength(prev + ' ' + chunk) <= MAX_CHARS) {
      merged[merged.length - 1] = prev + ' ' + chunk;
    } else {
      merged.push(chunk);
    }
  }
  return merged;
}

// Glue the pieces of one block that are too short to be spoken alone onto a
// neighbour. A short piece leads into the piece after it when the two fit the
// cap together, otherwise it trails the piece before it, and when neither
// works it is spoken alone, because a short chunk is better than lost words.
//
// Short sentences used to be dropped outright here: anything under 6
// characters, a question or exclamation under 15, a bold or italic line under
// 20. So "No!" and "Yes." were never heard. Only a piece with nothing to say
// in it, such as a stray bracket, can still go.
function glueShortPieces(pieces) {
  const list = pieces.slice();
  const out  = [];
  const fits = (a, b) => spokenLength(a + ' ' + b) <= MAX_CHARS;
  for (let i = 0; i < list.length; i++) {
    const piece = list[i];
    const said  = hasContent(piece);
    if (said && piece.length >= minFor(piece)) { out.push(piece); continue; }
    if (i + 1 < list.length && fits(piece, list[i + 1])) {
      // Prefer gluing forward: the fragment leads into what follows.
      list[i + 1] = piece + ' ' + list[i + 1];
    } else if (out.length > 0 && fits(out[out.length - 1], piece)) {
      out[out.length - 1] = out[out.length - 1] + ' ' + piece;
    } else if (said) {
      out.push(piece);   // last resort: keep it. Words are never lost.
    }
  }
  return out;
}

// The final guard: no chunk leaves longer than the cap. The splitting above
// measures the raw text, which is nearly always the longer form, but a symbol
// can grow when it is spoken ("£5" is "5 pounds"), so a chunk full of them can
// pass every earlier check and still be too long to say. Those are split again
// at a clause or a space, with a smaller window each time until every piece
// fits as spoken.
function capChunks(chunks) {
  const out = [];
  for (const chunk of chunks) {
    if (spokenLength(chunk) <= MAX_CHARS) { out.push(chunk); continue; }
    let budget = MAX_CHARS;
    let pieces = [chunk];
    do {
      pieces = glueShortPieces(hardSplitAtWord(chunk, budget));
      budget -= 10;
    } while (budget >= 40 && pieces.some(p => spokenLength(p) > MAX_CHARS));
    pieces.forEach(p => out.push(p));
  }
  return out;
}


// ---------------------------------------------------------------------------
// Continuation sentence detection
// ---------------------------------------------------------------------------

// ---------------------------------------------------------------------------
// Sentence splitter
// Built once per call, and ABBREVS is module-level so the Set isn't rebuilt
// ---------------------------------------------------------------------------

const _ABBREVS = new Set([
  'mr','mrs','ms','dr','prof','sr','jr','vs','etc','i.e','e.g','fig',
  'dept','approx','est','inc','ltd','corp','co','no','vol','rev',
  'gen','col','sgt','jan','feb','mar','apr','jun','jul','aug','sep',
  'oct','nov','dec','mon','tue','wed','thu','fri','sat','sun',
  'st','ave','blvd','rd','pt','mt','ft','op','ed','pp','ph',
]);

function splitSentences(text) {
  const results = [];
  let   current = "";

  // Every sentence is kept, however short, since deciding what is too short
  // to stand alone is the caller's job and it glues rather than drops. This
  // used to refuse anything under 6 characters, which is where "No!" went.
  // A piece with nothing to say is still left out, but a closing tag inside
  // it rides on the sentence before, because the worker looks for it there.
  const keep = (piece) => {
    if (!piece) return;
    if (hasContent(piece)) { results.push(piece); return; }
    const closing = piece.match(/\|\/[A-Z0-9]+\|/g);
    if (closing && results.length) results[results.length - 1] += closing.join('');
  };

  for (let i = 0; i < text.length; i++) {
    const ch = text[i];
    current += ch;

    if (ch !== '.' && ch !== '!' && ch !== '?') continue;

    // Consume full ellipsis as one token
    if (ch === '.' && text[i+1] === '.' && text[i+2] === '.') {
      current += '..';
      i += 2;
      continue;
    }

    // A closing quote or bracket straight after the stop belongs to this
    // sentence. Without this step the split fell between the stop and the
    // quote, so '"Is it hard?" asked Tom.' became '"Is it hard?' and
    // '" asked Tom.', and the lower-case word after the quote was never seen.
    let k = i + 1;
    while (k < text.length && _RX_CLOSER.test(text[k])) k++;

    // Look ahead past whitespace
    let j = k;
    while (j < text.length && text[j] === ' ') j++;
    const nextChar = text[j] || '';
    const prevWord = current.trim().split(_RX_WHITESPACE).pop().replace(/[.!?]+$/, '').toLowerCase();

    const isDecimal    = ch === '.' && /\d/.test(text[i-1] || '') && /\d/.test(nextChar);
    const isAbbrev     = ch === '.' && (_ABBREVS.has(prevWord) || prevWord.length <= 2);
    const isInitial    = ch === '.' && prevWord.length === 1 && /[a-z]/i.test(prevWord);
    const noCapNext    = nextChar && !/[A-Z"'\u201C\u2018(]/.test(nextChar);
    const isEOT        = j >= text.length;
    // Dotted identifier: "Local.Input", "Upper(Local.Input", "System.User"
    // Extract the last identifier token (after any paren/bracket) and check if
    // it's PascalCase followed by a letter, so property access not a sentence end.
    const _lastIdent = (prevWord.match(/[A-Za-z_][A-Za-z0-9_]*$/) || [''])[0];
    const isDottedPath = ch === '.' && /^[A-Z][a-zA-Z]+$/.test(_lastIdent) && /[a-zA-Z_]/.test(nextChar);

    if (isDecimal || isAbbrev || isInitial || isDottedPath || (noCapNext && !isEOT)) continue;

    current += text.slice(i + 1, k);
    i = k - 1;
    keep(current.trim());
    current = "";
  }

  keep(current.trim());
  return results;
}


// ---------------------------------------------------------------------------
// Clause splitter
// ---------------------------------------------------------------------------

// Each clause keeps the comma or semicolon that ended it, and no clause is
// thrown away for being short. Both used to happen: the split consumed the
// separator and a filter then dropped every piece under 6 characters, so
// "red; green; blue; a deep violet; teal." was read as "red" and "a deep
// violet". The caller glues the short ones back together, and with the
// separators still in place the result reads as it was written.
function splitOnClauses(text) {
  let parts;

  if (text.includes(';')) {
    parts = packClauses(text.split(/(?<=;)/));
  } else {
    const conjComma = text.split(/(?<=,)\s*(?=(?:and|but|or|so|yet|although|because|however|therefore|whereas)\b)/i);
    if (conjComma.length > 1) {
      parts = conjComma;
    } else if (text.includes(',')) {
      parts = splitOnCommas(text);
    } else {
      const conjSplit = text.split(/\s+(?=\b(?:and|but|or|so)\b\s+)/i);
      parts = conjSplit.length > 1 ? conjSplit : [text];
    }
  }
  parts = parts.map(s => s.trim()).filter(Boolean);

  // Merge orphan conjunction fragments back to their predecessor.
  // e.g. ["agent outputs", "or stored data."] -> ["agent outputs or stored data."]
  // A fragment is an orphan if it starts with a conjunction and is short, so 40
  // characters or fewer.
  const _CONJ = /^(and|but|or|so|yet|nor|for)\b/i;
  const merged = [];
  for (const part of parts) {
    if (merged.length > 0 && _CONJ.test(part) && part.length <= 40) {
      merged[merged.length - 1] = merged[merged.length - 1] + ' ' + part;
    } else {
      merged.push(part);
    }
  }
  return merged;
}

// Only a comma with a space after it is a place to split, so the comma in
// "1,250" is left alone, and each part keeps its own comma.
function splitOnCommas(text) {
  return packClauses(text.split(/(?<=,)(?=\s)/));
}

// Pack neighbouring clauses together up to the target length, so a run of
// short ones ("red; green; blue;") is one piece and not three tiny chunks.
// Semicolon clauses were each emitted alone before, whatever their length.
function packClauses(parts) {
  const results = [];
  let   current = "";

  for (const part of parts) {
    const candidate = current + part;
    if (candidate.trim().length > TARGET_CHARS && current.trim()) {
      results.push(current.trim());
      current = part;
    } else {
      current = candidate;
    }
  }
  if (current.trim()) results.push(current.trim());
  return results;
}


// ---------------------------------------------------------------------------
// Hard word-boundary split (last resort)
// ---------------------------------------------------------------------------

function hardSplitAtWord(text, maxLen) {
  // A tail shorter than this sounds like a detached fragment when spoken alone,
  // so a split that would create one is rebalanced toward the midpoint instead.
  const MIN_TAIL = 30;
  const parts = [];
  let   rem   = text;

  while (rem.length > maxLen) {
    // The last clause boundary in the window, which is punctuation with a
    // space after it. Requiring the space keeps "3.14", a dotted address and
    // "minutes.|/LIST|" in one piece, where any stop at all used to do.
    let splitAt = -1;
    for (let i = Math.min(maxLen, rem.length - 1); i > MIN_CHARS; i--) {
      if (rem[i] === ' ' && /[.!?,;:]/.test(rem[i - 1])) { splitAt = i; break; }
    }
    if (splitAt < 0) {
      // Otherwise the last space, and when there is no space at all, the full
      // window. The fallback here used to be MIN_CHARS, so a long address or
      // file path with no space in it came out as dozens of 6-character
      // chunks.
      const space = rem.lastIndexOf(' ', maxLen);
      splitAt = space > MIN_CHARS ? space : _cutClearOfMarker(rem, maxLen);
    }
    // Tail guard: greedy splitting can leave a tiny orphan ("correct.") as the
    // final piece. When the remainder would be under MIN_TAIL, move the split
    // to a word boundary near the midpoint so both halves are natural phrases.
    const tail = rem.length - splitAt;
    if (tail > 0 && tail < MIN_TAIL) {
      const mid  = Math.floor(rem.length / 2);
      const back = rem.lastIndexOf(' ', mid);
      const fwd  = rem.indexOf(' ', mid);
      const cand = (back >= MIN_CHARS) ? back : (fwd > 0 && fwd <= maxLen ? fwd : splitAt);
      if (cand >= MIN_CHARS && cand <= maxLen) splitAt = cand;
    }
    parts.push(rem.substring(0, splitAt).trim());
    rem = rem.substring(splitAt).trim();
  }
  if (rem) parts.push(rem);
  return parts;
}

// Where to cut a run that has no space in it. That is the full window, unless
// the cut would land inside a layout marker, in which case it moves to the
// marker's edge so the tag is not broken into two pieces of stray text.
function _cutClearOfMarker(text, at) {
  const rx = /\|\/?[A-Z0-9]+\|/g;
  let m;
  while ((m = rx.exec(text)) && m.index < at) {
    const end = m.index + m[0].length;
    if (at < end) return m.index > MIN_CHARS ? m.index : end;
  }
  return at;
}


// ---------------------------------------------------------------------------
// Readability gate
// ---------------------------------------------------------------------------

// Kept under its old name. It used to ask for 6 characters and two letters,
// which is what dropped short clauses and every number; it now asks only
// whether there is something to say.
function isReadable(s) {
  return hasContent(s);
}


// ---------------------------------------------------------------------------
// Line breaks in typed and pasted text
// ---------------------------------------------------------------------------

// Turn plain text into the chunker's input, keeping its lines. A blank line
// is a paragraph break and a single newline is a line break, and both become
// |BREAK| so a chunk ends where the line does. Every newline used to be
// collapsed to a space, so a heading ran into its paragraph and the lines of
// a verse ran into each other.
//
// The one exception is text that something else wrapped, such as a paragraph
// copied out of a PDF or an email, where every line ends mid-phrase. Breaking
// there would stop the voice at each line end, so a long line that does not
// end in punctuation is joined to the next one, and a word hyphenated across
// the break is closed up. Long means 60 characters, or 40 when the next line
// carries on in lower case, since verse and headings are shorter than that or
// end in punctuation.
function customTextToMarked(text) {
  const paragraphs = String(text == null ? '' : text).replace(/\r\n?/g, '\n').split(/\n[ \t]*\n/);
  const out = [];
  for (const para of paragraphs) {
    const lines = para.split('\n').map(l => l.replace(/[ \t ]+/g, ' ').trim()).filter(Boolean);
    let current = '';
    let lastLine = '';
    for (const line of lines) {
      const unfinished = lastLine && !/[.!?:;,]["'\u201D\u2019)\]]*$/.test(lastLine);
      const wrapped = unfinished &&
        (lastLine.length >= 60 || (lastLine.length >= 40 && /^[a-z]/.test(line)));
      if (wrapped && /[A-Za-z]-$/.test(current) && /^[a-z]/.test(line)) {
        current = current.slice(0, -1) + line;
      } else if (wrapped) {
        current += ' ' + line;
      } else {
        if (current) out.push(current);
        current = line;
      }
      lastLine = line;
    }
    if (current) out.push(current);
  }
  return out.join(' |BREAK| ');
}


// =============================================================================
// TEXT EXTRACTION
// =============================================================================
//
// Semantic markers injected into the text stream so server.py can apply
// the correct prosodic treatment to each content type:
//
//   |H1| ... |/H1|            a top-level heading, longest pause either side
//   |H2| ... |/H2|            a section heading
//   |H3| ... |/H3|            a sub-section heading
//   |BOLD| ... |/BOLD|        bold or strong emphasis
//   |ITALIC| ... |/ITALIC|    italic or soft emphasis
//   |CODE| ... |/CODE|        an inline code term
//   |CALLOUT| ... |/CALLOUT|  a note, warning, tip or blockquote
//   |CAPTION| ... |/CAPTION|  a figure or table caption
//   |LIST| ... |/LIST|        one item of a bulleted or numbered list
//   |BREAK|                   a paragraph or block boundary
//
// The server strips the markers once it has used them, so they never reach XTTS.
//
// |LIST| is the one marker I close tight against its text, with no space in
// front of the closing tag. That keeps the tag and the last word as a single
// token, so none of the length-based splitters below can cut between them and
// leave "|/LIST|" as a chunk with nothing to say. It also makes the emitted
// shape canonical, which is what the server's decoder and the tests read.
// =============================================================================

// ---------------------------------------------------------------------------
// Cleaning what the page walker found
// ---------------------------------------------------------------------------
//
// The walker runs inside the page, where none of this file exists, so it only
// marks what it found and the judgement is made here, where it can be tested
// without a DOM. It puts the text of every link, and of anything acting as a
// link or a button, between \u0001 and \u0002, and it hands a plain-text
// document back whole behind \u0003.

// Labels that belong to the page's controls and not to the writing: the words
// on a pager, a "read more" link, a jump back to the top. Each pattern is the
// whole label, in lower case with the punctuation gone.
const _NAV_LABELS = [
  'next', 'previous', 'prev',
  '(?:next|previous|prev) (?:page|post|article|chapter|section|entry)',
  '(?:older|newer)(?: (?:posts|entries|articles|comments))?',
  '(?:read|learn|see|show|view|load) more',
  'continue reading',
  '(?:back|return|scroll|jump) to (?:the )?top',
  'skip to (?:the )?(?:main )?content', 'skip navigation',
  'external link',
  '(?:this )?(?:link )?opens? in (?:a )?new (?:tab|window)',
  'links? to (?:an? )?(?:external site|pdf|document|page)',
];
const _RX_NAV_LABEL = new RegExp('^(?:' + _NAV_LABELS.join('|') + ')$');

// What a page adds inside a link for screen readers, as Canvas does with
// "(Links to an external site.)". It is never part of the sentence, so it comes
// out of link text wherever the link sits.
const _RX_LINK_ASIDE = /[(\[]?\s*(?:links? to (?:an? )?(?:external site|pdf|document|page)|(?:this )?(?:link )?opens? in (?:a )?new (?:tab|window)|external link)\.?\s*[)\]]?/gi;

// Is this text, taken whole, one of those labels? Arrows, chevrons and
// punctuation round it do not count, so "« Previous" and "Read more..." are
// labels, and "Next, we train the model" is not.
function isNavLabel(text) {
  const t = String(text == null ? '' : text)
    .replace(/\|\/?[A-Z0-9]+\|/g, ' ')
    .replace(/[^A-Za-z0-9]+/g, ' ')
    .trim().toLowerCase();
  return t ? _RX_NAV_LABEL.test(t) : false;
}

// Take the page furniture out of what the walker returned.
//
// "Next", "Previous", "read more" and the rest used to be deleted by regexes
// run over the whole text, so they went from ordinary sentences as well:
// "Next, we train the model. Previous work used..." lost both words. They are
// worth skipping only as the label on a control, so a label is dropped when it
// is the whole text of a link or button and stands apart from the prose, or
// when it is a whole block by itself, and is never touched inside a sentence.
function cleanPageText(raw) {
  let text = String(raw == null ? '' : raw);

  // A plain-text document keeps its lines, the way Custom Text does.
  if (text.charAt(0) === '\u0003') text = customTextToMarked(text.slice(1));

  // Link text. The screen-reader aside goes, and a link that is nothing but a
  // label is held back as \u0004 until its surroundings are known.
  const labels = [];
  text = text.replace(/\u0001([^\u0001\u0002]*)\u0002/g, (whole, inner) => {
    const kept = inner.replace(_RX_LINK_ASIDE, ' ');
    if (!isNavLabel(kept)) return kept;
    labels.push(kept.replace(/\|BREAK\|/g, ' '));
    return inner.includes('|BREAK|') ? ' |BREAK| \u0004 |BREAK| ' : '\u0004';
  }).replace(/[\u0001\u0002\u0003]/g, '');

  // A label link is dropped when it stands apart: nothing follows it in its
  // block, and what comes before it is nothing or a finished sentence, as in
  // a teaser that ends "... Read more". One in the middle of a sentence
  // ("click Next to continue") belongs to the sentence and stays.
  const finished = /(?:[.!?:\u2026]|\.\.\.)["'\u201D\u2019)\]]*\s*$/;
  let n = 0;
  text = text.split('|BREAK|').map(block => {
    if (block.indexOf('\u0004') < 0) return block;
    const bits = block.split('\u0004');
    let out = bits[0];
    for (let i = 1; i < bits.length; i++) {
      const label  = labels[n++] || '';
      const before = bits.slice(0, i).join(' ').replace(/\|\/?[A-Z0-9]+\|/g, ' ');
      const after  = bits.slice(i).join(' ');
      const apart  = !hasContent(after) && (!hasContent(before) || finished.test(before));
      out += (apart ? ' ' : label) + bits[i];
    }
    return out;
  })
    // A block that is nothing but a label goes too, whatever element it was.
    .filter(block => !isNavLabel(block))
    .join('|BREAK|');

  return text
    // Web addresses are noise read aloud. The stop that ends the sentence is
    // not part of the address, though, and it used to be taken along with it,
    // which ran the sentence into the next one.
    .replace(/(\s*)https?:\/\/[^\s|]+/g, (whole, lead) => {
      const tail = (whole.match(/[.,;:!?)\]'"]+$/) || [''])[0];
      return /^[.,;:!?]/.test(tail) ? tail : lead + tail;
    })
    .replace(/\s*\(\s*\)/g, '')
    .replace(/\[\d+\]/g, '')
    // Symbol conversion
    .replace(/!=/g,' not equals ')
    .replace(/>=/g,' greater than or equal to ')
    .replace(/<=/g,' less than or equal to ')
    .replace(/->/g,' arrow ')
    .replace(/=>/g,' arrow ')
    .replace(/&&/g,' and ')
    // A wrapper left with nothing in it, such as a bullet that held one label.
    .replace(/\|(H[1-3]|BOLD|ITALIC|CODE|CALLOUT|CAPTION|LIST)\|[\s.]*\|\/\1\|/g, ' ')
    // Collapse
    .replace(/(\|BREAK\|\s*){2,}/g, ' |BREAK| ')
    // Keep the closing list marker tight against its item, so the tag
    // and the last word stay one token and no length-based split can
    // strand the tag in a chunk of its own.
    .replace(/\s+\|\/LIST\|/g, '|/LIST|')
    .replace(/\s+/g, ' ')
    // A break with nothing before it or after it separates nothing.
    .replace(/^(?:\s*\|BREAK\|)+|(?:\|BREAK\|\s*)+$/g, '')
    .trim();
}

// Chrome shows a PDF in a plugin that no extension can read into, so the best
// the popup can do is recognise one and say what to do instead.
function looksLikePdfUrl(url) {
  return /\.pdf(?:$|[?#])/i.test(String(url || ''));
}

// Wrap a text-extraction promise so it can never hang the UI. If the page does
// not respond in time (navigating, heavy iframes, a frame that never returns),
// resolve with a sentinel the caller turns into a clear message instead of
// leaving the popup stuck on a progress line forever.
function _withExtractionTimeout(promise, ms = 8000) {
  return Promise.race([
    promise,
    new Promise(res => setTimeout(() => res("__KAM_TIMEOUT__"), ms)),
  ]);
}

function getSelectedText(tab) {
  return new Promise((resolve) => {
    _readerTabs(tab, (tabs) => {
      // Guard: extension pages (the KAM dashboard), chrome:// and the Web Store
      // can't be scripted. Without this the call fails with an opaque
      // "Cannot access contents of url" and the button appears to do nothing.
      const _u = (tabs && tabs[0] && tabs[0].url) || "";
      if (!tabs || !tabs[0] || /^(chrome|edge|about|chrome-extension|devtools):/.test(_u)
          || _u.includes("chromewebstore.google.com")) {
        resolve("__KAM_NO_PAGE__");
        return;
      }
      chrome.scripting.executeScript(
        {
          target: { tabId: tabs[0].id, allFrames: true },
          func: () => {
            // Only true kix-canvas Google Docs genuinely can't be selected.
            if (document.querySelector('.kix-canvas-tile-content')) {
              return '__KAM_CANVAS_DOC__';
            }
            // A PDF in Chrome's viewer is one embed with no text in the DOM,
            // and a selection made inside it never reaches this document. I say
            // so, since "select some text" is no help to someone who just did.
            const _pdfEmbed = document.querySelector(
              'embed[type="application/pdf"], embed[type="application/x-google-chrome-pdf"]');
            if (String(document.contentType || '').toLowerCase() === 'application/pdf' ||
                (_pdfEmbed && document.body &&
                 String(document.body.innerText || '').trim().length < 20)) {
              return '__KAM_PDF__';
            }

            const sel = window.getSelection();
            if (!sel || sel.rangeCount === 0) return '';

            const range = sel.getRangeAt(0);

            // Walk every node in the selection using TreeWalker.
            // Inject |BREAK| whenever we cross a block-level element boundary.
            // This works on any site whatever its DOM structure.
            const BLOCK_TAGS = new Set(['p','li','dt','dd','div','section','article',
              'blockquote','h1','h2','h3','h4','h5','h6','td','th','tr',
              'header','footer','main','aside','pre','ul','ol','dl']);
            const INLINE_TAGS = {
              'b': ['|BOLD|','|/BOLD|'], 'strong': ['|BOLD|','|/BOLD|'],
              'i': ['|ITALIC|','|/ITALIC|'], 'em': ['|ITALIC|','|/ITALIC|'],
              'code': ['|CODE|','|/CODE|'], 'kbd': ['|CODE|','|/CODE|'],
              'mark': ['|BOLD|','|/BOLD|']
            };
            const SKIP_TAGS = new Set(['script','style','noscript','template',
              'svg','textarea','select']);

            const parts = [];
            let   lastWasBlock = false;

            // True if the running output ends with whitespace or an open
            // bracket, meaning a separator is already there and I mustn't add one.
            function _endsClean() {
              for (let k = parts.length - 1; k >= 0; k--) {
                const p = parts[k];
                if (!p) continue;
                const last = p[p.length - 1];
                return /\s/.test(last) || last === '(' || last === '[' || last === undefined;
              }
              return true;   // nothing yet
            }
            // True if a string starts with whitespace or closing punctuation,
            // meaning a separator already exists on that side.
            function _startsClean(s) {
              if (!s) return true;
              return /^[\s.,;:!?)\]}'"]/.test(s);
            }

            // Push a text fragment, preserving its exact content. Inserts a
            // single space only when neither the running output nor the new text
            // already provides a separator, so existing punctuation and
            // spacing (". ", ")", "training. However") is never lost or merged.
            function _pushText(t) {
              if (!t) return;
              if (parts.length && !_endsClean() && !_startsClean(t)) {
                parts.push(' ');
              }
              parts.push(t);
              lastWasBlock = false;
            }

            // Simpler approach: clone the fragment and walk it
            const frag = range.cloneContents();

            function walk(node) {
              if (node.nodeType === Node.TEXT_NODE) {
                // Preserve the node's text verbatim (including any leading/
                // trailing whitespace the page author put there); only collapse
                // runs of whitespace, never strip edge characters.
                const t = node.textContent.replace(/[ \t\r\n]+/g, ' ');
                _pushText(t);
                return;
              }
              const tag = node.nodeName.toLowerCase();

              // --- Equations ---
              // MathML / MathJax / KaTeX render equations as nested glyph
              // elements (MathJax often into <svg>), so naively walking them
              // gives garbled text, and the hidden source copy then gets read a
              // second time. So I intercept the container and emit clean source
              // just once.
              const isMathRoot = (
                tag === 'math' || tag === 'mjx-container' ||
                (node.classList && (node.classList.contains('katex') ||
                                    node.classList.contains('MathJax')))
              );
              if (isMathRoot) {
                let eq = '';
                try {
                  // 1. The LaTeX source, which is the most faithful, and both
                  //    MathJax and KaTeX embed it.
                  const ann = node.querySelector &&
                    node.querySelector('annotation[encoding="application/x-tex"]');
                  if (ann && ann.textContent.trim()) eq = ann.textContent.trim();
                  // 2. Accessibility text set by the renderer.
                  if (!eq && node.getAttribute) {
                    eq = (node.getAttribute('alttext') ||
                          node.getAttribute('aria-label') || '').trim();
                  }
                  // 3. Fall back to visible text, minus the hidden source copy.
                  if (!eq && node.cloneNode) {
                    const c = node.cloneNode(true);
                    c.querySelectorAll(
                      'annotation, annotation-xml, mjx-assistive-mml, .katex-mathml'
                    ).forEach(n => n.remove());
                    eq = (c.textContent || '').replace(/\s+/g, ' ').trim();
                  }
                } catch (e) { /* fall through to empty */ }
                if (eq) _pushText(' ' + eq + ' ');
                return;   // never walk into the equation's internals
              }
              // Skip hidden equation-source duplicates encountered on their own.
              if (['annotation', 'annotation-xml'].includes(tag)) return;
              if (node.classList && (node.classList.contains('katex-mathml') ||
                  tag === 'mjx-assistive-mml')) return;

              // What is in the markup but not on the page. A selection is
              // cloned whole, so Ctrl+A then Speak used to read out inline
              // script, CSS and JSON-LD, the options of every dropdown, and on
              // Wikipedia the "[edit]" links and reference numbers. This sits
              // after the maths check on purpose, because MathJax draws its
              // equations in an <svg> that has to be read from the container.
              if (SKIP_TAGS.has(tag)) return;
              if (node.classList && (node.classList.contains('mw-editsection') ||
                  (tag === 'sup' && node.classList.contains('reference')))) return;

              // Heading → inject level marker + breaks
              if (['h1','h2','h3','h4','h5','h6'].includes(tag)) {
                if (!lastWasBlock) parts.push(' |BREAK| ');
                const level = tag === 'h1' ? 'H1' : tag === 'h2' ? 'H2' : 'H3';
                parts.push('|' + level + '|');
                Array.from(node.childNodes).forEach(walk);
                parts.push('|/' + level + '| |BREAK| ');
                lastWasBlock = true;
                return;
              }

              // Inline semantic markers
              if (INLINE_TAGS[tag]) {
                parts.push(' ' + INLINE_TAGS[tag][0]);
                Array.from(node.childNodes).forEach(walk);
                parts.push(INLINE_TAGS[tag][1] + ' ');
                return;
              }

              // A list item, wrapped so the server knows the chunk was a bullet
              // and can give it the gap between items rather than the gap
              // between sentences. Nothing else carries that fact: once the
              // marker is stripped, "Boil the water." from a bullet and the
              // same words from a paragraph are the same string.
              //
              // I only wrap an item that has something to say in it, so an empty
              // bullet does not arrive as a pair of tags round nothing. A letter
              // or a digit is enough, the same test the chunker uses; this
              // asked for two letters before, so a bullet reading "2020" or "A"
              // lost its marker and with it the pause between items.
              if (tag === 'li') {
                if (parts.length > 0 && !lastWasBlock) parts.push(' |BREAK| ');
                const _at = parts.length;
                Array.from(node.childNodes).forEach(walk);
                const _inner = parts.slice(_at).join('').replace(/\|\/?[A-Z0-9]+\|/g, '');
                if (/[A-Za-z0-9]/.test(_inner)) {
                  parts.splice(_at, 0, '|LIST| ');
                  parts.push('|/LIST|');
                }
                parts.push(' |BREAK| ');
                lastWasBlock = true;
                return;
              }

              // Block elements → inject |BREAK| before and after
              if (BLOCK_TAGS.has(tag)) {
                if (parts.length > 0 && !lastWasBlock) parts.push(' |BREAK| ');
                Array.from(node.childNodes).forEach(walk);
                parts.push(' |BREAK| ');
                lastWasBlock = true;
                return;
              }

              // Everything else, so span, a and the rest, gets recursed into.
              // Punctuation sitting outside the element, like "(<a>2024</a>)" or
              // "<a>x</a>. Next", is
              // in sibling text nodes and is preserved by _pushText's boundary
              // logic, so citations and sentence-final stops survive intact.
              Array.from(node.childNodes).forEach(walk);
            }

            Array.from(frag.childNodes).forEach(walk);

            let text = parts.join('')
              .replace(/(\|BREAK\|\s*){2,}/g, ' |BREAK| ')
              // Pull the closing list marker tight against its item, so the tag
              // and the last word stay one token and no length-based split can
              // strand the tag in a chunk of its own.
              .replace(/\s+\|\/LIST\|/g, '|/LIST|')
              // Citation numbers like [1], as Read Page takes them out too.
              .replace(/\[\d+\]/g, '')
              // Collapse only spaces and tabs, not punctuation, so ". However"
              // survives.
              .replace(/[ \t]{2,}/g, ' ')
              .replace(/\s+/g, ' ')
              .trim();

            // If we got nothing useful from DOM walk (e.g. plain text selection),
            // fall back to raw selection string and split on newlines. The test
            // is for something to say and not for length, because a selection
            // of "No!" or "2020" is short and still perfectly good.
            if (!/[A-Za-z0-9]/.test(text.replace(/\|\/?[A-Z0-9]+\|/g, ''))) {
              text = sel.toString()
                .replace(/\[\d+\]/g, '')
                .replace(/\r\n|\n/g, ' |BREAK| ')
                .replace(/(\|BREAK\|\s*){2,}/g, ' |BREAK| ')
                .replace(/\s+/g, ' ')
                .trim();
            }

            return text;
          }
        },
        (results) => {
          const pdfUrl = looksLikePdfUrl(_u);
          if (chrome.runtime.lastError) {
            if (pdfUrl) { resolve("__KAM_PDF__"); return; }
            showError("Can't read this page: " + chrome.runtime.lastError.message
                      + " — try reloading the page, or use Custom Text.");
            resolve("");
            return;
          }
          const vals = (results || []).map(r => r && r.result);
          const real = v => typeof v === 'string' && v && v !== '__KAM_CANVAS_DOC__' && v !== '__KAM_PDF__';
          // A PDF is only reported when no frame had a selection to offer, so a
          // page that merely embeds one is still read as a page.
          if (!vals.some(real) && (pdfUrl || vals.includes('__KAM_PDF__'))) {
            resolve("__KAM_PDF__");
            return;
          }
          if (vals.includes('__KAM_CANVAS_DOC__') && !vals.some(real)) {
            showError("This looks like a Google Doc — its text is drawn on a canvas and can't be selected directly. Copy the text (Ctrl+C), switch to the Custom Text tab, and paste it there to read it.");
            resolve("");
            return;
          }
          // The frame with the selection is the one returning non-empty text.
          const r = vals.filter(real)
                        .sort((a, b) => b.length - a.length)[0] || "";
          if (!r) showError("Select some text on the page first, then press Speak.");
          resolve(r);
        }
      );
    });
  });
}

function getPageText(tab) {
  return new Promise((resolve) => {
    _readerTabs(tab, (tabs) => {
      // Guard: extension pages (the KAM dashboard), chrome:// and the Web Store
      // can't be scripted. Without this the call fails with an opaque
      // "Cannot access contents of url" and the button appears to do nothing.
      const _u = (tabs && tabs[0] && tabs[0].url) || "";
      if (!tabs || !tabs[0] || /^(chrome|edge|about|chrome-extension|devtools):/.test(_u)
          || _u.includes("chromewebstore.google.com")) {
        resolve("__KAM_NO_PAGE__");
        return;
      }
      chrome.scripting.executeScript(
        {
          target: { tabId: tabs[0].id, allFrames: true },
          func: () => {
            // Only true canvas-rendered Google Docs (kix) genuinely can't be
            // read; signal that. Plain skills.google / iframe content is read
            // normally below. I no longer block by hostname, since a lot of
            // those pages keep the real text in an iframe that allFrames reaches.
            if (document.querySelector('.kix-canvas-tile-content')) {
              return '__KAM_CANVAS_DOC__';
            }
            // Chrome shows a PDF through a plugin, so the document holds one
            // embed and no text at all. I say so, since otherwise the walk
            // comes back empty and the popup reports a failure it cannot explain.
            const _pdfEmbed = document.querySelector(
              'embed[type="application/pdf"], embed[type="application/x-google-chrome-pdf"]');
            if (String(document.contentType || '').toLowerCase() === 'application/pdf' ||
                (_pdfEmbed && document.body &&
                 String(document.body.innerText || '').trim().length < 20)) {
              return '__KAM_PDF__';
            }

            // Block tags that create chunk boundaries
            const BLOCK = new Set(['p','li','dt','dd','blockquote','pre',
              'h1','h2','h3','h4','h5','h6','td','th','tr','section',
              'article','aside','div','main','header','footer','form',
              'details','summary']);
            // Never read, wherever they sit.
            const SKIP  = new Set(['script','style','noscript','template','nav',
              'iframe','svg','img','picture','video','audio','canvas','object',
              'embed','button','input','select','textarea','dialog']);
            // Page furniture. Whether it is read depends on where it sits, which
            // shouldSkip works out. These four used to be in SKIP outright, so an
            // article's own <header> lost its title, byline and opening line, and
            // a page wrapped in one <form>, as ASP.NET and SharePoint pages are,
            // gave no text at all.
            const FURNITURE = new Set(['header','footer','aside','form']);
            const ROLE_FURNITURE = { banner: 'header', contentinfo: 'footer', complementary: 'aside' };
            const SKIP_ROLES = new Set(['navigation','dialog','alertdialog','search']);
            // Whole class names. This used to be one regex with \b either side,
            // and a hyphen is a word boundary, so "post-header", "has-sidebar",
            // "menu-item-has-children", "modal-open" and "ad-free-content" all
            // matched and the wrapper holding the article was skipped with them.
            const SKIP_CLASSES = new Set(['nav','navbar','navigation','menu','sidebar',
              'ad','ads','advert','advertisement','cookie','cookies','popup','modal',
              'sr-only','visually-hidden','visuallyhidden','screen-reader-text',
              'screenreader-only','screen-reader-only','skip-link',
              'breadcrumb','breadcrumbs','pagination','pager','page-numbers',
              'nav-links','post-navigation','toc',
              // Wikipedia: edit links, the reference list, navigation boxes,
              // categories, and whatever it marks as not for print.
              'mw-editsection','reflist','references','navbox','catlinks',
              'mw-jump-link','noprint']);
            const SKIP_IDS = new Set(['sitesub','toc']);
            const INLINE_MAP = {
              'b':'BOLD','strong':'BOLD','em':'ITALIC','i':'ITALIC',
              'code':'CODE','kbd':'CODE','mark':'BOLD','samp':'CODE'
            };

            const body = document.body;
            const textLen = el => (el ? String(el.textContent || '').length : 0);
            const all = sel => {
              try {
                if (typeof document.querySelectorAll === 'function') {
                  return Array.from(document.querySelectorAll(sel));
                }
                const one = document.querySelector(sel);
                return one ? [one] : [];
              } catch (e) { return []; }
            };
            const biggest = list => list.reduce(
              (best, el) => (!best || textLen(el) > textLen(best)) ? el : best, null);
            const bodyLen = textLen(body) || 1;
            // Enough text to be the content and not a stub.
            const substantial = el => !!el && (textLen(el) >= 200 || textLen(el) * 4 >= bodyLen);

            // Find the best content root. The first <article> used to win, so a
            // feed, a blog index or a forum thread read its first card and
            // stopped. <main> is the page's own statement of where the content
            // is, so it goes first. Failing that, one article that holds most of
            // the article text is the article; several of similar size are a
            // feed, and the element that holds them all is the content.
            function pickRoot() {
              const main = biggest(all("main, [role='main']"));
              if (substantial(main)) return main;
              const articles = all('article').filter(a =>
                !(a.parentElement && a.parentElement.closest && a.parentElement.closest('article')));
              if (articles.length === 1 && substantial(articles[0])) return articles[0];
              if (articles.length > 1) {
                const top   = biggest(articles);
                const total = articles.reduce((n, a) => n + textLen(a), 0);
                if (substantial(top) && textLen(top) * 10 >= total * 6) return top;
                for (let p = articles[0].parentElement; p; p = p.parentElement) {
                  if (articles.every(a => p.contains(a))) return p;
                }
              }
              for (const sel of ['#content', '.content', '#main']) {
                const el = biggest(all(sel));
                if (substantial(el)) return el;
              }
              return body;
            }
            const root = pickRoot() || body;
            // With no content root to go by, the walk covers the whole page and
            // has to leave the page's own header, footer and sidebars out.
            const rootIsPage = !root || root === body || root === document.documentElement;

            const parts = [];
            let lastWasBlock = false;
            let linkDepth = 0;

            const classTokens = el =>
              String((el.getAttribute && el.getAttribute('class')) || '')
                .toLowerCase().split(/\s+/).filter(Boolean);

            // checkVisibility answers for the element and everything above it in
            // one call. It says no for an element with no box of its own, which
            // includes display:contents wrappers whose children are perfectly
            // visible, so a no is confirmed against the computed style.
            function hiddenByCss(el) {
              if (typeof el.checkVisibility === 'function') {
                try {
                  if (el.checkVisibility({ checkVisibilityCSS: true, visibilityProperty: true })) return false;
                } catch (e) { /* older signature; the style check below decides */ }
              }
              let style = null;
              try { style = window.getComputedStyle(el); } catch (e) {}
              if (!style) return false;
              return style.display === 'none' || style.visibility === 'hidden' ||
                     style.visibility === 'collapse' || style.contentVisibility === 'hidden';
            }

            const holdsH1 = el => !!(el.querySelector && el.querySelector('h1'));
            const wrapsPage = el => textLen(el) * 2 >= bodyLen;
            // Mostly links: a list of related stories, a share bar, a tag cloud.
            function linkHeavy(el) {
              const total = textLen(el);
              if (!total || !el.querySelectorAll) return false;
              let linked = 0;
              try { el.querySelectorAll('a').forEach(a => { linked += textLen(a); }); } catch (e) {}
              return linked * 10 >= total * 6;
            }

            function shouldSkip(el, tag) {
              if (SKIP.has(tag)) return true;
              if (el.getAttribute('aria-hidden') === 'true') return true;
              if (el.getAttribute('hidden') !== null) return true;
              const role = String(el.getAttribute('role') || '').toLowerCase();
              if (SKIP_ROLES.has(role)) return true;
              if (SKIP_IDS.has(String(el.id || '').toLowerCase())) return true;
              const tokens = classTokens(el);
              if (tokens.some(t => SKIP_CLASSES.has(t))) return true;
              if (tag === 'sup' && tokens.includes('reference')) return true;

              // Header, footer, aside and form, by tag, role or class name.
              // Inside a content root they are part of the content: the header
              // of an article is its title and byline. Out on the page they are
              // furniture, unless one holds the h1, or is a form wrapped round
              // the whole page.
              const kind = FURNITURE.has(tag) ? tag : (ROLE_FURNITURE[role] ||
                (tokens.includes('header') ? 'header' : tokens.includes('footer') ? 'footer' : ''));
              if (kind) {
                if (rootIsPage) {
                  if (!holdsH1(el) && !(kind === 'form' && wrapsPage(el))) return true;
                } else if ((kind === 'aside' || kind === 'footer') && linkHeavy(el)) {
                  return true;
                }
              }
              return hiddenByCss(el);
            }

            // Equations. MathML, MathJax and KaTeX draw one as nested glyph
            // elements and keep the source in a hidden copy, so walking in read
            // the glyphs and then the source, the same equation twice. This is
            // the selection walker's handling, repeated here because the two
            // functions run inside the page and cannot share code. It gives the
            // source once for an equation, an empty string for a hidden copy met
            // on its own, and null for anything that is not maths.
            function mathSource(node, tag) {
              const cl  = node.classList;
              const has = c => !!(cl && cl.contains(c));
              if (tag === 'annotation' || tag === 'annotation-xml' ||
                  tag === 'mjx-assistive-mml' || has('katex-mathml')) return '';
              if (!(tag === 'math' || tag === 'mjx-container' || has('katex') || has('MathJax'))) return null;
              let eq = '';
              try {
                const ann = node.querySelector &&
                  node.querySelector('annotation[encoding="application/x-tex"]');
                if (ann && ann.textContent.trim()) eq = ann.textContent.trim();
                if (!eq && node.getAttribute) {
                  eq = (node.getAttribute('alttext') || node.getAttribute('aria-label') || '').trim();
                }
                if (!eq && node.cloneNode) {
                  const c = node.cloneNode(true);
                  c.querySelectorAll('annotation, annotation-xml, mjx-assistive-mml, .katex-mathml')
                    .forEach(n => n.remove());
                  eq = (c.textContent || '').replace(/\s+/g, ' ').trim();
                }
              } catch (e) { /* fall through to empty */ }
              return eq;
            }

            // The text of a heading or an inline element, without what the walk
            // would skip. textContent was used for this, which reads hidden
            // children too, so a Wikipedia heading came out as "History[edit]",
            // and it reads a <br> as nothing, which glued the two lines of a
            // title together.
            function flatText(node) {
              if (node.nodeType === Node.TEXT_NODE) return node.textContent || '';
              if (node.nodeType !== Node.ELEMENT_NODE) return '';
              const tag = node.tagName.toLowerCase();
              if (tag === 'br') return ' ';
              if (shouldSkip(node, tag)) return '';
              const eq = mathSource(node, tag);
              if (eq !== null) return ' ' + eq + ' ';
              let s = '';
              Array.from(node.childNodes).forEach(c => { s += flatText(c); });
              return s;
            }

            function isCssBlock(el) {
              try {
                return /^(block|flex|grid|list-item|table|flow-root)/.test(
                  String(window.getComputedStyle(el).display || ''));
              } catch (e) { return false; }
            }
            // A space where two pieces of text meet, unless one is there already.
            const gap = () => {
              if (parts.length && !lastWasBlock && !/\s$/.test(parts[parts.length - 1])) parts.push(' ');
            };
            // The text of a link, or of something acting as one, goes between
            // two control characters. The popup decides from that whether it is
            // a label such as "Next" or "Read more" and leaves it out, which it
            // can only do knowing the words came from a control and not from a
            // sentence.
            const openLink  = on => { if (on && linkDepth++ === 0) parts.push('\u0001'); };
            const closeLink = on => { if (on && --linkDepth === 0) parts.push('\u0002'); };

            function walk(node) {
              if (node.nodeType === Node.TEXT_NODE) {
                const t = (node.textContent || '').replace(/\s+/g, ' ');
                if (t.trim()) { parts.push(t); lastWasBlock = false; }
                // Whitespace between two elements is still a space. Dropping it
                // is what joined neighbouring links and spans into one word.
                else if (t) gap();
                return;
              }
              if (node.nodeType !== Node.ELEMENT_NODE) return;

              const tag = node.tagName.toLowerCase();

              // A <br> ends a line, so the line of verse or of an address
              // before it is a chunk of its own. It used to be nothing at all,
              // which gave "Roses are redViolets are blue".
              if (tag === 'br') {
                if (!lastWasBlock && parts.length > 0) { parts.push(' |BREAK| '); lastWasBlock = true; }
                return;
              }
              if (shouldSkip(node, tag)) return;

              const eq = mathSource(node, tag);
              if (eq !== null) {
                if (eq) { parts.push(' ' + eq + ' '); lastWasBlock = false; }
                return;   // never walk into the equation's internals
              }

              // Headings
              if (['h1','h2','h3','h4','h5','h6'].includes(tag)) {
                const t = flatText(node).replace(/\s+/g, ' ').trim();
                if (!t) return;
                if (!lastWasBlock) parts.push(' |BREAK| ');
                const level = tag === 'h1' ? 'H1' : tag === 'h2' ? 'H2' : 'H3';
                parts.push('|' + level + '|' + t + '.|/' + level + '| |BREAK| ');
                lastWasBlock = true;
                return;
              }

              // Inline semantic markers
              if (INLINE_MAP[tag]) {
                const t = flatText(node).replace(/\s+/g, ' ').trim();
                if (t) {
                  parts.push(' |' + INLINE_MAP[tag] + '|' + t + '|/' + INLINE_MAP[tag] + '| ');
                  lastWasBlock = false;
                }
                return;
              }

              // Code blocks get skipped, since they're too noisy for TTS
              if (tag === 'pre') return;

              // A closed <details> shows its summary and nothing else, so that
              // is all there is to read.
              const kids = (tag === 'details' && !node.open && node.getAttribute('open') === null)
                ? Array.from(node.childNodes).filter(c =>
                    c.nodeType === Node.ELEMENT_NODE && c.tagName.toLowerCase() === 'summary')
                : Array.from(node.childNodes);

              const role = String(node.getAttribute('role') || '').toLowerCase();
              const isLink = tag === 'a' || role === 'button' || role === 'link' ||
                             role === 'menuitem' || role === 'tab';

              // A list item, wrapped so the server can pace it as a bullet.
              // See the note on the selection walker above: an item with
              // nothing to say in it is left unwrapped, because the marker
              // would otherwise arrive as a chunk of its own.
              if (tag === 'li') {
                if (!lastWasBlock && parts.length > 0) parts.push(' |BREAK| ');
                const _at = parts.length;
                openLink(isLink);
                kids.forEach(walk);
                closeLink(isLink);
                const _inner = parts.slice(_at).join('').replace(/\|\/?[A-Z0-9]+\|/g, '');
                if (!/[A-Za-z0-9]/.test(_inner)) return;
                parts.splice(_at, 0, '|LIST| ');
                parts.push('|/LIST| |BREAK| ');
                lastWasBlock = true;
                return;
              }

              // Block elements, where I inject the break boundaries
              if (BLOCK.has(tag)) {
                const hadContent = parts.length;
                if (!lastWasBlock && parts.length > 0) parts.push(' |BREAK| ');
                openLink(isLink);
                kids.forEach(walk);
                closeLink(isLink);
                if (parts.length > hadContent) {
                  parts.push(' |BREAK| ');
                  lastWasBlock = true;
                }
                return;
              }

              // Anything else that the page lays out as a block gets a space
              // either side, so its words do not run into its neighbours'. The
              // rest is inline and is recursed into as it stands.
              const blockish = isCssBlock(node);
              if (blockish) gap();
              openLink(isLink);
              kids.forEach(walk);
              closeLink(isLink);
              if (blockish) gap();
            }

            Array.from(root.childNodes).forEach(walk);

            // Keep the page's title when the content root does not hold it,
            // which is common where the h1 sits in a banner above the article.
            if (!rootIsPage && !holdsH1(root)) {
              const h1 = document.querySelector('h1');
              const title = h1 && !hiddenByCss(h1) ? flatText(h1).replace(/\s+/g, ' ').trim() : '';
              if (title) parts.unshift('|H1|' + title + '.|/H1| |BREAK| ');
            }

            // Nav labels, addresses, citations and symbols are dealt with in
            // the popup by cleanPageText, since that part needs no DOM.
            const text = parts.join('')
              // Collapse
              .replace(/(\|BREAK\|\s*){2,}/g, ' |BREAK| ')
              // Keep the closing list marker tight against its item, so the tag
              // and the last word stay one token and no length-based split can
              // strand the tag in a chunk of its own.
              .replace(/\s+(\u0002?)\|\/LIST\|/g, '$1|/LIST|')
              .replace(/\s+/g, ' ')
              .trim();

            // Nothing to read from the walk. A text file, an RFC or a raw source
            // view is one <pre>, which the walk leaves out as code, so the
            // visible text is handed over as plain text, flagged with a control
            // character so the popup keeps its line breaks.
            if (!/[A-Za-z0-9]/.test(text.replace(/\|\/?[A-Z0-9]+\|/g, ''))) {
              const plain = String((body && (body.innerText || body.textContent)) || '');
              if (/[A-Za-z0-9]/.test(plain)) return '\u0003' + plain;
            }

            return text;
          }
        },
        (results) => {
          const pdfUrl = looksLikePdfUrl(_u);
          // A genuine injection failure must not look like "no text found".
          if (chrome.runtime.lastError) {
            if (pdfUrl) { resolve("__KAM_PDF__"); return; }
            showError("Can't read this page: " + chrome.runtime.lastError.message
                      + " — try reloading the page, or use Custom Text.");
            resolve("");
            return;
          }
          // allFrames returns one result per frame, so I pick the richest text,
          // which is the content iframe rather than the top-page header.
          const vals = (results || []).map(r => r && r.result).filter(t => typeof t === 'string');
          const texts = vals
            .filter(t => t !== '__KAM_CANVAS_DOC__' && t !== '__KAM_PDF__')
            .map(cleanPageText);
          const best = texts.sort((a, b) => b.length - a.length)[0] || "";
          if (!best && (pdfUrl || vals.includes('__KAM_PDF__'))) {
            resolve("__KAM_PDF__");
            return;
          }
          if (!best && vals.includes('__KAM_CANVAS_DOC__')) {
            showError("This looks like a Google Doc — its text is drawn on a canvas and can't be read directly. Copy the text (Ctrl+C), switch to the Custom Text tab, and paste it there.");
            resolve("");
            return;
          }
          if (!best) showError("Page text extraction failed — try again.");
          resolve(best);
        }
      );
    });
  });
}
