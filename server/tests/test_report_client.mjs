// The client half of the report loop: which chunk the popup and dashboard
// think is playing, the id a report carries, what the report form asks for and
// pre-fills, what the dashboard says KAM did, and how many temperature steps a
// mass thumbs-down takes. Each part lifts the real functions out of the
// extension and runs them against a fake Chrome and a tiny fake DOM.
//
// KAM_EXT_DIR points the suite at another copy of the extension, which is how
// I check that these tests fail against the code from before the fixes.
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const EXT = process.env.KAM_EXT_DIR || path.join(HERE, '..', '..', 'extension');
const read = f => fs.readFileSync(path.join(EXT, f), 'utf8');
const bg = read('background.js');
const dash = read('dashboard.js');
const popup = read('popup.js');

let PASS=0, FAIL=0;
const check=(l,g,w)=>{ const ok=JSON.stringify(g)===JSON.stringify(w);
  ok?(PASS++,console.log('  ok   '+l)):(FAIL++,console.log(`  FAIL ${l}\n         got ${JSON.stringify(g)} want ${JSON.stringify(w)}`)); };

const grab = (src, name) => {
  let i = src.indexOf(`function ${name}(`);
  if (i < 0) throw new Error(`function ${name} not found`);
  if (src.slice(Math.max(0, i - 6), i) === 'async ') i -= 6;   // keep it async
  // Step over the parameter list first, since a default like opts = {} would
  // otherwise close the brace count before the body has even started.
  let p=0, j=src.indexOf('(', i);
  for (; j<src.length; j++){ if(src[j]==='(')p++; else if(src[j]===')'){p--; if(!p){j++;break;} } }
  let d=0;
  for (; j<src.length; j++){ if(src[j]==='{')d++; else if(src[j]==='}'){d--; if(!d){j++;break;} } }
  return src.slice(i,j);
};
// The same, but empty when the function is not there, for code that only the
// fixed version has. The old version then fails on behaviour, not on a throw.
const opt = (src, name) => { try { return grab(src, name); } catch (e) { return ''; } };
// A top-level const object, from its declaration to the closing "};".
const grabConst = (src, name) => {
  const i = src.indexOf(`const ${name} = {`);
  if (i < 0) return '';
  return src.slice(i, src.indexOf('\n};', i) + 3);
};
const grabListener = (src, head) => {
  const i = src.indexOf(head);
  if (i < 0) throw new Error('listener not found');
  let d=0, j=src.indexOf('{', i);
  for (; j<src.length; j++){ if(src[j]==='{')d++; else if(src[j]==='}'){d--; if(!d){j++;break;} } }
  return src.slice(i, j) + ');';
};

async function section(title, fn) {
  console.log(`\n=== ${title} ===`);
  try { await fn(); }
  catch (e) { FAIL++; console.log(`  FAIL ${title} threw: ${e && e.message}`); }
}
const tick = () => new Promise(r => setTimeout(r, 0));
const ticks = async (n = 8) => { for (let i = 0; i < n; i++) await tick(); };

function fakeEl(id) {
  const cls = new Set();
  return {
    id, value: '', textContent: '', placeholder: '', style: {}, dataset: {},
    classList: { add: c => cls.add(c), remove: c => cls.delete(c), contains: c => cls.has(c),
                 toggle: (c, on) => { const v = on === undefined ? !cls.has(c) : on; v ? cls.add(c) : cls.delete(c); return v; } },
  };
}
function fakeDocument(values = {}) {
  const els = {};
  const doc = { els, getElementById: id => (els[id] ||= Object.assign(fakeEl(id), values[id] !== undefined ? { value: values[id] } : {})) };
  return doc;
}

// =============================================================================
// 1. The chunk the popup and dashboard rate is the one playing
// =============================================================================
await section('chunkReady is sent as each chunk starts to play', async () => {
  const events = [];
  const holder = {};
  const chrome = { runtime: { sendMessage: m => {
    if (m.action === 'chunkReady') events.push(['ready', m.chunkId]);
    return Promise.resolve();
  } } };
  const kamFetch = async (url, opts) => {
    const idx = JSON.parse(opts.body).index - 1;
    return { ok: true, headers: { get: () => 'id-' + idx }, arrayBuffer: async () => new Uint8Array(48).buffer };
  };
  const toPlayerAsync = async msg => {
    events.push(['play', msg.seq]);
    setTimeout(() => { const f = holder.w.pending[msg.seq]; if (f) f({ status: 'finished' }); }, 5);
    return { status: 'received' };
  };
  holder.w = new Function('chrome', 'kamFetch', 'toPlayerAsync', 'sanitizeText', 'detectPositionHint',
    'ensurePlayer', 'ensureOffscreen', 'toReadingTab', 'digestPlayedChunks', 'SERVER', `
    let isStopped = false, isPaused = false, isPlaying = false, currentChunkIndex = 0;
    let allChunks = ['first chunk here', 'second chunk here', 'third chunk here'];
    let displayChunks = allChunks.slice();
    let chunkIdByIndex = {}, playedChunks = [], sessionId = 1, _playSeqCounter = 0, _pendingPlay = {};
    let _hwPrefetchTarget = 3, KAM_TOKEN = 't', readingTabId = null, currentFetchAbort = null;
    const _speakAborts = new Set();
    ${grab(bg, 'fetchAudioBase64')}
    ${opt(bg, 'announceChunk')}
    ${grab(bg, 'speakChunks')}
    return { speakChunks, get pending() { return _pendingPlay; }, stop() { isStopped = true; } };`)(
    chrome, kamFetch, toPlayerAsync, t => t, () => null,
    async () => {}, async () => {}, () => {}, () => {}, 'http://s');
  await holder.w.speakChunks(1);
  holder.w.stop();
  check('each chunk is announced right after it starts, never ahead of it', events,
        [['play', 1], ['ready', 'id-0'], ['play', 2], ['ready', 'id-1'], ['play', 3], ['ready', 'id-2']]);
});

await section('announceChunk names the playing chunk, with its id and index', async () => {
  const sent = [];
  const a = new Function('chrome', `
    let chunkIdByIndex = { 1: 'srv-b' };
    let allChunks = ['a text', 'b text'];
    ${grab(bg, 'announceChunk')}
    return announceChunk;`)({ runtime: { sendMessage: m => { sent.push(m); return Promise.resolve(); } } });
  a(1); a(0);
  check('the id, text and index go together; no id means no announcement', sent,
        [{ action: 'chunkReady', chunkId: 'srv-b', chunkText: 'b text', index: 1 }]);
});

// =============================================================================
// 2. A report carries the real chunk id from the popup to the dashboard
// =============================================================================
await section('popup keeps each chunk id and puts it in the hand-off', async () => {
  let listener = null;
  const chrome = { runtime: { onMessage: { addListener: f => { listener = f; } } } };
  const p = new Function('chrome', `
    let _lastChunkId = null, _lastChunkText = '', _chunkIds = {};
    function _showFeedbackButtons() {}
    ${grabListener(popup, 'chrome.runtime.onMessage.addListener((request) => {')}
    return { get ids() { return _chunkIds; }, get id() { return _lastChunkId; } };`)(chrome);
  listener({ action: 'chunkReady', chunkId: 'srv-7', chunkText: 'Seven', index: 6 });
  check('the id is kept against its chunk index', p.ids, { 6: 'srv-7' });
  check('and is the one the thumbs act on', p.id, 'srv-7');
  check('the ⚑ flag reports the chunk with its own id',
        /reportHash\(text, _chunkIds\[i\]\)/.test(popup), true);
  check('a popup opened mid-read is given the ids', /_chunkIds\s*=\s*Object\.assign\(\{\}, response\.chunkIds/.test(popup), true);
});

await section('popup thumbs-down opens the report with the id', async () => {
  let opened = null;
  const chrome = { runtime: { getURL: f => 'chrome-extension://x/' + f },
                   tabs: { query: (q, cb) => cb([]), create: o => { opened = o.url; } } };
  const t = new Function('chrome', 'document', `
    let _lastChunkId = 'srv-9', _lastChunkText = 'Fish & chips', _feedbackOpen = false;
    ${opt(popup, 'reportHash')}
    ${grab(popup, '_hideFeedbackButtons')}
    ${grab(popup, 'thumbsDown')}
    return thumbsDown;`)(chrome, fakeDocument());
  t();
  check('the hand-off carries text and id', opened, 'chrome-extension://x/player.html#report:Fish%20%26%20chips&id=srv-9');
});

// The dashboard side of the hand-off, with the real form sync around it.
function loadHashHandler(issue, hash) {
  const doc = fakeDocument({ 'r-issue': issue });
  const shown = [];
  const h = new Function('window', 'history', 'document', 'showTab', `
    let _lastChunkText = '', _lastChunkId = 'stale', _reportLocked = false;
    ${opt(dash, 'parseReportHash')}
    ${grabConst(dash, 'REPORT_FIELDS')}
    ${dash.includes('const REPORT_FIELD_DEFAULT') ? dash.slice(dash.indexOf('const REPORT_FIELD_DEFAULT'), dash.indexOf('\n', dash.indexOf('const REPORT_FIELD_DEFAULT'))) : ''}
    ${opt(dash, 'reportFieldSpec')}
    ${opt(dash, 'suggestedToken')}
    ${opt(dash, '_syncReportForm')}
    ${grab(dash, '_handleReportHash')}
    return { run: _handleReportHash, sync: typeof _syncReportForm === 'function' ? _syncReportForm : () => {},
             get id() { return _lastChunkId; }, get text() { return _lastChunkText; } };`)(
    { location: { hash, pathname: '/player.html' } }, { replaceState() {} }, doc, t => shown.push(t));
  return { h, doc, shown };
}

await section('dashboard takes the id from the hand-off', async () => {
  {
    const { h, shown } = loadHashHandler('PRONUNCIATION', '#report:Fish%20%26%20chips&id=srv-9');
    check('handled', h.run(), true);
    check('the text is whole, & and all', h.text, 'Fish & chips');
    check('the real id is used, not dropped', h.id, 'srv-9');
    check('the report tab opens', shown, ['report']);
  }
  {
    const { h } = loadHashHandler('PRONUNCIATION', '#report:Old%20style');
    h.run();
    check('an old hand-off without an id still works', [h.text, h.id], ['Old style', null]);
  }
});

// =============================================================================
// 3. The token is only pre-filled for a pronunciation report
// =============================================================================
await section('the token pre-fill', async () => {
  {
    const { h, doc } = loadHashHandler('HALLUCINATION', '#report:The%20BOW%20model%20works&id=x');
    h.run();
    check('a hallucination report starts with no token, so nothing gets blacklisted by default',
          doc.els['r-token'].value, '');
  }
  {
    const { h, doc } = loadHashHandler('PRONUNCIATION', '#report:The%20BOW%20model%20works&id=x');
    h.run();
    check('a pronunciation report suggests the word in capitals', doc.els['r-token'].value, 'BOW');
    doc.els['r-issue'].value = 'HALLUCINATION';
    h.sync();
    check('switching away clears the suggestion', doc.els['r-token'].value, '');
    doc.els['r-issue'].value = 'PRONUNCIATION';
    h.sync();
    doc.els['r-token'].value = 'model'; delete doc.els['r-token'].dataset.auto;   // the user typed
    doc.els['r-issue'].value = 'SKIP';
    h.sync();
    check('a word the user typed is kept whatever the issue', doc.els['r-token'].value, 'model');
    check('and the box says what SKIP needs', doc.els['r-token-label'].textContent, 'Word that was dropped');
  }
});

// =============================================================================
// 4. What the dashboard says happened
// =============================================================================
await section('the chunk explanation only claims what the server does', async () => {
  const explain = new Function(`
    function esc(s) { return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }
    ${grab(dash, '_explainChunk')}
    return _explainChunk;`)();
  const halluc = explain({ quality_flags: 'HALLUCINATION', quality_score: 0.5 }).fix;
  check('a hallucination flag claims no temperature change', /temperature (nudged|lowered|auto)/i.test(halluc) && !/Not fixed/.test(halluc), false);
  check('and says it was not fixed', /Not fixed automatically/.test(halluc), true);
  const cut = explain({ quality_flags: 'CUTOFF', quality_score: 0.5 }).fix;
  check('a cut-off claims no split adjustment', /split/i.test(cut), false);
  const acc = explain({ whisper_accuracy: 0.7, quality_score: 0.8 }).fix;
  check('low accuracy in a chunk the auto-tune skips claims no tuning', /0\.02/.test(acc), false);
  const flat = explain({ pitch_variance: 4, whisper_accuracy: 0.9, quality_score: 0.57 }).fix;
  check('an accurate flat chunk in the auto-tune band says the profile went up', /raised the temperature/.test(flat), true);
  const rated = explain({ quality_flags: 'HALLUCINATION', applied_action: 'Hallucination logged. Temperature lowered to 0.3.' }).fix;
  check('the server\'s own record comes first where there is one', rated.startsWith('What KAM did after your rating: Hallucination logged.'), true);
});

// submitReport with its real dependencies and a stub server.
function loadSubmit(fields, reply) {
  const doc = fakeDocument(Object.assign({ 'r-action': 'AUTO', 'r-confidence': 'HIGH' }, fields));
  const sent = [], toasts = [];
  const s = new Function('document', 'api', 'showToast', 'addLog', 'refreshReports', 'setTimeout', `
    let _lastChunkText = 'Some chunk text here', _lastChunkId = 'srv-1', _reportLocked = true;
    ${grabConst(dash, 'ISSUE_ACTION_MAP')}
    ${grabConst(dash, 'REPORT_FIELDS')}
    ${dash.includes('const REPORT_FIELD_DEFAULT') ? dash.slice(dash.indexOf('const REPORT_FIELD_DEFAULT'), dash.indexOf('\n', dash.indexOf('const REPORT_FIELD_DEFAULT'))) : ''}
    ${opt(dash, 'reportFieldSpec')}
    ${opt(dash, 'reportReplyText')}
    ${grab(dash, 'clearReport')}
    ${grab(dash, 'submitReport')}
    return submitReport;`)(doc, async (p, m, body) => { sent.push(body); return reply; },
                          t => toasts.push(t), () => {}, () => {}, () => 0);
  return { s, doc, sent, toasts };
}

await section('the report form asks for the word SKIP, CUTOFF and PUNCT need', async () => {
  for (const issue of ['SKIP', 'CUTOFF', 'PUNCT']) {
    const { s, sent, toasts } = loadSubmit({ 'r-issue': issue }, {});
    s(); await ticks();
    check(`${issue} without a word is not sent`, sent.length, 0);
    check(`${issue} says what to type`, toasts.length === 1 && toasts[0].length > 20, true);
  }
  const { s, sent } = loadSubmit({ 'r-issue': 'SKIP', 'r-token': 'gravity' }, { applied: true, result: 'ok' });
  s(); await ticks();
  check('with the word it is sent, with the chunk id', [sent.length, sent[0] && sent[0].token, sent[0] && sent[0].chunk_id], [1, 'gravity', 'srv-1']);
});

await section('the server\'s reply is what the form shows', async () => {
  const shows = async reply => {
    const { s, doc } = loadSubmit({ 'r-issue': 'TOO_SLOW' }, reply);
    s(); await ticks();
    return doc.els['submit-result'].textContent;
  };
  check('a rule made', await shows({ applied: true, result: 'blacklisted token: foo' }), 'blacklisted token: foo');
  check('nothing done is not called applied', await shows({ applied: true, result: 'no_action' }),
        'Logged. Nothing was changed for this report.');
  check('a failure says so', await shows({ applied: true, result: 'error: disk full' }),
        'Logged, but the server could not act on it (disk full).');
  check('the server\'s message wins', await shows({ applied: true, result: 'x', message: 'Already at the fastest speed.' }),
        'Already at the fastest speed.');
  check('pending says why', await shows({ pending: true, reason: 'MEDIUM confidence: logged.' }), 'MEDIUM confidence: logged.');
});

// =============================================================================
// 5. A mass thumbs-down steps each profile once
// =============================================================================
await section('mass thumbs-down counts once per profile', async () => {
  const card = (id, rejected) => ({ dataset: { id, text: 'text of ' + id },
    querySelector: sel => (sel === '.chunk-thumb-down' ? { classList: { contains: c => c === 'rejected' && rejected } } : null) });
  const cards = [card('a1'), card('a2'), card('b1', true), card('b2'), card('c1')];
  const profiles = { a1: 'mid|short|clean|plain', a2: 'mid|short|clean|plain',
                     b1: 'dense|long|commas|technical', b2: 'dense|long|commas|technical' };
  const verdicts = [], toasts = [];
  const api = async (p, m, body) => {
    if (p.startsWith('/diagnose/')) {
      const id = decodeURIComponent(p.slice(10));
      if (!profiles[id]) throw new Error('no record');
      return { found: true, labelling: { sentence_type: 'statement', profile: profiles[id], has_math: 0 } };
    }
    if (p === '/chunk/verdict') { verdicts.push(body.record_only ? [body.chunk_id, body.verdict, 'record_only'] : [body.chunk_id, body.verdict]); return { applied_text: 'ok' }; }
    return {};
  };
  const v = new Function('api', 'showToast', 'refreshStats', 'cards', `
    function _selectedCards() { return cards; }
    function _paintCardVerdict() {}
    function _clearSelection() {}
    ${opt(dash, 'profileKeyFromDiagnosis')}
    ${opt(dash, 'planMassRejection')}
    ${opt(dash, 'massRejectionSummary')}
    ${opt(dash, '_massReject')}
    ${grab(dash, '_massVerdict')}
    return _massVerdict;`)(api, t => toasts.push(t), () => {}, cards);
  v('sounded_wrong');
  await ticks(20);
  const stepped = verdicts.filter(v => v.length === 2), recordedOnly = verdicts.filter(v => v[2] === 'record_only');
  check('one step per profile, nothing re-sent for a chunk already rejected, an unknown one alone',
        stepped, [['a1', 'sounded_wrong'], ['c1', 'sounded_wrong']]);
  check('the rest of a stepped profile are recorded as rejected without a second step',
        recordedOnly.length, 2);
  check('and the toast says so', /2 chunks marked hallucination.*2 more recorded as rejected.*1 already rejected/.test(toasts[0] || ''), true);
  verdicts.length = 0;
  v('sounded_perfect');
  await ticks(20);
  check('a mass thumbs-up still rates every chunk', verdicts.length, 5);
});

console.log(`\n${PASS} passed, ${FAIL} failed`);
process.exit(FAIL?1:0);
