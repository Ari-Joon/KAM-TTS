// Shared marker grammar (KAM_MARKER_RE, heading/paragraph hint regexes).
// Guarded: if markers.js is missing from the extension folder, importScripts
// throws and would otherwise kill this whole service worker (no playback, and
// the popup misreports the server as offline). Fall back to inline definitions
// so the extension keeps working, and log loudly so the cause is visible.
try {
  importScripts("markers.js");
} catch (e) {
  console.error("[TTS-BG] markers.js failed to load — using inline fallback. Restore markers.js to the extension folder.", e);
  self.KAM_MARKER_RE        = /\|\/?(?:H1|H2|H3|BOLD|ITALIC|CODE|CALLOUT|CAPTION|BREAK)\|/g;
  self.KAM_HEADING_START_RE = /^\s*\|H[123]\|/;
  self.KAM_PARAGRAPH_END_RE = /\|BREAK\|\s*$/;
  self.kamStripMarkers = t => String(t).replace(self.KAM_MARKER_RE, " ").replace(/\s+/g, " ").trim();
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

// Drop-in fetch for authed server endpoints: awaits the token before sending,
// so a request fired before the token arrives is never rejected with 403.
async function kamFetch(url, opts = {}) {
  const tok = await _ensureToken();
  return fetch(url, Object.assign({}, opts, {
    headers: Object.assign({}, opts.headers || {}, { "X-KAM-Token": tok })
  }));
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
let sessionId         = 0;
let readingTabId      = null;
let _playSeqCounter   = 0;      // unique id per chunk play
let _pendingPlay      = {};     // seq → resolver, for the completion broadcast
let currentFetchAbort = null;
let playerTabId       = null;
let playerReady       = false;

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

async function ensurePlayer() {
  // Brings up BOTH the audio path (offscreen) and the dashboard UI. Kept under
  // the original name so existing call sites need no change.
  await ensureOffscreen();
  await ensureDashboardTab();
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
  isPaused  = false;
  isPlaying = false;
  if (currentFetchAbort) { try { currentFetchAbort.abort(); } catch (_) {} currentFetchAbort = null; }
  // Resolve any awaiters so no loop is stranded on a pending completion.
  for (const k in _pendingPlay) { try { _pendingPlay[k]({ reason: "torndown" }); } catch (_) {} }
  _pendingPlay = {};
  stopPlayerAudio();
}

function stripMarkersForDisplay(text) {
  return kamStripMarkers(text);   // shared grammar — see markers.js
}

// =============================================================================
// MESSAGE HANDLER
// =============================================================================

chrome.runtime.onMessage.addListener((request, sender, sendResponse) => {

  // Completion broadcast from offscreen, keyed by chunk seq. This is the single
  // signal that advances the playback loop.
  if (request.action === "chunkComplete") {
    const r = _pendingPlay[request.seq];
    if (r) { delete _pendingPlay[request.seq]; r({ status: "finished", reason: request.reason }); }
    return false;
  }

  if (request.action === "startSpeaking") {
    const chunks = request.chunks || [];
    if (chunks.length === 0) { sendResponse({ status: "no_chunks" }); return true; }

    // Ignore an exact duplicate of what we are ALREADY playing (double-click or
    // redelivered message) so it doesn't restart the same read.
    const sameAsCurrent = isPlaying && !isStopped &&
      allChunks.length === chunks.length &&
      allChunks[0] === chunks[0] &&
      allChunks[allChunks.length - 1] === chunks[chunks.length - 1];
    if (sameAsCurrent) { sendResponse({ status: "already_playing" }); return true; }

    // Any other start (including a different selection) cleanly REPLACES the
    // current playback. teardownPlayback() bumps sessionId so the old loop dies
    // at its next await before this one begins, so there's never more than one.
    teardownPlayback();

    const newSession  = ++sessionId;   // fresh id after teardown's bump
    isStopped         = false;
    isPaused          = false;
    allChunks         = chunks;
    displayChunks     = chunks.map(stripMarkersForDisplay);
    currentChunkIndex = request.startIndex || 0;
    currentSpeed      = request.speed || 1.0;
    sessionStartTs    = Date.now() / 1000;   // session window for solid marking
    playedChunks      = [];                   // ids of chunks fully heard this session
    chunkIdByIndex    = {};                   // reset per-read index->id map
    sessionDigestible = (request.digestible !== false);  // false for Custom Text
    // Push the user's speed to the server so synthesis pacing matches the
    // slider even if it changed while the server was offline.
    kamFetch(`${SERVER}/settings`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-KAM-Token": KAM_TOKEN },
      body: JSON.stringify({ speed: currentSpeed })
    }).catch(() => {});
    readingTabId      = request.tabId || null;
    speakChunks(newSession);
    sendResponse({ status: "started" });
    return true;
  }

  if (request.action === "stop") {
    teardownPlayback();
    // Digest the chunks heard before the stop. playedChunks holds only fully
    // played chunks, so the 3-4 prefetched chunks waiting ahead are excluded.
    digestPlayedChunks();
    currentChunkIndex = 0; allChunks = []; displayChunks = [];
    toReadingTab({ action: "clearOverlay" });
    sendResponse({ status: "stopped" });
    return true;
  }

  if (request.action === "getStatus") {
    sendResponse({ isPlaying, isPaused, currentChunkIndex, chunks: allChunks, totalChunks: allChunks.length });
    return true;
  }

  if (request.action === "setSpeed") {
    currentSpeed = request.speed;
    // Single source of truth: speed is applied by the SERVER at synthesis
    // (model-native pacing, no pitch shift). Forwarding it to the audio
    // element's playbackRate as well multiplied the two speeds together.
    kamFetch(`${SERVER}/settings`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-KAM-Token": KAM_TOKEN },
      body: JSON.stringify({ speed: request.speed })
    }).catch(() => {});
    sendResponse({ status: "ok" });
    return true;
  }

  if (request.action === "setVolume") {
    toPlayer({ action: "setVolume", volume: request.volume });
    sendResponse({ status: "ok" });
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

  if (request.action === "jumpTo") {
    teardownPlayback();
    const s = ++sessionId;
    isStopped = false;
    currentChunkIndex = request.index;
    speakChunks(s);
    sendResponse({ status: "jumping" });
    return true;
  }

  if (request.action === "keepAlive") {
    sendResponse({ status: "ok" });
    return true;
  }

  // The dashboard data proxy, since player.html routes its server fetches here
  if (request.action === "dashboardFetch") {
    const opts = {
      method: request.method || "GET",
      headers: { "X-KAM-Token": KAM_TOKEN, ...(request.body ? { "Content-Type": "application/json" } : {}) },
      body:    request.body ? JSON.stringify(request.body) : undefined,
    };
    kamFetch(`${SERVER}${request.path}`, opts)
      .then(r => r.json())
      .then(data => sendResponse({ ok: true, data }))
      .catch(e  => sendResponse({ ok: false, error: String(e) }));
    return true;
  }

  return false;
});

// =============================================================================
// FETCH
// =============================================================================

function sanitizeText(text) {
  return text
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
// heading announce pause; a chunk closing a paragraph gets a longer breath.
function detectPositionHint(rawText) {
  if (KAM_HEADING_START_RE.test(rawText)) return "heading";
  if (KAM_PARAGRAPH_END_RE.test(rawText)) return "paragraph_end";
  return null;
}

async function fetchAudioBase64(text, idx) {
  const safe = sanitizeText(text);
  if (!safe || safe.length < 2) return null;
  const position = detectPositionHint(text);
  const controller  = new AbortController();
  currentFetchAbort = controller;
  try {
    const res = await kamFetch(`${SERVER}/speak`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-KAM-Token": KAM_TOKEN },
      body: JSON.stringify({ text: safe, index: (idx != null ? idx + 1 : null), position: position }),
      signal: controller.signal
    });
    if (!res.ok) throw new Error(`Server ${res.status}`);
    // Forward chunk ID to popup for quality feedback, and record it against this
    // chunk's index so the solid-digest can reference the exact server id.
    const chunkId = res.headers.get('X-Chunk-Id');
    if (chunkId) {
      if (idx != null) chunkIdByIndex[idx] = chunkId;
      chrome.runtime.sendMessage({ action: 'chunkReady', chunkId, chunkText: text }).catch(() => {});
    }
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
    if (currentFetchAbort === controller) currentFetchAbort = null;
  }
}

// =============================================================================
// PLAYBACK LOOP
// =============================================================================

async function speakChunks(mySession) {
  await ensurePlayer();
  if (isStopped || mySession !== sessionId) return;

  isPlaying = true;

  // Prefetch cache: key=chunkIndex, value=Promise<base64|null>
  const prefetchCache = {};

  function prefetch(idx) {
    if (idx >= allChunks.length) return;
    if (prefetchCache[idx]) return;
    prefetchCache[idx] = fetchAudioBase64(allChunks[idx], idx).catch((e) => {
      // Surface the failure: a silent null here looks like a mystery playback
      // crash. AbortError is normal teardown; everything else is logged.
      if (!e || e.name !== "AbortError") {
        console.error(`[TTS-BG] chunk ${idx + 1} fetch failed:`, e && e.message);
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

  // Kick off the initial window (only if still the active session)
  if (mySession === sessionId && !isStopped) {
    fillPrefetchWindow(currentChunkIndex);
  }

  while (currentChunkIndex < allChunks.length && !isStopped && mySession === sessionId) {
    try {
      while (isPaused && !isStopped && mySession === sessionId) {
        await new Promise(r => setTimeout(r, 50));
      }
      if (isStopped || mySession !== sessionId) break;

      // Get the audio, either from the cache if it's already fetching or fresh
      prefetch(currentChunkIndex);
      let base64 = await prefetchCache[currentChunkIndex];
      delete prefetchCache[currentChunkIndex];

      // Keep the sliding window full ahead of the current position.
      fillPrefetchWindow(currentChunkIndex + 1);

      if (!base64) { currentChunkIndex++; continue; }
      if (isStopped || mySession !== sessionId) break;
      if (isPaused) { prefetchCache[currentChunkIndex] = Promise.resolve(base64); continue; }

      chrome.runtime.sendMessage({ action: "chunkStatus", current: currentChunkIndex + 1, total: allChunks.length }).catch(() => {});
      toReadingTab({ action: "updateOverlay", text: displayChunks[currentChunkIndex] || allChunks[currentChunkIndex], current: currentChunkIndex + 1, total: allChunks.length });

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

  if (mySession === sessionId) {
    isPlaying = false;
    toReadingTab({ action: "clearOverlay" });
    chrome.runtime.sendMessage({ action: "playbackFinished" }).catch(() => {});
    // Natural finish: digest every chunk that was fully played. Uses the
    // explicit played list (not a time window) so prefetched chunks waiting
    // ahead are never counted.
    digestPlayedChunks();
  }
}

// Send the texts of fully-played chunks to the server to be marked SOLID and
// gently reinforced. This runs on a natural finish and on a user stop, and in
// both cases
// only chunks the user actually heard are included (prefetched/unheard chunks
// are never in playedChunks). Clears the list so a chunk is never digested
// twice. No-op if nothing was played.
function digestPlayedChunks() {
  if (!sessionDigestible) { playedChunks = []; return; }  // Custom Text: never digest
  if (!playedChunks.length) return;
  const ids = playedChunks.slice();
  playedChunks = [];
  kamFetch(`${SERVER}/session/complete`, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-KAM-Token": KAM_TOKEN },
    body: JSON.stringify({ played: ids })
  }).catch(() => {});
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
// Also run once now, for the case where the SW just woke on its own.
_warmStart();
