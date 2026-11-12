/* Accents and symbols, turned into something the voice can say.

   The server speaks English from plain ASCII, so both the popup's chunker and
   the worker's sanitiser end by replacing every non-ASCII character with a
   space. That was the whole treatment for a long time, and it quietly damaged
   ordinary text: "café" was spoken as "caf", "Brontë" as "Bront", "£50" as
   "50", "20°C" as "20 C" and "3½" as "3". Nothing failed, it just read wrong.

   kamSpeakable runs before that strip. Accented letters lose the accent and
   keep the letter, and the symbols people actually meet on a page become the
   words for them. Whatever is still non-ASCII afterwards is left for the
   caller to strip as before.

   One file, loaded by the worker (importScripts), the popup (script tag) and
   the tests (require), so the three cannot drift apart. */
'use strict';

(function (root) {
  const CURRENCY = { '£': ['pound', 'pounds'], '€': ['euro', 'euros'], '¥': ['yen', 'yen'],
                     '₹': ['rupee', 'rupees'], '₩': ['won', 'won'], '₽': ['rouble', 'roubles'] };
  const FRACTION = { '½': ['a half', 'and a half'], '¼': ['a quarter', 'and a quarter'],
                     '¾': ['three quarters', 'and three quarters'],
                     '⅓': ['a third', 'and a third'], '⅔': ['two thirds', 'and two thirds'] };
  const GREEK = { 'α': 'alpha', 'β': 'beta', 'γ': 'gamma', 'δ': 'delta', 'ε': 'epsilon', 'ζ': 'zeta',
                  'η': 'eta', 'θ': 'theta', 'ι': 'iota', 'κ': 'kappa', 'λ': 'lambda', 'μ': 'mu',
                  'ν': 'nu', 'ξ': 'xi', 'π': 'pi', 'ρ': 'rho', 'σ': 'sigma', 'ς': 'sigma', 'τ': 'tau',
                  'υ': 'upsilon', 'φ': 'phi', 'χ': 'chi', 'ψ': 'psi', 'ω': 'omega',
                  'Γ': 'gamma', 'Δ': 'delta', 'Θ': 'theta', 'Λ': 'lambda', 'Ξ': 'xi', 'Π': 'pi',
                  'Σ': 'sigma', 'Φ': 'phi', 'Ψ': 'psi', 'Ω': 'omega' };
  // Letters NFKD does not decompose, since they are letters in their own right
  // rather than a base letter plus a mark.
  const LETTER = { 'ß': 'ss', 'æ': 'ae', 'Æ': 'Ae', 'œ': 'oe', 'Œ': 'Oe', 'ø': 'o', 'Ø': 'O',
                   'đ': 'd', 'Đ': 'D', 'ł': 'l', 'Ł': 'L', 'þ': 'th', 'Þ': 'Th', 'ð': 'd', 'Ð': 'D',
                   'ı': 'i' };
  const SYMBOL = { '×': ' times ', '÷': ' divided by ', '−': '-', '·': ' ', '™': '', '®': '',
                   '©': ' copyright ', '§': ' section ', '¶': ' ', '†': '', '‡': '', '№': ' number ',
                   'µ': ' micro', '²': ' squared', '³': ' cubed', '¹': '', '′': "'", '″': '"',
                   '‹': "'", '›': "'", '«': '"', '»': '"', '„': '"', '‚': "'", '¿': '', '¡': '' };

  const esc = s => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const anyOf = table => new RegExp('[' + Object.keys(table).map(esc).join('') + ']', 'g');
  const CUR_KEYS = Object.keys(CURRENCY).map(esc).join('');
  // "£1,250.50", "€3.5m", "£2 billion": the amount, then any scale word, then
  // the currency, which is the order it is said in.
  const MONEY_RE = new RegExp('([' + CUR_KEYS + '])\\s?(\\d[\\d,]*(?:\\.\\d+)?)' +
                              '(\\s?(?:k|m|bn|million|billion|trillion|thousand)\\b)?', 'gi');
  const SCALE = { k: ' thousand', m: ' million', bn: ' billion' };
  const FRACTION_RE = new RegExp('(\\d)?\\s?(' + Object.keys(FRACTION).map(esc).join('|') + ')', 'g');
  const GREEK_RE = anyOf(GREEK), LETTER_RE = anyOf(LETTER), SYMBOL_RE = anyOf(SYMBOL);
  const CUR_RE = new RegExp('[' + CUR_KEYS + ']', 'g');

  function kamSpeakable(text) {
    let t = String(text == null ? '' : text);
    if (!/[^\x00-\x7F]/.test(t)) return t;        // plain ASCII, nothing to do

    t = t.replace(MONEY_RE, (whole, sym, amount, scale) => {
      const names = CURRENCY[sym];
      const sc = scale ? (SCALE[scale.trim().toLowerCase()] || ' ' + scale.trim()) : '';
      const one = !sc && /^1(?:\.0+)?$/.test(amount);
      return `${amount}${sc} ${one ? names[0] : names[1]}`;
    });
    t = t.replace(CUR_RE, sym => ` ${CURRENCY[sym][1]} `);   // a symbol with no amount

    t = t.replace(/°\s?C\b/g, ' degrees Celsius').replace(/°\s?F\b/g, ' degrees Fahrenheit')
         .replace(/°/g, ' degrees ');
    t = t.replace(FRACTION_RE, (whole, digit, f) =>
      digit ? `${digit} ${FRACTION[f][1]}` : ` ${FRACTION[f][0]}`);
    t = t.replace(SYMBOL_RE, s => SYMBOL[s]);
    t = t.replace(GREEK_RE, g => ` ${GREEK[g]} `);
    t = t.replace(LETTER_RE, l => LETTER[l]);

    // Accents: split each letter from its marks and drop the marks. NFKD also
    // opens ligatures (ﬁ becomes fi) and full-width forms. Then back to NFC so
    // nothing I did not mean to touch stays decomposed.
    try { t = t.normalize('NFKD').replace(/[̀-ͯ]/g, '').normalize('NFC'); }
    catch (e) { /* no normalize; the caller's strip still applies */ }
    return t;
  }

  /* True when most of the letters are not Latin script, which means the text is
     not something an English voice can read. The ASCII strip would otherwise
     turn it into nothing and the read would "finish" in silence. */
  function kamMostlyNonLatin(text) {
    const t = String(text == null ? '' : text);
    let letters, latin;
    try {
      letters = (t.match(/\p{L}/gu) || []).length;
      latin = (t.match(/\p{Script=Latin}/gu) || []).length;
    } catch (e) { return false; }
    return letters >= 20 && latin / letters < 0.5;
  }

  root.kamSpeakable = kamSpeakable;
  root.kamMostlyNonLatin = kamMostlyNonLatin;
  if (typeof module !== 'undefined' && module.exports) module.exports = { kamSpeakable, kamMostlyNonLatin };
})(typeof self !== 'undefined' ? self : globalThis);
