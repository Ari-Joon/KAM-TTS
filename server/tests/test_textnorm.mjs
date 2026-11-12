// Accents and symbols become something the voice can say (textnorm.js), and
// the worker's sanitiser uses it before its ASCII strip. The failure this
// guards: "café" spoken as "caf" and "£50" as "50", silently.
import fs from 'node:fs';
import { createRequire } from 'node:module';
const require = createRequire(import.meta.url);
const { kamSpeakable, kamMostlyNonLatin } = require('../../extension/textnorm.js');

let PASS = 0, FAIL = 0;
const check = (l, g, w) => {
  const ok = JSON.stringify(g) === JSON.stringify(w);
  ok ? (PASS++, console.log('  ok   ' + l))
     : (FAIL++, console.log(`  FAIL ${l}\n         got  ${JSON.stringify(g)}\n         want ${JSON.stringify(w)}`));
};
const tidy = s => s.replace(/\s+/g, ' ').trim();
const say = s => tidy(kamSpeakable(s));

console.log('\n=== accents lose the mark and keep the letter ===');
check('French', say('Café déjà vu, naïve façade'), 'Cafe deja vu, naive facade');
check('names', say('Brontë, Zoë, Müller, Søren, Łukasz in São Paulo'), 'Bronte, Zoe, Muller, Soren, Lukasz in Sao Paulo');
check('letters that are not a base plus a mark', say('Straße, encyclopædia, œuvre'), 'Strasse, encyclopaedia, oeuvre');
check('ligatures open up', say('the ﬁrst ﬂoor'), 'the first floor');
check('plain ASCII is returned untouched', kamSpeakable('Nothing to do here: 100% plain.'), 'Nothing to do here: 100% plain.');

console.log('\n=== money is said in the order it is spoken ===');
check('pounds with pence', say('It cost £1,250.50 in total'), 'It cost 1,250.50 pounds in total');
check('one pound is singular', say('only £1 each'), 'only 1 pound each');
check('euros', say('€20 or £50'), '20 euros or 50 pounds');
check('a scale letter', say('raised €3.5m and £2bn'), 'raised 3.5 million euros and 2 billion pounds');
check('a scale word', say('worth £2 billion'), 'worth 2 billion pounds');
check('a symbol with no amount', say('priced in £ and €'), 'priced in pounds and euros');

console.log('\n=== the symbols a page actually has ===');
check('degrees', say('20°C, 68°F and a 90° turn'), '20 degrees Celsius, 68 degrees Fahrenheit and a 90 degrees turn');
check('fractions', say('3½ cups and ¼ teaspoon'), '3 and a half cups and a quarter teaspoon');
check('times and divide', say('2×3 and 6÷2'), '2 times 3 and 6 divided by 2');
check('trademarks go quietly', say('Windows® and KAM™'), 'Windows and KAM');
check('Greek letters are named', say('angle θ and π radians'), 'angle theta and pi radians');
check('powers', say('10 m² and 5 cm³'), '10 m squared and 5 cm cubed');

console.log('\n=== nothing non-ASCII is left that has a spoken form ===');
const out = kamSpeakable('Café £50 20°C ½ θ ® Straße ﬁ');
check('all ASCII afterwards', /[^\x00-\x7F]/.test(out), false);

console.log('\n=== text an English voice cannot read is recognised ===');
check('Japanese', kamMostlyNonLatin('これは日本語の文章です。英語ではありませんので読めません。'), true);
check('Russian', kamMostlyNonLatin('Это русский текст, который не может быть прочитан.'), true);
check('English with a few accents', kamMostlyNonLatin('The café in São Paulo serves crème brûlée every day.'), false);
check('a short snippet is not judged', kamMostlyNonLatin('日本語'), false);

console.log('\n=== the worker runs it before stripping to ASCII ===');
const bg = fs.readFileSync(new URL('../../extension/background.js', import.meta.url), 'utf8');
const grab = name => {
  const i = bg.indexOf(`function ${name}(`);
  let d = 0, j = bg.indexOf('{', i);
  for (; j < bg.length; j++) { if (bg[j] === '{') d++; else if (bg[j] === '}') { d--; if (!d) { j++; break; } } }
  return bg.slice(i, j);
};
const sanitizeText = new Function('KAM_MARKER_RE', 'kamSpeakable', `${grab('sanitizeText')}; return sanitizeText;`)(
  /\|\/?(?:H1|H2|H3|BOLD|ITALIC|CODE|CALLOUT|CAPTION|LIST|BREAK)\|/g, kamSpeakable);
check('a sentence with accents and money survives whole',
      sanitizeText('Zoë paid £50 at the café.'), 'Zoe paid 50 pounds at the cafe.');
check('the maths symbols it already knew still work', sanitizeText('x ≤ y'), 'x less than or equal to y');

console.log(`\n${PASS} passed, ${FAIL} failed`);
process.exit(FAIL ? 1 : 0);
