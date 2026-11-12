// Playback as the reader meets it: a server that dies mid-read, carrying on
// where a read left off, private reads, a faster start, a speed change that is
// heard at once, the header bar's mechanics, and the highlight finding the
// right place on the page.
//
// Each part lifts the real code out of background.js or content.js, gives it a
// fake Chrome, a fake server and (for the bar) a tiny fake DOM, and checks the
// behaviour that used to be wrong.
//
// KAM_EXT_DIR points the suite at another copy of the extension, which is how
// I check that these tests fail against the code from before the changes.
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const EXT = process.env.KAM_EXT_DIR || path.join(HERE, '..', '..', 'extension');
const read = f => fs.readFileSync(path.join(EXT, f), 'utf8');
const bg = read('background.js');
const content = read('content.js');

// The worker logs each failed fetch, and several parts fail fetches on purpose.
console.error = () => {};

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

// A run of top-level statements, from the first marker up to (not including)
// the second. Used for the worker's block of playback state and for constants,
// so the tests run on the real declarations rather than a copy of them.
const between = (src, from, to) => {
  const i = src.indexOf(from);
  const j = i < 0 ? -1 : src.indexOf(to, i);
  if (i < 0 || j < 0) throw new Error(`block ${from.slice(0, 30)} not found`);
  return src.slice(i, j);
};

// The worker is loaded with whatever of its helpers the copy under test has,
// so that code from before the changes still runs and fails on what it does
// rather than on a helper it never had.
const opt = (f, ...args) => { try { return f(...args); } catch (e) { return ''; } };

// A section that throws (a function missing from older code, say) counts as a
// failure rather than ending the run, so every part still reports.
async function section(title, fn) {
  console.log(`\n=== ${title} ===`);
  try { await fn(); }
  catch (e) { FAIL++; console.log(`  FAIL ${title} threw: ${e && e.message}`); }
}

const sleep = ms => new Promise(r => setTimeout(r, ms));
// Wait for something to become true rather than for a fixed time, so the
// tests are as fast as the code and do not depend on a guess.
const until = async (cond, ms = 2000) => {
  const t0 = Date.now();
  while (!cond()) { if (Date.now() - t0 > ms) return false; await sleep(2); }
  return true;
};

// =============================================================================
// The worker, with a server and a player that do as they are told
// =============================================================================
const WORKER_FNS = ['startRead', 
  'toReadingTab', 'teardownPlayback', 'stripMarkersForDisplay', '_openDashboardForRead', 'ensurePlayer',
  '_pushSpeed', '_tabInfo', '_beginRead', '_announcePreparing',
  'forgetResume', '_saveRead', '_savePos', '_loadResume', 'getResume', '_tabForResume', 'resumeRead',
  'haltRead', 'fetchAudioBase64', 'speakChunks', 'announceChunk', 'digestPlayedChunks', 'prepareClear',
];

function loadWorker(o = {}) {
  const bcast = [], tabMsgs = [], wire = [], plays = [];
  const store = o.store || {};
  const tabs = o.tabs || {};
  // up: answers at all. downFrom: chunks from this index on meet a dead
  // server. failFrom: chunks from this index on get a 500. status: a status
  // for one chunk. hold: chunks whose reply waits for release().
  const srv = Object.assign({ up: true, health: undefined, downFrom: Infinity, failFrom: Infinity,
                              status: {}, hold: new Set(), held: {}, signals: {} }, o.server);
  const counts = { hw: 0, dashboard: 0, stopAudio: 0 };
  const holder = {};
  let listener = null;

  const keys = k => (Array.isArray(k) ? k : [k]);
  const chrome = {
    runtime: {
      lastError: undefined,
      sendMessage: m => { bcast.push(m); return Promise.resolve(); },
      onMessage: { addListener: f => { listener = f; } },
    },
    tabs: {
      sendMessage: (id, m) => { tabMsgs.push([id, m]); return Promise.resolve(); },
      get: async id => { if (!tabs[id]) throw new Error('No tab with id'); return tabs[id]; },
      query: async () => Object.values(tabs),
    },
    storage: { local: {
      get: async k => Object.fromEntries(keys(k).filter(x => x in store).map(x => [x, store[x]])),
      set: async obj => { Object.assign(store, JSON.parse(JSON.stringify(obj))); },
      remove: async k => { keys(k).forEach(x => delete store[x]); },
    } },
  };

  const aborted = () => Object.assign(new Error('aborted'), { name: 'AbortError' });
  const kamFetch = (url, opts = {}) => {
    const body = opts.body ? JSON.parse(opts.body) : {};
    if (url.endsWith('/settings')) {
      wire.push(['settings', body.speed]);
      return srv.up ? Promise.resolve({ ok: true }) : Promise.reject(new TypeError('Failed to fetch'));
    }
    if (url.endsWith('/speak/cancel')) { wire.push(['cancel', body.epoch]); return Promise.resolve({ ok: true }); }
    if (url.endsWith('/session/complete')) { wire.push(['digest', body.played]); return Promise.resolve({ ok: true }); }
    if (!url.endsWith('/speak')) return Promise.resolve({ ok: true });
    const idx = body.index - 1;
    wire.push(['speak', idx, body]);
    const sig = opts.signal;
    (srv.signals[idx] ||= []).push(sig);
    return new Promise((res, rej) => {
      if (sig && sig.aborted) return rej(aborted());
      if (sig) sig.addEventListener('abort', () => rej(aborted()));
      const answer = () => {
        if (!srv.up || idx >= srv.downFrom) return rej(new TypeError('Failed to fetch'));
        const st = srv.status[idx] || (idx >= srv.failFrom ? 500 : 200);
        res({ ok: st === 200, status: st, headers: { get: () => 'id-' + idx },
              arrayBuffer: async () => new Uint8Array(48).buffer });
      };
      if (srv.hold.has(idx)) (srv.held[idx] ||= []).push(answer); else answer();
    });
  };
  const release = idx => { srv.hold.delete(idx); (srv.held[idx] || []).splice(0).forEach(f => f()); };

  // The offscreen player. By default a chunk "plays" for a couple of
  // milliseconds; with manual the test ends each chunk itself.
  const finish = seq => { const f = holder.w.pending[seq]; if (f) f({ status: 'finished' }); };
  const toPlayerAsync = async msg => {
    if (msg.action !== 'playAudioOffscreen') return { status: 'ok' };
    plays.push(msg.seq);
    if (!o.manual) setTimeout(() => finish(msg.seq), 2);
    return { status: 'received' };
  };

  const env = {
    chrome, kamFetch, toPlayerAsync,
    stopPlayerAudio: () => { counts.stopAudio++; },
    sanitizeText: t => String(t).trim(),
    detectPositionHint: () => null,
    ensureOffscreen: o.ensureOffscreen || (async () => {}),
    ensureDashboardTab: o.ensureDashboardTab || (async () => { counts.dashboard++; }),
    _refreshHwHints: () => { counts.hw++; },
    _serverUp: async () => (srv.health === undefined ? srv.up : srv.health),
    kamStripMarkers: t => t,
    pausePlayerAudio: () => {}, resumePlayerAudio: () => {},
    SERVER: 'http://s',
  };
  holder.w = new Function(...Object.keys(env), `
    let KAM_TOKEN = "t", _hwPrefetchTarget = ${o.hw || 3};
    ${between(bg, 'let isStopped ', '// AUDIO (offscreen document)')}
    ${opt(between, bg, 'const RESUME_KEY', 'function forgetResume(')}
    ${WORKER_FNS.map(n => opt(grab, bg, n)).join('\n')}
    ${grabListener(bg, 'chrome.runtime.onMessage.addListener((request, sender, sendResponse) => {')}
    return { get pending() { return _pendingPlay; }, get index() { return currentChunkIndex; },
             get playing() { return isPlaying; }, get priv() { return sessionPrivate; } };`)(...Object.values(env));

  // A message the worker does not know is answered with nothing, as Chrome
  // does, rather than left hanging.
  const send = req => new Promise(res => { if (!listener(req, {}, res)) res(undefined); });
  const of = action => bcast.filter(m => m.action === action);
  return {
    w: holder.w, send, bcast, tabMsgs, wire, plays, store, srv, counts, release,
    // The chunk numbers that started to play, in order, as the popup hears them.
    statuses: () => of('chunkStatus').map(m => m.current),
    finished: () => of('playbackFinished').length,
    errors: () => of('playbackError'),
    speaks: () => wire.filter(x => x[0] === 'speak'),
    toTab: id => tabMsgs.filter(t => t[0] === id).map(t => t[1].action),
    endPlaying: () => finish(plays[plays.length - 1]),
  };
}

const CH = n => Array.from({ length: n }, (_, i) => `chunk number ${i + 1} of the read`);

// =============================================================================
// 17. A server that dies mid-read is not a normal finish
// =============================================================================
await section('one failed chunk is skipped; the read goes on', async () => {
  const k = loadWorker({ server: { status: { 2: 500 } } });
  await k.send({ action: 'startSpeaking', chunks: CH(6), tabId: 5 });
  await until(() => k.finished() || k.errors().length);
  check('the other five chunks play', k.statuses(), [1, 2, 4, 5, 6]);
  check('it ends as a finish, with no error', [k.finished(), k.errors().length], [1, 0]);
});

await section('two failures in a row stop the read and say so', async () => {
  const k = loadWorker({ server: { downFrom: 2, health: false } });
  await k.send({ action: 'startSpeaking', chunks: CH(6), tabId: 5, title: 'T', url: 'https://a.test/' });
  await until(() => k.errors().length || k.finished());
  check('the chunks before the failure were heard', k.statuses(), [1, 2]);
  check('it is not reported as finished', k.finished(), 0);
  const e = k.errors()[0] || {};
  check('the popup and dashboard are told, with the place', [e.message, e.index, e.total],
        ['The server stopped responding. Start it again and press Resume.', 2, 6]);
  check('the reading tab is told too', k.toTab(5).includes('playbackError'), true);
  check('the bar is not cleared as though the read had ended', k.toTab(5).includes('clearOverlay'), false);
  check('the position is the first chunk that failed', k.w.index, 2);
  check('and that is what is saved for resuming', k.store.kamLastReadPos && k.store.kamLastReadPos.index, 2);
  const st = await k.send({ action: 'getStatus' });
  check('the list is kept and nothing is playing', [st.isPlaying, st.totalChunks, st.error && st.error.index], [false, 6, 2]);

  // Start the server again and press Resume.
  k.srv.downFrom = Infinity; k.srv.health = true;
  const offer = (await k.send({ action: 'getResume' })).resume;
  check('Resume is offered at that chunk', offer && [offer.index, offer.total, offer.halted], [2, 6, true]);
  check('resumeRead starts it', await k.send({ action: 'resumeRead' }), { status: 'started' });
  await until(() => k.finished());
  check('and it carries on from the chunk that failed', k.statuses(), [1, 2, 3, 4, 5, 6]);
  check('a read heard to the end leaves nothing to resume', [k.store.kamLastRead, k.store.kamLastReadPos], [undefined, undefined]);
});

await section('the message tells a dead server from a failing one', async () => {
  const k = loadWorker({ server: { failFrom: 0, health: true } });
  await k.send({ action: 'startSpeaking', chunks: CH(4), tabId: 5 });
  await until(() => k.errors().length || k.finished());
  const e = k.errors()[0] || {};
  check('a server that is up but failing is worded as that', [e.message, e.serverUp, e.index],
        ['The server could not make the audio. Press Resume to try again.', true, 0]);
  check('a failure at the first chunk is still offered for Resume',
        ((await k.send({ action: 'getResume' })).resume || {}).index, 0);
});

await section('a one-chunk read against a dead server is an error, not a finish', async () => {
  const k = loadWorker({ server: { up: false } });
  await k.send({ action: 'startSpeaking', chunks: CH(1), tabId: 5 });
  await until(() => k.errors().length || k.finished());
  check('announced as an error', [k.errors().length, k.finished()], [1, 0]);
});

await section('a 409, an abort and an empty chunk are not failures', async () => {
  {
    const k = loadWorker({ server: { status: { 1: 409, 2: 409 } } });
    await k.send({ action: 'startSpeaking', chunks: CH(5), tabId: 5 });
    await until(() => k.finished() || k.errors().length);
    check('two 409s in a row do not stop the read', [k.statuses(), k.finished(), k.errors().length], [[1, 4, 5], 1, 0]);
  }
  {
    const k = loadWorker({ server: { hold: new Set([0, 1, 2]) } });
    await k.send({ action: 'startSpeaking', chunks: CH(5), tabId: 5 });
    await until(() => k.speaks().length === 3);
    await k.send({ action: 'stop' });
    await sleep(30);
    check('requests aborted by a stop are not announced as a server failure', k.errors().length, 0);
  }
  {
    const k = loadWorker();
    await k.send({ action: 'startSpeaking', chunks: ['first chunk', '   ', '  ', 'fourth chunk'], tabId: 5 });
    await until(() => k.finished() || k.errors().length);
    check('chunks that clean to nothing are passed over', [k.statuses(), k.finished(), k.errors().length], [[1, 4], 1, 0]);
    check('and nothing is asked of the server for them', k.speaks().map(s => s[1]), [0, 3]);
  }
});

await section('a jump made while a chunk is being synthesised lands on that chunk', async () => {
  const k = loadWorker({ server: { hold: new Set([0]) } });
  await k.send({ action: 'startSpeaking', chunks: CH(8), tabId: 5 });
  await until(() => k.speaks().length >= 1);
  await k.send({ action: 'jumpTo', index: 4 });
  await until(() => k.statuses().length >= 1);
  check('the first chunk heard is the one jumped to, not the one after', k.statuses()[0], 5);
});

// =============================================================================
// 20. Resume where you left off
// =============================================================================
const PAGE = 'https://a.test/article';
const pageTabs = () => ({ 9: { id: 9, url: PAGE, incognito: false, active: true } });

await section('a read is saved as it advances, and Stop keeps it', async () => {
  const k = loadWorker({ manual: true, tabs: pageTabs() });
  const chunks = CH(5);
  await k.send({ action: 'startSpeaking', chunks, tabId: 9, speed: 1.2, title: 'An article', url: PAGE });
  await until(() => k.plays.length === 1);
  const r = k.store.kamLastRead || {}, p = k.store.kamLastReadPos || {};
  check('the list is stored once, with its page', [r.chunks, r.total, r.title, r.url, r.speed], [chunks, 5, 'An article', PAGE, 1.2]);
  check('the position is stored apart from it', [p.index, p.id === r.id, 'chunks' in p], [0, true, false]);
  check('no resume is offered while it is playing', await k.send({ action: 'getResume' }), { resume: null });

  k.endPlaying(); await until(() => k.plays.length === 2);
  k.endPlaying(); await until(() => k.plays.length === 3);
  check('the position follows the chunk that is playing', k.store.kamLastReadPos.index, 2);

  await k.send({ action: 'stop' });
  check('Stop leaves the saved read where it was', [!!k.store.kamLastRead, k.store.kamLastReadPos.index], [true, 2]);
  const got = (await k.send({ action: 'getResume' })).resume || {};
  check('getResume describes it', [got.index, got.total, got.title, got.url, typeof got.savedAt], [2, 5, 'An article', PAGE, 'number']);

  await k.send({ action: 'jumpTo', index: 1 });
  await until(() => k.finished() >= 1);
  check('a loop run over the emptied list does not wipe it', !!k.store.kamLastRead, true);

  // The worker is restarted, or the browser is: nothing left but storage.
  const k2 = loadWorker({ store: k.store, tabs: pageTabs() });
  const again = (await k2.send({ action: 'getResume' })).resume || {};
  check('a new worker offers the same read', [again.index, again.total, again.title], [2, 5, 'An article']);
  check('resumeRead starts it', await k2.send({ action: 'resumeRead' }), { status: 'started' });
  await until(() => k2.finished());
  check('playing from the saved chunk to the end', k2.statuses(), [3, 4, 5]);
  check('at the speed it was last heard at', k2.wire[0], ['settings', 1.2]);
  check('with the bar on the page it came from', k2.toTab(9)[0], 'overlayPreparing');
  check('and a natural finish clears it', [k2.store.kamLastRead, k2.store.kamLastReadPos], [undefined, undefined]);
  check('so nothing is offered afterwards', await k2.send({ action: 'getResume' }), { resume: null });
  check('and resumeRead has nothing to start', await k2.send({ action: 'resumeRead' }), { status: 'none' });
});

await section('nothing to resume at the first chunk, and forgetResume clears', async () => {
  const k = loadWorker({ server: { hold: new Set([0]) } });
  await k.send({ action: 'startSpeaking', chunks: CH(4), tabId: 5, title: 'T', url: PAGE });
  await until(() => k.speaks().length >= 1);
  await k.send({ action: 'stop' });
  check('a read stopped before its first chunk is not offered', await k.send({ action: 'getResume' }), { resume: null });

  const seeded = { kamLastRead: { id: 'r', chunks: CH(4), title: 'T', url: PAGE, savedAt: 1 },
                   kamLastReadPos: { id: 'r', index: 3, savedAt: 2 } };
  const k2 = loadWorker({ store: seeded });
  check('a saved read is offered', ((await k2.send({ action: 'getResume' })).resume || {}).index, 3);
  await k2.send({ action: 'forgetResume' });
  check('forgetResume removes both keys', [seeded.kamLastRead, seeded.kamLastReadPos], [undefined, undefined]);
  check('and the offer goes', await k2.send({ action: 'getResume' }), { resume: null });

  const stray = { kamLastRead: { id: 'new', chunks: CH(4) }, kamLastReadPos: { id: 'old', index: 3 } };
  check('a position left by another read is not used',
        await loadWorker({ store: stray }).send({ action: 'getResume' }), { resume: null });
});

await section('a new read replaces the saved one; one too large to save removes it', async () => {
  const k = loadWorker({ server: { hold: new Set([0]) } });
  await k.send({ action: 'startSpeaking', chunks: CH(3), tabId: 5 });
  await until(() => !!k.store.kamLastRead);
  const other = ['a different read', 'with other chunks'];
  await k.send({ action: 'startSpeaking', chunks: other, tabId: 5 });
  await until(() => k.store.kamLastRead && k.store.kamLastRead.total === 2);
  check('the saved read is the new one', k.store.kamLastRead.chunks, other);

  const big = ['x'.repeat(1100000), 'y'.repeat(1100000)];
  await k.send({ action: 'startSpeaking', chunks: big, tabId: 5 });
  await until(() => !k.store.kamLastRead);
  check('a list over the cap is not stored, and the older read does not linger',
        [k.store.kamLastRead, k.store.kamLastReadPos], [undefined, undefined]);
});

// =============================================================================
// 25. Private reads
// =============================================================================
await section('a private read is marked on every request and never saved', async () => {
  const earlier = () => ({ kamLastRead: { id: 'old', chunks: CH(3), title: 'Earlier', url: PAGE, savedAt: 1 },
                           kamLastReadPos: { id: 'old', index: 1, savedAt: 2 } });
  {
    const k = loadWorker({ store: earlier(), manual: true });
    await k.send({ action: 'startSpeaking', chunks: CH(4), tabId: 5, private: true, title: 'Secret', url: 'https://b.test/' });
    await until(() => k.plays.length === 1);
    check('getStatus says the read is private', (await k.send({ action: 'getStatus' })).private, true);
    for (let n = 2; n <= 4; n++) { k.endPlaying(); await until(() => k.plays.length === n); }
    k.endPlaying(); await until(() => k.finished());
    check('every /speak of it says private', k.speaks().map(s => s[2].private), [true, true, true, true]);
    check('nothing of it reaches storage, start to finish',
          [k.store.kamLastRead.id, k.store.kamLastRead.title, k.store.kamLastReadPos.index], ['old', 'Earlier', 1]);
  }
  {
    const tabs = { 7: { id: 7, url: 'https://b.test/', incognito: true } };
    const k = loadWorker({ store: earlier(), tabs });
    await k.send({ action: 'startSpeaking', chunks: CH(3), tabId: 7 });
    await until(() => k.finished());
    check('a read from an incognito tab is private without being asked', k.speaks().map(s => s[2].private), [true, true, true]);
    check('and is not saved either', [k.store.kamLastRead.id, k.store.kamLastReadPos.index], ['old', 1]);
  }
  {
    const k = loadWorker({ tabs: pageTabs() });
    await k.send({ action: 'startSpeaking', chunks: CH(3), tabId: 9 });
    await until(() => k.finished());
    check('an ordinary read does not mention it', k.speaks().map(s => 'private' in s[2]), [false, false, false]);
    check('and getStatus says so', (await k.send({ action: 'getStatus' })).private, false);
  }
});

// =============================================================================
// 13. A faster start
// =============================================================================
await section('head:true goes on the first request of a run and no other', async () => {
  const k = loadWorker();
  await k.send({ action: 'startSpeaking', chunks: CH(6), startIndex: 2, tabId: 5 });
  await until(() => k.finished());
  const heads = () => k.speaks().filter(s => 'head' in s[2]).map(s => [s[1], s[2].head]);
  check('only the chunk the read starts on', heads(), [[2, true]]);
  await k.send({ action: 'jumpTo', index: 0 });
  await until(() => k.finished() === 2);
  check('a jump is a new run, with its own first request', heads(), [[2, true], [0, true]]);
  check('the prefetch depth is asked for again as a read starts', k.counts.hw, 1);
});

await section('synthesis starts before the audio path is up, and a stop there is clean', async () => {
  let open;
  const gate = new Promise(r => { open = r; });
  const k = loadWorker({ ensureOffscreen: () => gate, server: { hold: new Set([0, 1, 2]) } });
  await k.send({ action: 'startSpeaking', chunks: CH(6), tabId: 5, speed: 1.4 });
  const asked = await until(() => k.speaks().length === 3, 300);
  check('the first window is requested while the player is still coming up', asked, true);
  check('behind the speed, which is sent first', k.wire[0], ['settings', 1.4]);
  const epoch = k.speaks()[0] ? k.speaks()[0][2].epoch : null;
  await k.send({ action: 'stop' });
  check('a stop in that window aborts every request', [0, 1, 2].map(i => !!(k.srv.signals[i] && k.srv.signals[i][0].aborted)), [true, true, true]);
  check('and cancels their epoch at the server', k.wire.some(x => x[0] === 'cancel' && x[1] === epoch), true);
  open(); await sleep(20);
  check('nothing plays afterwards', k.plays.length, 0);
});

// =============================================================================
// 15. A speed change is heard on the next chunk
// =============================================================================
await section('a speed change drops what was prefetched and keeps the playing chunk', async () => {
  const k = loadWorker({ manual: true, server: { hold: new Set([2]) } });
  await k.send({ action: 'startSpeaking', chunks: CH(8), tabId: 5, speed: 1.0 });
  await until(() => k.plays.length === 1 && k.speaks().length === 4);
  const e1 = k.speaks()[0][2].epoch;
  const stops = k.counts.stopAudio, before = k.wire.length;

  await k.send({ action: 'setSpeed', speed: 1.5 });
  const after = k.wire.slice(before);
  check('the new speed is sent, then the old epoch is cancelled', after, [['settings', 1.5], ['cancel', e1]]);
  check('the request still out is aborted', k.srv.signals[2][0].aborted, true);
  check('the playing chunk is left alone', [k.counts.stopAudio - stops, Object.keys(k.w.pending).length, k.plays.length], [0, 1, 1]);

  await until(() => k.speaks().length >= 7);
  const fresh = k.wire.slice(before).filter(x => x[0] === 'speak');
  check('the chunks after it are asked for again', fresh.map(s => s[1]), [1, 2, 3]);
  check('in a newer epoch', fresh.every(s => s[2].epoch > e1), true);
  check('and never the one that is playing', k.speaks().filter(s => s[1] === 0).length, 1);
  const wireNow = k.wire.slice(before);
  check('with the speed push sent before any of them',
        wireNow.findIndex(x => x[0] === 'settings') < wireNow.findIndex(x => x[0] === 'speak'), true);

  const n = k.wire.length;
  await k.send({ action: 'setSpeed', speed: 1.5 });
  await sleep(300);
  check('the same speed again (the popup opening) disturbs nothing',
        k.wire.slice(n).filter(x => x[0] !== 'settings'), []);

  k.endPlaying();
  await until(() => k.plays.length === 2);
  check('and the next chunk plays from the new request', k.statuses(), [1, 2]);
});

await section('a speed change before the first chunk is ready asks for it again', async () => {
  const k = loadWorker({ manual: true, server: { hold: new Set([0]) } });
  await k.send({ action: 'startSpeaking', chunks: CH(4), tabId: 5, speed: 1.0 });
  await until(() => k.speaks().some(s => s[1] === 0));
  await k.send({ action: 'setSpeed', speed: 1.3 });
  check('the request at the old speed is aborted', k.srv.signals[0][0].aborted, true);
  await until(() => k.speaks().filter(s => s[1] === 0).length === 2);
  k.release(0);
  await until(() => k.plays.length === 1);
  check('the first chunk still plays, it is not skipped', k.statuses(), [1]);
  check('head went on the first request only', k.speaks().filter(s => s[1] === 0).map(s => !!s[2].head), [true, false]);
});

// =============================================================================
// 10/22. The header bar, worker side
// =============================================================================
await section('a read starting in another tab clears the old tab first', async () => {
  const k = loadWorker({ server: { hold: new Set([0]) } });
  await k.send({ action: 'startSpeaking', chunks: CH(3), tabId: 5 });
  check('the page is told at once that a chunk is being prepared', k.toTab(5), ['overlayPreparing']);
  const prep = k.tabMsgs[0][1];
  check('with the text and the count', [prep.text, prep.current, prep.total], ['Preparing the first chunk…', 1, 3]);

  await k.send({ action: 'startSpeaking', chunks: ['another read', 'in another tab'], tabId: 6 });
  check('the old tab loses its bar', k.toTab(5), ['overlayPreparing', 'clearOverlay']);
  check('before the new tab gets one', k.tabMsgs.map(t => t[0]), [5, 5, 6]);

  await k.send({ action: 'startSpeaking', chunks: ['a third read', 'in the same tab'], tabId: 6 });
  check('a new read in the same tab does not clear it', k.toTab(6), ['overlayPreparing', 'overlayPreparing']);
  await k.send({ action: 'stop' });
  check('a stop removes the Preparing bar', k.toTab(6).slice(-1), ['clearOverlay']);
});

// =============================================================================
// 23. The dashboard opens once per read, if wanted, and is never waited for
// =============================================================================
await section('the dashboard opens once per read and stays closed if asked', async () => {
  {
    const k = loadWorker();
    await k.send({ action: 'startSpeaking', chunks: CH(3), tabId: 5 });
    await until(() => k.finished() === 1 && k.counts.dashboard === 1);
    await k.send({ action: 'jumpTo', index: 0 });
    await until(() => k.finished() === 2);
    check('a jump in the same read does not open it again', k.counts.dashboard, 1);
    await k.send({ action: 'startSpeaking', chunks: ['a new read', 'of two chunks'], tabId: 5 });
    await until(() => k.finished() === 3 && k.counts.dashboard === 2);
    check('a new read does', k.counts.dashboard, 2);
  }
  {
    const k = loadWorker({ store: { kamOpenDashboardOnRead: false } });
    await k.send({ action: 'startSpeaking', chunks: CH(3), tabId: 5 });
    await until(() => k.finished() === 1);
    await sleep(10);
    check('with the setting off it is never opened', k.counts.dashboard, 0);
  }
  {
    const k = loadWorker({ ensureDashboardTab: () => new Promise(() => {}) });
    await k.send({ action: 'startSpeaking', chunks: CH(2), tabId: 5 });
    check('a dashboard that never opens does not hold the audio back', await until(() => k.finished() === 1, 500), true);
  }
});

// =============================================================================
// 21. The highlight: one normalisation, and the right occurrence
// =============================================================================
const hl = () => new Function(`
  ${between(content, 'const _HL_LETTERS', 'function _hlFold(')}
  ${['_hlFold', '_hlNormalise', '_normaliseForMatch', '_hlFindFrom', '_hlLocate', '_hlScrollDelta'].map(n => grab(content, n)).join('\n')}
  return { _hlNormalise, _normaliseForMatch, _hlLocate, _hlScrollDelta };`)();

await section('a chunk with a hyphen, a dash or a slash matches the page exactly', async () => {
  const h = hl();
  // The page as the text map joins it, and a chunk as the popup sends it.
  const exact = (page, chunk) => {
    const { norm, idxMap } = h._hlNormalise(page, true);
    const target = h._normaliseForMatch(chunk);
    const hit = h._hlLocate(norm, target, 0);
    if (!hit || norm.slice(hit.start, hit.end) !== target) return null;
    return page.slice(idxMap[hit.start], idxMap[hit.end - 1] + 1);
  };
  check('a hyphen', exact('It is a well-known result in the field.', 'a well-known result'), 'a well-known result');
  check('an en dash, which the chunk carries as a comma',
        exact('The years 2010–2015 were lean ones.', 'The years 2010, 2015 were lean'), 'The years 2010–2015 were lean');
  check('an em dash', exact('He waited—nothing came—and left.', 'He waited, nothing came, and left.'), 'He waited—nothing came—and left');
  check('a slash', exact('See the Input/output chapter for more.', 'the Input/output chapter'), 'the Input/output chapter');
  check('an underscore and a plus', exact('where x_t = t+1 holds', 'x_t = t+1'), 'x_t = t+1');
  check('accents, which the chunk has lost', exact('The café that Brontë found naïve.', 'The cafe that Bronte found naive.'),
        'The café that Brontë found naïve');
  check('accents written as letter plus mark', exact('A résumé arrived.', 'A resume arrived.'), 'A résumé arrived');
  check('letters with no decomposition', exact('Die Straße in Århus, bei Łódź.', 'Die Strasse in Arhus, bei Lodz.'),
        'Die Straße in Århus, bei Łódź');
  check('both sides come out the same', [h._normaliseForMatch('Well-Known: 2010–2015!'), h._hlNormalise(' Well-Known: 2010–2015!', true).norm],
        ['well known 2010 2015', 'well known 2010 2015 ']);
});

await section('a repeated phrase is found after the previous match, not at the top', async () => {
  const h = hl();
  const page = 'Overview. This part sets out the plan in full. Method. Then the work begins in earnest. ' +
               'Overview. A second look at the same plan follows here.';
  const joined = h._hlNormalise(page, true).norm;
  const first = joined.indexOf('overview'), second = joined.indexOf('overview', first + 1);
  const t = h._normaliseForMatch('Overview.');
  check('from the top it is the first', h._hlLocate(joined, t, 0).start, first);
  const mid = h._hlLocate(joined, h._normaliseForMatch('Then the work begins in earnest.'), 0);
  check('after the chunk before it, it is the second', h._hlLocate(joined, t, mid.next).start, second);
  check('with nothing ahead it falls back to the whole page', h._hlLocate(joined, t, joined.length - 5).start, first);

  // The fuzzy anchors look forward first as well.
  const twice = h._hlNormalise('The quick brown fox sat down. Filler text here. The quick brown fox sat down again today.', true).norm;
  const fuzzy = h._normaliseForMatch('The quick brown fox sat down beside me.');
  const a = h._hlLocate(twice, fuzzy, 0), b = h._hlLocate(twice, fuzzy, 40);
  check('a rewritten chunk anchors on its opening words, ahead of the last match',
        [a.start, b.start], [0, twice.indexOf('the quick', 10)]);
  check('and the next search starts from the words that really matched', a.next <= a.end, true);
  check('a chunk that is nowhere on the page matches nothing', h._hlLocate(joined, 'entirely different words appear', 0), null);
});

await section('auto-scroll moves only what is off screen', async () => {
  const h = hl();
  check('on screen: no scroll', h._hlScrollDelta(300, 360, 60, 800), 0);
  check('below the window: up to a quarter of the way down', h._hlScrollDelta(1200, 1260, 60, 800), 1200 - (60 + 185));
  check('under the header bar: scrolled back down', h._hlScrollDelta(20, 80, 60, 800), 20 - (60 + 185));
  check('cut off at the bottom edge counts as off screen', h._hlScrollDelta(770, 830, 60, 800) > 0, true);
  check('too tall to sit a quarter down: to the top of the band', h._hlScrollDelta(900, 1600, 60, 800), 900 - 68);
  check('already where it would be put: no twitch', h._hlScrollDelta(68, 900, 60, 800), 0);
});

await section('text under a nav or a hidden ancestor is left out of the map', async () => {
  const skip = new Function('Node', 'window', `
    ${between(content, 'const _HL_SKIP_INSIDE', '\n')}
    ${['_isMathEl', '_hlHidden', '_hlSkip'].map(n => grab(content, n)).join('\n')}
    return _hlSkip;`)({ ELEMENT_NODE: 1 }, { getComputedStyle: () => ({ display: 'block', visibility: 'visible' }) });
  // An element as _hlSkip sees it: its tag, the tags above it, and whether
  // the browser would say it is rendered.
  const el = (tag, above = [], shown = true) => ({
    nodeType: 1, tagName: tag.toUpperCase(),
    closest: sel => (sel.split(',').map(s => s.trim()).some(s => s === tag || above.includes(s)) ? {} : null),
    checkVisibility: () => shown,
  });
  check('a paragraph in an article is read', skip(el('p', ['article'])), false);
  check('a link inside a nav is not', skip(el('a', ['li', 'ul', 'nav'])), true);
  check('a span inside a button is not', skip(el('span', ['button'])), true);
  check('a paragraph inside a hidden panel is not', skip(el('p', ['div'], false)), true);
  check('text directly in a form is not', skip(el('form')), true);
  check('but a page wrapped in one form is still read', skip(el('p', ['div', 'form'])), false);
});

// =============================================================================
// 10/22. The header bar on the page, with a small fake DOM
// =============================================================================
function fakePage({ margin = '', htmlOverflow = 'visible' } = {}) {
  const shadows = [];
  const mk = tag => {
    const ls = {};
    const el = {
      tagName: String(tag).toUpperCase(), id: '', children: [], parent: null, textContent: '', title: '',
      attrs: {}, offsetHeight: 50,
      style: { setProperty(k, v) { this[k] = v; } },
      appendChild(c) { this.children.push(c); c.parent = this; return c; },
      remove() { if (this.parent) { this.parent.children = this.parent.children.filter(x => x !== this); this.parent = null; } },
      setAttribute(k, v) { this.attrs[k] = String(v); },
      getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; },
      addEventListener(t, f) { (ls[t] ||= []).push(f); },
      click() { (ls.click || []).forEach(f => f()); },
      attachShadow(opts) { const root = mk('#shadow'); root.mode = opts.mode; root.host = el; shadows.push(root); return root; },
    };
    return el;
  };
  const body = mk('body'), html = mk('html'), head = mk('head');
  body.style.marginTop = margin;
  const bars = () => body.children.filter(c => c.id === 'tts-overlay');
  const document = {
    body, head, documentElement: html, createElement: mk,
    getElementById: id => (id === 'tts-overlay' ? bars()[0] || null : null),
    querySelectorAll: sel => (sel === '#tts-overlay' ? bars() : []),
    querySelector: () => null, addEventListener() {},
  };
  const window = { innerHeight: 800, innerWidth: 1000, addEventListener() {},
                   getComputedStyle: el => ({ overflowY: el === html ? htmlOverflow : 'visible' }) };
  window.self = window; window.top = window;
  const sent = [];
  let onMsg = null;
  const chrome = {
    runtime: { id: 'ext', lastError: undefined, onMessage: { addListener: f => { onMsg = f; } },
               sendMessage: (m, cb) => { sent.push(m.action); if (cb) cb({}); return Promise.resolve(); } },
    storage: { local: { get: (defaults, cb) => cb({ ...defaults }), set() {} }, onChanged: { addListener() {} } },
  };
  new Function('globalThis', 'chrome', 'document', 'window', content)({}, chrome, document, window);
  // The bar's parts, wherever they hang: under the shadow root, or under the
  // host itself in code from before there was one.
  const parts = () => {
    const host = bars()[0];
    if (!host) return null;
    const overlay = (shadows.find(s => s.host === host) || host).children[0];
    const [row, textRow] = overlay.children;
    return { host, pause: row.children[0], progress: row.children[2], text: textRow.textContent };
  };
  return { body, shadows, sent, parts, bars, tell: m => onMsg(m, {}, () => {}) };
}

await section('the bar shows Preparing, then the chunk, then an error it can resume from', async () => {
  const p = fakePage({ margin: '12px' });
  p.tell({ action: 'overlayPreparing', text: 'Preparing the first chunk…', current: 1, total: 4 });
  check('the bar is up before any chunk has been made', p.parts() && [p.parts().text, p.parts().progress.textContent],
        ['Preparing the first chunk…', '1 / 4']);
  check('in a closed shadow root, out of reach of the page\'s stylesheet',
        [p.shadows.length, p.shadows[0] && p.shadows[0].mode, p.parts().host.children.length], [1, 'closed', 0]);
  check('and the page is pushed down by its height', p.body.style.marginTop, '54px');

  p.tell({ action: 'updateOverlay', text: 'The first chunk of the read.', current: 1, total: 4 });
  check('the first chunk replaces the message, in the same bar', [p.bars().length, p.parts().text], [1, 'The first chunk of the read.']);

  p.tell({ action: 'playbackError', message: 'The server stopped responding. Start it again and press Resume.', index: 2, total: 4 });
  check('a failure is said in the bar, at the chunk it happened on',
        [p.parts().text, p.parts().progress.textContent], ['The server stopped responding. Start it again and press Resume.', '3 / 4']);
  check('and the pause button turns into play', p.parts().pause.textContent, '▶');
  p.parts().pause.click();
  check('which asks the worker to resume, not to unpause', p.sent, ['resumeRead']);

  p.tell({ action: 'overlayPreparing', text: 'Preparing the first chunk…', current: 3, total: 4 });
  check('resuming puts the bar back to Preparing', [p.parts().text, p.parts().pause.textContent], ['Preparing the first chunk…', '⏸']);
  p.parts().pause.click();
  check('and the button pauses again', p.sent, ['resumeRead', 'pauseResume']);

  p.tell({ action: 'clearOverlay' });
  check('clearing removes the bar', p.bars().length, 0);
  check('and gives the page back the margin it had', p.body.style.marginTop, '12px');
});

await section('the body margin is only ever given back, never wiped', async () => {
  const p = fakePage({ margin: '30px' });
  p.tell({ action: 'clearOverlay' });
  check('a page that never had a bar keeps its own margin', p.body.style.marginTop, '30px');

  const shell = fakePage({ margin: '5px', htmlOverflow: 'hidden' });
  shell.tell({ action: 'overlayPreparing', text: 'Preparing the first chunk…', current: 1, total: 2 });
  check('a full-height app shell gets the bar', shell.bars().length, 1);
  check('but its body is not pushed', shell.body.style.marginTop, '5px');
  shell.tell({ action: 'clearOverlay' });
  check('and is untouched afterwards', shell.body.style.marginTop, '5px');
});

console.log(`\n${PASS} passed, ${FAIL} failed`);
process.exit(FAIL?1:0);
