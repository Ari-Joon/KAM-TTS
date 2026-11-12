// Shared marker grammar (KAM_MARKER_RE, heading/paragraph hint regexes).
// Guarded: if markers.js is missing from the extension folder, importScripts
// throws and would otherwise kill this whole service worker (no playback, and
// the popup misreports the server as offline). Fall back to inline definitions
// so the extension keeps working, and log loudly so the cause is visible.
try {
  importScripts("markers.js");
} catch (e) {
  console.error("[TTS-BG] markers.js failed to load — using inline fallback. Restore markers.js to the extension folder.", e);
  self.KAM_MARKER_RE        = /\|\/?(?:H1|H2|H3|BOLD|ITALIC|CODE|CALLOUT|CAPTION|LIST|BREAK)\|/g;
  self.KAM_HEADING_START_RE = /^\s*\|H([123])\|/;
  self.KAM_LIST_END_RE      = /\|\/LIST\|\s*$/;
  self.KAM_PARAGRAPH_END_RE = /\|BREAK\|\s*$/;
  self.kamStripMarkers = t => String(t).replace(self.KAM_MARKER_RE, " ").replace(/\s+/g, " ").trim();
}
// Accents and symbols to speakable ASCII (see textnorm.js). Guarded the same
// way: without it the sanitiser falls back to the plain strip it always did.
try {
  importScripts("textnorm.js");
} catch (e) {
  console.error("[TTS-BG] textnorm.js failed to load — accents and symbols will be dropped.", e);
  self.kamSpeakable = t => String(t);
}
// The reading code the popup also uses: taking text off a page and cutting it
// into chunks (see reader-core.js). The worker needs it for the right-click
// menu, which starts a read with no popup open. Without it the menu says so
// and everything else carries on.
let _readerLoaded = false;
try {
  importScripts("reader-core.js");
  _readerLoaded = true;
} catch (e) {
  console.error("[TTS-BG] reader-core.js failed to load — the right-click menu cannot read.", e);
}

const SERVER = "http://127.0.0.1:5050";
// Shared local API token. The server rejects requests without it so arbitrary
// websites cannot drive the local TTS API. Must match server.py and kam_host.py.
// Per-install API token: fetched from the server's /token endpoint on first
// contact and cached in chrome.storage. No secret ships in source.
let KAM_TOKEN = "";
async function _ensureToken() {
  if (KAM_TOKEN) return KAM_TOKEN;
  try {
    const st = await chrome.storage.local.get("kamToken");
    if (st.kamToken) { KAM_TOKEN = st.kamToken; return KAM_TOKEN; }
  } catch (e) {}
  try {
    const r = await fetch(SERVER + "/token");
    if (r.ok) {
      KAM_TOKEN = (await r.json()).token || "";
      if (KAM_TOKEN) chrome.storage.local.set({ kamToken: KAM_TOKEN });
    }
  } catch (e) { /* server down — retried on next call */ }
  return KAM_TOKEN;
}

// Forget the cached token, in memory and in storage, so the next _ensureToken
// asks the server again.
async function _dropToken() {
  KAM_TOKEN = "";
  try { await chrome.storage.local.remove("kamToken"); } catch (e) {}
}

// Drop-in fetch for authed server endpoints: awaits the token before sending,
// so a request fired before the token arrives is never rejected with 403.
//
// A 403 means the cached token is stale. The server makes a new one whenever
// kam_token.txt is missing or cannot be written, and the cache used to be kept
// forever, so every call after that was refused until storage was cleared by
// hand. So on a 403 I drop the cache, fetch the token once more and retry once,
// and only if the server really hands out a different token, since a second
// try with the same one would be refused the same way.
async function kamFetch(url, opts = {}) {
  const send = tok => fetch(url, Object.assign({}, opts, {
    headers: Object.assign({}, opts.headers || {}, { "X-KAM-Token": tok })
  }));
  const tok = await _ensureToken();
  const res = await send(tok);
  if (res.status !== 403) return res;
  await _dropToken();
  const fresh = await _ensureToken();
  if (!fresh || fresh === tok) return res;
  return send(fresh);
}
_ensureToken();

// Hardware-adapted runtime hints, fetched from the server (which derives them
// from hardware_profile.json). Null until the first successful fetch.
let _hwPrefetchTarget = null;
async function _refreshHwHints() {
  try {
    const r = await kamFetch(SERVER + "/standby", {
      headers: { "X-KAM-Token": KAM_TOKEN }
    });
    if (r.ok) {
      const d = await r.json();
      if (d && typeof d.prefetch_target === "number") {
        _hwPrefetchTarget = d.prefetch_target;
      }
    }
  } catch (e) { /* server not up — keep the fallback */ }
}
_refreshHwHints();
setInterval(_refreshHwHints, 300000);   // re-check every 5 min

// pings, status broadcasts) can momentarily miss a service worker that is
// transitioning, which produces an Uncaught (in promise) "No SW" that's
// harmless since the sender retries on its own. Real errors still show up.
self.addEventListener("unhandledrejection", (ev) => {
  const m = ev && ev.reason && (ev.reason.message || String(ev.reason));
  if (m && /No SW|message channel closed|Could not establish connection/i.test(m)) {
    ev.preventDefault();
  }
});

// Keep the service worker alive, since MV3 workers die after about 30s idle.
// This is guarded, because if chrome.alarms isn't available, which happens when
// the running extension still has an old manifest without the "alarms"
// permission, then throwing at the top level would kill worker registration and
// every message to it comes back "No SW". So it fails soft and the rest of the
// worker still loads.
try {
  if (chrome.alarms && chrome.alarms.create) {
    chrome.alarms.create('keepAlive', { periodInMinutes: 0.4 });
    chrome.alarms.onAlarm.addListener((alarm) => {
      if (alarm.name !== 'keepAlive') return;
      // Ping the offscreen doc so its idle timer keeps resetting even when no
      // chunk is mid-play. Combined with the offscreen-side silent tone this
      // makes reaping between chunks effectively impossible.
      try { chrome.runtime.sendMessage({ target: "offscreen", action: "keepAlive" }).catch(() => {}); } catch (e) {}
      // Same idea for the native host: a quiet port is an idle worker, and an
      // idle worker is a closed port, which kam_host.py treats as "kill the
      // server". So the port is kept warm for as long as it is open.
      try { _hostPing(); } catch (e) {}
      try {
        chrome.storage.session.get('playerTabId').then(d => {
          if (d && d.playerTabId && playerTabId === null) playerTabId = d.playerTabId;
        }).catch(() => {});
      } catch (e) { /* session storage unavailable — ignore */ }
    });
  } else {
    console.warn('[TTS-BG] chrome.alarms unavailable — keepAlive disabled. ' +
                 'Reload the extension so the new manifest (with "alarms") takes effect.');
  }
} catch (e) {
  console.warn('[TTS-BG] alarm setup failed (non-fatal):', e);
}

// --- Keep-alive port acceptor ---
// offscreen.js holds a persistent port ("kam-keepalive") open and pings it
// frequently. Receiving a port message resets this worker's idle timer; we also
// post back so the channel stays demonstrably active. This keeps the worker
// alive across long synthesis fetches so they are never frozen mid-request.
let _keepAlivePorts = new Set();
try {
  chrome.runtime.onConnect.addListener((port) => {
    if (port.name !== "kam-keepalive") return;
    _keepAlivePorts.add(port);
    port.onMessage.addListener(() => {
      // Echo back, since a round-trip on the port inside the idle window is what
      // actually keeps the worker awake. An open port on its own isn't enough.
      try { port.postMessage({ ack: Date.now() }); } catch (_) {}
    });
    port.onDisconnect.addListener(() => { _keepAlivePorts.delete(port); });
  });
} catch (e) {
  console.warn('[TTS-BG] onConnect setup failed (non-fatal):', e);
}

let isStopped         = false;
let isPaused          = false;
let isPlaying         = false;
let currentChunkIndex = 0;
let allChunks         = [];
let displayChunks     = [];
let currentSpeed      = 1.0;
let sessionStartTs    = 0;     // when the current read began (epoch seconds)
let playedChunks      = [];    // server ids of chunks fully played this session
let chunkIdByIndex    = {};    // index -> real server chunk id (from X-Chunk-Id)
let sessionDigestible = true;  // whether this read's chunks may be marked solid
// What the server needs to keep chunks in reading order. readId names the read
// (a jump stays in it, so the feed keeps numbering its chunks as one read);
// _speakEpoch changes on every new read, jump and stop, and the server turns
// away anything from an epoch older than the newest it has seen. Kept strictly
// increasing even within one millisecond, since cancelling epoch N supersedes
// N+1 too.
let readId      = "";
let _speakEpoch = 0;
function _nextEpoch() { _speakEpoch = Math.max(Date.now(), _speakEpoch + 2); return _speakEpoch; }
let sessionId         = 0;
let readingTabId      = null;
let _playSeqCounter   = 0;      // unique id per chunk play
let _pendingPlay      = {};     // seq → resolver, for the completion broadcast
// Every /speak request still in flight. The prefetch window keeps two or three
// going at once, and this used to be a single slot holding only the newest, so
// a stop or a jump aborted one and left the rest synthesising on a serial GPU
// ahead of the new read. Teardown now aborts the lot.
const _speakAborts    = new Set();
let playerTabId       = null;
let playerReady       = false;
// Whether this read is private: the popup asked for it, or the reading tab is
// an incognito one. A private read says so in every /speak and is never saved
// for resuming, so nothing of it outlives this worker's memory.
let sessionPrivate    = false;
let sessionTitle      = "";     // the page being read, for the resume offer
let sessionUrl        = "";
// Set when the server stopped answering in the middle of a read. The list and
// the position are kept, so Resume carries on from the chunk that failed.
let readHalted        = false;
let lastPlaybackError = null;   // { message, index, total } of that halt
// The id of the read this worker is keeping in storage for resuming, or ""
// when the current read is not being saved (private, too large, or stopped).
let _savedReadId      = "";
let _speedKnown       = false;  // currentSpeed came from the user, not the default
// The latest /settings speed push. Every /speak waits behind it, since the
// server paces a chunk at whatever speed it holds when synthesis begins.
let _speedPush        = Promise.resolve();
// The running loop's way to drop what it prefetched when the speed changes.
let _respeed          = null;
let _dashboardRead    = "";     // the read the dashboard was last opened for

// =============================================================================
// AUDIO (offscreen document) + DASHBOARD TAB
// =============================================================================
// Audio plays in an offscreen document, which has no autoplay restriction, needs
// no gesture and needs no visible tab, and that's what fixed the silent-playback
// problem. The visible
// dashboard UI (Chunks/Report/Rules/AI) is a separate player.html tab.

let _offscreenCreating = null;

async function _hasOffscreen() {
  if (chrome.runtime.getContexts) {
    try {
      const ctxs = await chrome.runtime.getContexts({
        contextTypes: ["OFFSCREEN_DOCUMENT"],
        documentUrls: [chrome.runtime.getURL("offscreen.html")],
      });
      return ctxs.length > 0;
    } catch { /* fall through */ }
  }
  if (chrome.offscreen && chrome.offscreen.hasDocument) {
    try { return await chrome.offscreen.hasDocument(); } catch {}
  }
  return false;
}

async function ensureOffscreen() {
  if (await _hasOffscreen()) { playerReady = true; return; }
  if (_offscreenCreating) { await _offscreenCreating; return; }
  _offscreenCreating = chrome.offscreen.createDocument({
    url: chrome.runtime.getURL("offscreen.html"),
    reasons: ["AUDIO_PLAYBACK"],
    justification: "Play locally-synthesised cloned-voice TTS audio in the background.",
  }).then(() => { playerReady = true; })
    .catch(async (e) => {
      if (await _hasOffscreen()) { playerReady = true; return; }
      console.error("[KAM] offscreen create failed:", e);
    })
    .finally(() => { _offscreenCreating = null; });
  await _offscreenCreating;
}

async function ensureDashboardTab() {
  // Opens the player.html dashboard tab, which is the visible UI, or reuses one
  // that's already there. It opens unfocused and pinned so it doesn't interrupt
  // whatever you're reading.
  const url = chrome.runtime.getURL("player.html");
  try {
    const existing = await chrome.tabs.query({ url });
    if (existing && existing.length > 0) { playerTabId = existing[0].id; return; }
  } catch {}
  try {
    const tab = await chrome.tabs.create({ url, active: false, pinned: true });
    playerTabId = tab.id;
    try { await chrome.storage.session.set({ playerTabId: tab.id }); } catch {}
  } catch (e) {
    console.error("[KAM] dashboard tab open failed:", e);
  }
}

// The dashboard used to open on every run of the loop, so it came back each
// time a chunk was jumped to even after being closed, and there was no way to
// say it was not wanted. It now opens once per read, and only while
// kamOpenDashboardOnRead is not false (the default is on). readId is the same
// across jumps and a resume of the same read, so closing the tab during a read
// is respected for the rest of it.
async function _openDashboardForRead() {
  if (_dashboardRead === readId) return;
  _dashboardRead = readId;
  let wanted = true;
  try {
    const d = await chrome.storage.local.get("kamOpenDashboardOnRead");
    wanted = !(d && d.kamOpenDashboardOnRead === false);
  } catch (e) { /* storage unavailable; keep the default */ }
  if (wanted) await ensureDashboardTab();
}

async function ensurePlayer() {
  // Brings up the audio path (offscreen) and, when wanted, the dashboard UI.
  // Only the offscreen document is waited for, since it is what plays. The
  // loop used to wait for the dashboard tab as well, which held the first
  // chunk back for a tab query and a tab create that audio never needed.
  await ensureOffscreen();
  _openDashboardForRead().catch(() => {});
}

// Audio messages go to the offscreen document (target:"offscreen"). offscreen.js
// ignores anything without that field; player.html tab ignores anything WITH it.
function toPlayer(msg) {
  try {
    const p = chrome.runtime.sendMessage({ ...msg, target: "offscreen" });
    if (p && typeof p.catch === "function") p.catch(() => {});
  } catch (_) { /* no receiver / SW transitioning — ignore */ }
}

function toPlayerAsync(msg) {
  return new Promise(resolve => {
    chrome.runtime.sendMessage({ ...msg, target: "offscreen" }, res => {
      if (chrome.runtime.lastError) { resolve(null); return; }
      resolve(res);
    });
  });
}

function pausePlayerAudio()  { return toPlayerAsync({ action: "pauseAtPosition" }); }
function resumePlayerAudio() { return toPlayerAsync({ action: "resumeFromPosition" }); }

async function stopPlayerAudio() {
  if (!(await _hasOffscreen())) return;
  await toPlayerAsync({ action: "stopOffscreen" });
}

function toReadingTab(msg) {
  if (readingTabId !== null) chrome.tabs.sendMessage(readingTabId, msg).catch(() => {});
}

// Single authoritative teardown. Guarantees no playback loop survives: bumping
// sessionId makes every running loop's `mySession === sessionId` check fail at
// its next await, so it exits. We also abort in-flight fetches, stop offscreen
// audio, and resolve any pending chunk-completion promises so no awaiter hangs.
// Both startSpeaking and stop call this, so a re-trigger can never leave two
// loops running (the cause of the slowdowns/crashes).
function teardownPlayback() {
  sessionId++;                       // invalidate every existing loop
  isStopped = true;
  const wasPaused = isPaused;
  isPaused  = false;
  isPlaying = false;
  for (const c of _speakAborts) { try { c.abort(); } catch (_) {} }
  _speakAborts.clear();
  // Aborting here does not reach the server, whose threads would go on to
  // synthesise every queued chunk of this run for nobody. So it is told.
  if (_speakEpoch) {
    kamFetch(`${SERVER}/speak/cancel`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ epoch: _speakEpoch }),
    }).catch(() => {});
  }
  // Teardown always leaves playback unpaused, since stopOffscreen lifts the
  // offscreen gate too. Nothing said so, though, so after a jump or a new read
  // started while paused the overlay and popup went on showing play while the
  // audio ran, and the next click paused it. So the pages are told.
  if (wasPaused) {
    toReadingTab({ action: "resumedPlaying" });
    chrome.runtime.sendMessage({ action: "resumedPlaying" }).catch(() => {});
  }
  // Resolve any awaiters so no loop is stranded on a pending completion.
  for (const k in _pendingPlay) { try { _pendingPlay[k]({ reason: "torndown" }); } catch (_) {} }
  _pendingPlay = {};
  stopPlayerAudio();
}

function stripMarkersForDisplay(text) {
  return kamStripMarkers(text);   // shared grammar — see markers.js
}

// =============================================================================
// SPEED, PRIVACY AND THE TAB BEING READ
// =============================================================================

// Tell the server the speed and keep the promise, so the /speak requests that
// follow can wait for it. The wait is bounded, since a server that takes the
// connection and then says nothing would otherwise hold every chunk behind it.
function _pushSpeed(speed) {
  const sent = kamFetch(`${SERVER}/settings`, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-KAM-Token": KAM_TOKEN },
    body: JSON.stringify({ speed })
  }).catch(() => {});
  _speedPush = Promise.race([sent, new Promise(r => setTimeout(r, 1500))]);
  return _speedPush;
}

async function _tabInfo(tabId) {
  if (tabId == null) return null;
  try { return (await chrome.tabs.get(tabId)) || null; } catch (e) { return null; }
}

// The last steps of starting a read, which have to wait for one question: is
// the reading tab an incognito one? If it is the read is private whatever the
// popup said, and that has to be known before the first /speak goes out and
// before anything is written to storage.
async function _beginRead(mySession) {
  const tab = await _tabInfo(readingTabId);
  if (mySession !== sessionId) return;      // stopped or replaced while I asked
  if (tab && tab.incognito) sessionPrivate = true;
  _saveRead();
  speakChunks(mySession);
}

// The "Preparing" state of the header bar. Until the first chunk has been
// synthesised there was nothing on the page at all, which on a slow machine is
// most of a minute of wondering whether the click was taken.
function _announcePreparing() {
  toReadingTab({ action: "overlayPreparing", text: "Preparing the first chunk…",
                 current: currentChunkIndex + 1, total: allChunks.length });
}

// =============================================================================
// RESUME WHERE YOU LEFT OFF
// =============================================================================
// All of a read used to live in this worker's globals. Stop emptied them, and a
// worker restart or a browser restart lost them, so a long article could only
// be started again from the top. The read is now kept in chrome.storage.local
// as it advances: the chunk list once under kamLastRead, and the moving
// position under kamLastReadPos, so each chunk costs one small write rather
// than the whole list again.
//
// A private read is never written. A list over about 2 MB serialised is not
// written either, because chrome.storage.local has a 10 MB quota shared with
// everything else the extension keeps and a write that size on every read
// start is not worth what it buys; such a read simply cannot be resumed.
const RESUME_KEY       = "kamLastRead";
const RESUME_POS_KEY   = "kamLastReadPos";
const RESUME_MAX_CHARS = 2000000;

function forgetResume() {
  _savedReadId = "";
  try { return chrome.storage.local.remove([RESUME_KEY, RESUME_POS_KEY]).catch(() => {}); }
  catch (e) { return Promise.resolve(); }
}

// Called once as a read starts. A private read leaves storage exactly as it
// found it, so an earlier saved read is still there afterwards. A read too
// large to save does replace the earlier one, with nothing, since offering to
// resume an older article after this one would be wrong.
function _saveRead() {
  if (sessionPrivate) { _savedReadId = ""; return; }
  let size = Infinity;
  try { size = JSON.stringify(allChunks).length; } catch (e) {}
  if (size > RESUME_MAX_CHARS) { forgetResume(); return; }
  _savedReadId = readId;
  const now = Date.now();
  try {
    chrome.storage.local.set({
      [RESUME_KEY]: { id: readId, url: sessionUrl, title: sessionTitle, chunks: allChunks,
                      total: allChunks.length, startTs: sessionStartTs,
                      digestible: sessionDigestible, speed: currentSpeed, savedAt: now },
      [RESUME_POS_KEY]: { id: readId, index: currentChunkIndex, speed: currentSpeed, savedAt: now },
    }).catch(() => {});
  } catch (e) { /* storage unavailable; the read still plays */ }
}

// Called as each chunk starts to play. Guarded by the id, so a read that was
// never saved (or was forgotten since) writes nothing.
function _savePos(index) {
  if (!_savedReadId || _savedReadId !== readId) return;
  try {
    chrome.storage.local.set({
      [RESUME_POS_KEY]: { id: readId, index, speed: currentSpeed, savedAt: Date.now() },
    }).catch(() => {});
  } catch (e) {}
}

// The saved read and where it had got to, or null when there is nothing that
// could be carried on: nothing saved, or a position at or past the end.
async function _loadResume() {
  let d = null;
  try { d = await chrome.storage.local.get([RESUME_KEY, RESUME_POS_KEY]); } catch (e) { return null; }
  const read = d && d[RESUME_KEY], pos = d && d[RESUME_POS_KEY];
  if (!read || !Array.isArray(read.chunks) || !read.chunks.length) return null;
  // A position belongs to the read it names. One left over from another read
  // would point at the wrong chunk, so it counts as the start.
  const mine  = pos && pos.id === read.id && Number.isInteger(pos.index);
  const index = mine ? pos.index : 0;
  if (index < 0 || index >= read.chunks.length) return null;
  return { read, index, speed: (mine && pos.speed) || read.speed || null,
           savedAt: (mine && pos.savedAt) || read.savedAt || 0 };
}

// What the popup asks before offering Resume. Null while a read is playing,
// and null for a saved read still at its first chunk, since there is nothing
// to carry on from. A read the server failed is the exception: the message
// told the user to press Resume, so it is offered even at chunk one.
async function getResume() {
  if (isPlaying && !isStopped) return { resume: null };
  if (readHalted && allChunks.length && currentChunkIndex < allChunks.length) {
    return { resume: { index: currentChunkIndex, total: allChunks.length, title: sessionTitle,
                       url: sessionUrl, savedAt: Date.now(), halted: true } };
  }
  const s = await _loadResume();
  if (!s || s.index === 0) return { resume: null };
  return { resume: { index: s.index, total: s.read.chunks.length, title: s.read.title || "",
                     url: s.read.url || "", savedAt: s.savedAt } };
}

// Which tab a resumed read belongs to. The tab it was started in may be gone
// (that is half the reason for resuming), so I look for the page by its URL
// and fall back to the tab the popup named. Never an incognito tab: a saved
// read is by definition not a private one, and attaching it to an incognito
// tab would make it look as though it were.
async function _tabForResume(url, hint) {
  let tabs = [];
  try { tabs = (await chrome.tabs.query({})) || []; } catch (e) { tabs = []; }
  const open   = tabs.filter(t => t && t.id != null && !t.incognito);
  const hinted = hint != null ? open.find(t => t.id === hint) : null;
  if (url) {
    if (hinted && hinted.url === url) return hinted.id;
    const same = open.filter(t => t.url === url);
    if (same.length) return (same.find(t => t.active) || same[0]).id;
  }
  return hinted ? hinted.id : null;
}

// Carry on a read. A read the server failed is still in memory, position and
// all, so it restarts from there (which also covers a private read, which is
// in memory and nowhere else). Otherwise the saved read is put back into the
// globals and played from its saved position.
async function resumeRead(request) {
  request = request || {};
  if (isPlaying && !isStopped) return { status: "none" };
  if (readHalted && allChunks.length && currentChunkIndex < allChunks.length) {
    teardownPlayback();
    const s = ++sessionId;
    isStopped = false; isPaused = false;
    readHalted = false; lastPlaybackError = null;
    if (request.speed) { currentSpeed = request.speed; _speedKnown = true; }
    // The server may be a new process since the failure, holding its own speed.
    _pushSpeed(currentSpeed);
    _refreshHwHints();
    _announcePreparing();
    speakChunks(s);
    return { status: "started" };
  }
  const saved = await _loadResume();
  if (!saved) return { status: "none" };
  const tabId = await _tabForResume(saved.read.url, request.tabId);
  if (isPlaying && !isStopped) return { status: "none" };   // a read began while I looked

  teardownPlayback();
  digestPlayedChunks();
  const s = ++sessionId;
  isStopped = false; isPaused = false;
  readHalted = false; lastPlaybackError = null;
  allChunks         = saved.read.chunks;
  displayChunks     = allChunks.map(stripMarkersForDisplay);
  currentChunkIndex = saved.index;
  // The slider wins when I know it. After a worker restart I do not, so the
  // speed the read was last heard at is the better guess than the default.
  if (request.speed) { currentSpeed = request.speed; _speedKnown = true; }
  else if (!_speedKnown && saved.speed) currentSpeed = saved.speed;
  // The same id and start time as before, so the server's feed numbers these
  // chunks as the rest of the read they belong to rather than as a new one.
  readId            = saved.read.id || (Date.now().toString(36) + Math.random().toString(36).slice(2, 6));
  sessionStartTs    = saved.read.startTs || Date.now() / 1000;
  playedChunks      = [];
  chunkIdByIndex    = {};
  sessionDigestible = (saved.read.digestible !== false);
  sessionPrivate    = false;
  sessionTitle      = saved.read.title || "";
  sessionUrl        = saved.read.url || "";
  _savedReadId      = readId;
  _pushSpeed(currentSpeed);
  _refreshHwHints();
  if (readingTabId !== null && readingTabId !== tabId) toReadingTab({ action: "clearOverlay" });
  readingTabId = tabId;
  _announcePreparing();
  speakChunks(s);
  return { status: "started" };
}

// =============================================================================
// A SERVER THAT STOPPED ANSWERING
// =============================================================================
// Every failed /speak used to come back as "no audio" and the loop skipped on,
// so when the server died the rest of the read failed in milliseconds and the
// popup was told playback had finished. The loop now counts failures and calls
// this instead: the read stops where it is, the position is kept at the first
// chunk that failed, and the pages are told what happened in words.
async function haltRead(index) {
  const total = allChunks.length;
  // Teardown stops the loop and the requests still out. It does not touch the
  // list, and the played chunks are left undigested for now, since a digest
  // sent to a server that is down would simply be lost.
  teardownPlayback();
  const s = sessionId;
  currentChunkIndex = index;
  readHalted = true;
  _savePos(index);
  // Down, or up and failing? The two need different advice. Bounded, because a
  // server that is hung rather than gone would never answer.
  const up = await Promise.race([_serverUp(), new Promise(r => setTimeout(() => r(false), 3000))]);
  if (s !== sessionId) return;              // a new read began while I asked
  const message = up
    ? "The server could not make the audio. Press Resume to try again."
    : "The server stopped responding. Start it again and press Resume.";
  lastPlaybackError = { message, index, total };
  const msg = { action: "playbackError", message, index, total, serverUp: up };
  chrome.runtime.sendMessage(msg).catch(() => {});
  toReadingTab(msg);
}

// =============================================================================
// MESSAGE HANDLER
// =============================================================================

// =============================================================================
// THE NATIVE HOST, OWNED HERE
// =============================================================================
//
// The power button used to live only in the dashboard, and it had to, because
// kam_host.py kills the server when its port closes ("clean up the child so we
// don't leave an orphan"). A port opened from the popup dies the moment the
// popup loses focus, which is a few seconds into a boot that takes most of a
// minute, so a popup button would have looked like it worked and then killed
// the server halfway through loading torch.
//
// So the worker holds the port instead. It outlives the popup, the existing
// keepAlive alarm pings the host every 24 seconds so neither the port nor the
// worker goes idle, and the server therefore stays up for as long as Chrome
// does.
//
// The dashboard used to hold a port of its own, so a server started from the
// dashboard died when its tab closed (seen 12 Sep). It now goes through here
// too, and each host message is relayed to it whole, log lines included, so
// the dashboard console reads exactly as it did.
const HOST_NAME = "com.kam.tts";
const HOST_MISSING = "Native host not found. Start the server once with Start KAM TTS.bat " +
                     "(it repairs the registration), then quit and reopen Chrome.";
let _hostPort  = null;
let _hostState = { connected: false, running: false, ready: false,
                   stage: "", error: "", startedAt: 0, pid: null };

function hostSnapshot() {
  return Object.assign({}, _hostState);
}

// Tell anyone listening (the popup and the dashboard) that something moved.
// `event` is the host message that caused it, when there was one. Wrapped
// because with no page open there is no receiver and that rejects.
function _hostBroadcast(event) {
  const msg = { action: "hostEvent", state: hostSnapshot() };
  if (event) msg.event = event;
  try {
    chrome.runtime.sendMessage(msg).catch(() => {});
  } catch (e) { /* nobody listening */ }
}

function _hostConnect() {
  if (_hostPort) return _hostPort;
  try {
    _hostPort = chrome.runtime.connectNative(HOST_NAME);
  } catch (e) {
    _hostPort = null;
    _hostState.error = HOST_MISSING;
    _hostBroadcast({ type: "error", message: HOST_MISSING });
    return null;
  }
  _hostState.connected = true;
  _hostState.error = "";
  const port = _hostPort;
  port.onMessage.addListener(m => {
    if (!m || m.type === "pong") return;
    if (m.type === "stage")  _hostState.stage = m.stage || "";
    if (m.type === "ready")  { _hostState.ready = true; _hostState.running = true; _hostState.stage = "ready"; }
    // A pid arrives only with a server the host launched itself, which is the
    // only kind it can stop. See hostStop.
    if (m.type === "status") { _hostState.running = !!m.running; _hostState.pid = m.running ? (m.pid || null) : null; }
    if (m.type === "error")  _hostState.error = m.message || "host error";
    if (m.type === "exit")   { _hostState.running = false; _hostState.ready = false; _hostState.stage = ""; _hostState.pid = null; }
    _hostBroadcast(m);
  });
  port.onDisconnect.addListener(() => {
    const err = chrome.runtime.lastError;
    if (_hostPort === port) _hostPort = null;
    _hostState.connected = false;
    // Not cleared: whether the server is up is now a question for the health
    // check, since the host going away does not by itself say it stopped.
    if (err && !_hostState.error) _hostState.error = HOST_MISSING;
    _hostBroadcast({ type: "disconnect", message: err ? err.message : "" });
  });
  return port;
}

function hostStart() {
  if (!_hostConnect()) return;
  _hostState.ready = false;
  _hostState.error = "";
  _hostState.pid = null;
  _hostState.stage = "process-started";
  _hostState.startedAt = Date.now();
  _hostBroadcast();
  try { _hostPort.postMessage({ cmd: "start" }); } catch (e) {
    _hostState.error = String(e && e.message || e);
    _hostBroadcast({ type: "error", message: _hostState.error });
  }
}

// Two ways to stop. A server this worker's host launched is stopped through the
// host, which lets synthesis finish and reports the exit. Anything else, started
// with Start KAM TTS.bat or found already running when the host was asked to
// start, the host cannot stop, since it only stops its own child. So the server
// is asked directly; /shutdown is token-guarded like every other route.
async function hostStop() {
  if (_hostPort && _hostState.pid) {
    try { _hostPort.postMessage({ cmd: "stop" }); return; } catch (e) { /* port gone; ask directly */ }
  }
  let res = null;
  try {
    res = await kamFetch(SERVER + "/shutdown", { method: "POST",
      headers: { "Content-Type": "application/json" }, body: "{}" });
  } catch (e) { res = null; }
  if (res && res.ok) {
    _hostState.running = false; _hostState.ready = false;
    _hostState.stage = ""; _hostState.pid = null;
    _hostBroadcast({ type: "log", line: "[HOST] Asked the server to shut down." });
    _hostBroadcast({ type: "stopping" });
    return;
  }
  const message = res && res.status === 403
    ? "The server refused to stop: its token does not match this extension. Close its window instead."
    : "Could not reach the server to stop it. If it is still running, close its window.";
  _hostBroadcast({ type: "error", message });
}

// Asking never opens a port, since a port opened only to ask is a host process
// started for nothing. Pages that open mid-boot get the snapshot and then events.
function hostStatus() {
  if (_hostPort) {
    try { _hostPort.postMessage({ cmd: "status" }); } catch (e) { /* already gone */ }
  }
}

function _hostPing() {
  if (!_hostPort) return;
  // Traffic in either direction resets the worker's idle timer, which is what
  // keeps the port, and therefore the server, alive between chunks.
  try { _hostPort.postMessage({ cmd: "ping" }); } catch (e) { _hostPort = null; }
}

// --- Finishing an update ---
// The server has put the new files in place, but the old code is still what is
// running on both sides. Stop the server, wait for it to go (a new one would
// meet the old one's lock), then reload the extension. The new worker picks up
// where this left off in _resumeAfterUpdate.
function _serverUp() {
  return fetch(SERVER + "/health", { cache: "no-store" }).then(r => r.ok, () => false);
}

async function _updateRestart(req) {
  try { await chrome.storage.local.set({ kamResume: { at: Date.now(), from: req.from, to: req.to } }); } catch (e) {}
  await hostStop();
  for (let i = 0; i < 40 && await _serverUp(); i++) await new Promise(r => setTimeout(r, 500));
  chrome.runtime.reload();
}

// After the reload an update asked for: start the server again, bring the
// dashboard back to the front, and leave it a note to say what changed.
async function _resumeAfterUpdate() {
  let saved = null;
  try { saved = (await chrome.storage.local.get("kamResume")).kamResume; } catch (e) { return; }
  if (!saved) return;
  try { await chrome.storage.local.remove("kamResume"); } catch (e) {}
  if (Date.now() - saved.at > 5 * 60 * 1000) return;   // not this restart
  try { await chrome.storage.local.set({ kamUpdated: { at: Date.now(), from: saved.from, to: saved.to } }); } catch (e) {}
  hostStart();
  await ensureDashboardTab();
  if (playerTabId) { try { await chrome.tabs.update(playerTabId, { active: true }); } catch (e) {} }
}

// Start a read: the one way in, for the popup's Speak button and for the
// right-click menu alike. `request` is the popup's startSpeaking message, or
// the same fields put together by readFromMenu.
function startRead(request) {
  const chunks = request.chunks || [];
  if (chunks.length === 0) return { status: "no_chunks" };

  // Ignore an exact duplicate of what we are ALREADY playing (double-click or
  // redelivered message) so it doesn't restart the same read.
  const sameAsCurrent = isPlaying && !isStopped &&
    allChunks.length === chunks.length &&
    allChunks[0] === chunks[0] &&
    allChunks[allChunks.length - 1] === chunks[chunks.length - 1];
  if (sameAsCurrent) return { status: "already_playing" };

  // Any other start (including a different selection) cleanly REPLACES the
  // current playback. teardownPlayback() bumps sessionId so the old loop dies
  // at its next await before this one begins, so there's never more than one.
  teardownPlayback();
  // Digest what was heard of the read being replaced before its list is
  // reset below. Only stop used to do this, so starting a new selection over
  // a running one threw away every chunk already listened to. It runs while
  // sessionDigestible still describes the old read, which is the right one.
  digestPlayedChunks();

  const newSession  = ++sessionId;   // fresh id after teardown's bump
  isStopped         = false;
  isPaused          = false;
  readHalted        = false;
  lastPlaybackError = null;
  allChunks         = chunks;
  displayChunks     = chunks.map(stripMarkersForDisplay);
  currentChunkIndex = request.startIndex || 0;
  currentSpeed      = request.speed || 1.0;
  _speedKnown       = true;
  sessionStartTs    = Date.now() / 1000;   // session window for solid marking
  readId            = Date.now().toString(36) + Math.random().toString(36).slice(2, 6);
  playedChunks      = [];                   // ids of chunks fully heard this session
  chunkIdByIndex    = {};                   // reset per-read index->id map
  sessionDigestible = (request.digestible !== false);  // false for Custom Text
  sessionPrivate    = (request.private === true);
  sessionTitle      = request.title || "";
  sessionUrl        = request.url || "";
  // Push the user's speed to the server so synthesis pacing matches the
  // slider even if it changed while the server was offline.
  _pushSpeed(currentSpeed);
  // The prefetch depth comes from the server, and a worker that started
  // before the server had none until the five-minute refresh came round, so
  // it is asked for again as each read starts. Not waited for.
  _refreshHwHints();
  // A read moving to another tab used to leave the old tab with its header
  // bar, its highlight and its pushed-down body, and that bar's buttons then
  // paused and stopped the new tab's read. So the old tab is cleared first.
  const newTabId = request.tabId || null;
  if (readingTabId !== null && readingTabId !== newTabId) toReadingTab({ action: "clearOverlay" });
  readingTabId      = newTabId;
  _announcePreparing();
  // The read is under way from here, so a popup asking for the status in the
  // moment before the loop starts is told so.
  isPlaying         = true;
  _beginRead(newSession);
  return { status: "started" };
}

chrome.runtime.onMessage.addListener((request, sender, sendResponse) => {

  // Completion broadcast from offscreen, keyed by chunk seq. This is the single
  // signal that advances the playback loop.
  if (request.action === "chunkComplete") {
    const r = _pendingPlay[request.seq];
    if (r) { delete _pendingPlay[request.seq]; r({ status: "finished", reason: request.reason }); }
    return false;
  }

  if (request.action === "startSpeaking") {
    sendResponse(startRead(request));
    return true;
  }

  if (request.action === "stop") {
    teardownPlayback();
    // Digest the chunks heard before the stop. playedChunks holds only fully
    // played chunks, so the 3-4 prefetched chunks waiting ahead are excluded.
    digestPlayedChunks();
    currentChunkIndex = 0; allChunks = []; displayChunks = [];
    readHalted = false; lastPlaybackError = null; sessionPrivate = false;
    // The saved read stays in storage, since carrying on after a stop is the
    // point of it. Clearing the id only stops this worker moving its position.
    _savedReadId = "";
    toReadingTab({ action: "clearOverlay" });
    sendResponse({ status: "stopped" });
    return true;
  }

  if (request.action === "getStatus") {
    // chunkIds lets a popup opened mid-read report a chunk by its real id.
    // error is the last halt, so a popup opened after the server failed can
    // still say why nothing is playing.
    sendResponse({ isPlaying, isPaused, currentChunkIndex, chunks: allChunks, totalChunks: allChunks.length,
                   chunkIds: Object.assign({}, chunkIdByIndex),
                   private: sessionPrivate, error: lastPlaybackError });
    return true;
  }

  if (request.action === "getResume") {
    getResume().then(sendResponse, () => sendResponse({ resume: null }));
    return true;
  }

  if (request.action === "resumeRead") {
    resumeRead(request).then(sendResponse, () => sendResponse({ status: "none" }));
    return true;
  }

  if (request.action === "forgetResume") {
    forgetResume();
    readHalted = false; lastPlaybackError = null;
    sendResponse({ status: "ok" });
    return true;
  }

  if (request.action === "setSpeed") {
    // The popup sends this every time it opens, with the speed the server
    // already has, so only a real change may disturb what is prefetched.
    const changed = Number(request.speed) !== Number(currentSpeed);
    currentSpeed = request.speed;
    _speedKnown  = true;
    // Single source of truth: speed is applied by the SERVER at synthesis
    // (model-native pacing, no pitch shift). Forwarding it to the audio
    // element's playbackRate as well multiplied the two speeds together.
    _pushSpeed(request.speed);
    // The chunks waiting ahead were made at the old speed, so a change used to
    // be heard about five chunks late. The loop drops them and asks again. The
    // push above is sent first and those requests wait behind it.
    if (changed && _respeed && isPlaying && !isStopped) {
      _respeed();
      _savePos(currentChunkIndex);
    }
    sendResponse({ status: "ok" });
    return true;
  }

  if (request.action === "setVolume") {
    // Stored rather than relayed: the storage listener below pushes it to the
    // offscreen document, and storage is what a new offscreen document reads.
    _saveGain(request.volume);
    sendResponse({ status: "ok" });
    return true;
  }

  // The offscreen document asks for the volume as it starts, since it can use
  // chrome.runtime and nothing else, so it cannot read storage itself.
  if (request.action === "getPlaybackGain") {
    _readGain().then(gain => sendResponse({ gain }));
    return true;
  }

  if (request.action === "playerReady") {
    playerReady = true;
    // Use the sender tab ID, which is more reliable than chrome.tabs.create's
    if (sender && sender.tab && sender.tab.id) {
      playerTabId = sender.tab.id;
      try { chrome.storage.session.set({ playerTabId: sender.tab.id }); } catch {}
    }
    sendResponse({ status: "ok" });
    return true;
  }

  if (request.action === "pauseResume") {
    if (isPaused) {
      isPaused = false;
      resumePlayerAudio();
      toReadingTab({ action: "resumedPlaying" });
      chrome.runtime.sendMessage({ action: "resumedPlaying" }).catch(() => {});
      sendResponse({ status: "ok", isPaused });
    } else {
      isPaused = true;
      pausePlayerAudio();
      toReadingTab({ action: "pausedPlaying" });
      chrome.runtime.sendMessage({ action: "pausedPlaying" }).catch(() => {});
      sendResponse({ status: "ok", isPaused });
    }
    return true;
  }

  if (request.action === "prepareClear") {
    prepareClear().then(sendResponse, () => sendResponse({ keep: [] }));
    return true;
  }

  if (request.action === "jumpTo") {
    teardownPlayback();
    const s = ++sessionId;
    isStopped = false;
    readHalted = false; lastPlaybackError = null;
    currentChunkIndex = request.index;
    speakChunks(s);
    sendResponse({ status: "jumping" });
    return true;
  }

  if (request.action === "keepAlive") {
    sendResponse({ status: "ok" });
    return true;
  }

  // --- Starting the server from anywhere, not just the dashboard ---
  if (request.action === "hostStart") { hostStart(); sendResponse(hostSnapshot()); return true; }
  if (request.action === "hostStop")  { hostStop();  sendResponse(hostSnapshot()); return true; }
  if (request.action === "hostState") { hostStatus(); sendResponse(hostSnapshot()); return true; }
  if (request.action === "updateRestart") { _updateRestart(request); sendResponse({ ok: true }); return true; }

  // The dashboard data proxy, since player.html routes its server fetches here
  if (request.action === "dashboardFetch") {
    const opts = {
      method: request.method || "GET",
      headers: { "X-KAM-Token": KAM_TOKEN, ...(request.body ? { "Content-Type": "application/json" } : {}) },
      body:    request.body ? JSON.stringify(request.body) : undefined,
    };
    kamFetch(`${SERVER}${request.path}`, opts)
      .then(_dashboardReply)
      .then(sendResponse)
      .catch(e  => sendResponse({ ok: false, error: String(e) }));
    return true;
  }

  return false;
});

// Turn a server reply into what the dashboard proxy sends back. A refusal used
// to come back as ok with {error:"unauthorised"} for data, so the console poll
// painted the server online while every call was being turned away. A 401 or
// 403 is an error now. Other statuses still pass their JSON through, because
// some routes (the updater, say) explain a refusal in the body and the
// dashboard shows that explanation.
async function _dashboardReply(r) {
  if (r.status === 401 || r.status === 403) {
    return { ok: false, error: "The server refused this extension's token (" + r.status + ")." };
  }
  return { ok: true, data: await r.json() };
}

// =============================================================================
// VOLUME
// =============================================================================
// One stored gain factor (0 to 6, where 1 is 100%) in chrome.storage.local.
// The dashboard kept it in its own localStorage and the offscreen document in
// chrome.storage.session, which an offscreen document cannot even reach, so
// after a restart, or whenever the offscreen document was made again, playback
// fell back to 100% while the slider still showed what was chosen.
function _clampGain(g) {
  const n = Number(g);
  return Number.isFinite(n) ? Math.max(0, Math.min(6, n)) : null;
}

async function _readGain() {
  try {
    const d = await chrome.storage.local.get("playbackGain");
    const g = _clampGain(d && d.playbackGain);
    return g === null ? 1.0 : g;
  } catch (e) { return 1.0; }
}

function _saveGain(g) {
  const v = _clampGain(g);
  if (v === null) return;
  try { chrome.storage.local.set({ playbackGain: v }).catch(() => {}); } catch (e) {}
}

try {
  chrome.storage.onChanged.addListener((changes, area) => {
    if (area !== "local" || !changes.playbackGain) return;
    const g = _clampGain(changes.playbackGain.newValue);
    if (g !== null) toPlayer({ action: "setVolume", volume: g });
  });
} catch (e) { /* storage unavailable; the offscreen document keeps its last value */ }

// =============================================================================
// FETCH
// =============================================================================

function sanitizeText(text) {
  // Accents and everyday symbols first, while they can still be told apart:
  // the last step below replaces whatever is still non-ASCII with a space.
  return kamSpeakable(text)
    // Strip the semantic layout markers like |H1|, |/H1|, |BOLD| and |BREAK|
    // before synthesis. They're hints for the UI and the classifier rather than
    // speakable text, and leaving them in was
    // sending "|H1|4.3.2 Reading…" to the server and wedging the first chunk.
    .replace(KAM_MARKER_RE, ' ')
    .replace(/[\u2018\u2019]/g, "'").replace(/[\u201C\u201D]/g, '"')
    .replace(/[\u2013\u2014]/g, ', ').replace(/[\u2026]/g, '...')
    .replace(/[\u00A0]/g, ' ').replace(/[\u2022\u2023\u2043]/g, '')
    // Map the maths and logic symbols to spoken words before the ASCII strip
    // below, otherwise the [^\x20-\x7E] replace deletes them and the equations
    // go silent.
    .replace(/[\u21D2\u2192\u27F6\u27F9]/g, ' implies ')   // ⇒ → ⟶ ⟹
    .replace(/[\u21D4\u2194\u27FA]/g, ' if and only if ')  // ⇔ ↔ ⟺
    .replace(/\u2227/g, ' and ')        // ∧
    .replace(/\u2228/g, ' or ')         // ∨
    .replace(/[\u00AC\u02DC]/g, ' not ')// ¬
    .replace(/\u2200/g, ' for all ')    // ∀
    .replace(/\u2203/g, ' there exists ') // ∃
    .replace(/\u2208/g, ' in ')         // ∈
    .replace(/\u2209/g, ' not in ')     // ∉
    .replace(/\u2286/g, ' subset of ')  // ⊆
    .replace(/\u2282/g, ' proper subset of ') // ⊂
    .replace(/\u222A/g, ' union ')      // ∪
    .replace(/\u2229/g, ' intersection ') // ∩
    .replace(/\u2260/g, ' not equal to ') // ≠
    .replace(/\u2264/g, ' less than or equal to ') // ≤
    .replace(/\u2265/g, ' greater than or equal to ') // ≥
    .replace(/\u2261/g, ' equivalent to ') // ≡
    .replace(/\u221E/g, ' infinity ')   // ∞
    .replace(/\u2211/g, ' sum of ')     // ∑
    .replace(/\u220F/g, ' product of ') // ∏
    .replace(/\u221A/g, ' square root of ') // √
    .replace(/[\u00D7]/g, ' times ')    // ×
    .replace(/[\u00F7]/g, ' divided by ') // ÷
    .replace(/[\u00B1]/g, ' plus or minus ') // ±
    .replace(/\u2248/g, ' approximately ') // ≈
    .replace(/\u2234/g, ' therefore ')  // ∴
    .replace(/\u2235/g, ' because ')    // ∵
    .replace(/\u22A2/g, ' proves ')     // ⊢
    .replace(/\u22A8/g, ' entails ')    // ⊨
    .replace(/[^\x20-\x7E]/g, ' ').replace(/\s+/g, ' ').trim();
}

// The document-structure hint for prosody, read from the chunk's raw markers
// before sanitizeText strips them. A chunk from an h1, h2 or h3 gets the
// heading announce pause; a chunk closing a bullet gets the inter-item gap; a
// chunk closing a paragraph gets a longer breath.
//
// This hint is the only structural channel the server actually has, since
// sanitizeText strips every marker out of the text before it is posted, so
// anything the server needs to know about the shape of the page has to be
// decided here and named in this string.
function detectPositionHint(rawText) {
  // The level, not just the fact of being a heading. This used to return the
  // generic "heading" for all three, so the server's h1_heading, h2_heading and
  // h3_heading labels were defined and unreachable: every heading whatever its
  // depth got the same 62 ms, and a chapter boundary sounded like a sub-section.
  const heading = KAM_HEADING_START_RE.exec(rawText);
  if (heading)                            return "h" + heading[1];
  if (KAM_LIST_END_RE.test(rawText))      return "list_item";
  if (KAM_PARAGRAPH_END_RE.test(rawText)) return "paragraph_end";
  return null;
}

// mySession is the read this request belongs to. A request that outlives its
// read (one that finished just as it was aborted, or that nothing aborted) must
// not touch the next read's state, since its index means a different chunk
// there: it used to write its server id into the new chunkIdByIndex and
// announce its old text as the chunk now playing, so a thumbs-up rated the
// wrong chunk.
//
// opts carries the two things only some requests say. head marks the first
// request of a run, which lets the server skip the short wait it uses to put
// near-simultaneous requests in order, since nothing can be ahead of it.
// private marks a private read, which the server must not keep the text of.
// Both are left out when they do not apply, so an ordinary request reads as
// it always did.
async function fetchAudioBase64(text, idx, mySession, epoch, opts) {
  const safe = sanitizeText(text);
  if (!safe || safe.length < 2) return null;
  const position = detectPositionHint(text);
  const controller  = new AbortController();
  _speakAborts.add(controller);
  const current = () => mySession === undefined || mySession === sessionId;
  const body = { text: safe, index: (idx != null ? idx + 1 : null), position: position,
                 read_id: readId || null, read_ts: sessionStartTs || null,
                 read_total: allChunks.length, epoch: epoch || null };
  if (opts && opts.head)    body.head = true;
  if (opts && opts.private) body.private = true;
  try {
    const res = await kamFetch(`${SERVER}/speak`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-KAM-Token": KAM_TOKEN },
      body: JSON.stringify(body),
      signal: controller.signal
    });
    // 409: the server turned it away because a newer read began first. That
    // is the ordering working, not a failure, so it is not logged as one.
    if (res.status === 409) return null;
    if (!res.ok) throw new Error(`Server ${res.status}`);
    if (!current()) return null;     // superseded read; its audio is no use now
    // Record the server's id against this chunk's index, so the solid-digest
    // and the feedback buttons can name the exact chunk. It is NOT announced
    // here. This runs two or three chunks ahead of the one playing, and
    // announcing it at fetch time is what made a thumbs-up or a report land
    // on a chunk that had not been heard yet. announceChunk does it when the
    // chunk starts to play.
    const chunkId = res.headers.get('X-Chunk-Id');
    if (chunkId && idx != null) chunkIdByIndex[idx] = chunkId;
    const buf   = await res.arrayBuffer();
    const bytes = new Uint8Array(buf);
    // Built in chunks, which avoids a stack overflow on large WAV buffers
    const CHUNK = 8192;
    let bin = "";
    for (let i = 0; i < bytes.byteLength; i += CHUNK) {
      bin += String.fromCharCode.apply(null, bytes.subarray(i, i + CHUNK));
    }
    return btoa(bin);
  } finally {
    _speakAborts.delete(controller);
  }
}

// =============================================================================
// PLAYBACK LOOP
// =============================================================================

async function speakChunks(mySession) {
  // The epoch moves when the speed changes mid-read (see respeed below), so
  // each request takes the one current when it is issued.
  let myEpoch = _nextEpoch();
  const live = () => !isStopped && mySession === sessionId;

  // Prefetch cache: key=chunkIndex, value=Promise<base64|null>
  let prefetchCache = {};
  // Which of those nulls were real failures. A null also means a chunk that
  // cleans to nothing, a 409 from a newer read, or an abort, and none of those
  // says anything about the server's health.
  const failed = {};
  // Bumped each time the prefetched chunks are dropped, so a request issued
  // before the drop cannot be mistaken for one issued after it.
  let gen = 0;
  const headIdx = currentChunkIndex;   // the chunk this run starts on
  let headSent  = false;
  // The chunk whose audio the loop already has in hand, playing or held by a
  // pause. A speed change keeps that one and drops the rest.
  let heldIdx   = -1;

  function prefetch(idx) {
    if (idx >= allChunks.length) return;
    if (prefetchCache[idx]) return;
    const epoch = myEpoch, g = gen;
    const opts = { private: sessionPrivate };
    if (idx === headIdx && !headSent) { headSent = true; opts.head = true; }
    delete failed[idx];
    // Behind the speed push, so the server holds the right speed before it
    // starts on this chunk. A request overtaken while it waited is not sent.
    prefetchCache[idx] = _speedPush.then(() => {
      if (!live() || g !== gen) return null;
      return fetchAudioBase64(allChunks[idx], idx, mySession, epoch, opts);
    }).catch((e) => {
      // Surface the failure: a silent null here looks like a mystery playback
      // crash. AbortError is normal teardown; everything else is logged.
      if (!e || e.name !== "AbortError") {
        console.error(`[TTS-BG] chunk ${idx + 1} fetch failed:`, e && e.message);
        if (g === gen && live()) failed[idx] = true;
      }
      return null;
    });
  }

  // Prefetch depth: how many chunks ahead to keep synthesising. Server synthesis
  // is serial (single GPU under _inference_lock) and takes ~30-45s per chunk,
  // while a chunk plays in ~10-15s. A shallow 2-ahead window let playback catch
  // up to synthesis and stall, which was the dead-air-between-chunks bug. A
  // deeper window
  // issues requests early so the server pipeline stays saturated and the next
  // chunk's audio is ready the instant the current one ends. Requests queue
  // server-side in order, so issuing them early costs nothing but latency hidden.
  // Prefetch depth adapts to the machine: the server measures synthesis speed
  // (RTF) and tells us how deep to buffer. Slow machines get a deeper window so
  // playback doesn't stall; fast machines use a shallow one and waste less.
  // Falls back to 2 when the server hasn't reported a value.
  function _prefetchAhead() {
    const n = (typeof _hwPrefetchTarget === 'number') ? _hwPrefetchTarget : 3;
    return Math.max(1, n - 1);   // "target chunks in flight" → chunks ahead
  }

  function fillPrefetchWindow(fromIdx) {
    const ahead = _prefetchAhead();
    for (let i = fromIdx; i <= fromIdx + ahead && i < allChunks.length; i++) {
      if (mySession !== sessionId || isStopped) return;
      prefetch(i);
    }
  }

  // --- A speed change during the read ---
  // Keeps the chunk in hand and drops every chunk after it: the requests still
  // out are aborted, the server is told to give up that epoch as teardown
  // does, and a new epoch is taken for what is asked next. The playing chunk
  // is not touched. The handler has already sent the new speed, and prefetch
  // waits behind that push, so the refill cannot be made at the old speed.
  //
  // A slider being dragged fires many of these, so the refill waits for a
  // quarter of a second of quiet rather than asking and dropping on each one.
  let refillTimer = null;
  let settleAt    = 0;
  const respeed = () => {
    if (!live()) return;
    const held = heldIdx === currentChunkIndex;
    const kept = held ? prefetchCache[currentChunkIndex] : null;
    const stale = Object.keys(prefetchCache).some(k => !(held && Number(k) === currentChunkIndex));
    if (stale || _speakAborts.size) {
      const oldEpoch = myEpoch;
      myEpoch = _nextEpoch();
      gen++;
      prefetchCache = {};
      if (kept) prefetchCache[currentChunkIndex] = kept;
      for (const c of _speakAborts) { try { c.abort(); } catch (_) {} }
      _speakAborts.clear();
      kamFetch(`${SERVER}/speak/cancel`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ epoch: oldEpoch }),
      }).catch(() => {});
    }
    settleAt = Date.now() + 250;
    clearTimeout(refillTimer);
    refillTimer = setTimeout(() => {
      if (!live()) return;
      fillPrefetchWindow(heldIdx === currentChunkIndex ? currentChunkIndex + 1 : currentChunkIndex);
    }, 250);
  };
  _respeed = respeed;

  // Synthesis starts now, before the audio path is up. The loop used to wait
  // for ensurePlayer first, so the server sat idle for as long as the offscreen
  // document (and the dashboard tab) took to appear. A stop during that wait
  // is still clean: each request registers its abort controller before it is
  // sent, and the epoch it carries is the one teardown cancels.
  isPlaying = true;
  fillPrefetchWindow(currentChunkIndex);

  await ensurePlayer();
  if (!live()) return;

  // Consecutive failed fetches, and the first chunk of the current run of them.
  let failStreak = 0, firstFailed = -1;

  while (currentChunkIndex < allChunks.length && !isStopped && mySession === sessionId) {
    try {
      while (isPaused && !isStopped && mySession === sessionId) {
        await new Promise(r => setTimeout(r, 50));
      }
      if (isStopped || mySession !== sessionId) break;

      // Get the audio, either from the cache if it's already fetching or fresh
      prefetch(currentChunkIndex);
      const pending = prefetchCache[currentChunkIndex], g = gen;
      let base64 = await pending;
      // Only my own entry is removed: a speed change may have put a newer
      // request in its place while I waited.
      if (prefetchCache[currentChunkIndex] === pending) delete prefetchCache[currentChunkIndex];

      // A stop or a jump aborts the request I was waiting on, and that abort
      // arrives here as "no audio". This used to fall through to the skip
      // below and add one to currentChunkIndex, which by then belonged to the
      // new run: a jump made while a chunk was still being synthesised started
      // one chunk past the one asked for.
      if (isStopped || mySession !== sessionId) break;

      // The speed changed while I waited, so what I was waiting for was
      // dropped. Ask for the same chunk again once the slider has settled.
      if (g !== gen) {
        while (live() && Date.now() < settleAt) await new Promise(r => setTimeout(r, 50));
        continue;
      }

      // Keep the sliding window full ahead of the current position.
      fillPrefetchWindow(currentChunkIndex + 1);

      if (!base64) {
        // One failure is skipped as before. Two in a row means the server has
        // gone, so the loop stops rather than failing its way to the end.
        if (failed[currentChunkIndex]) {
          if (!failStreak) firstFailed = currentChunkIndex;
          if (++failStreak >= 2) break;
        }
        currentChunkIndex++;
        continue;
      }
      failStreak = 0;
      heldIdx = currentChunkIndex;
      if (isPaused) { prefetchCache[currentChunkIndex] = Promise.resolve(base64); continue; }

      chrome.runtime.sendMessage({ action: "chunkStatus", current: currentChunkIndex + 1, total: allChunks.length }).catch(() => {});
      toReadingTab({ action: "updateOverlay", text: displayChunks[currentChunkIndex] || allChunks[currentChunkIndex], current: currentChunkIndex + 1, total: allChunks.length });
      _savePos(currentChunkIndex);

      await ensureOffscreen();
      if (isStopped || mySession !== sessionId) break;

      const estSecs = Math.max(1, (base64.length * 3) / 4) / 48000;

      // --- Serial playback dispatch ---
      // Exactly one thing signals completion: offscreen.js posts a chunkComplete
      // broadcast keyed by this seq when the audio element fires its real
      // `ended` event. I wait for that and nothing else. The send's own response
      // only confirms the message was delivered, and it must never advance the
      // loop, since it resolves the instant offscreen acknowledges receipt
      // (before any audio plays). Conflating "received" with "finished" is what
      // made the dashboard run ahead of the audio.
      const playSeq = ++_playSeqCounter;
      const ended = new Promise(resolve => { _pendingPlay[playSeq] = resolve; });

      // Fire the play message. I don't await audio completion on this call,
      // because offscreen would hold the response open for the whole chunk and
      // Chrome
      // force-closes a held message channel at ~30s, so a held response that
      // loses the race stalled the loop for 30s per chunk. Completion comes
      // solely from the seq-keyed chunkComplete broadcast below. Here we only
      // confirm the message was delivered (so we can recreate a reaped offscreen
      // doc); offscreen acks receipt immediately via paused_hold or by starting
      // playback, and the broadcast carries the real end.
      let delivered = await toPlayerAsync({ action: "playAudioOffscreen", audio: base64, seq: playSeq, estSecs: estSecs });
      if (delivered === null && !isStopped && mySession === sessionId) {
        // null = no live offscreen doc received it; recreate and resend once.
        await ensureOffscreen();
        delivered = await toPlayerAsync({ action: "playAudioOffscreen", audio: base64, seq: playSeq, estSecs: estSecs });
      }
      if (isStopped || mySession !== sessionId) { delete _pendingPlay[playSeq]; break; }

      // offscreen refused because the user paused right at this boundary, so I
      // hold the same chunk until they resume and then retry it, with no
      // advance and no re-synthesis.
      if (delivered && delivered.reason === "paused_hold") {
        delete _pendingPlay[playSeq];
        while (isPaused && !isStopped && mySession === sessionId) {
          await new Promise(r => setTimeout(r, 100));
        }
        prefetchCache[currentChunkIndex] = Promise.resolve(base64);
        continue;
      }

      // The offscreen document has taken the chunk and is playing it, so this
      // is the chunk the popup and dashboard should rate or report now.
      announceChunk(currentChunkIndex);

      // Wait for the REAL end of this chunk's audio via the seq-keyed
      // chunkComplete broadcast. The safety timeout is a backstop only: audio
      // length (estSecs) plus a small slack for decode/dispatch. If the end
      // signal is ever missed the loop still advances close to the true audio
      // length rather than stalling, which keeps playback continuous.
      const safety = (async () => {
        let elapsed = 0;
        // estSecs is generated audio seconds. The server bakes speed into the
        // synthesis itself and playbackRate stays at 1.0, so this really is wall
        // time. I give it 3s of slack.
        const limit = estSecs * 1000 + 3000;
        while (elapsed < limit && !isStopped && mySession === sessionId) {
          await new Promise(r => setTimeout(r, 250));
          if (!isPaused) elapsed += 250;
        }
        return { reason: "timeout" };
      })();

      const playResult = await Promise.race([ended, safety]);
      delete _pendingPlay[playSeq];

      if (isStopped || mySession !== sessionId) break;

      // A late pause that landed mid-chunk: hold and retry the same index.
      if (playResult && playResult.reason === "paused_hold") {
        while (isPaused && !isStopped && mySession === sessionId) {
          await new Promise(r => setTimeout(r, 100));
        }
        prefetchCache[currentChunkIndex] = Promise.resolve(base64);
        continue;
      }
      if (isPaused) { prefetchCache[currentChunkIndex] = Promise.resolve(base64); continue; }

      // This chunk's audio has finished playing, so I record its real server id
      // as fully heard. Only chunks that get this far can become
      // "solid"; prefetched chunks waiting ahead never do.
      const _playedId = chunkIdByIndex[currentChunkIndex];
      if (_playedId) playedChunks.push(_playedId);

      currentChunkIndex++;

    } catch (e) {
      console.error("[TTS] Loop error:", e);
      if (!isStopped && mySession === sessionId) currentChunkIndex++;
    }
  }

  if (_respeed === respeed) _respeed = null;
  clearTimeout(refillTimer);

  // The loop ended on failures: two in a row, or the read ran out with the
  // last fetch still failed (a one-chunk selection with the server down is the
  // common case of that). Either way it did not finish, and saying it had is
  // what made a dead server look like the end of the article.
  if (failStreak > 0 && live()) {
    await haltRead(firstFailed);
    return;
  }

  if (mySession === sessionId) {
    isPlaying = false;
    toReadingTab({ action: "clearOverlay" });
    chrome.runtime.sendMessage({ action: "playbackFinished" }).catch(() => {});
    // A read heard to the end has nothing left to resume. Guarded by the id,
    // so a loop that ran over an emptied list (a jump after a stop) cannot
    // wipe a read that was saved for carrying on.
    if (_savedReadId && _savedReadId === readId) forgetResume();
    // Natural finish: digest every chunk that was fully played. Uses the
    // explicit played list (not a time window) so prefetched chunks waiting
    // ahead are never counted.
    digestPlayedChunks();
  }
}

// Tell the popup and dashboard which chunk is now playing, with its server id,
// so their feedback names the chunk being heard. Nothing is sent when the
// server gave no id, since a report keyed on a guess is worse than none.
function announceChunk(idx) {
  const chunkId = chunkIdByIndex[idx];
  if (!chunkId) return;
  chrome.runtime.sendMessage({ action: 'chunkReady', chunkId, chunkText: allChunks[idx],
                               index: idx }).catch(() => {});
}

// Send the texts of fully-played chunks to the server to be marked SOLID and
// gently reinforced. This runs on a natural finish and on a user stop, and in
// both cases
// only chunks the user actually heard are included (prefetched/unheard chunks
// are never in playedChunks). Clears the list so a chunk is never digested
// twice. No-op if nothing was played.
function digestPlayedChunks() {
  if (!sessionDigestible) { playedChunks = []; return Promise.resolve(); }  // Custom Text: never digest
  if (!playedChunks.length) return Promise.resolve();
  const ids = playedChunks.slice();
  playedChunks = [];
  return kamFetch(`${SERVER}/session/complete`, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-KAM-Token": KAM_TOKEN },
    body: JSON.stringify({ played: ids })
  }).catch(() => {});
}

// The dashboard is about to clear the chunk history. Mark what has been heard
// as solid first, and name the current read's chunks that are made but not yet
// heard so the clear keeps them and they can still be marked once heard.
async function prepareClear() {
  await digestPlayedChunks();
  const keep = [];
  if (isPlaying && !isStopped) {
    for (const [i, id] of Object.entries(chunkIdByIndex)) {
      if (Number(i) >= currentChunkIndex && id) keep.push(id);
    }
  }
  return { keep };
}

// --- Warm start ---
// Create the offscreen document as soon as the service worker starts (browser
// launch, extension reload, or SW wake). The offscreen doc's heartbeat then
// keeps this SW alive, so subsequent playback clicks are never lost to a reaped
// worker ("No SW" / multi-minute delay before the first chunk).
function _warmStart() {
  try { ensureOffscreen().catch(() => {}); } catch (e) {}
}
try { chrome.runtime.onStartup.addListener(_warmStart); } catch (e) {}
try { chrome.runtime.onInstalled.addListener(_warmStart); } catch (e) {}

// --- Content script for tabs that were already open ---
// Chrome only injects the manifest's content scripts into pages loaded after
// the extension is. A tab open before an install, a reload, or the reload an
// update does had no content.js, so a read there played with no header bar and
// no highlight, and nothing said why. So on install and on startup it goes into
// every open web page. content.js guards itself, so a tab that already has a
// live copy is left alone.
function _injectableUrl(url) {
  return /^https?:/.test(url || "") && !/^https:\/\/chromewebstore\.google\.com/.test(url);
}

async function _injectContentIntoOpenTabs() {
  let tabs = [];
  try { tabs = await chrome.tabs.query({}); } catch (e) { return 0; }
  let n = 0;
  for (const t of tabs) {
    // A discarded tab has no page to run in, and it gets the manifest script
    // anyway when it is brought back.
    if (!t || t.id == null || t.discarded || !_injectableUrl(t.url)) continue;
    try {
      await chrome.scripting.executeScript({ target: { tabId: t.id, allFrames: true }, files: ["content.js"] });
      n++;
    } catch (e) { /* a page that refuses scripting, or one closed meanwhile */ }
  }
  return n;
}
try { chrome.runtime.onInstalled.addListener(() => { _injectContentIntoOpenTabs(); }); } catch (e) {}
try { chrome.runtime.onStartup.addListener(() => { _injectContentIntoOpenTabs(); }); } catch (e) {}
// =============================================================================
// THE RIGHT-CLICK MENU
// =============================================================================
// Three entries: read the selection, read the page from where you clicked, and
// read the whole page. Each starts a read with no popup open, so everything
// the popup's Speak button does happens here instead: take the text off the
// page, cut it into chunks, start. Anything that goes wrong is said in the
// page's header bar, since there is nowhere else to say it.
const MENU_SELECTION = "kam-read-selection";
const MENU_FROM_HERE = "kam-read-from-here";
const MENU_PAGE      = "kam-read-page";

function _createMenus() {
  if (!chrome.contextMenus) return;
  try {
    chrome.contextMenus.removeAll(() => {
      const make = (id, title, contexts) => {
        try { chrome.contextMenus.create({ id, title, contexts }, () => { void chrome.runtime.lastError; }); }
        catch (e) { /* already there, or menus unavailable */ }
      };
      make(MENU_SELECTION, "Read selection with KAM TTS", ["selection"]);
      make(MENU_FROM_HERE, "Read from here with KAM TTS", ["page", "link"]);
      make(MENU_PAGE,      "Read this page with KAM TTS", ["page", "link"]);
    });
  } catch (e) { /* menus unavailable; the popup still reads */ }
}

// reader-core.js reports why a page could not be read through showError. In
// the popup that paints a line; here it is kept for the notice in the page.
let _readerProblem = "";
function showError(msg) { _readerProblem = String(msg || ""); }

// The same text with everything but letters, digits and single spaces gone, in
// lower case and with accents folded, so text taken from the page and text
// that has been through the chunker can be compared.
function _plainForMatch(text) {
  return kamSpeakable(String(text || "").replace(KAM_MARKER_RE, " "))
    .toLowerCase().replace(/[^a-z0-9]+/g, " ").trim();
}

// Which chunk "here" is: the first chunk in which the clicked block's opening
// words appear. The chunks are joined, since a block's opening can run across
// a chunk boundary, and shorter openings are tried when the full one is not
// found, since cleaning can change a few words (a citation, a symbol). Not
// found means the top, which is where a read starts anyway.
function anchorIndex(chunks, anchorText) {
  const want = _plainForMatch(anchorText);
  if (!want || !chunks || !chunks.length) return 0;
  const starts = [];
  let joined = "";
  for (const c of chunks) {
    starts.push(joined.length);
    joined += _plainForMatch(c) + " ";
  }
  for (const n of [60, 40, 25, 15]) {
    if (want.length < Math.min(n, 15) && n !== 15) continue;
    const key = want.slice(0, n).trim();
    if (key.length < 8 && key !== want) continue;
    const at = joined.indexOf(key);
    if (at < 0) continue;
    let i = 0;
    while (i + 1 < starts.length && starts[i + 1] <= at) i++;
    return i;
  }
  return 0;
}

function _menuNotice(tabId, text) {
  try { chrome.tabs.sendMessage(tabId, { action: "overlayNotice", text }).catch(() => {}); } catch (e) {}
}

async function _menuAnchorText(tabId, frameId) {
  try {
    const r = await chrome.tabs.sendMessage(tabId, { action: "getMenuAnchor" },
                                            frameId != null ? { frameId } : undefined);
    return (r && r.text) || "";
  } catch (e) { return ""; }
}

// The speed to read at: what the user last set here, else what the server has.
async function _menuSpeed() {
  if (_speedKnown) return currentSpeed;
  try {
    const r = await kamFetch(`${SERVER}/settings`, {});
    const d = await r.json();
    const v = Number(d && d.speed);
    if (Number.isFinite(v) && v > 0) return v;
  } catch (e) { /* server did not say; the default will do */ }
  return currentSpeed || 1.0;
}

async function _menuPrivate() {
  try { return !!(await chrome.storage.local.get("kamPrivateRead")).kamPrivateRead; }
  catch (e) { return false; }
}

async function readFromMenu(info, tab) {
  if (!tab || tab.id == null) return { status: "no_tab" };
  const tabId = tab.id;
  const mode = info.menuItemId === MENU_SELECTION ? "selection"
             : info.menuItemId === MENU_FROM_HERE ? "from-here" : "page";
  if (!_readerLoaded) {
    _menuNotice(tabId, "KAM TTS could not load its reader. Reload the extension at chrome://extensions.");
    return { status: "no_reader" };
  }
  if (!(await _serverUp())) {
    _menuNotice(tabId, "The KAM TTS server is not running. Open the KAM TTS popup and press the power button, then try again.");
    return { status: "server_down" };
  }
  try {
    chrome.tabs.sendMessage(tabId, { action: "overlayPreparing",
      text: mode === "selection" ? "Reading your selection…" : "Reading the page…" }).catch(() => {});
  } catch (e) {}

  // Asked for before the page is read, since reading takes a moment and a
  // second right-click would move it.
  const anchor = mode === "from-here" ? await _menuAnchorText(tabId, info.frameId) : "";

  _readerProblem = "";
  const text = await _withExtractionTimeout(mode === "selection" ? getSelectedText(tab) : getPageText(tab));
  const problem = extractionProblem(text) || (text ? "" : (_readerProblem ||
    (mode === "selection" ? "No text is selected. Highlight some text first."
                          : "KAM TTS could not find text to read on this page.")));
  if (problem) { _menuNotice(tabId, problem); return { status: "no_text" }; }

  if (kamMostlyNonLatin(text.replace(KAM_MARKER_RE, " "))) {
    _menuNotice(tabId, "KAM TTS reads English only for now. This text looks like another language.");
    return { status: "not_english" };
  }

  let chunks = splitIntoChunks(text);
  chunks = await applyBoundaryCorrections(chunks);
  if (!chunks.length) {
    _menuNotice(tabId, "Nothing readable was found in that text.");
    return { status: "no_chunks" };
  }

  const request = {
    chunks, tabId, digestible: true,
    startIndex: mode === "from-here" ? anchorIndex(chunks, anchor) : 0,
    speed: await _menuSpeed(),
    title: tab.title || "", url: tab.url || "",
  };
  if (await _menuPrivate()) request.private = true;
  return startRead(request);
}

try { chrome.runtime.onInstalled.addListener(_createMenus); } catch (e) {}
try { chrome.runtime.onStartup.addListener(_createMenus); } catch (e) {}
try {
  if (chrome.contextMenus) {
    chrome.contextMenus.onClicked.addListener((info, tab) => {
      readFromMenu(info, tab).catch(e => {
        console.error("[TTS-BG] right-click read failed:", e);
        if (tab && tab.id != null) _menuNotice(tab.id, "KAM TTS could not start that read. Try the popup instead.");
      });
    });
  }
} catch (e) { /* no menus; the popup still reads */ }

// Also run once now, for the case where the SW just woke on its own.
_warmStart();
_resumeAfterUpdate();
