// The popup's chunker and the text it is fed, run from the real code in
// popup.js: that nothing with a letter or a digit in it is dropped, that no
// chunk is longer than the server will speak, which conjunctions merge and
// when, how a run with no space in it is cut, what line breaks in Custom Text
// become, and which "Next" is a button and which is a word in a sentence.
//
// The failures these guard were all silent. A short sentence was thrown away,
// a long chunk was cut off by the server, a word was deleted from the middle
// of a paragraph, and in each case the read carried on as if nothing happened.
//
// KAM_EXT_DIR points the suite at another copy of the extension, which is how
// I check that these tests fail against the code from before the fixes.
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

// textnorm.js is loaded the way the popup loads it, as a global the chunker
// finds for itself, so the two are tested together and not through a stub.
const require = createRequire(import.meta.url);
const norm = require(path.join(EXT, 'textnorm.js'));
globalThis.kamSpeakable = norm.kamSpeakable;
globalThis.kamMostlyNonLatin = norm.kamMostlyNonLatin;

let PASS = 0, FAIL = 0;
const check = (l, g, w) => {
  const ok = JSON.stringify(g) === JSON.stringify(w);
  ok ? (PASS++, console.log('  ok   ' + l))
     : (FAIL++, console.log(`  FAIL ${l}\n         got  ${JSON.stringify(g)}\n         want ${JSON.stringify(w)}`));
};

// --- Lifting real code out of popup.js --------------------------------------
// The chunking section is self-contained, from the size constants down to the
// first UI helper, so it is lifted whole, as test_markers.mjs does. The names
// that only the fixed version has come back undefined from the old one, so the
// old version fails on what it does and not on a throw.
const grab = (src, name) => {
  let i = src.indexOf(`function ${name}(`);
  if (i < 0) throw new Error(`function ${name} not found`);
  let p = 0, j = src.indexOf('(', i);
  for (; j < src.length; j++) { if (src[j] === '(') p++; else if (src[j] === ')') { p--; if (!p) { j++; break; } } }
  let d = 0;
  for (; j < src.length; j++) { if (src[j] === '{') d++; else if (src[j] === '}') { d--; if (!d) { j++; break; } } }
  return src.slice(i, j);
};
const opt = (src, name) => { try { return grab(src, name); } catch (e) { return ''; } };
const between = (src, from, ...tos) => {
  const a = src.indexOf(from);
  if (a < 0) return '';
  const ends = tos.map(t => src.indexOf(t, a)).filter(b => b >= 0);
  return ends.length ? src.slice(a, Math.min(...ends)) : '';
};

// The section ends at the first UI helper in a copy from before the reading
// code moved, and at the TEXT EXTRACTION banner in reader-core.js since.
const chunkerSrc = between(popup, 'const TARGET_CHARS', 'function renderTextView(', '// TEXT EXTRACTION');
if (!chunkerSrc) throw new Error('could not find the chunking section');
const cleanerSrc = between(popup, 'const _NAV_LABELS', 'function _withExtractionTimeout(');
const maybe = n => `(typeof ${n} === 'undefined' ? undefined : ${n})`;
const K = new Function(`
  ${chunkerSrc}
  ${cleanerSrc}
  ${opt(popup, 'resumeLine')}
  ${opt(popup, 'extractionProblem')}
  return { splitIntoChunks, splitSentences, hardSplitAtWord, MAX_CHARS,
           spokenLength: ${maybe('spokenLength')},
           mergeBoundaryFragments: ${maybe('mergeBoundaryFragments')},
           customTextToMarked: ${maybe('customTextToMarked')},
           isNavLabel: ${maybe('isNavLabel')},
           cleanPageText: ${maybe('cleanPageText')},
           looksLikePdfUrl: ${maybe('looksLikePdfUrl')},
           resumeLine: ${maybe('resumeLine')},
           extractionProblem: ${maybe('extractionProblem')} };`)();
const { splitIntoChunks, MAX_CHARS } = K;

// What a chunk costs the server: its text with the markers gone and the
// symbols spelt out, which is what the worker posts. The old code has no such
// function, so its chunks are measured the same way here.
const MARK = /\|\/?[A-Z0-9]+\|/g;
const spoken = s => norm.kamSpeakable(String(s).replace(MARK, ' ')).replace(/\s+/g, ' ').trim();
// Every run of letters or digits, in order. If the chunks hold the same runs
// as the text they came from, nothing that could be said was lost.
const words = s => (String(s).replace(MARK, ' ').match(/[A-Za-z0-9]+/g) || []);
const lost = (text, chunks) => {
  const out = words(chunks.join(' '));
  const missing = [];
  let at = 0;
  for (const w of words(text)) {
    const i = out.indexOf(w, at);
    if (i < 0) missing.push(w); else at = i + 1;
  }
  return missing;
};
const longest = chunks => Math.max(0, ...chunks.map(c => spoken(c).length));

console.log('\n=== MAX_CHARS is one the server can speak whole ===');
check('the cap is under the XTTS limit of 180', MAX_CHARS <= 180, true);

console.log('\n=== nothing with a letter or a digit in it is dropped ===');
let text = '"Is it hard?" asked Tom. No!';
let chunks = splitIntoChunks(text);
console.log('  chunks: ' + JSON.stringify(chunks));
check('a quoted question keeps its words', lost(text, chunks), []);
check('and the closing quote stays with the question', chunks.join(' ').includes('"Is it hard?" asked Tom.'), true);
check('the short exclamation is spoken', chunks.join(' ').includes('No!'), true);

text = 'The palette we settled on after a long afternoon of arguing about it in the studio was this: '
     + 'red; green; blue; a deep violet that nobody could name; teal.';
chunks = splitIntoChunks(text);
console.log('  chunks: ' + JSON.stringify(chunks));
check('a list of short clauses keeps every item', lost(text, chunks), []);
check('and the semicolons that separate them', (chunks.join(' ').match(/;/g) || []).length, 4);

text = 'Year |BREAK| Sales |BREAK| 2020 |BREAK| 70 |BREAK| 2021 |BREAK| 85';
chunks = splitIntoChunks(text);
check('table cells that are only numbers are spoken', lost(text, chunks), []);
check('each cell is still its own chunk', chunks, ['Year', 'Sales', '2020', '70', '2021', '85']);

check('a short label is a chunk', splitIntoChunks('Step 1 |BREAK| Open the lid and pour.'), ['Step 1', 'Open the lid and pour.']);
check('a one-word answer is a chunk', splitIntoChunks('Yes.'), ['Yes.']);
check('a short question is a chunk', splitIntoChunks('Why?'), ['Why?']);
check('a bold-only line keeps its markers', splitIntoChunks('|BOLD|Note|/BOLD|'), ['|BOLD|Note|/BOLD|']);
check('a short sentence joins its neighbour and is not lost',
      splitIntoChunks('Yes. I think that is the right answer.'), ['Yes. I think that is the right answer.']);
check('one at the end of a paragraph joins the sentence before',
      splitIntoChunks('I think that is the right answer. Yes.'), ['I think that is the right answer. Yes.']);
check('a single letter is content', lost('A |BREAK| B |BREAK| C', splitIntoChunks('A |BREAK| B |BREAK| C')), []);

console.log('\n=== only punctuation and symbols can still go ===');
check('a line of dashes is not a chunk', splitIntoChunks('First line here. |BREAK| ----- |BREAK| Second line here.'),
      ['First line here.', 'Second line here.']);
check('text with nothing to say gives no chunks at all', splitIntoChunks('... !!! ???'), []);
check('an empty bullet is not a chunk', splitIntoChunks('|LIST| |/LIST| |BREAK| Real text here.'), ['Real text here.']);

console.log('\n=== accents and symbols count as something to say ===');
chunks = splitIntoChunks('£50 |BREAK| café |BREAK| π |BREAK| ½');
check('money, an accented word, a Greek letter and a fraction all survive', chunks, ['£50', 'café', 'π', '½']);
check('the chunk keeps the text as it was written, for the page highlight', splitIntoChunks('Un café à £5.'), ['Un café à £5.']);
check('and non-Latin text is recognised before a silent read starts',
      norm.kamMostlyNonLatin('这是一段完全用中文写成的文字，用来测试语言检测功能是否正常工作。'), true);
check('while English with a few foreign words is not', norm.kamMostlyNonLatin('The word 東京 means Tokyo, the eastern capital of Japan.'), false);

console.log('\n=== no chunk is longer than the server will speak ===');
const verse = 'And God said, Let there be light. And there was light. And God saw the light, that it was good. '
  + 'And God divided the light from the darkness. And God called the light Day. And the darkness he called Night. '
  + 'And the evening and the morning were the first day.';
chunks = splitIntoChunks(verse);
console.log('  lengths: ' + JSON.stringify(chunks.map(c => c.length)));
check('verse where every sentence opens with And stays under the cap', longest(chunks) <= MAX_CHARS, true);
check('and loses none of its words', lost(verse, chunks), []);
check('and keeps every full stop', (chunks.join(' ').match(/\./g) || []).length, 7);

const longSentence = 'The committee, having met on three separate occasions over the course of the spring term to consider '
  + 'the proposals put before it by the faculty, by the student body and by the trustees, and having heard evidence '
  + 'from every department that wished to give it, concluded after a good deal of argument that none of the '
  + 'proposals could be adopted in the form in which it had been submitted and that each would have to be revised.';
chunks = splitIntoChunks(longSentence);
check('a 400-character sentence is split under the cap', longest(chunks) <= MAX_CHARS, true);
check('with every word kept', lost(longSentence, chunks), []);

// Symbols grow when they are spoken, so a chunk that fits as written can be
// too long to say. The cap is on what is said.
const prices = Array.from({ length: 40 }, (_, i) => `£${i + 1}`).join(' ') + '.';
chunks = splitIntoChunks(prices);
check('a run of prices fits as spoken, not just as written', longest(chunks) <= MAX_CHARS, true);
check('and every price is still there', lost(prices, chunks), []);

// A seeded shuffle of awkward pieces, since the faults above were each found
// by one unlucky combination and there are more combinations than I can list.
let seed = 20261112;
const rnd = n => { seed = (seed * 1103515245 + 12345) & 0x7fffffff; return seed % n; };
const BITS = ['No!', 'Yes.', 'Why?', '"Is it so?" he asked.', 'red; green; blue; teal.', '2020', '70%',
  'Step 1', 'And then it rained.', 'But nobody minded.', 'For a while it was quiet.', 'So they left.',
  'However, the door was locked.', '|BOLD|Note|/BOLD|', 'It cost £1,250.50 in all.', 'See https://example.com/a/b.',
  'The quick brown fox jumps over the lazy dog, and the dog, being lazy, does nothing at all about it.',
  'A', 'Dr. Smith arrived at 3.15 p.m. on the dot.', 'x'.repeat(40), 'café', '(aside)', '|LIST| one item|/LIST|'];
let worst = 0, dropped = [];
for (let n = 0; n < 300; n++) {
  const parts = [];
  for (let k = 0, len = 1 + rnd(9); k < len; k++) parts.push(BITS[rnd(BITS.length)] + (rnd(3) ? ' ' : ' |BREAK| '));
  const t = parts.join('');
  const c = splitIntoChunks(t);
  worst = Math.max(worst, longest(c));
  const gone = lost(t, c);
  if (gone.length && dropped.length < 3) dropped.push({ text: t, gone });
}
check('300 shuffled texts: no chunk over the cap', worst <= MAX_CHARS, true);
check('300 shuffled texts: no word lost', dropped, []);

console.log('\n=== which conjunctions merge, and when ===');
check('a short And sentence joins the one before, full stop and all',
      splitIntoChunks('The rain came down hard. And then it stopped.'),
      ['The rain came down hard. And then it stopped.']);
check('so does But', splitIntoChunks('The rain came down hard. But nobody minded.'),
      ['The rain came down hard. But nobody minded.']);
check('For opens a sentence of its own', splitIntoChunks('The rain came down hard. For a while it was quiet.'),
      ['The rain came down hard.', 'For a while it was quiet.']);
check('so does So', splitIntoChunks('The rain came down hard. So they went home.'),
      ['The rain came down hard.', 'So they went home.']);
check('and However', splitIntoChunks('The rain came down hard. However, they stayed out.'),
      ['The rain came down hard.', 'However, they stayed out.']);
check('and Because', splitIntoChunks('They went home early. Because it rained, the match was off.'),
      ['They went home early.', 'Because it rained, the match was off.']);
const full = 'The committee met on three separate occasions over the spring term to consider the proposals put before it by the faculty and by the trustees.';
chunks = splitIntoChunks(full + ' And it rained.');
check('a merge that would pass the cap is not made', chunks, [full, 'And it rained.']);
check('nothing is glued on after a heading', splitIntoChunks('|H2|Results.|/H2| |BREAK| And so it goes.'),
      ['|H2|Results.|/H2|', 'And so it goes.']);
check('nor is the first line of a new paragraph pulled into the one before',
      splitIntoChunks('The rain came down hard. |BREAK| And then it stopped.'),
      ['The rain came down hard.', 'And then it stopped.']);
check('a line left unfinished takes the "or ..." line that follows it',
      splitIntoChunks('Check the agent outputs |BREAK| or stored data.'), ['Check the agent outputs or stored data.']);
check('a finished line does not, and keeps its stop',
      splitIntoChunks('In the beginning there was a long line of verse that ran on. |BREAK| And the next line began with And, as they all do here.'),
      ['In the beginning there was a long line of verse that ran on.', 'And the next line began with And, as they all do here.']);

console.log('\n=== a learned boundary correction obeys the same cap ===');
if (K.mergeBoundaryFragments) {
  check('a reported fragment is joined back', K.mergeBoundaryFragments(['The agent outputs', 'correct. Next one.'], ['correct.']),
        ['The agent outputs correct. Next one.']);
  check('but not into a chunk the server would cut', K.mergeBoundaryFragments([full, 'correct. Next one.'], ['correct.']),
        [full, 'correct. Next one.']);
} else {
  check('the merge can be reached without the server', false, true);
}

console.log('\n=== a run with no space in it ===');
const url = 'https://example.com/' + 'a'.repeat(250);
chunks = splitIntoChunks('See ' + url);
console.log('  lengths: ' + JSON.stringify(chunks.map(c => c.length)));
check('a 270-character address is cut at the cap, not into 6-character pieces', chunks.length <= 3, true);
check('with none of it lost', chunks.join('').replace(/\s/g, ''), ('See ' + url).replace(/\s/g, ''));
check('and nothing over the cap', longest(chunks) <= MAX_CHARS, true);
const pieces = K.hardSplitAtWord('p'.repeat(400), 150);
check('the splitter itself cuts a spaceless run at the window', pieces.map(p => p.length), [150, 150, 100]);
check('a decimal is not a place to cut',
      K.hardSplitAtWord('The measured value came out at 3.14159 ' + 'and '.repeat(40), 60)[0].includes('3.14159'), true);

console.log('\n=== line breaks in Custom Text ===');
const prep = K.customTextToMarked || (t => t.trim());
chunks = splitIntoChunks(prep('Chapter One\n\nIt was a dark night.\nThe rain fell.'));
check('a heading does not run into its paragraph', chunks[0], 'Chapter One');
check('a single newline ends a chunk too', chunks, ['Chapter One', 'It was a dark night.', 'The rain fell.']);
chunks = splitIntoChunks(prep('Roses are red\nViolets are blue\nSugar is sweet\nAnd so are you'));
check('lines of verse stay separate, the one that opens with And included', chunks,
      ['Roses are red', 'Violets are blue', 'Sugar is sweet', 'And so are you']);
if (K.customTextToMarked) {
  check('a blank line is a paragraph break', K.customTextToMarked('One.\n\n\nTwo.'), 'One. |BREAK| Two.');
  check('Windows line endings are the same thing', K.customTextToMarked('One.\r\nTwo.'), 'One. |BREAK| Two.');
  // Text copied out of a PDF or an email breaks every line mid-phrase. Those
  // are not line ends anyone wrote, so they are closed up.
  const wrapped = 'The committee met on three separate occasions over the spring\n'
                + 'term to consider the proposals put before it by the faculty.\n'
                + 'Nothing was decided.';
  check('a paragraph that was hard-wrapped is joined back up', K.customTextToMarked(wrapped),
        'The committee met on three separate occasions over the spring term to consider the proposals put before it by the faculty. |BREAK| Nothing was decided.');
  check('a word hyphenated across the wrap is closed up',
        K.customTextToMarked('The committee met on three separate occasions to consider the infor-\nmation put before it.'),
        'The committee met on three separate occasions to consider the information put before it.');
  check('a short line is never taken for a wrapped one', K.customTextToMarked('Ingredients\nflour\neggs'),
        'Ingredients |BREAK| flour |BREAK| eggs');
} else {
  check('Custom Text has a line-break step', false, true);
}

console.log('\n=== sentences and their closing quotes ===');
check('a quote that ends a sentence keeps its closing mark',
      K.splitSentences('He said "stop." Then he left.'), ['He said "stop."', 'Then he left.']);
check('two quoted sentences split between the quotes',
      K.splitSentences('"Go." "No."'), ['"Go."', '"No."']);
check('a closing list tag still rides on the last sentence',
      K.splitSentences('|LIST| Add the tea. Wait three minutes.|/LIST|'), ['|LIST| Add the tea.', 'Wait three minutes.|/LIST|']);

console.log('\n=== which "Next" is a button ===');
if (K.isNavLabel && K.cleanPageText) {
  const { isNavLabel, cleanPageText } = K;
  const A = s => '\u0001' + s + '\u0002';      // the walker's marks round link text
  for (const l of ['Next', 'Previous', '« Previous', 'Next »', 'Read more', 'Read more...', 'LEARN MORE',
                   'See more', 'Back to top', '↑ Back to top', 'External link', 'Next page', 'Older posts',
                   'Opens in new tab', 'Links to an external site.', 'Skip to main content']) {
    check(`"${l}" is a label`, isNavLabel(l), true);
  }
  for (const l of ['Next, we train the model.', 'Previous work used a smaller set.', 'Read more books',
                   'Next steps', 'The next page', 'More', '', 'Top 10 results']) {
    check(`"${l}" is not`, isNavLabel(l), false);
  }

  const prose = 'Next, we train the model. Previous work used a smaller set, so read more carefully and see more of it.';
  check('running prose is left exactly as it was', cleanPageText(prose), prose);
  check('a pager is dropped', cleanPageText(`The end of the story. |BREAK| ${A('Previous')} ${A('Next')} |BREAK| About us.`),
        'The end of the story. |BREAK| About us.');
  check('a Read more link after a teaser is dropped',
        cleanPageText(`A long piece about tea and how to make it… ${A('Read more')} |BREAK| Another piece.`),
        'A long piece about tea and how to make it… |BREAK| Another piece.');
  check('a link in the middle of a sentence stays', cleanPageText(`Click ${A('Next')} to carry on with the form.`),
        'Click Next to carry on with the form.');
  check('a link that ends an unfinished sentence stays', cleanPageText(`To carry on, press ${A('Next')}.`),
        'To carry on, press Next.');
  check('an ordinary link keeps its words', cleanPageText(`See the ${A('next chapter of the book')} for more.`),
        'See the next chapter of the book for more.');
  check('a label standing as a whole block is dropped', cleanPageText('Real text. |BREAK| Back to top |BREAK| More real text.'),
        'Real text. |BREAK| More real text.');
  check('a bullet that held only a label leaves no empty bullet',
        cleanPageText(`|LIST| ${A('Read more')}|/LIST| |BREAK| |LIST| Tea|/LIST|`), '|LIST| Tea|/LIST|');
  check('a label link that was a block of its own does not join its neighbours',
        cleanPageText(`First paragraph. ${A(' |BREAK| Read more |BREAK| ')} Second paragraph.`),
        'First paragraph. |BREAK| Second paragraph.');
  check('the screen-reader aside comes out of a link',
        cleanPageText(`Read ${A('the course handbook (Links to an external site.)')} first.`), 'Read the course handbook first.');
  check('but the same words in a sentence stay', cleanPageText('This button opens in a new tab when pressed.'),
        'This button opens in a new tab when pressed.');

  console.log('\n=== addresses and citations ===');
  check('an address goes and the sentence keeps its full stop',
        cleanPageText('Visit https://example.com/page. Then come back.'), 'Visit. Then come back.');
  check('an address in the middle of a sentence leaves the rest', cleanPageText('See https://example.com/a for details.'),
        'See for details.');
  check('an address does not take a closing tag with it', cleanPageText('|LIST| Docs at https://example.com/x|/LIST|'),
        '|LIST| Docs at|/LIST|');
  check('citation numbers go', cleanPageText('Tea is a drink.[1] It is old.[23]'), 'Tea is a drink. It is old.');
  check('the marks never reach the chunker', /[\u0001-\u0004]/.test(cleanPageText(`${A('Home')} and ${A('Next')}`)), false);

  console.log('\n=== a plain-text document keeps its lines ===');
  check('text handed over as plain is broken at its line ends',
        cleanPageText('\u0003Title of the memo\n\nFirst point.\nSecond point.'),
        'Title of the memo |BREAK| First point. |BREAK| Second point.');
} else {
  check('the nav-label decision is a function that can be tested', false, true);
}

console.log('\n=== small things the popup says ===');
if (K.looksLikePdfUrl) {
  check('a PDF address is recognised', K.looksLikePdfUrl('https://example.com/paper.pdf'), true);
  check('with a query or a page anchor', [K.looksLikePdfUrl('https://x.org/a.PDF?dl=1'), K.looksLikePdfUrl('file:///C:/a.pdf#page=3')], [true, true]);
  check('a page about PDFs is not', K.looksLikePdfUrl('https://example.com/pdf-guide.html'), false);
} else {
  check('a PDF address can be recognised', false, true);
}
if (K.extractionProblem) {
  check('a PDF gets the message that says what to do',
        K.extractionProblem('__KAM_PDF__'), 'PDFs cannot be read directly yet. Copy the text and paste it into Custom Text.');
  check('real text is not a problem', K.extractionProblem('Some text.'), '');
} else {
  check('a PDF has a message of its own', false, true);
}
if (K.resumeLine) {
  check('the resume line gives the place first', K.resumeLine({ index: 179, total: 300, title: 'A Long Title', url: 'https://x' }),
        'Left off at 180 of 300 · A Long Title');
  check('a worker with nothing saved shows nothing', [K.resumeLine(null), K.resumeLine(undefined), K.resumeLine({})], ['', '', '']);
} else {
  check('the resume line can be built', false, true);
}

console.log(`\n${'='.repeat(62)}\n  ${PASS} passed, ${FAIL} failed\n${'='.repeat(62)}`);
process.exit(FAIL ? 1 : 0);
