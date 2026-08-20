// The |LIST| marker on the extension side, end to end: that both page walkers
// wrap a bullet, that the chunker keeps the closing tag with the item's last
// chunk instead of stranding it, and that the reader turns that into the
// list_item position hint the server reads.
//
// The hint is the part that matters. background.js strips every marker out of
// the text before it posts, so the position field is the only structural
// channel the server has, and a marker that popup.js emits but the hint never
// looks at is a marker that does nothing at all.
import fs from 'node:fs';

const read = f => fs.readFileSync(new URL('../../extension/' + f, import.meta.url), 'utf8');
const markersSrc   = read('markers.js');
const popupSrc     = read('popup.js');
const backgroundSrc= read('background.js');
const contentSrc   = read('content.js');

let PASS = 0, FAIL = 0;
const check = (l, g, w) => {
  const ok = JSON.stringify(g) === JSON.stringify(w);
  ok ? (PASS++, console.log('  ok   ' + l))
     : (FAIL++, console.log(`  FAIL ${l}\n         got  ${JSON.stringify(g)}\n         want ${JSON.stringify(w)}`));
};
const has = (l, hay, needle) => check(l, hay.includes(needle), true);
const hasnt = (l, hay, needle) => check(l, hay.includes(needle), false);

// --- Lifting real code out of the extension ---------------------------------
// Everything below runs the actual source rather than a copy of it, so the
// suite fails when the extension changes and not when I forget to update a
// fixture.

// Pull one named top-level function out by brace counting from its opening.
function grab(src, name) {
  const i = src.indexOf(`function ${name}(`);
  if (i < 0) throw new Error(`no function ${name} in source`);
  let d = 0, j = i;
  for (; j < src.length; j++) {
    if (src[j] === '{') d++;
    else if (src[j] === '}') { d--; if (!d) { j++; break; } }
  }
  return src.slice(i, j);
}

// Pull one of popup.js's injected page functions out. Brace counting does not
// work here, since the bodies hold regex literals with an unmatched "}" in a
// character class, so I bracket on the two lines that wrap every injected
// function instead. It is layout-sensitive on purpose: if the shape changes
// this throws rather than testing something else by accident.
function injectedFunction(anchor) {
  const lines = popupSrc.split(/\r?\n/).map(l => l.replace(/\s+$/, ''));
  const at = lines.findIndex(l => l.includes(anchor));
  if (at < 0) throw new Error(`anchor not found: ${anchor}`);
  let start = -1, end = -1;
  for (let i = at; i >= 0; i--) if (lines[i] === '          func: () => {') { start = i; break; }
  for (let i = at; i < lines.length; i++) if (lines[i] === '          }') { end = i; break; }
  if (start < 0 || end < 0) throw new Error(`could not bracket the walker at ${anchor}`);
  const body = lines.slice(start, end + 1).join('\n').replace(/^ *func: /, '');
  return eval('(' + body + ')');
}

// --- A DOM small enough to read ---------------------------------------------
globalThis.Node = { TEXT_NODE: 3, ELEMENT_NODE: 1 };

const txt = s => ({ nodeType: 3, nodeName: '#text', textContent: s, childNodes: [] });
const el = (tag, ...kids) => {
  const node = {
    nodeType: 1,
    tagName: tag.toUpperCase(),
    nodeName: tag.toUpperCase(),
    childNodes: kids,
    className: '',
    classList: { contains: () => false },
    getAttribute: () => null,
    querySelector: () => null,
    querySelectorAll: () => [],
    get textContent() { return kids.map(k => k.textContent).join(''); },
  };
  node.cloneNode = () => node;
  return node;
};

// One prose paragraph, then a three-item list whose last item is empty. The
// items carry a trailing space on purpose, since that is what real markup gives
// you and it is the case the closing tag has to be pulled back through.
const makeTree = () => [
  el('p', txt('The kettle matters more than the leaf.')),
  el('ul',
    el('li', txt('Boil the water ')),
    el('li', txt('Add the tea. Wait three minutes. ')),
    el('li')),
];

globalThis.window = { getComputedStyle: () => ({ display: 'block', visibility: 'visible' }) };

function runPageWalker() {
  const root = el('article', ...makeTree());
  globalThis.document = { querySelector: sel => (sel === 'article' ? root : null), body: root };
  return injectedFunction('const BLOCK = new Set(')();
}

function runSelectionWalker() {
  const frag = { childNodes: makeTree() };
  globalThis.document = { querySelector: () => null };
  globalThis.window.getSelection = () => ({
    rangeCount: 1,
    getRangeAt: () => ({ cloneContents: () => frag }),
    toString: () => '',
  });
  return injectedFunction('const BLOCK_TAGS = new Set(')();
}

console.log('\n=== the marker grammar stays in step across the four files ===');
const markersApi = new Function(markersSrc + `
  return { KAM_MARKER_NAMES, KAM_MARKER_RE, KAM_LIST_END_RE, KAM_HEADING_START_RE,
           KAM_PARAGRAPH_END_RE, kamStripMarkers };`)();
const { KAM_MARKER_NAMES, KAM_LIST_END_RE, kamStripMarkers } = markersApi;

has('LIST is part of the grammar', KAM_MARKER_NAMES, 'LIST');
// A closing |/BREAK| is never emitted, so the probe only closes the wrappers.
const probe = KAM_MARKER_NAMES.map(n => `|${n}| word ` + (n === 'BREAK' ? '' : `|/${n}| `)).join('');
check('markers.js strips every name it declares', /\|/.test(kamStripMarkers(probe)), false);

// background.js falls back to an inline copy when markers.js fails to load, and
// a fallback that has drifted is worse than none: it would quietly stop
// stripping whatever the real grammar had gained.
const fallback = backgroundSrc.match(/self\.KAM_MARKER_RE\s*=\s*(\/.*?\/g);/);
check('background.js has an inline fallback', Boolean(fallback), true);
check('and it matches the real grammar', fallback[1], markersApi.KAM_MARKER_RE.toString());
check('its list rule matches too',
      (backgroundSrc.match(/self\.KAM_LIST_END_RE\s*=\s*(\/.*?\/);/) || [])[1],
      KAM_LIST_END_RE.toString());

// popup.js and content.js each strip markers on their own for display, so they
// have to know about LIST as well or a bullet reads with pipes in it.
const stripForDisplay = new Function(grab(popupSrc, 'stripMarkersForDisplay') + '; return stripMarkersForDisplay;')();
check('popup.js display stripper clears the probe', /\|/.test(stripForDisplay(probe)), false);
const contentStrip = new Function(
  contentSrc.slice(contentSrc.indexOf('const _MARKERS = {'), contentSrc.indexOf('function _applyOverlayStyle'))
  + '; return _stripMarkers;')();
check('content.js display stripper clears the probe', /\|/.test(contentStrip(probe)), false);

console.log('\n=== both walkers wrap a list item ===');
for (const [name, out] of [['page', runPageWalker()], ['selection', runSelectionWalker()]]) {
  has(`${name} walker opens the item`,  out, '|LIST| Boil the water');
  has(`${name} walker closes it tight`, out, 'Boil the water|/LIST|');
  has(`${name} walker leaves prose alone`, out, 'The kettle matters more than the leaf.');
  hasnt(`${name} walker wraps nothing for an empty item`, out, '|LIST| |/LIST|');
  check(`${name} walker wraps each item once`,
        (out.match(/\|LIST\|/g) || []).length, 2);
  // The closing tag has to stay one token with the last word, since a space in
  // front of it gives the length-based splitters somewhere to cut.
  hasnt(`${name} walker never leaves a space before the close`, out, ' |/LIST|');
}

console.log('\n=== the chunker keeps the closing tag with the last chunk ===');
// Lift the whole chunking section, which is self-contained: everything from the
// size constants down to isReadable.
const chunkerStart = popupSrc.indexOf('const TARGET_CHARS');
const chunkerEnd   = popupSrc.indexOf('function renderTextView(');
if (chunkerStart < 0 || chunkerEnd < 0) throw new Error('could not find the chunking section');
const splitIntoChunks = new Function(
  popupSrc.slice(chunkerStart, chunkerEnd) + '; return splitIntoChunks;')();

const chunks = splitIntoChunks(runPageWalker());
console.log('  chunks: ' + JSON.stringify(chunks));
check('no chunk is just the closing tag',
      chunks.filter(c => c.replace(/\|\/?[A-Z]+\|/g, '').trim().length < 2).length, 0);
check('the one-sentence item is a single chunk',
      chunks.filter(c => c.includes('Boil the water')).length, 1);
has('and it carries both tags', chunks.find(c => c.includes('Boil the water')), '|/LIST|');
// The two-sentence item splits, and the closing tag has to ride the last piece,
// since that is the chunk whose trailing gap is the gap between items.
const twoSentence = chunks.filter(c => c.includes('Add the tea') || c.includes('Wait three minutes'));
check('the two-sentence item splits in two', twoSentence.length, 2);
hasnt('the first piece does not close the item', twoSentence[0], '|/LIST|');
has('the last piece does',                      twoSentence[1], '|/LIST|');

console.log('\n=== the reader turns that into a position hint ===');
// Every pattern comes from markers.js rather than being written out again here,
// since a hand-copied regex that is subtly wrong makes this whole section pass
// for the wrong reason. I got that exact bug in a scratch harness while writing
// it: a lost backslash turned the paragraph rule into an alternation that
// matched the empty string, so every chunk came back as paragraph_end.
const detectPositionHint = new Function(
  `const KAM_HEADING_START_RE   = ${markersApi.KAM_HEADING_START_RE};
   const KAM_LIST_END_RE        = ${KAM_LIST_END_RE};
   const KAM_PARAGRAPH_END_RE   = ${markersApi.KAM_PARAGRAPH_END_RE};
   ${grab(backgroundSrc, 'detectPositionHint')}
   return detectPositionHint;`)();

check('a chunk closing a bullet asks for item pacing',
      detectPositionHint('|LIST| Boil the water.|/LIST|'), 'list_item');
check('the middle of a long bullet asks for nothing',
      detectPositionHint('|LIST| Add the tea.'), null);
check('and its last piece asks for item pacing',
      detectPositionHint('Wait three minutes.|/LIST|'), 'list_item');
check('a heading still outranks it',
      detectPositionHint('|H2|Making Tea.|/H2|'), 'heading');
check('ordinary prose is still unmarked',
      detectPositionHint('The kettle matters more than the leaf.'), null);
check('a chunk closing a paragraph still asks for the breath',
      detectPositionHint('The kettle matters more than the leaf. |BREAK|'), 'paragraph_end');

// Every chunk the walker produces has to resolve to a hint the server knows, or
// the label silently falls back to generic pacing.
const KNOWN_HINTS = new Set(['heading', 'list_item', 'paragraph_end', null]);
check('every chunk maps to a known hint',
      chunks.every(c => KNOWN_HINTS.has(detectPositionHint(c))), true);

console.log(`\n${'='.repeat(60)}\n  ${PASS} passed, ${FAIL} failed\n${'='.repeat(60)}`);
process.exit(FAIL ? 1 : 0);
