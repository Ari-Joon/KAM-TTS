// What OCR saw, turned into something worth listening to.
//
// The text here is deliberately synthetic. Greek-letter filler exercises the
// shapes that matter, a number wedged between a full stop and a capital, a
// hyphen at a line end, a running head, without any of it depending on a
// particular edition of a particular book.
import fs from 'node:fs';

const src = fs.readFileSync(new URL('../../extension/scan-text.js', import.meta.url), 'utf8');
const T = new Function(src + '; return KamScanText;')();

let PASS = 0, FAIL = 0;
const check = (l, g, w) => {
  const ok = JSON.stringify(g) === JSON.stringify(w);
  ok ? (PASS++, console.log('  ok   ' + l))
     : (FAIL++, console.log(`  FAIL ${l}\n         got  ${JSON.stringify(g)}\n         want ${JSON.stringify(w)}`));
};

console.log('\n=== page furniture is not part of the reading ===');
const furn = T.stripFurniture([
  'GENESIS', '412', 'Alpha beta gamma delta.', 'a', '', 'Epsilon zeta eta.', 'THE END.',
]);
check('a page number on its own line goes', furn.includes('412'), false);
check('a running head in capitals goes',    furn.includes('GENESIS'), false);
check('gutter speckle goes',                furn.includes('a'), false);
check('the actual sentences stay',          furn, ['Alpha beta gamma delta.', 'Epsilon zeta eta.', 'THE END.']);
// The last one is the interesting keep: capitals AND a full stop is a shouted
// sentence, not a heading, so it survives.

console.log('\n=== print wraps mid-word, speech does not ===');
check('a hyphen at the line end is closed up',
      T.joinWrapped(['begin-', 'ning of the matter']), ['beginning of the matter']);
check('a real hyphenated word is untouched',
      T.joinWrapped(['a well-known result']), ['a well-known result']);
check('a line that did not finish its sentence continues',
      T.joinWrapped(['Alpha beta', 'gamma delta.']), ['Alpha beta gamma delta.']);
check('a line that finished one starts a new line',
      T.joinWrapped(['Alpha beta.', 'Gamma delta.']), ['Alpha beta.', 'Gamma delta.']);
check('a closing quote counts as finished',
      T.joinWrapped(['he said "go."', 'Gamma delta.']), ['he said "go."', 'Gamma delta.']);

console.log('\n=== verse numbers count, which is how they are told apart ===');
// The whole point: a verse marker and a quantity look identical, so the
// sequence is the only thing separating them.
let r = T.stripVerseNumbers('1 Alpha beta gamma. 2 Delta epsilon. 3 Zeta eta theta.');
check('a run of markers goes', r.text, 'Alpha beta gamma. Delta epsilon. Zeta eta theta.');
check('and it says how many it took', r.removed, 3);

r = T.stripVerseNumbers('The journey took 40 days. Then they rested.');
check('a quantity followed by a lower-case word is not a marker',
      r.text, 'The journey took 40 days. Then they rested.');
check('and nothing was removed', r.removed, 0);

r = T.stripVerseNumbers('12 Alpha beta. 13 Gamma delta. 14 Epsilon zeta.');
check('a photograph of mid-chapter seeds wherever it starts',
      r.text, 'Alpha beta. Gamma delta. Epsilon zeta.');

r = T.stripVerseNumbers('1 Alpha beta. 97 Gamma delta.');
check('a number far out of sequence is left alone', r.text, 'Alpha beta. 97 Gamma delta.');

r = T.stripVerseNumbers('4 Alpha beta. 6 Gamma delta. 7 Epsilon zeta.');
check('a small skip is still believed, since OCR drops one',
      r.text, 'Alpha beta. Gamma delta. Epsilon zeta.');

r = T.stripVerseNumbers('It was 1947. Alpha beta gamma.');
check('a year before a capital is not eaten when out of sequence',
      r.text, 'It was 1947. Alpha beta gamma.');

console.log('\n=== columns are told, not guessed ===');
// Tesseract interleaves two columns when its layout analysis guesses wrong, and
// the result reads smoothly while being nonsense, so the caller declares them.
check('one column is the whole page', T.columnSlices(1000, 1), [[0, 1000]]);
const two = T.columnSlices(1000, 2);
check('two columns split down the middle', [two[0][0], two[1][1]], [0, 1000]);
check('with a small overlap so a letter on the seam survives',
      two[0][1] > 500 && two[1][0] < 500, true);
check('columns are clamped to something sane', T.columnSlices(900, 9).length, 3);

console.log('\n=== the whole pipeline on a page-shaped input ===');
const page = [
  'PSALMS', '119',
  '1 Alpha beta gamma delta epsilon',
  'zeta eta theta.',
  '2 Iota kappa lambda mu nu xi omi-',
  'cron pi rho.',
  '3 Sigma tau upsilon phi chi psi omega.',
].join('\n');
const out = T.clean(page);
check('the running head and page number are gone',
      /PSALMS|119/.test(out.text), false);
check('the wrapped word is closed up', /omicron/.test(out.text), true);
check('the verse markers are gone', /(^|\s)[123]\s/.test(out.text), false);
check('three markers were found', out.versesRemoved, 3);
check('and the words all survived',
      /Alpha beta gamma delta epsilon zeta eta theta\./.test(out.text), true);
console.log('  ---\n' + out.text.split('\n').map(l => '  | ' + l).join('\n'));

console.log('\n=== the cleanup can be turned off ===');
const raw = T.clean('1 Alpha beta.', { verses: false });
check('leaving the numbers in is possible', /1 Alpha/.test(raw.text), true);

console.log(`\n${'='.repeat(62)}\n  ${PASS} passed, ${FAIL} failed\n${'='.repeat(62)}`);
process.exit(FAIL ? 1 : 0);
