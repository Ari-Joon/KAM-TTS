// Regression tests for twelve extension defects found in one review. Each part
// lifts the real code out of the extension, gives it a fake Chrome and a tiny
// fake DOM, and checks the behaviour that used to be wrong. Where a whole file
// has to run (content.js, offscreen.js, scan-ui.js, recorder-ui.js) it is
// evaluated as-is with only the browser around it faked.
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

// The worker's message handler is an anonymous arrow, so it is taken whole,
// from the addListener call to its closing brace.
const grabListener = (src, head) => {
  const i = src.indexOf(head);
  if (i < 0) throw new Error('listener not found');
  let d=0, j=src.indexOf('{', i);
  for (; j<src.length; j++){ if(src[j]==='{')d++; else if(src[j]==='}'){d--; if(!d){j++;break;} } }
  return src.slice(i, j) + ');';
};

// A section that throws (a function missing from older code, say) counts as a
// failure rather than ending the run, so every part still reports.
async function section(title, fn) {
  console.log(`\n=== ${title} ===`);
  try { await fn(); }
  catch (e) { FAIL++; console.log(`  FAIL ${title} threw: ${e && e.message}`); }
}

const tick = () => new Promise(r => setTimeout(r, 0));
const ticks = async (n = 6) => { for (let i = 0; i < n; i++) await tick(); };

// --- Small fakes shared by several parts ---
function fakeStorage(init = {}) {
  const store = { ...init };
  return {
    store,
    get: async k => (typeof k === 'string' ? { [k]: store[k] } : { ...store }),
    set: async o => { Object.assign(store, o); },
    remove: async k => { delete store[k]; },
  };
}

function fakeEl(id) {
  const listeners = {};
  const cls = new Set();
  return {
    id, textContent: '', title: '', value: '', disabled: false, innerHTML: '',
    style: {}, dataset: {}, children: [], parentNode: null, clientWidth: 600,
    listeners,
    classList: { add: (...c) => c.forEach(x => cls.add(x)), remove: (...c) => c.forEach(x => cls.delete(x)),
                 contains: c => cls.has(c), toggle: (c, on) => { const v = on === undefined ? !cls.has(c) : on; v ? cls.add(c) : cls.delete(c); return v; } },
    addEventListener(t, f) { (listeners[t] ||= []).push(f); },
    fire(t, ev = {}) { (listeners[t] || []).forEach(f => f(ev)); if (this['on' + t]) this['on' + t](ev); },
    setAttribute() {}, focus() {}, setPointerCapture() {}, remove() {},
    appendChild(c) { this.children.push(c); c.parentNode = this; return c; },
    removeChild(c) { this.children = this.children.filter(x => x !== c); c.parentNode = null; return c; },
    getContext: () => ({ setTransform() {}, clearRect() {}, fillRect() {}, globalAlpha: 1, fillStyle: '' }),
    getBoundingClientRect: () => ({ left: 0, width: 600 }),
    querySelector: () => null, querySelectorAll: () => [],
  };
}

function fakeDocument() {
  const els = {};
  const listeners = {};
  return {
    els, listeners, readyState: 'complete',
    documentElement: {}, head: fakeEl('head'), body: fakeEl('body'),
    getElementById: id => (els[id] ||= fakeEl(id)),
    addEventListener(t, f) { (listeners[t] ||= []).push(f); },
    fire(t, ev = {}) { (listeners[t] || []).forEach(f => f(ev)); },
    querySelectorAll: () => [], createElement: t => fakeEl(t),
  };
}

// =============================================================================
// #4 a stale token is dropped and fetched again, once
// =============================================================================
function tokenServer(issue, accept) {
  const sent = [];
  let tokenCalls = 0;
  const fetch = async (url, opts = {}) => {
    if (url.endsWith('/token')) { tokenCalls++; return { ok: true, json: async () => ({ token: issue.shift() }) }; }
    const tok = opts.headers && opts.headers['X-KAM-Token'];
    sent.push(tok);
    return { ok: tok === accept, status: tok === accept ? 200 : 403 };
  };
  return { fetch, sent, get tokenCalls() { return tokenCalls; } };
}

await section('worker: kamFetch recovers from a stale cached token', async () => {
  const load = (storage, srv) => new Function('chrome', 'fetch', 'SERVER', `
    let KAM_TOKEN = "";
    ${grab(bg, '_ensureToken')}
    ${grab(bg, '_dropToken')}
    ${grab(bg, 'kamFetch')}
    return { kamFetch, get tok() { return KAM_TOKEN; } };`)({ storage: { local: storage } }, srv.fetch, 'http://s');
  {
    const st = fakeStorage({ kamToken: 'old' }), srv = tokenServer(['new'], 'new');
    const w = load(st, srv);
    const r = await w.kamFetch('http://s/settings', {});
    check('a 403 with the old token is retried with the new one', srv.sent, ['old', 'new']);
    check('and the retry is what the caller gets', r.status, 200);
    check('storage now holds the new token', st.store.kamToken, 'new');
  }
  {
    const st = fakeStorage({ kamToken: 'same' }), srv = tokenServer(['same'], 'nobody');
    const w = load(st, srv);
    const r = await w.kamFetch('http://s/settings', {});
    check('when the server hands back the same token there is no second try', srv.sent, ['same']);
    check('and the 403 is returned as it was', r.status, 403);
  }
  {
    const st = fakeStorage({ kamToken: 'good' }), srv = tokenServer([], 'good');
    const w = load(st, srv);
    await w.kamFetch('http://s/settings', {});
    check('an accepted token never asks for another', [srv.sent, srv.tokenCalls], [['good'], 0]);
  }
});

await section('popup: kamFetch recovers from a stale cached token', async () => {
  const st = fakeStorage({ kamToken: 'old' }), srv = tokenServer(['new'], 'new');
  const p = new Function('chrome', 'fetch', `
    let KAM_TOKEN = "";
    let _tokenPromise = null;
    ${grab(popup, '_ensureToken')}
    ${grab(popup, 'kamFetch')}
    return { kamFetch };`)({ storage: { local: st } }, srv.fetch);
  const r = await p.kamFetch('http://127.0.0.1:5050/settings', {});
  check('retried once with the fresh token', [srv.sent, r.status], [['old', 'new'], 200]);
  check('storage updated', st.store.kamToken, 'new');
});

await section('dashboard proxy: a refused token is not reported as ok', async () => {
  const reply = new Function(`${grab(bg, '_dashboardReply')} return _dashboardReply;`)();
  const json = (status, body) => ({ status, ok: status < 300, json: async () => body });
  check('403 is an error', (await reply(json(403, { error: 'unauthorised' }))).ok, false);
  check('401 is an error too', (await reply(json(401, {}))).ok, false);
  check('200 passes its data', await reply(json(200, { a: 1 })), { ok: true, data: { a: 1 } });
  check('a 400 still passes its explanation through', await reply(json(400, { ok: false, reason: 'why' })),
        { ok: true, data: { ok: false, reason: 'why' } });
});

await section('recorder: recFetch recovers from a stale token', async () => {
  const srv = tokenServer(['old', 'new'], 'new');
  const src = read('recorder-ui.js');
  const r = new Function('fetch', `
    let _recToken = null;
    ${grab(src, 'recToken')}
    ${grab(src, 'recFetch')}
    return { recFetch };`)(srv.fetch);
  const res = await r.recFetch('/voices');
  check('retried once with the fresh token', [srv.sent, res.status], [['old', 'new'], 200]);
});

// =============================================================================
// #1 teardown aborts every request, and a superseded one changes nothing
// #7 teardown tells the pages playback is no longer paused
// =============================================================================
function loadPlayback({ kamFetch, paused = false } = {}) {
  const bcast = [], tabMsgs = [];
  const chrome = {
    runtime: { sendMessage: m => { bcast.push(m); return Promise.resolve(); } },
    tabs: { sendMessage: (id, m) => { tabMsgs.push([id, m.action]); return Promise.resolve(); } },
  };
  const w = new Function('chrome', 'kamFetch', 'stopPlayerAudio', 'sanitizeText', 'detectPositionHint', 'SERVER', `
    let sessionId = 1, isStopped = false, isPaused = ${paused}, isPlaying = true;
    let readingTabId = 5, _pendingPlay = {}, chunkIdByIndex = {}, KAM_TOKEN = "t";
    const _speakAborts = new Set();
    ${grab(bg, 'toReadingTab')}
    ${grab(bg, 'teardownPlayback')}
    ${grab(bg, 'fetchAudioBase64')}
    return { teardownPlayback, fetchAudioBase64, aborts: _speakAborts,
             get sessionId() { return sessionId; }, set sessionId(v) { sessionId = v; },
             get ids() { return chunkIdByIndex; }, get paused() { return isPaused; } };`)(
    chrome, kamFetch, () => {}, t => t, () => null, 'http://s');
  return { w, bcast, tabMsgs };
}

await section('every in-flight /speak is aborted on teardown', async () => {
  const signals = [];
  const hanging = (url, opts) => new Promise((res, rej) => {
    signals.push(opts.signal);
    opts.signal.addEventListener('abort', () => rej(Object.assign(new Error('aborted'), { name: 'AbortError' })));
  });
  const { w } = loadPlayback({ kamFetch: hanging });
  const runs = [0, 1, 2].map(i => w.fetchAudioBase64('chunk number ' + i, i, 1).catch(e => e.name));
  await ticks();
  check('three requests in flight', signals.length, 3);
  w.teardownPlayback();
  check('all three aborted, not just the newest', signals.map(s => s.aborted), [true, true, true]);
  // Raced against a timer, since a request nothing aborted never settles.
  const ended = await Promise.race([Promise.all(runs), new Promise(r => setTimeout(() => r('still hanging'), 200))]);
  check('each run ended as an abort', ended, ['AbortError', 'AbortError', 'AbortError']);
  check('nothing left tracked', w.aborts.size, 0);
});

await section('a superseded request cannot write into the next read', async () => {
  let release;
  const slow = () => new Promise(res => { release = res; });
  const reply = { ok: true, headers: { get: () => 'srv-7' }, arrayBuffer: async () => new Uint8Array([65, 66]).buffer };
  {
    const { w, bcast } = loadPlayback({ kamFetch: slow });
    const run = w.fetchAudioBase64('old chunk text', 3, 1);
    await ticks();
    w.sessionId = 2;          // a new read has started meanwhile
    release(reply);
    check('its audio is dropped', await run, null);
    check('the new read\'s id map is untouched', w.ids, {});
    check('no chunkReady for the old text', bcast.filter(m => m.action === 'chunkReady').length, 0);
  }
  {
    const { w, bcast } = loadPlayback({ kamFetch: slow });
    const run = w.fetchAudioBase64('current chunk text', 3, 1);
    await ticks();
    release(reply);
    check('the current read still gets its audio', await run, 'QUI=');
    check('and records its id', w.ids, { 3: 'srv-7' });
    // Announcing happens when the chunk starts to play, not at fetch time,
    // which test_report_client.mjs covers.
    check('but does not announce it yet', bcast.filter(m => m.action === 'chunkReady').length, 0);
  }
});

await section('teardown while paused says so', async () => {
  {
    const { w, bcast, tabMsgs } = loadPlayback({ kamFetch: async () => ({}), paused: true });
    w.teardownPlayback();
    check('the reading tab hears resumedPlaying', tabMsgs, [[5, 'resumedPlaying']]);
    check('so do the popup and dashboard', bcast.map(m => m.action), ['resumedPlaying']);
    check('and the worker is unpaused', w.paused, false);
  }
  {
    const { w, bcast, tabMsgs } = loadPlayback({ kamFetch: async () => ({}), paused: false });
    w.teardownPlayback();
    check('teardown while playing sends nothing extra', [tabMsgs, bcast], [[], []]);
  }
});

// =============================================================================
// #11 a new read digests the one it replaces, and the benchmark never digests
// =============================================================================
await section('starting a new read digests the old one first', async () => {
  const posts = [];
  let listener = null;
  const chrome = {
    runtime: { onMessage: { addListener: f => { listener = f; } }, sendMessage: () => Promise.resolve() },
    tabs: { sendMessage: () => Promise.resolve() },
  };
  const kamFetch = (url, opts) => { posts.push([url, JSON.parse(opts.body)]); return Promise.resolve({}); };
  const w = new Function('chrome', 'kamFetch', 'speakChunks', 'stopPlayerAudio', 'stripMarkersForDisplay', 'SERVER', `
    let isStopped = false, isPaused = false, isPlaying = false, currentChunkIndex = 0;
    let allChunks = [], displayChunks = [], currentSpeed = 1, sessionStartTs = 0;
    let playedChunks = [], chunkIdByIndex = {}, sessionDigestible = true, sessionId = 0;
    let readingTabId = null, _pendingPlay = {}, playerReady = false, playerTabId = null;
    let KAM_TOKEN = "t";
    const _speakAborts = new Set();
    ${grab(bg, 'toReadingTab')}
    ${grab(bg, 'teardownPlayback')}
    ${grab(bg, 'digestPlayedChunks')}
    ${grabListener(bg, 'chrome.runtime.onMessage.addListener((request, sender, sendResponse) => {')}
    return { set played(v) { playedChunks = v; }, set playing(v) { isPlaying = v; } };`)(
    chrome, kamFetch, () => {}, () => {}, t => t, 'http://s');
  const send = req => new Promise(res => listener(req, {}, res));
  const digests = () => posts.filter(p => p[0].endsWith('/session/complete')).map(p => p[1].played);

  await send({ action: 'startSpeaking', chunks: ['a one', 'a two'], digestible: true });
  w.played = ['a1', 'a2']; w.playing = true;
  await send({ action: 'startSpeaking', chunks: ['b one', 'b two'], digestible: true });
  check('the replaced read\'s heard chunks are digested', digests(), [['a1', 'a2']]);

  posts.length = 0;
  await send({ action: 'startSpeaking', chunks: ['c one'], digestible: false });
  w.played = ['c1']; w.playing = true;
  await send({ action: 'startSpeaking', chunks: ['d one'], digestible: true });
  check('a non-digestible read is still never digested', digests(), []);
});

await section('the benchmark read is not digestible', async () => {
  const i = dash.indexOf("tp-benchmark");
  const call = dash.slice(i, dash.indexOf("_tuneStatus('Speaking benchmark", i));
  check('its startSpeaking passes digestible:false', /digestible:\s*false/.test(call), true);
});

// =============================================================================
// #2 content.js reaches tabs that were already open, once per live copy
// =============================================================================
await section('the worker injects content.js into open web pages', async () => {
  const done = [];
  const tabs = [
    { id: 1, url: 'https://example.com/a' },
    { id: 2, url: 'http://localhost:8000/' },
    { id: 3, url: 'chrome://extensions/' },
    { id: 4, url: 'https://chromewebstore.google.com/detail/x' },
    { id: 5, url: 'https://example.org/', discarded: true },
    { id: 6, url: 'chrome-extension://abc/player.html' },
    { id: 7, url: 'https://refuses.example/' },
  ];
  const chrome = {
    tabs: { query: async () => tabs },
    scripting: { executeScript: async o => {
      if (o.target.tabId === 7) throw new Error('Cannot access contents of the page');
      done.push([o.target.tabId, o.target.allFrames, o.files]);
    } },
  };
  const inject = new Function('chrome', `
    ${grab(bg, '_injectableUrl')}
    ${grab(bg, '_injectContentIntoOpenTabs')}
    return _injectContentIntoOpenTabs;`)(chrome);
  const n = await inject();
  check('only live http and https pages, every frame', done,
        [[1, true, ['content.js']], [2, true, ['content.js']]]);
  check('a page that refuses is skipped quietly', n, 2);
  check('both install and startup inject', [
    /onInstalled\.addListener\(\(\) => \{ _injectContentIntoOpenTabs\(\)/.test(bg),
    /onStartup\.addListener\(\(\) => \{ _injectContentIntoOpenTabs\(\)/.test(bg)], [true, true]);
});

await section('content.js runs once per live copy and replaces an orphan', async () => {
  const src = read('content.js');
  const fakeChromeFor = id => {
    const c = { listeners: 0, runtime: { id, onMessage: { addListener: () => { c.listeners++; } } },
                storage: { local: { get: () => {} }, onChanged: { addListener: () => {} } } };
    return c;
  };
  const g = {};
  const doc = fakeDocument();
  const run = chrome => new Function('globalThis', 'chrome', 'document', src)(g, chrome, doc);
  const a = fakeChromeFor('ext');
  run(a);
  check('first copy listens', a.listeners, 1);
  run(a);
  check('a second injection beside a live copy does nothing', a.listeners, 1);
  // An overlay drawn by the first copy, which is about to be orphaned.
  let removed = 0;
  doc.els['tts-overlay'] = fakeEl('tts-overlay');
  doc.querySelectorAll = sel => (sel === '#tts-overlay' ? [{ remove: () => { removed++; } }] : []);
  a.runtime.id = undefined;             // extension reloaded, old runtime gone
  const b = fakeChromeFor('ext');
  run(b);
  check('after a reload the new copy takes over', b.listeners, 1);
  check('and clears the orphan\'s dead header bar', removed, 1);
});

// =============================================================================
// #5 a stopped or superseded chunk leaves nothing behind
// #9 the offscreen document asks the worker for the stored volume
// =============================================================================
await section('offscreen: elements, blobs and stored volume', async () => {
  const src = read('offscreen.js');
  const doc = fakeDocument();
  const revoked = [];
  let blobN = 0, listener = null, gainAsk = null;
  class FakeAudio {
    constructor(src) { this.src = src; this.paused = true; this.volume = 1; this.ls = {}; this.parentNode = null; }
    addEventListener(t, f) { (this.ls[t] ||= []).push(f); }
    play() { this.paused = false; return Promise.resolve(); }
    pause() { this.paused = true; }
    fire(t) { (this.ls[t] || []).forEach(f => f()); }
  }
  const chrome = {
    runtime: {
      lastError: undefined,
      sendMessage(m, cb) { if (m.action === 'getPlaybackGain') gainAsk = cb; return Promise.resolve(); },
      connect: () => ({ onMessage: { addListener() {} }, onDisconnect: { addListener() {} }, postMessage() {} }),
      onMessage: { addListener: f => { listener = f; } },
    },
  };
  const URLx = { createObjectURL: () => 'blob:' + (++blobN), revokeObjectURL: u => revoked.push(u) };
  new Function('chrome', 'document', 'Audio', 'URL', 'Blob', 'setInterval', 'window', src)(
    chrome, doc, FakeAudio, URLx, class {}, () => 0, {});
  const send = m => listener({ target: 'offscreen', ...m }, {}, () => {});
  const chunks = () => doc.body.children.filter(a => String(a.src).startsWith('blob:'));

  check('the offscreen document asks for the stored volume as it starts', typeof gainAsk, 'function');
  if (gainAsk) gainAsk({ gain: 0.5 });

  send({ action: 'playAudioOffscreen', audio: btoa('RIFF1'), seq: 1 });
  check('the stored volume applies to the next chunk', chunks()[0] && chunks()[0].volume, 0.5);
  send({ action: 'playAudioOffscreen', audio: btoa('RIFF2'), seq: 2 });
  check('a superseded chunk is taken out of the page', chunks().map(a => a.src), ['blob:2']);
  check('and its blob freed', revoked, ['blob:1']);
  send({ action: 'stopOffscreen' });
  check('a stopped chunk is taken out too', chunks().length, 0);
  check('and its blob freed', revoked, ['blob:1', 'blob:2']);
  send({ action: 'playAudioOffscreen', audio: btoa('RIFF3'), seq: 3 });
  chunks()[0].fire('ended');
  check('a chunk that ends still cleans up', [chunks().length, revoked.length], [0, 3]);
  check('only the keep-alive element is left', doc.body.children.length, 1);
});

await section('worker: one stored volume, handed to the offscreen document', async () => {
  const st = fakeStorage({ playbackGain: 2.5 });
  const w = new Function('chrome', `
    ${grab(bg, '_clampGain')}
    ${grab(bg, '_readGain')}
    ${grab(bg, '_saveGain')}
    return { _readGain, _saveGain };`)({ storage: { local: st } });
  check('reads what is stored', await w._readGain(), 2.5);
  w._saveGain(9); await tick();
  check('saves within 0..6', st.store.playbackGain, 6);
  w._saveGain('junk'); await tick();
  check('ignores nonsense', st.store.playbackGain, 6);
  delete st.store.playbackGain;
  check('nothing stored means 100%', await w._readGain(), 1);
});

await section('dashboard: where the volume slider starts', async () => {
  const f = new Function(`${grab(dash, 'volumeStartPct')} return volumeStartPct;`)();
  check('a stored gain wins', f(1.5, '300'), { pct: 150, migrate: false });
  check('the old localStorage value is carried over once', f(undefined, '250'), { pct: 250, migrate: true });
  check('nothing at all is 100%', f(undefined, null), { pct: 100, migrate: false });
  check('the dashboard no longer keeps its own copy', /localStorage\.setItem\('kamPlaybackVolume'/.test(dash), false);
});

// =============================================================================
// #3 the console follows a restarted server
// =============================================================================
await section('dashboard console after a server restart', async () => {
  // The server's /console, as server.py computes it.
  const server = { log: [], total: 0 };
  const say = (...l) => { server.log.push(...l); server.total += l.length; };
  const api = async p => {
    const since = parseInt(p.split('since=')[1], 10) || 0;
    const start = Math.max(0, server.log.length - (server.total - since));
    return { lines: server.log.slice(start), cursor: server.total };
  };
  const shown = [];
  const c = new Function('api', 'document', 'addLog', `
    let _logSince = 0, _firstConsolePoll = true;
    ${grab(dash, 'pollConsole')}
    return { pollConsole, get since() { return _logSince; } };`)(api, { getElementById: () => null }, l => shown.push(l));
  say(...Array.from({ length: 40 }, (_, i) => 'old ' + i));
  c.pollConsole(); await ticks();
  say('old live line');
  c.pollConsole(); await ticks();
  check('lines from the first server show', shown, ['old live line']);
  server.log = []; server.total = 0;          // restarted
  say('boot 1', 'boot 2');
  c.pollConsole(); await ticks();
  check('the cursor follows the new server back', c.since, 2);
  say('[TTS] first chunk');
  c.pollConsole(); await ticks();
  check('and its new lines appear', shown, ['old live line', '[TTS] first chunk']);
});

// =============================================================================
// #10 no inline handlers, which the extension CSP blocks
// =============================================================================
await section('AI panel headers toggle without inline handlers', async () => {
  check('no on*= attributes in markup built by dashboard.js', (dash.match(/<[^>]*\son[a-z]+=["']/g) || []).length, 0);
  const doc = fakeDocument();
  const sec = fakeEl('ai-sec-stats');
  const body = fakeEl('body'), arrow = fakeEl('arrow');
  sec.querySelector = s => (s === '.ai-section-body' ? body : arrow);
  doc.els['ai-sec-stats'] = sec;
  // The delegated listener is the addEventListener call that reads
  // [data-ai-toggle]; it runs to the next function in the file.
  const at = dash.indexOf("closest('[data-ai-toggle]')");
  if (at < 0) throw new Error('no delegated header listener');
  const from = dash.lastIndexOf('document.addEventListener(', at);
  const to = dash.indexOf('\nfunction ', at);
  new Function('document', `
    ${grab(dash, 'toggleAiSection')}
    ${dash.slice(from, to)}`)(doc);
  const header = { closest: s => (s === '[data-ai-toggle]' ? { dataset: { aiToggle: 'ai-sec-stats' } } : null) };
  doc.fire('click', { target: header });
  check('a click on a header collapses its section', body.classList.contains('collapsed'), true);
});

// =============================================================================
// #12 opening the scan panel on a live session starts watching it
// =============================================================================
await section('scan panel picks up a session that is already open', async () => {
  const src = read('scan-ui.js');
  const doc = fakeDocument();
  doc.els['scan-panel'] = Object.assign(fakeEl('scan-panel'), { style: { display: 'none' } });
  doc.els['scan-cols'] = Object.assign(fakeEl('scan-cols'), { value: '1' });
  const intervals = [], pageTokens = [];
  let issued = ['old', 'new'];
  const status = { open: true, url: 'http://10.0.0.2:5051/?k=abc', code: '123', pages: 1, seconds_left: 600 };
  const api = async p => (p === '/scan/status' ? status : {});
  const fetch = async (url, opts) => {
    if (url.endsWith('/token')) return { ok: true, json: async () => ({ token: issued.shift() }) };
    const t = opts.headers['X-KAM-Token'];
    pageTokens.push(t);
    return t === 'new' ? { ok: true, status: 200, blob: async () => ({}) } : { ok: false, status: 403 };
  };
  const QRCode = function () {}; QRCode.CorrectLevel = { M: 0 };
  const KamOcr = { readImage: async () => ({ text: 'Read aloud.' }), release: async () => {} };
  const KamScanText = { clean: t => ({ text: t, versesRemoved: 0 }) };
  new Function('document', 'SERVER', 'api', 'fetch', 'QRCode', 'KamOcr', 'KamScanText', 'showToast',
               'setInterval', 'clearInterval', src)(
    doc, 'http://s', api, fetch, QRCode, KamOcr, KamScanText, () => {},
    (f, ms) => { intervals.push(ms); return intervals.length; }, () => {});
  doc.els['scan-pill'].fire('click');
  await ticks(20);
  check('opening the panel starts the status poll', intervals, [2000]);
  check('a stale page token is replaced and retried once', pageTokens, ['old', 'new']);
  check('and the page is read', doc.els['scan-text'].value, 'Read aloud.');
});

// =============================================================================
// #6 closing the recorder mid-take leaves no timer running
// =============================================================================
await section('recorder closes cleanly during a take', async () => {
  const src = read('recorder-ui.js');
  const doc = fakeDocument();
  const live = new Set();
  let next = 0, recording = false;
  const win = { addEventListener() {}, devicePixelRatio: 1 };
  const r = new Function('document', 'window', 'setInterval', 'clearInterval', 'requestAnimationFrame',
    'cancelAnimationFrame', 'getComputedStyle', 'recIsRecording', 'recHasMic', 'recStart', 'recRelease',
    'recElapsed', 'recSampleRate', 'recLevel', 'recInit', `${src}
    return { recToggle, recClose };`)(
    doc, win, () => { live.add(++next); return next; }, id => live.delete(id), () => 0, () => {},
    () => ({ getPropertyValue: () => '' }), () => recording, () => true,
    () => { recording = true; return true; }, () => { recording = false; },
    () => 1, () => 24000, () => 0, async () => ({ ok: true }));
  const overlay = doc.els['rec-overlay'];
  overlay.classList.add('open');
  await r.recToggle();
  check('a take starts its timer', live.size, 1);
  doc.fire('keydown', { key: 'Escape' });
  check('Escape does not throw a take away while it is recording', overlay.classList.contains('open'), true);
  r.recClose();
  check('closing stops the timer', live.size, 0);
  const btn = doc.els['rec-btn'];
  check('and puts the button back', [btn.textContent, btn.classList.contains('recording')], ['●', false]);
  check('and the label', doc.els['rec-timer'].textContent, 'Ready when you are.');
});

// =============================================================================
// #8 the dashboard is not exposed to web pages
// =============================================================================
await section('player.html is not web-accessible', async () => {
  const m = JSON.parse(read('manifest.json'));
  const exposed = (m.web_accessible_resources || []).some(w => (w.resources || []).includes('player.html'));
  check('no web page can frame the dashboard', exposed, false);
  check('content.js never needs it', /player\.html/.test(read('content.js')), false);
});

console.log(`\n${PASS} passed, ${FAIL} failed`);
process.exit(FAIL?1:0);
