// The dashboard's "Needs attention" block, private reads, the open-on-read
// setting, and the keyboard and screen-reader basics. Each part lifts the real
// functions out of dashboard.js and runs them against a tiny fake DOM, and the
// markup checks read player.html as it is shipped.
//
// KAM_EXT_DIR points the suite at another copy of the extension, which is how
// I check that these tests fail against the code from before the changes.
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const EXT = process.env.KAM_EXT_DIR || path.join(HERE, '..', '..', 'extension');
const read = f => fs.readFileSync(path.join(EXT, f), 'utf8');
const dash = read('dashboard.js');
const html = read('player.html');

let PASS=0, FAIL=0;
const check=(l,g,w)=>{ const ok=JSON.stringify(g)===JSON.stringify(w);
  ok?(PASS++,console.log('  ok   '+l)):(FAIL++,console.log(`  FAIL ${l}\n         got ${JSON.stringify(g)} want ${JSON.stringify(w)}`)); };

const grab = (src, name) => {
  let i = src.indexOf(`function ${name}(`);
  if (i < 0) throw new Error(`function ${name} not found`);
  if (src.slice(Math.max(0, i - 6), i) === 'async ') i -= 6;   // keep it async
  // Step over the parameter list first, since a default like kind = 'error'
  // would otherwise confuse the brace count before the body has started.
  let p=0, j=src.indexOf('(', i);
  for (; j<src.length; j++){ if(src[j]==='(')p++; else if(src[j]===')'){p--; if(!p){j++;break;} } }
  let d=0;
  for (; j<src.length; j++){ if(src[j]==='{')d++; else if(src[j]==='}'){d--; if(!d){j++;break;} } }
  return src.slice(i,j);
};
// The same, but empty when the function is not there, for code that only the
// new version has. The old version then fails on behaviour, not on a throw.
const opt = (src, name) => { try { return grab(src, name); } catch (e) { return ''; } };
// A one-line top-level const, or nothing when it is not there.
const constLine = (src, name) => {
  const i = src.indexOf(`const ${name} =`);
  return i < 0 ? '' : src.slice(i, src.indexOf('\n', i));
};
const grabConst = (src, name) => {
  const i = src.indexOf(`const ${name} = {`);
  if (i < 0) return '';
  return src.slice(i, src.indexOf('\n};', i) + 3);
};
// The opening tag of the element with this id in player.html.
const tagOf = id => (html.match(new RegExp('<[^<>]*\\bid="' + id + '"[^<>]*>')) || [''])[0];
const attr = (tag, name) => { const m = tag.match(new RegExp('\\b' + name + '="([^"]*)"')); return m ? m[1] : null; };

async function section(title, fn) {
  console.log(`\n=== ${title} ===`);
  try { await fn(); }
  catch (e) { FAIL++; console.log(`  FAIL ${title} threw: ${e && e.message}`); }
}
const tick = () => new Promise(r => setTimeout(r, 0));
const ticks = async (n = 8) => { for (let i = 0; i < n; i++) await tick(); };

function fakeEl(id) {
  const cls = new Set(), attrs = {};
  return {
    id, value: '', textContent: '', innerHTML: '', title: '', hidden: false, disabled: false,
    style: {}, dataset: {}, attrs,
    classList: { add: c => cls.add(c), remove: c => cls.delete(c), contains: c => cls.has(c),
                 toggle: (c, on) => { const v = on === undefined ? !cls.has(c) : on; v ? cls.add(c) : cls.delete(c); return v; } },
    setAttribute: (k, v) => { attrs[k] = String(v); },
    removeAttribute: k => { delete attrs[k]; },
    getAttribute: k => (k in attrs ? attrs[k] : null),
    querySelector: () => null, querySelectorAll: () => [], appendChild() {},
  };
}
function fakeDocument() {
  const els = {};
  return { els, body: fakeEl('body'), getElementById: id => (els[id] ||= fakeEl(id)),
           createElement: () => fakeEl(''), querySelectorAll: () => [] };
}

// The shared pieces every part below needs, lifted once.
const HELPERS = `
  ${grab(dash, 'esc')}
  ${opt(dash, 'escAttr')}
  ${constLine(dash, 'PRIVATE_LABEL')}
  ${opt(dash, 'isPrivateRow')}
  ${opt(dash, 'chunkTextHTML')}
  ${opt(dash, 'chunkTextBlock')}
`;
const or = (name, fallback) => `(typeof ${name} === 'function' ? ${name} : ${fallback})`;

// =============================================================================
// 1. Needs attention: which flags, in what order
// =============================================================================
const attn = new Function(`
  ${HELPERS}
  ${constLine(dash, 'ATTENTION_TOP')}
  ${opt(dash, 'attentionPlan')}
  ${opt(dash, 'attentionRowHTML')}
  ${opt(dash, 'attentionHTML')}
  ${opt(dash, 'attentionFixRequest')}
  ${opt(dash, 'attentionDismissRequest')}
  ${opt(dash, 'attentionReply')}
  return {
    plan: ${or('attentionPlan', '() => ({ rows: [], rest: 0, live: 0, total: 0 })')},
    row:  ${or('attentionRowHTML', "() => ''")},
    html: ${or('attentionHTML', "() => null")},
    fix:  ${or('attentionFixRequest', '() => undefined')},
    dismiss: ${or('attentionDismissRequest', '() => undefined')},
    reply: ${or('attentionReply', '() => ({})')},
  };`)();

const flag = (id, pattern, hits, more) => Object.assign({ id, pattern, hits, issue: 'PRONUNCIATION', ts: 100 + id, expected_hint: '' }, more);

await section('which flags are shown, and in what order', async () => {
  const flags = [flag(1, 'CNF', 2), flag(2, 'SQL', 9), flag(3, 'never again', 0), flag(4, 'GUI', 2, { ts: 999 })];
  const p = attn.plan(flags, false);
  check('most hits first, the newer of two equals first, and none with no hits', p.rows.map(f => f.id), [2, 4, 1]);
  check('the one with no hits waits behind the toggle', p.rest, 1);
  check('the tab counts the ones that have come up again', p.live, 3);
  check('show all brings the rest, still in order', attn.plan(flags, true).rows.map(f => f.id), [2, 4, 1, 3]);
  check('the list it was given is left in the order it came', flags.map(f => f.id), [1, 2, 3, 4]);

  const many = Array.from({ length: 14 }, (_, i) => flag(i + 1, 'w' + i, 14 - i)).concat([flag(50, 'zero', 0)]);
  const top = attn.plan(many, false);
  check('at most ten by default', top.rows.length, 10);
  check('and they are the ten with the most hits', top.rows.map(f => f.id), [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]);
  check('the toggle counts everything left over', top.rest, 5);
  check('the tab still counts all fourteen', top.live, 14);
  check('show all shows every one', attn.plan(many, true).rows.length, 15);

  check('hits sent as text still sort as numbers', attn.plan([flag(1, 'a', '9'), flag(2, 'b', '10')], false).rows.map(f => f.id), [2, 1]);
  check('a row with no id or no pattern is dropped, not drawn broken',
        attn.plan([{ pattern: 'x', hits: 3 }, { id: 5, pattern: '  ', hits: 3 }, null, flag(6, 'ok', 1)], false).rows.map(f => f.id), [6]);
  check('something that is not a list is no flags', attn.plan({ error: 'not found' }, false).total, 0);
});

// =============================================================================
// 2. The row: what it says, and that page text cannot become markup
// =============================================================================
await section('a flag row', async () => {
  const h = attn.row(flag(7, 'CNF', 3, { expected_hint: 'C.N.F' }));
  check('the pattern is shown', h.includes('>CNF<'), true);
  check('how often it has come up', h.includes('came up 3 times'), true);
  check('once is not "1 times"', attn.row(flag(7, 'CNF', 1)).includes('came up 1 time<'), true);
  check('the reported issue', h.includes('>PRONUNCIATION<'), true);
  check('the box is pre-filled with the hint', /<input[^>]*value="C\.N\.F"/.test(h), true);
  check('and says what to type', h.includes('placeholder="How should it be said? e.g. C.N.F"'), true);
  check('the row carries the flag id', h.includes('data-flag-id="7"'), true);
  check('a Fix and a Dismiss button', [/<button class="attn-fix"[^>]*>Fix<\/button>/.test(h), /<button class="attn-dismiss"[^>]*>Dismiss<\/button>/.test(h)], [true, true]);
  check('no inline handlers, which the extension CSP blocks', /\son[a-z]+\s*=/i.test(h), false);

  const evil = attn.row(flag('9"><b>', '<script>alert(1)</script> "x" \'y\'', 2,
                             { issue: '<img src=x>', expected_hint: '"><script>bad()</script>' }));
  check('no tag from the pattern, the hint, the issue or the id survives', /<script|<img|<b>/i.test(evil), false);
  check('the pattern is still readable as text', evil.includes('&lt;script&gt;alert(1)&lt;/script&gt;'), true);
  // Every double quote left must belong to the markup itself. One from the
  // data would close an attribute early, and the rest would be read as HTML.
  const quotedOk = evil.replace(/&quot;/g, '').split('"').length % 2 === 1;
  check('quotes from the data are escaped, so the attributes stay closed', [quotedOk, evil.includes('&quot;x&quot;')], [true, true]);
  check('the hint cannot break out of the value attribute', /value="&quot;&gt;&lt;script&gt;bad\(\)&lt;\/script&gt;"/.test(evil), true);
});

await section('the block as a whole', async () => {
  check('no flags is nothing at all, which hides the block', attn.html([], false), '');
  const some = [flag(1, 'CNF', 2), flag(2, 'quiet', 0)];
  const h = attn.html(some, false) || '';
  check('it is headed Needs attention', h.includes('Needs attention'), true);
  check('one row by default', (h.match(/class="attn-row"/g) || []).length, 1);
  check('and a toggle for the rest', h.includes('show all (1 more)'), true);
  const all = attn.html(some, true) || '';
  check('show all draws both and offers the way back', [(all.match(/class="attn-row"/g) || []).length, all.includes('show fewer')], [2, true]);
  check('no toggle when there is nothing behind it', (attn.html([flag(1, 'CNF', 2)], false) || '').includes('attn-more'), false);
  const quiet = attn.html([flag(2, 'quiet', 0)], false) || '';
  check('only quiet flags: no rows, but still a way to reach them',
        [(quiet.match(/class="attn-row"/g) || []).length, quiet.includes('show all (1 more)')], [0, true]);
});

// =============================================================================
// 3. Fix and Dismiss: what is sent, and what happens after
// =============================================================================
await section('what Fix and Dismiss send', async () => {
  check('Fix posts say to the flag', attn.fix(7, '  C.N.F '), { path: '/rules/flags/7/fix', method: 'POST', body: { say: 'C.N.F' } });
  check('an empty box sends nothing', [attn.fix(7, '   '), attn.fix(7, null)], [null, null]);
  check('no id sends nothing', attn.fix('', 'x'), null);
  check('an odd id cannot change the path', (attn.fix('7/../x', 'a') || {}).path, '/rules/flags/7%2F..%2Fx/fix');
  check('Dismiss posts to the flag', attn.dismiss(7), { path: '/rules/flags/7/dismiss', method: 'POST' });
  check('the server\'s message is what is shown', attn.reply({ ok: true, message: 'CNF is now said C.N.F' }, 'Fix saved.'),
        { ok: true, text: 'CNF is now said C.N.F' });
  check('no message falls back', attn.reply({ ok: true }, 'Fix saved.'), { ok: true, text: 'Fix saved.' });
  check('a refusal is not a success, with a message', attn.reply({ ok: false, message: 'Not a word in the text.' }, 'x'),
        { ok: false, text: 'Not a word in the text.' });
  check('or with an error', attn.reply({ ok: false, error: 'flag not found' }, 'x'), { ok: false, text: 'flag not found' });
  check('a server without the route is not a success', attn.reply({ error: 'not found' }, 'x').ok, false);
});

// The DOM half, with the real functions and a stub server.
function loadBlock(server) {
  const doc = fakeDocument();
  const host = fakeEl('rules-attention');
  host.style.display = 'none';
  // The rows the block holds: what was there before a redraw and what is
  // there after it, so a typed value can be followed across.
  let rows = [];
  const afterRows = [];
  Object.defineProperty(host, 'innerHTML', { get: () => host._html || '', set: v => { host._html = v; rows = afterRows.splice(0); } });
  host.querySelectorAll = () => rows;
  doc.els['rules-attention'] = host;
  const tab = fakeEl('tab');
  const badge = fakeEl('rules-attn-badge');
  badge.closest = () => tab;
  doc.els['rules-attn-badge'] = badge;
  const sent = [], toasts = [], calls = { rules: 0 };
  const api = async (p, method, body) => {
    sent.push(body === undefined ? [p, method || 'GET'] : [p, method, body]);
    const r = server(p, method, body);
    if (r instanceof Error) throw r;
    return r;
  };
  const b = new Function('document', 'api', 'showToast', 'refreshRules', `
    ${HELPERS}
    ${constLine(dash, 'ATTENTION_TOP')}
    let _flagsCache = [], _flagsShowAll = false;
    ${opt(dash, 'attentionPlan')}
    ${opt(dash, 'attentionRowHTML')}
    ${opt(dash, 'attentionHTML')}
    ${opt(dash, 'attentionFixRequest')}
    ${opt(dash, 'attentionDismissRequest')}
    ${opt(dash, 'attentionReply')}
    ${opt(dash, '_setAttentionBadge')}
    ${opt(dash, '_renderAttention')}
    ${opt(dash, 'refreshAttention')}
    ${opt(dash, '_attentionFix')}
    ${opt(dash, '_attentionDismiss')}
    const none = async () => false;
    return { refresh: ${or('refreshAttention', 'none')}, fix: ${or('_attentionFix', 'none')},
             dismiss: ${or('_attentionDismiss', 'none')},
             render: ${or('_renderAttention', '() => {}')},
             get cache() { return _flagsCache; }, set showAll(v) { _flagsShowAll = v; } };`)(
    doc, api, (text, kind) => toasts.push(kind ? [text, kind] : [text]), () => { calls.rules++; });
  return { b, host, badge, tab, sent, toasts, calls, setRows: r => { rows = r; }, queueRows: r => afterRows.push(...r) };
}

await section('opening the Rules tab fills the block', async () => {
  const flags = [flag(1, 'CNF', 2), flag(2, 'SQL', 5), flag(3, 'quiet', 0)];
  const { b, host, badge, tab, sent, toasts } = loadBlock(() => ({ flags }));
  await b.refresh();
  check('it asks the server for the flags', sent, [['/rules/flags', 'GET']]);
  check('the block is shown', host.style.display, 'block');
  check('with the busiest flag first', host.innerHTML.indexOf('>SQL<') < host.innerHTML.indexOf('>CNF<') && host.innerHTML.indexOf('>SQL<') > 0, true);
  check('the tab shows how many have come up again', [badge.textContent, badge.hidden], ['2', false]);
  check('and says what the number is', /2 reported words/.test(tab.attrs['aria-label'] || ''), true);
  check('nothing is toasted for a plain refresh', toasts, []);
});

await section('a server without the route, or one that is off, hides it quietly', async () => {
  for (const [why, server] of [
    ['a 404 that is not JSON', () => new Error('SyntaxError: Unexpected token <')],
    ['a server that is off', () => new Error('TypeError: Failed to fetch')],
    ['a JSON 404', () => ({ error: 'not found' })],
    ['an empty list', () => ({ flags: [] })],
  ]) {
    const { b, host, badge, toasts } = loadBlock(server);
    host.style.display = 'block'; host.innerHTML = 'stale'; badge.hidden = false; badge.textContent = '4';
    await b.refresh();
    check(`${why}: the block is hidden and empty, the count gone, and nothing is said`,
          [host.style.display, host.innerHTML, badge.hidden, toasts], ['none', '', true, []]);
  }
});

await section('Fix posts say, shows the reply, and refreshes both lists', async () => {
  let flags = [flag(7, 'CNF', 3), flag(8, 'SQL', 1)];
  const server = p => {
    if (p === '/rules/flags/7/fix') { flags = flags.filter(f => f.id !== 7); return { ok: true, message: 'CNF will be said C.N.F from now on.' }; }
    return { flags };
  };
  const { b, host, sent, toasts, calls } = loadBlock(server);
  await b.refresh();
  const btn = fakeEl('btn');
  await b.fix('7', ' C.N.F ', btn); await ticks();
  check('say is what was typed, trimmed', sent[1], ['/rules/flags/7/fix', 'POST', { say: 'C.N.F' }]);
  check('the server\'s message is the toast, as good news', toasts, [['CNF will be said C.N.F from now on.', 'info']]);
  check('the block is asked for again', sent.filter(s => s[0] === '/rules/flags').length, 2);
  check('and so is the rules table', calls.rules, 1);
  check('the fixed flag has gone from the block', [host.innerHTML.includes('>CNF<'), host.innerHTML.includes('>SQL<')], [false, true]);
});

await section('a Fix that does nothing says so and changes nothing', async () => {
  {
    const { b, sent, toasts, calls } = loadBlock(() => ({ flags: [flag(7, 'CNF', 3)] }));
    await b.fix('7', '   ', fakeEl('btn')); await ticks();
    check('an empty box is not sent', sent, []);
    check('and says what to type', toasts.length === 1 && /C\.N\.F/.test(toasts[0][0]), true);
    check('nothing is refreshed', calls.rules, 0);
  }
  {
    const { b, toasts, calls } = loadBlock(p => (p.endsWith('/fix') ? { ok: false, message: 'That is not a word in the text.' } : { flags: [] }));
    const btn = fakeEl('btn');
    await b.fix('7', 'x', btn); await ticks();
    check('a refusal is shown as the error it is', toasts, [['That is not a word in the text.', 'error']]);
    check('the rules table is left alone and the button works again', [calls.rules, btn.disabled], [0, false]);
  }
  {
    const { b, toasts, calls } = loadBlock(p => (p.endsWith('/fix') ? new Error('Failed to fetch') : { flags: [] }));
    const btn = fakeEl('btn');
    await b.fix('7', 'x', btn); await ticks();
    check('a failed request is said, once', [toasts.length, /Failed to fetch/.test((toasts[0] || [''])[0])], [1, true]);
    check('and nothing is refreshed', [calls.rules, btn.disabled], [0, false]);
  }
});

await section('Dismiss removes the row', async () => {
  const { b, host, badge, sent } = loadBlock(p => (p.endsWith('/dismiss') ? { ok: true } : { flags: [flag(7, 'CNF', 3), flag(8, 'SQL', 1)] }));
  await b.refresh();
  await b.dismiss('7', fakeEl('btn')); await ticks();
  check('it posts to the flag', sent[1], ['/rules/flags/7/dismiss', 'POST']);
  check('the row is gone and the other stays', [host.innerHTML.includes('>CNF<'), host.innerHTML.includes('>SQL<')], [false, true]);
  check('the count follows', badge.textContent, '1');
  await b.dismiss('8', fakeEl('btn')); await ticks();
  check('dismissing the last one hides the block', [host.style.display, host.innerHTML, badge.hidden], ['none', '', true]);
});

await section('a redraw keeps what was typed in the other rows', async () => {
  const { b, setRows, queueRows } = loadBlock(() => ({ flags: [flag(7, 'CNF', 3), flag(8, 'SQL', 1)] }));
  await b.refresh();
  const row = (id, value, was) => { const inp = { value, defaultValue: was }; return { dataset: { flagId: id }, querySelector: () => inp, inp }; };
  setRows([row('7', 'C.N.F', 'C.N.F'), row('8', 'sequel', '')]);
  const after = [row('7', 'C.N.F', 'C.N.F'), row('8', '', '')];
  queueRows(after);
  b.render();
  check('the typed value is put back, the untouched one left as drawn', after.map(r => r.inp.value), ['C.N.F', 'sequel']);
});

// =============================================================================
// 4. Private reads
// =============================================================================
const priv = new Function(`
  ${HELPERS}
  ${dash.slice(dash.indexOf('const _FACET_ICON'), dash.indexOf('\n', dash.indexOf('const _FACET_ICON')))}
  ${grab(dash, '_chunkCardHTML')}
  return { card: _chunkCardHTML,
           isPrivate: ${or('isPrivateRow', '() => null')},
           text: ${or('chunkTextHTML', "() => ''")} };`)();
const LABEL = '🔒 Private read';

await section('a private read in the live feed', async () => {
  const row = { chunk_id: 'p-1', chunk_text: '', private: 1, seq: 4, total: 9, sentence_type: 'sentence', char_count: 0 };
  const h = priv.card(row, null, null);
  check('it says Private read', h.includes(LABEL), true);
  check('in the dim text style', /<span class="private-read">🔒 Private read<\/span>/.test(h), true);
  check('with its chunk number badge', h.includes('chunk 4/9'), true);
  check('both thumbs still act on the chunk id', (h.match(/class="chunk-thumb[^"]*"[^>]*data-chunk-id="p-1"/g) || []).length, 2);
  // The report affordance is the text block being a button. The thumbs are
  // real <button>s, so anything else carrying the role or a tab stop is it.
  const textDiv = (h.match(/<div class="chunk-text"[^>]*>/) || [''])[0];
  check('the text block offers no report', [textDiv, /role="button"|tabindex/.test(textDiv)], ['<div class="chunk-text">', false]);

  const open = priv.card({ chunk_id: 'c-1', chunk_text: 'Fish & <chips>', seq: 1, total: 2 }, 0.9, null);
  const openDiv = (open.match(/<div class="chunk-text"[^>]*>/) || [''])[0];
  check('an ordinary chunk shows its text, escaped', open.includes('Fish &amp; &lt;chips&gt;'), true);
  check('and can be reported from the keyboard', [/role="button"/.test(openDiv), /tabindex="0"/.test(openDiv)], [true, true]);
  check('and is not called private', open.includes(LABEL), false);
});

await section('what counts as private', async () => {
  check('the server\'s flag', [priv.isPrivate({ private: 1, chunk_text: '' }), priv.isPrivate({ private: true, chunk_text: '' })], [true, true]);
  check('an empty text with no flag, as the browser and AI routes send it', priv.isPrivate({ chunk_text: '' }), true);
  check('a missing or blank text too', [priv.isPrivate({}), priv.isPrivate({ chunk_text: '   ' }), priv.isPrivate({ chunk_text: null })], [true, true, true]);
  check('a chunk with text is not', [priv.isPrivate({ chunk_text: 'Hello', private: 0 }), priv.isPrivate({ chunk_text: 'Hello' })], [false, false]);
});

// The feed renderer, to see what the card itself carries.
await section('the feed card for a private read cannot be reported', async () => {
  const kids = [];
  const body = { scrollTop: 0, get children() { return kids; }, get firstElementChild() { return kids[0] || null; },
    get lastChild() { return kids[kids.length - 1] || null; }, querySelector: () => null,
    querySelectorAll: () => kids.slice(),
    insertBefore(card, ref) { const at = kids.indexOf(card); if (at >= 0) kids.splice(at, 1);
      const i = ref ? kids.indexOf(ref) : -1; if (i < 0) kids.push(card); else kids.splice(i, 0, card); } };
  const mkCard = () => { const c = { dataset: {}, title: '', innerHTML: '', className: '',
      classList: { contains: n => c.className.split(' ').includes(n) },
      get nextElementSibling() { return kids[kids.indexOf(c) + 1] || null; },
      remove() { const i = kids.indexOf(c); if (i >= 0) kids.splice(i, 1); } }; return c; };
  const doc = { getElementById: id => (id === 'chunks-body' ? body : fakeEl(id)), createElement: mkCard };
  const render = new Function('document', `
    ${HELPERS}
    ${dash.slice(dash.indexOf('const _FACET_ICON'), dash.indexOf('\n', dash.indexOf('const _FACET_ICON')))}
    const _pendingVerdict = new Map(), _FEED_CAP = 40;
    let _feedSelectMode = false, _chunkCount = 0;
    ${grab(dash, '_effectiveVerdict')}
    ${grab(dash, '_chunkCardHTML')}
    ${grab(dash, '_renderChunkFeed')}
    return _renderChunkFeed;`)(doc);
  render([
    { chunk_id: 'p-1', chunk_text: '', private: 1, seq: 2, total: 2, ts: 20, report_count: 1, report_issue: 'OTHER' },
    { chunk_id: 'c-1', chunk_text: 'An ordinary chunk', seq: 1, total: 2, ts: 10 },
  ]);
  const [p, c] = kids;
  check('newest first, private one included', kids.map(k => k.dataset.id), ['p-1', 'c-1']);
  // The click router opens a report only for a card with text in data-text,
  // so an empty one here is what keeps a private card out of the Report tab.
  check('the private card carries no text to report', [p.dataset.text, p.innerHTML.includes(LABEL)], ['', true]);
  check('it is marked private and does not say click to report', [p.classList.contains('private'), /click/i.test(p.title)], [true, false]);
  check('the ordinary card still does', [c.dataset.text, /Click chunk text to report/.test(c.title), c.classList.contains('private')],
        ['An ordinary chunk', true, false]);
});

await section('the AI feed and the chunk browser take an empty text', async () => {
  {
    const doc = fakeDocument();
    const feed = new Function('document', `
      ${HELPERS}
      ${grab(dash, '_explainChunk')}
      ${grab(dash, '_renderAiFeed')}
      return _renderAiFeed;`)(doc);
    feed([{ chunk_id: 'p-1', chunk_text: '', seq: 2, total: 2, quality_score: 0.4, quality_flags: 'CUTOFF' },
          { chunk_id: 'p-2', chunk_text: null, seq: 3, total: 3 },
          { chunk_id: 'c-1', chunk_text: 'Say "hello" <now>', seq: 1, total: 2, quality_score: 0.9 }]);
    const out = doc.els['ai-feed-body'].innerHTML;
    check('both empty rows read Private read, not a blank', (out.match(/<div class="ai-text-preview"><span class="private-read">🔒 Private read<\/span><\/div>/g) || []).length, 2);
    check('a chunk with text shows it, escaped', out.includes('Say "hello" &lt;now&gt;</div>'), true);
    check('and its quote cannot close the attribute it is also kept in', out.includes('data-full-text="Say &quot;hello&quot; &lt;now&gt;"'), true);
  }
  {
    const doc = fakeDocument();
    const cards = [];
    doc.els['browser-body'] = Object.assign(fakeEl('browser-body'), { appendChild: c => cards.push(c) });
    const load = new Function('document', 'api', `
      ${HELPERS}
      ${dash.slice(dash.indexOf('const _FACET_ICON'), dash.indexOf('\n', dash.indexOf('const _FACET_ICON')))}
      let _browserFilter = 'low', _browserOffset = 0;
      ${grab(dash, 'explainChunkAnalysis')}
      ${grab(dash, 'loadBrowser')}
      return loadBrowser;`)(doc, async () => ({ total: 2, chunks: [
        { chunk_id: 'p-1', chunk_text: '', quality_score: 0.3, quality_flags: 'CUTOFF' },
        { chunk_id: 'c-1', chunk_text: 'Kept text', quality_score: 0.3 }] }));
    load('low', 0); await ticks();
    check('the browser lists both', cards.length, 2);
    check('the private one says so and cannot be picked for a report',
          [cards[0].innerHTML.includes(LABEL), cards[0].dataset.text, /click/i.test(cards[0].title)], [true, '', false]);
    check('the other is as before', [cards[1].innerHTML.includes('Kept text'), cards[1].dataset.text, cards[1].title],
          [true, 'Kept text', 'Click to select for report']);
  }
});

// =============================================================================
// 5. Opening the dashboard when a read starts
// =============================================================================
await section('the open-on-read setting', async () => {
  const f = new Function(`${opt(dash, 'openOnReadFromStore')} return ${or('openOnReadFromStore', '() => null')};`)();
  check('a key that was never written means yes', [f({}), f(undefined), f({ kamOpenDashboardOnRead: undefined })], [true, true, true]);
  check('a stored false turns it off', f({ kamOpenDashboardOnRead: false }), false);
  check('a stored true keeps it on', f({ kamOpenDashboardOnRead: true }), true);
  const box = tagOf('set-open-on-read');
  check('there is a checkbox for it in the settings panel', /type="checkbox"/.test(box), true);
  const panel = html.slice(html.indexOf('<div id="settings-panel"'), html.indexOf('<!-- The TTS Tuning dropdown'));
  check('inside the cog panel, with its label', [panel.includes('id="set-open-on-read"'), panel.includes('Open this dashboard when a read starts')], [true, true]);
  check('it reads the key the worker reads', /chrome\.storage\.local\.get\('kamOpenDashboardOnRead'/.test(dash), true);
  check('and writes it', /chrome\.storage\.local\.set\(\{ kamOpenDashboardOnRead:/.test(dash), true);
});

// =============================================================================
// 6. Keyboard and screen-reader basics
// =============================================================================
await section('clickable divs can be pressed from the keyboard', async () => {
  const k = new Function(`${opt(dash, 'keyActivates')} return ${or('keyActivates', '() => null')};`)();
  const el = sel => ({ matches: s => s.split(',').map(x => x.trim()).includes(sel) });
  const roleBtn = el('[role="button"][tabindex]'), th = el('th.rule-sort[tabindex]'), plain = el('div');
  check('Enter and Space press a role button', [k('Enter', roleBtn), k(' ', roleBtn)], [true, true]);
  check('and a sortable rule header', k('Enter', th), true);
  check('other keys do not', [k('a', roleBtn), k('Tab', roleBtn), k('Escape', roleBtn)], [false, false, false]);
  check('and nothing else is pressed', [k('Enter', plain), k('Enter', null), k(' ', {})], [false, false, false]);
  check('the keys are listened for', /addEventListener\('keydown',[^)]*=>\s*\{\s*if \(!keyActivates\(e\.key, e\.target\)\) return;/.test(dash), true);

  const heads = dash.match(/<div class="(?:ai-section-header|ro-toggle|lrn-toggle)"[^>]*>/g) || [];
  check('every collapsible header built in dashboard.js is a button with a tab stop',
        [heads.length >= 4, heads.every(h => /role="button"/.test(h) && /tabindex="0"/.test(h) && /aria-expanded=/.test(h))], [true, true]);
  const ths = html.match(/<th class="rule-sort"[^>]*>/g) || [];
  check('every sortable rule header has a tab stop', [ths.length, ths.every(t => /tabindex="0"/.test(t))], [5, true]);
  const tabs = html.match(/<button class="tab-btn[^"]*" data-tab="[a-z]+"/g) || [];
  check('the tab labels are real buttons', tabs.length, 6);
});

await section('icon-only buttons have a name', async () => {
  for (const id of ['power-btn', 'feed-pause', 'feed-select-toggle', 'feed-sel-cancel', 'clear-btn',
                    'settings-cog', 'scan-pill', 'rec-open', 'ai-feed-refresh']) {
    check(`#${id} has an aria-label`, (attr(tagOf(id), 'aria-label') || '').length > 3, true);
  }
  const card = priv.card({ chunk_id: 'c', chunk_text: 'x' }, null, 'up');
  const thumbs = card.match(/<button class="chunk-thumb[^>]*>/g) || [];
  check('both thumbs have one', thumbs.map(t => (attr(t, 'aria-label') || '').length > 3), [true, true]);
  check('and say which is the chosen one', thumbs.map(t => attr(t, 'aria-pressed')), ['true', 'false']);

  // The pause button's name follows what it does.
  const btn = fakeEl('feed-pause');
  const doc = fakeDocument(); doc.els['feed-pause'] = btn;
  const setPaused = new Function('document', `
    let _feedPaused = false, _feedPendingRows = null;
    const _renderChunkFeed = () => {};
    ${grab(dash, '_setFeedPaused')}
    return _setFeedPaused;`)(doc);
  setPaused(true);
  const paused = btn.ariaLabel;
  setPaused(false);
  check('pause feed is named for what a press will do', [paused, btn.ariaLabel], ['Resume the live feed', 'Pause the live feed']);
});

await section('what changes is announced', async () => {
  const status = tagOf('server-status');
  check('the server status line is a polite live region', attr(status, 'aria-live'), 'polite');
  const doc = fakeDocument();
  const made = [];
  doc.createElement = () => { const e = fakeEl('toast'); made.push(e); return e; };
  const appended = [];
  doc.body.appendChild = e => appended.push(e);
  const t = new Function('document', 'setTimeout', 'clearTimeout', `
    let _toastEl = null;
    ${opt(dash, '_ensureToast')}
    ${grab(dash, 'showToast')}
    return { showToast, ensure: ${or('_ensureToast', '() => {}')} };`)(doc, () => 0, () => {});
  t.ensure();
  check('the toast is there before its first message, so that message is heard', [made.length, appended.length], [1, 1]);
  t.showToast('Saved.', 'info');
  check('one toast, reused', made.length, 1);
  check('it is a polite live region', (made[0] || fakeEl('')).attrs['aria-live'], 'polite');
  check('and carries the message', (made[0] || {}).textContent, 'Saved.');
  check('the status pill is only written when it changes, so it is not announced every poll',
        /el\.textContent !== '● online'/.test(grab(dash, 'pollConsole')), true);
});

await section('no inline handlers anywhere', async () => {
  check('none in player.html', (html.match(/<[^>]*\son[a-z]+=["']/g) || []).length, 0);
  check('none in markup built by dashboard.js', (dash.match(/<[^>]*\son[a-z]+=["']/g) || []).length, 0);
});

// =============================================================================
// 7. Wording that follows the server
// =============================================================================
await section('the temperature control is described as a master dial', async () => {
  const at = html.indexOf('id="tp-temp"');
  // The temperature card: from its own opening tag to the next card's.
  const card = html.slice(html.lastIndexOf('<div class="tune-param">', at), html.indexOf('<div class="tune-param">', at));
  check('it says master dial', /master dial/i.test(card), true);
  check('and that it scales the learned values', /scales/i.test(card) && /learn/i.test(card), true);
  check('in the tooltip too', /master dial/i.test(attr(tagOf('tp-temp'), 'title') || ''), true);
});

await section('nothing still promises per-profile speed learning', async () => {
  const map = new Function(`${grabConst(dash, 'ISSUE_ACTION_MAP')} return ISSUE_ACTION_MAP;`)();
  for (const issue of ['TOO_FAST', 'TOO_SLOW']) {
    check(`${issue} no longer says "this kind of sentence"`, /kind of sentence/.test(map[issue].desc), false);
    check(`${issue} says the pace is per complexity`, /complexity/.test(map[issue].desc), true);
  }
  check('the learned panel no longer says rates follow anything but the band',
        /Pace is learned per complexity band only/.test(html), true);
  check('and says the dial scales the learned temperatures',
        /Temperatures apply per sentence type\./.test(html), false);
});

console.log(`\n${PASS} passed, ${FAIL} failed`);
process.exit(FAIL?1:0);
