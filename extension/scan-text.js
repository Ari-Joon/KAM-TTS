/* Turning what OCR saw into something worth listening to.

   Tesseract gives one line of text per line of print, which is not how anyone
   speaks. A page of a Bible is the awkward case that drove all of this: the
   lines are wrapped mid-word, the columns are narrow, there is a running head
   and a page number at the top, and every verse is preceded by its number. Read
   straight out it comes through as "one Alpha beta gamma two Delta epsilon",
   which is unlistenable, and it is the numbers rather than the words that ruin
   it.

   Everything here is a pure function of a string so it can be tested without a
   camera, a phone or a page of print. test_scan_text.mjs is the suite. */
'use strict';

const KamScanText = (() => {

  // --- Line furniture -------------------------------------------------------
  // What sits at the edges of a printed page and is not part of the reading:
  // a page number on its own line, a running head in capitals, and the stray
  // single characters OCR invents from the gutter and the page edge.

  function stripFurniture(lines) {
    return lines.filter(l => {
      const t = l.trim();
      if (!t) return false;
      if (/^\d{1,4}$/.test(t)) return false;   // a page number alone
      // One or two characters on a line of their own. In print that is a
      // superscript cross-reference marker or a mark on the paper, never a
      // word: a printed line wraps at the margin with several words on it, so
      // nothing legitimate ends up this short. Letters count here as much as
      // symbols do, since the footnote markers ARE letters.
      if (t.length <= 2) return false;
      // A short line in capitals with no sentence punctuation is a running head
      // rather than a sentence. Kept if it ends in a stop, since a real shouted
      // line does.
      if (t.length <= 32 && t === t.toUpperCase() && /[A-Z]/.test(t)
          && !/[.!?]$/.test(t)) return false;
      return true;
    });
  }

  // --- Wrapped lines --------------------------------------------------------
  // Print wraps mid-word and hyphenates. Joining has to undo the hyphen without
  // touching a word that is genuinely hyphenated, so only a hyphen at the very
  // end of a line counts, which is the one place print puts a soft break.

  function joinWrapped(lines) {
    const out = [];
    for (const raw of lines) {
      const line = raw.trim();
      if (!out.length) { out.push(line); continue; }
      const prev = out[out.length - 1];
      if (/[A-Za-z]-$/.test(prev)) {
        // "begin-" + "ning" is one word, so the hyphen goes with the join.
        out[out.length - 1] = prev.replace(/-$/, '') + line;
      } else if (/[.!?:;"”’)]$/.test(prev)) {
        out.push(line);                     // the previous line finished a thought
      } else {
        out[out.length - 1] = prev + ' ' + line;   // same sentence, next line
      }
    }
    return out;
  }

  // --- Verse numbers --------------------------------------------------------
  // The interesting problem. A verse number is a small number wedged between a
  // full stop and a capital, which is also exactly what a real number looks
  // like in "took 40 days. Then" or "in 1947. After". Matching on shape alone
  // either leaves the verse numbers in or eats real ones.
  //
  // What separates them is that verse numbers count. They run in sequence down
  // the page, so a candidate is only treated as a verse marker when it is the
  // number the sequence is expecting, give or take a small jump for a verse the
  // OCR misread or a column boundary. A year or a quantity almost never lands
  // on that value, and when it does it is one word, not a page of them.
  //
  // The sequence is seeded by the first candidate rather than assumed to start
  // at 1, since a photograph of the middle of a chapter starts wherever it
  // starts.

  const LOOKAHEAD = 3;   // how far the count may skip and still be believed

  function stripVerseNumbers(text) {
    // A candidate is a 1-3 digit number that begins the line, or follows the end
    // of a sentence, and is followed by a capitalised word. That is the shape a
    // verse marker has once the lines are joined.
    const re = /(^|[.!?;:”"’']\s+|\s)(\d{1,3})\s+(?=[“"‘']?[A-Z])/g;
    let expected = null;
    let removed = 0;
    const out = text.replace(re, (whole, lead, digits) => {
      const n = parseInt(digits, 10);
      if (expected === null) {
        // Seed on a plausible opening verse. A page rarely opens on a number
        // above 176, which is the longest psalm and so the practical ceiling.
        if (n >= 1 && n <= 176) { expected = n + 1; removed++; return lead; }
        return whole;
      }
      if (n >= expected && n <= expected + LOOKAHEAD) {
        expected = n + 1; removed++; return lead;
      }
      return whole;   // out of sequence, so it is a number in the text
    });
    return { text: out.replace(/\s{2,}/g, ' ').trim(), removed };
  }

  // --- Columns --------------------------------------------------------------
  // Two narrow columns are the normal layout for print like this, and asking
  // Tesseract to work it out is a gamble: when its layout analysis guesses
  // wrong it interleaves the columns line by line and the result is confident
  // nonsense that reads perfectly smoothly. So the caller says how many columns
  // there are and each one is read separately, in order. Explicit beats clever
  // when the failure is silent.

  function columnSlices(width, columns) {
    const n = Math.max(1, Math.min(3, columns | 0));
    if (n === 1) return [[0, width]];
    // A small overlap, since a letter sitting on the boundary should be seen by
    // one of the two passes rather than sliced down the middle.
    const overlap = Math.round(width * 0.02);
    const step = width / n;
    const out = [];
    for (let i = 0; i < n; i++) {
      const x0 = Math.max(0, Math.round(i * step) - (i ? overlap : 0));
      const x1 = Math.min(width, Math.round((i + 1) * step) + (i < n - 1 ? overlap : 0));
      out.push([x0, x1]);
    }
    return out;
  }

  // --- The pipeline ---------------------------------------------------------

  function clean(raw, opts) {
    const o = opts || {};
    let lines = String(raw || '').replace(/\r/g, '').split('\n');
    if (o.furniture !== false) lines = stripFurniture(lines);
    lines = joinWrapped(lines);
    let text = lines.join('\n')
      // OCR reads a lot of quotation marks as accents and a lot of ligatures as
      // rubbish, so the few that matter for speech get normalised.
      .replace(/[‘’`´]/g, "'")
      .replace(/[“”]/g, '"')
      .replace(/\bﬁ/g, 'fi').replace(/\bﬂ/g, 'fl')
      // A lone "l" or "I" between digits is a one; Tesseract confuses them
      // constantly in verse numbers and reference clutter.
      .replace(/(\d)[lI](\d)/g, '$11$2')
      .replace(/[ \t]{2,}/g, ' ');
    let removed = 0;
    if (o.verses !== false) {
      const r = stripVerseNumbers(text);
      text = r.text; removed = r.removed;
    }
    // Paragraphs read better than one wall, and the chunker downstream splits
    // on blank lines happily.
    text = text.split('\n').map(s => s.trim()).filter(Boolean).join('\n');
    return { text, versesRemoved: removed };
  }

  return { clean, stripFurniture, joinWrapped, stripVerseNumbers, columnSlices };
})();

if (typeof module !== 'undefined' && module.exports) module.exports = KamScanText;
