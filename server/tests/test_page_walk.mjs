// What Read Page and Selected Text take from a page, run from the two real
// walkers in popup.js against a DOM small enough to read.
//
// The walkers decide what on a page is the writing and what is furniture, and
// they were wrong in both directions: an article lost its own title because a
// <header> is "navigation", a wrapper called "post-header" was skipped because
// it contains the word header, a feed was read as far as its first card, and
// "Next" was deleted from the middle of sentences. None of it showed, since a
// page that reads short looks the same as a short page.
//
// The DOM here is a stand-in, so what this proves is the walkers' decisions,
// not that Chrome agrees with my idea of a DOM. KAM_EXT_DIR points the suite
// at another copy of the extension, which is how I check that these tests fail
// against the code from before the fixes.
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { createRequire } from 'node:module';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const EXT = process.env.KAM_EXT_DIR || path.join(HERE, '..', '..', 'extension');
// The reading code lives in reader-core.js now; a copy from before that move
// has it all in popup.js, so a missing core is not an error.
const _core = fs.existsSync(path.join(EXT, 'reader-core.js'))
  ? fs.readFileSync(path.join(EXT, 'reader-core.js'), 'utf8') + '\n' : '';
const popup = _core + fs.readFileSync(path.join(EXT, 'popup.js'), 'utf8');
const require = createRequire(import.meta.url);
globalThis.kamSpeakable = require(path.join(EXT, 'textnorm.js')).kamSpeakable;

let PASS = 0, FAIL = 0;
const check = (l, g, w) => {
  const ok = JSON.stringify(g) === JSON.stringify(w);
  ok ? (PASS++, console.log('  ok   ' + l))
     : (FAIL++, console.log(`  FAIL ${l}\n         got  ${JSON.stringify(g)}\n         want ${JSON.stringify(w)}`));
};
const has = (l, hay, needle) => check(l, String(hay).includes(needle), true);
const hasnt = (l, hay, needle) => check(l, String(hay).includes(needle), false);
const count = (hay, needle) => String(hay).split(needle).length - 1;

// --- Lifting real code out of popup.js --------------------------------------
// The injected functions are bracketed on the two lines that wrap each one, as
// test_markers.mjs does and for its reason: their bodies hold regex literals
// that brace counting trips over.
function injectedFunction(anchor) {
  const lines = popup.split(/\r?\n/).map(l => l.replace(/\s+$/, ''));
  const at = lines.findIndex(l => l.includes(anchor));
  if (at < 0) throw new Error(`anchor not found: ${anchor}`);
  let start = -1, end = -1;
  for (let i = at; i >= 0; i--) if (lines[i] === '          func: () => {') { start = i; break; }
  for (let i = at; i < lines.length; i++) if (lines[i] === '          }') { end = i; break; }
  if (start < 0 || end < 0) throw new Error(`could not bracket the walker at ${anchor}`);
  return eval('(' + lines.slice(start, end + 1).join('\n').replace(/^ *func: /, '') + ')');
}
const pageWalker      = injectedFunction('const BLOCK = new Set(');
const selectionWalker = injectedFunction('const BLOCK_TAGS = new Set(');

// The cleaning half lives in the popup and needs the chunker's helpers. The
// old code did its cleaning inside the walker, so there it is the identity.
const between = (from, ...tos) => {
  const a = popup.indexOf(from);
  if (a < 0) return '';
  const ends = tos.map(t => popup.indexOf(t, a)).filter(b => b >= 0);
  return ends.length ? popup.slice(a, Math.min(...ends)) : '';
};
const cleanerSrc = between('const _NAV_LABELS', 'function _withExtractionTimeout(');
const cleanPageText = cleanerSrc
  ? new Function(between('const TARGET_CHARS', 'function renderTextView(', '// TEXT EXTRACTION') + cleanerSrc + '; return cleanPageText;')()
  : t => t;

// --- A DOM small enough to read ---------------------------------------------
globalThis.Node = { TEXT_NODE: 3, ELEMENT_NODE: 1 };

const BLOCK_DISPLAY = new Set(['html','body','div','p','section','article','main','header','footer',
  'aside','form','ul','ol','h1','h2','h3','h4','h5','h6','pre','blockquote','details','summary','nav','table']);
const displayOf = tag => tag === 'li' ? 'list-item' : tag === 'td' ? 'table-cell'
  : BLOCK_DISPLAY.has(tag) ? 'block' : 'inline';

// One compound selector: a tag, then any mix of #id, .class and [attr=value].
function matchesOne(el, sel) {
  const m = sel.trim().match(/^([a-z0-9-]*)((?:[#.][\w-]+|\[[^\]]+\])*)$/i);
  if (!m) throw new Error('selector not supported by the test DOM: ' + sel);
  if (m[1] && el.tagName.toLowerCase() !== m[1].toLowerCase()) return false;
  for (const part of m[2].match(/[#.][\w-]+|\[[^\]]+\]/g) || []) {
    if (part[0] === '#') { if (el.id !== part.slice(1)) return false; }
    else if (part[0] === '.') { if (!el.classList.contains(part.slice(1))) return false; }
    else {
      const [name, value] = part.slice(1, -1).split('=');
      const got = el.getAttribute(name);
      if (got === null) return false;
      if (value !== undefined && got !== value.replace(/^["']|["']$/g, '')) return false;
    }
  }
  return true;
}

class El {
  constructor(tag, attrs = {}, kids = []) {
    this.nodeType = 1;
    this.tagName = this.nodeName = tag.toUpperCase();
    this.attrs = attrs;
    this.childNodes = [];
    this.parentElement = null;
    kids.forEach(k => this.append(k));
    const self = this;
    this.classList = { contains: c => self.className.split(/\s+/).includes(c) };
  }
  append(k) {
    const node = typeof k === 'string'
      ? { nodeType: 3, nodeName: '#text', textContent: k, childNodes: [] } : k;
    node.parentElement = this;
    this.childNodes.push(node);
  }
  getAttribute(n) { return n in this.attrs ? String(this.attrs[n]) : null; }
  get id() { return this.attrs.id || ''; }
  get className() { return this.attrs.class || ''; }
  get open() { return 'open' in this.attrs; }
  get textContent() { return this.childNodes.map(k => k.textContent).join(''); }
  get innerText() { return this.textContent; }
  get css() {
    const out = {};
    String(this.attrs.style || '').split(';').forEach(d => {
      const [k, v] = d.split(':');
      if (k && v) out[k.trim()] = v.trim();
    });
    return out;
  }
  descendants() {
    const out = [];
    const go = n => n.childNodes.forEach(k => { if (k.nodeType === 1) { out.push(k); go(k); } });
    go(this);
    return out;
  }
  matches(sel) { return sel.split(',').some(s => matchesOne(this, s)); }
  querySelectorAll(sel) { return this.descendants().filter(e => e.matches(sel)); }
  querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
  closest(sel) { for (let e = this; e; e = e.parentElement) if (e.matches(sel)) return e; return null; }
  contains(n) { for (let e = n; e; e = e.parentElement) if (e === this) return true; return false; }
  remove() {
    const sib = this.parentElement.childNodes;
    sib.splice(sib.indexOf(this), 1);
  }
  cloneNode() {
    return new El(this.tagName, Object.assign({}, this.attrs),
      this.childNodes.map(k => k.nodeType === 1 ? k.cloneNode(true) : k.textContent));
  }
}

// h('p.lede#top', 'text', child, ...): a tag with any classes and an id, and
// an attribute object first among the children when one is needed.
function h(spec, ...kids) {
  const [, tag, rest] = spec.match(/^([a-z0-9-]+)(.*)$/i);
  const attrs = {};
  const cls = (rest.match(/\.[\w-]+/g) || []).map(c => c.slice(1));
  if (cls.length) attrs.class = cls.join(' ');
  const id = rest.match(/#([\w-]+)/);
  if (id) attrs.id = id[1];
  if (kids[0] && typeof kids[0] === 'object' && !(kids[0] instanceof El)) Object.assign(attrs, kids.shift());
  return new El(tag, attrs, kids);
}

globalThis.window = {
  getComputedStyle: el => ({
    display: el.css.display || displayOf(el.tagName.toLowerCase()),
    visibility: el.css.visibility || 'visible',
  }),
};

function install(bodyKids, { contentType = 'text/html' } = {}) {
  const body = h('body', ...bodyKids);
  const html = h('html', body);
  globalThis.document = {
    body, documentElement: html, contentType,
    querySelector: sel => html.querySelector(sel),
    querySelectorAll: sel => html.querySelectorAll(sel),
  };
}
// Read Page on a page made of these nodes, as the chunker would be given it.
const readPage = (bodyKids, opts) => { install(bodyKids, opts); return cleanPageText(pageWalker()); };
const rawPage  = (bodyKids, opts) => { install(bodyKids, opts); return pageWalker(); };
// Selected Text with these nodes as the selection.
function readSelection(kids, opts) {
  install([], opts);
  const frag = h('div', ...kids);
  window.getSelection = () => ({
    rangeCount: 1,
    getRangeAt: () => ({ cloneContents: () => ({ childNodes: frag.childNodes }) }),
    toString: () => frag.textContent,
  });
  return selectionWalker();
}

// =============================================================================
console.log('\n=== an article keeps its own header ===');
let out = readPage([
  h('header', h('nav', h('a', 'Home'), ' ', h('a', 'About')), h('p', 'The Daily Leaf')),
  h('article',
    h('header', h('h1', 'How Tea Is Made'), h('p.byline', 'By Ada Lovelace'), h('p', 'It starts with a leaf.')),
    h('p', 'The leaf is picked by hand.'),
    h('footer', h('p', 'Filed under drinks.'))),
  h('footer', h('p', 'Copyright 2026 The Daily Leaf')),
]);
console.log('  ' + out);
has('the title is read', out, '|H1|How Tea Is Made.|/H1|');
has('and the byline', out, 'By Ada Lovelace');
has('and the opening line', out, 'It starts with a leaf.');
has('and the body', out, 'The leaf is picked by hand.');
hasnt('the site header is not', out, 'The Daily Leaf');
hasnt('nor the menu', out, 'About');

console.log('\n=== page furniture, when there is no content root to go by ===');
out = readPage([
  h('header', h('p', 'Site strapline')),
  h('div', h('h1', 'A Plain Page'), h('p', 'Its one paragraph.')),
  h('aside', h('p', 'Related rubbish')),
  h('footer', h('p', 'Small print')),
]);
has('the content is read', out, 'Its one paragraph.');
hasnt('the header is furniture', out, 'Site strapline');
hasnt('so is the aside', out, 'Related rubbish');
hasnt('and the footer', out, 'Small print');
out = readPage([h('header', h('h1', 'The Only Title'), h('p', 'A subtitle')), h('div', h('p', 'Body words.'))]);
has('a header that holds the h1 is content', out, '|H1|The Only Title.|/H1|');

console.log('\n=== a page wrapped in one form ===');
out = readPage([
  h('form#aspnetForm', h('input', { type: 'hidden', name: '__VIEWSTATE' }),
    h('div', h('h1', 'Council Minutes'), h('p', 'The meeting opened at nine.'))),
]);
has('an ASP.NET page is read through its form', out, 'The meeting opened at nine.');
out = readPage([
  h('form', h('div', h('p', 'A page with no heading, all of it inside the form that wraps the site.'))),
]);
has('with or without a heading in it', out, 'all of it inside the form');
out = readPage([
  h('form.search', h('label', 'Search this site')),
  h('div', h('p', 'The page itself is out here, and there is a good deal more of it than of the form.')),
]);
hasnt('a search box is still left out', out, 'Search this site');

console.log('\n=== class names are matched whole ===');
out = readPage([h('main',
  h('div.post-header', h('p', 'Posted on Tuesday')),
  h('div.has-sidebar', h('p', 'The main column')),
  h('div.ad-free-content', h('p', 'For members')),
  h('div.modal-open', h('p', 'Under a modal class')),
  h('ul', h('li.menu-item-has-children', 'A list entry')),
  h('div.sidebar', h('p', 'Side noise')),
  h('div.ad', h('p', 'Buy this')),
  h('div.cookie.banner', h('p', 'We use cookies')))]);
has('post-header is not a header', out, 'Posted on Tuesday');
has('has-sidebar is not a sidebar', out, 'The main column');
has('ad-free-content is not an ad', out, 'For members');
has('modal-open is not a modal', out, 'Under a modal class');
has('menu-item-has-children is not a menu', out, 'A list entry');
hasnt('a real sidebar is skipped', out, 'Side noise');
hasnt('a real ad is skipped', out, 'Buy this');
hasnt('a cookie banner is skipped', out, 'We use cookies');

console.log('\n=== what is not on the screen is not read ===');
out = readPage([h('main',
  h('p', 'Visible words.'),
  h('p', { style: 'display:none' }, 'Display none'),
  h('p', { style: 'visibility:hidden' }, 'Visibility hidden'),
  h('p', { hidden: '' }, 'Hidden attribute'),
  h('p', { 'aria-hidden': 'true' }, 'Aria hidden'),
  h('div', { role: 'navigation' }, h('p', 'Role navigation')),
  h('div', { role: 'dialog' }, h('p', 'Role dialog')),
  h('div', { role: 'search' }, h('p', 'Role search')),
  h('p', 'A link ', h('span.sr-only', 'for screen readers'), h('span.screen-reader-text', 'only'), h('span.visually-hidden', 'unseen'), 'here.'),
  h('details', h('summary', 'Closed summary'), h('p', 'Closed body')),
  h('details', { open: '' }, h('summary', 'Open summary'), h('p', 'Open body')))]);
has('visible text is read', out, 'Visible words.');
for (const gone of ['Display none', 'Visibility hidden', 'Hidden attribute', 'Aria hidden', 'Role navigation',
                    'Role dialog', 'Role search', 'for screen readers', 'only', 'unseen', 'Closed body']) {
  hasnt(`"${gone}" is not`, out, gone);
}
has('a closed details gives its summary', out, 'Closed summary');
has('an open one gives both', out, 'Open summary |BREAK| Open body');

// checkVisibility says no for an element with no box, and a display:contents
// wrapper has none while its children are in plain view.
const wrapper = h('div', { style: 'display:contents' }, h('p', 'Inside a contents wrapper.'));
wrapper.checkVisibility = () => false;
const gone = h('p', 'Hidden by an ancestor rule.');
gone.checkVisibility = () => false;
gone.attrs.style = 'display:none';
const shown = h('p', 'Checked and visible.');
shown.checkVisibility = () => true;
out = readPage([h('main', wrapper, gone, shown)]);
has('a display:contents wrapper is walked into', out, 'Inside a contents wrapper.');
hasnt('checkVisibility hides what it should', out, 'Hidden by an ancestor rule.');
has('and passes what it should', out, 'Checked and visible.');

console.log('\n=== Wikipedia ===');
out = readPage([
  h('nav', h('a', 'Main page')),
  h('main#content.mw-body',
    h('header.mw-body-header', h('h1#firstHeading', 'Tea')),
    h('div#bodyContent',
      h('div#siteSub.noprint', 'From Wikipedia, the free encyclopedia'),
      h('a.mw-jump-link', 'Jump to content'),
      h('div#mw-content-text',
        h('div.hatnote', 'For other uses, see Tea (disambiguation).'),
        h('p', 'Tea is a drink', h('sup.reference', h('a', '[1]')), ' made from leaves.'),
        h('div.mw-heading', h('h2', 'History'), h('span.mw-editsection', '[', h('a', 'edit'), ']')),
        h('p', 'It is very old.'),
        h('h3', h('span.mw-headline', 'Origins'), h('span.mw-editsection', '[edit]')),
        h('p', 'It came from China.'),
        h('div.reflist', h('ol.references', h('li', 'Smith 2001, page 4'))),
        h('div.navbox', 'Teas of the world'),
        h('div#catlinks.catlinks', 'Categories: Drinks'))))]);
console.log('  ' + out);
has('the article title is kept', out, '|H1|Tea.|/H1|');
has('a hatnote is kept', out, 'For other uses, see Tea (disambiguation).');
has('a sentence closes up round its reference number', out, 'Tea is a drink made from leaves.');
has('a section heading is read', out, '|H2|History.|/H2|');
has('and one with the edit link inside it comes out clean', out, '|H3|Origins.|/H3|');
for (const noise of ['edit', 'From Wikipedia', 'Jump to content', 'Smith 2001', 'Teas of the world', 'Categories', 'Main page']) {
  hasnt(`"${noise}" is left out`, out, noise);
}

console.log('\n=== the content root ===');
const card = n => h('article', h('h2', `Story ${n}`), h('p', `The text of story number ${n}.`));
out = readPage([h('div.feed', card(1), card(2), card(3))]);
check('a feed is read to the end, not just its first card',
      [1, 2, 3].map(n => out.includes(`The text of story number ${n}.`)), [true, true, true]);
out = readPage([
  h('main', h('h1', 'The Main Thing'), h('article', h('p', 'First part.')), h('article', h('p', 'Second part.'))),
  h('div', h('p', 'Outside main.'))]);
has('main is the root when there is one', out, 'Second part.');
hasnt('and what is outside it is not read', out, 'Outside main.');
const long = 'This is the article itself, and it goes on for a good while longer than any card does. ';
out = readPage([
  h('article', h('h1', 'The Real Article'), h('p', long.repeat(4))),
  h('div.more', h('article', h('p', 'A teaser card.')), h('article', h('p', 'Another teaser.')))]);
has('one article among teaser cards is the article', out, 'The Real Article');
hasnt('and the cards are not read', out, 'A teaser card.');
out = readPage([
  h('header', h('h1', 'Title Above The Article')),
  h('article', h('p', 'The article has no heading of its own.'), h('p', 'It has two paragraphs.'))]);
check('a title that sits outside the root is still read first', out.indexOf('|H1|Title Above The Article.|/H1|'), 0);

console.log('\n=== line breaks and neighbours ===');
out = readPage([h('main', h('p', 'Roses are red', h('br'), 'Violets are blue'))]);
check('a <br> ends a line', out, 'Roses are red |BREAK| Violets are blue');
out = readPage([h('main', h('p', h('a', 'Alpha'), '\n  ', h('a', 'Beta'), ' ', h('span', 'Gamma')))]);
check('whitespace between two links is still a space', out, 'Alpha Beta Gamma');
out = readPage([h('main', h('p',
  h('span', { style: 'display:block' }, 'First line'), h('span', { style: 'display:block' }, 'Second line')))]);
check('elements laid out as blocks do not run together', out, 'First line Second line');
out = readPage([h('main', h('p', h('span', 'un'), h('span', 'believ'), h('span', 'able')))]);
check('but inline neighbours that touch stay one word', out, 'unbelievable');
out = readPage([h('main', h('h1', 'Big', h('br'), 'Title'), h('p', 'Words.'))]);
has('a heading broken over two lines keeps its space', out, '|H1|Big Title.|/H1|');
out = rawPage([h('main', h('ul', h('li', '2020'), h('li', '70'), h('li')))]);
check('a bullet that is only a number is still a bullet', count(out, '|LIST|'), 2);

console.log('\n=== equations are read once ===');
const katex = () => h('span.katex',
  h('span.katex-mathml', h('math', h('semantics', h('mrow', 'E=mc2'),
    h('annotation', { encoding: 'application/x-tex' }, 'E = mc^2')))),
  h('span.katex-html', 'E=mc2'));
out = readPage([h('main', h('p', 'Energy is ', katex(), ' exactly.'))]);
console.log('  ' + out);
check('the source is read, once', count(out, 'E = mc^2'), 1);
hasnt('the glyphs are not', out, 'E=mc2');
has('and the sentence carries on round it', out, 'Energy is E = mc^2 exactly.');
out = readPage([h('main', h('p', 'Area is ',
  h('mjx-container', { 'aria-label': 'pi r squared' }, h('svg', h('title', 'glyphs')),
    h('mjx-assistive-mml', h('math', 'πr2'))), '.'))]);
check('MathJax gives its spoken label, once', count(out, 'pi r squared'), 1);
hasnt('and neither its glyphs', out, 'glyphs');
hasnt('nor its hidden copy', out, 'πr2');

console.log('\n=== a document that is one <pre> ===');
out = readPage([h('pre', 'Network Working Group\n\nStatus of this Memo\nThis memo is a test.\n')]);
check('a text file is read from its visible text, line by line', out,
      'Network Working Group |BREAK| Status of this Memo |BREAK| This memo is a test.');
out = readPage([h('main', h('p', 'Prose first.'), h('pre', 'code = 1'))]);
check('code inside a real page is still left out', out, 'Prose first.');

console.log('\n=== PDFs say what they are ===');
check('a document served as a PDF', rawPage([], { contentType: 'application/pdf' }), '__KAM_PDF__');
check('the viewer, which is one embed', rawPage([h('embed', { type: 'application/pdf' })]), '__KAM_PDF__');
out = rawPage([h('main', h('p', 'A page with plenty of its own text, and a PDF embedded beside it.'),
               h('embed', { type: 'application/pdf' }))]);
has('a page that merely embeds one is read as a page', out, 'plenty of its own text');
check('Selected Text says so too', readSelection([], { contentType: 'application/pdf' }), '__KAM_PDF__');

console.log('\n=== nav labels, from the page to the chunker ===');
out = readPage([h('main',
  h('p', 'Next, we train the model. Previous work used a smaller set.'),
  h('p', 'A long piece about tea and how it is made… ', h('a', 'Read more')),
  h('p', 'Click ', h('a', 'Next'), ' to carry on.'),
  h('div.story-links', h('a', '« Previous'), ' ', h('a', 'Next »')),
  h('div', { role: 'button' }, 'Back to top'),
  h('p', h('a', 'Learn more')),
  h('p', 'See the ', h('a', 'next chapter'), ' for the rest.'))]);
console.log('  ' + out);
has('the words stay in running prose', out, 'Next, we train the model. Previous work used a smaller set.');
has('a link inside a sentence stays', out, 'Click Next to carry on.');
has('an ordinary link keeps its text', out, 'See the next chapter for the rest.');
hasnt('a Read more link goes', out, 'Read more');
hasnt('a pager goes', out, '« Previous');
hasnt('a button-like label goes', out, 'Back to top');
hasnt('a label link alone in its paragraph goes', out, 'Learn more');
hasnt('and the marks round link text never get out', out, '\u0001');

console.log('\n=== Selected Text reads what was selected, not the markup ===');
out = readSelection([
  h('script', 'var tracking = 1;'),
  h('style', '.a { color: red }'),
  h('script', { type: 'application/ld+json' }, '{"@type":"Article"}'),
  h('noscript', 'Enable JavaScript'),
  h('template', 'Template text'),
  h('h2', 'History', h('span.mw-editsection', '[edit]')),
  h('p', 'Tea is old.', h('sup.reference', h('a', '[1]'))),
  h('select', h('option', 'Choose one')),
  h('textarea', 'Typed text'),
  h('svg', h('title', 'Icon title')),
  h('p', 'It came from China.[2] It spread west.'),
]);
console.log('  ' + out);
has('the prose is read', out, 'Tea is old.');
has('and the heading', out, '|H2| History|/H2|');
for (const junk of ['tracking', 'color', '@type', 'Enable JavaScript', 'Template text', 'edit',
                    'Choose one', 'Typed text', 'Icon title', '[1]', '[2]']) {
  hasnt(`"${junk}" is not`, out, junk);
}
has('a citation number leaves the sentence whole', out, 'It came from China. It spread west.');

console.log(`\n${'='.repeat(62)}\n  ${PASS} passed, ${FAIL} failed\n${'='.repeat(62)}`);
process.exit(FAIL ? 1 : 0);
