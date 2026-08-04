// =============================================================================
// The offscreen audio player, which is the only audio sink.
// =============================================================================
// This runs in a Chrome Offscreen Document, so there's no autoplay restriction,
// no user gesture needed and no visible tab required. It's the only place audio
// ever plays.
//
// Design notes (why it is shaped this way):
//   * background.js sends every audio message with target:"offscreen".
//     Anything without that field is ignored here so cross-context
//     broadcasts can never disturb playback.
//   * Chunk completion is reported back BOTH as the async response to the
//     playAudioOffscreen message and as a seq-keyed "chunkComplete" broadcast.
//     The broadcast is the reliable path, since chrome.runtime.sendMessage with
//     a
//     callback is delivered to every context (offscreen + player.html tab),
//     and the player tab returning synchronously can hijack/close the held-open
//     response channel before this document replies, which strands the loop. A
//     seq-keyed broadcast can't hit that race, so I fire both.
//   * A monotonic token guards every async callback so a stale ended/error
//     event from a superseded chunk cannot fire the wrong completion.
// =============================================================================

let currentAudio = null;
let currentSpeed = 1.0;
let currentToken = 0;
let pausedAt     = 0;

// --- Playback gain / volume boost ---
// Gain factor applied to every chunk. 0..1 attenuates cleanly; 1..6 boosts
// loudness. Boost above unity is made safe by a soft-clip curve + brick-wall
// limiter (see the Web Audio chain below), so louder never means distorted.
let _playbackGain = 1.0;   // 1.0 = 100% (unaltered)
try {
  chrome.storage.session.get('playbackGain').then(d => {
    if (d && typeof d.playbackGain === 'number') {
      _playbackGain = Math.max(0, Math.min(6, d.playbackGain));
      _applyGain(_playbackGain);
    }
  }).catch(() => {});
} catch (e) { /* session storage unavailable */ }

// One persistent AudioContext + boost graph, built lazily on first playback.
// Each chunk's <audio> element feeds this shared chain via a MediaElementSource.
let _audioCtx = null, _gainNode = null, _makeupNode = null, _boostReady = false;
// Per-element routing nodes so we can switch clean<->boosted live.
let _curSource = null, _curLimiter = null, _curSubsonic = null;

// Build the shared context + gain/makeup nodes once (per-element source/limiter
// are created in _connectToBoost).
function _ensureBoostGraph() {
  if (_boostReady) return true;
  try {
    _audioCtx = new (window.AudioContext || window.webkitAudioContext)();
    _gainNode = _audioCtx.createGain();
    _makeupNode = _audioCtx.createGain();
    _makeupNode.gain.value = 1.0;
    _boostReady = true;
    return true;
  } catch (e) {
    console.warn("[OFFSCREEN] boost graph unavailable, using element volume:", e && e.name);
    _boostReady = false;
    return false;
  }
}

// Route one audio element into the context. Builds the boost nodes, then wires
// the clean or boosted path for the current gain.
function _connectToBoost(audioEl) {
  if (!_ensureBoostGraph()) return false;
  try {
    if (_audioCtx.state === "suspended") _audioCtx.resume().catch(() => {});
    _curSource   = _audioCtx.createMediaElementSource(audioEl);
    _curSubsonic = _audioCtx.createBiquadFilter();
    _curSubsonic.type = "highpass"; _curSubsonic.frequency.value = 25; _curSubsonic.Q.value = 0.707;
    // Transparent brick-wall limiter: soft knee + slow-ish release so it only
    // tames genuine peaks, leaving the signal body undistorted (no waveshaper).
    _curLimiter  = _audioCtx.createDynamicsCompressor();
    _curLimiter.threshold.value = -1; _curLimiter.knee.value = 6; _curLimiter.ratio.value = 12;
    _curLimiter.attack.value = 0.004; _curLimiter.release.value = 0.25;
    _routeGraph(_playbackGain);
    _lastRoutedBoosted = _playbackGain > 1.0;
    return true;
  } catch (e) {
    console.warn("[OFFSCREEN] boost connect failed, using element volume:", e && e.name);
    return false;
  }
}

// (Re)wire the graph. gain<=1: CLEAN (source→gain→out), no processing at all.
// With gain above 1 it goes source, highpass, gain, limiter, makeup, out. That
// gives a loudness boost with transparent peak-limiting only, and no soft-clip
// distorting the waveform.
function _routeGraph(g) {
  if (!_boostReady || !_curSource) return;
  try {
    [_curSource, _curSubsonic, _gainNode, _curLimiter, _makeupNode].forEach(n => { try { n.disconnect(); } catch (_) {} });
    if (g > 1.0) {
      _curSource.connect(_curSubsonic); _curSubsonic.connect(_gainNode);
      _gainNode.connect(_curLimiter); _curLimiter.connect(_makeupNode);
      _makeupNode.connect(_audioCtx.destination);
      _makeupNode.gain.value = 1.0;   // limiter already sets the ceiling
    } else {
      _curSource.connect(_gainNode); _gainNode.connect(_audioCtx.destination);
    }
  } catch (e) { console.warn("[OFFSCREEN] route failed:", e && e.name); }
}

// Apply gain. If the current element is routed through the boost graph (only
// when boosting >1.0), drive the gain node; otherwise set element volume
// directly. Crossing above 100% takes effect on the NEXT chunk (which gets
// captured), so the current chunk is never silenced by a suspended context.
function _applyGain(g) {
  if (_curElementBoosted && _boostReady && _gainNode && _audioCtx) {
    const t = _audioCtx.currentTime;
    _gainNode.gain.cancelScheduledValues(t);
    _gainNode.gain.setTargetAtTime(g, t, 0.03);
    if (currentAudio) currentAudio.volume = 1.0;
  } else if (currentAudio) {
    currentAudio.volume = Math.max(0, Math.min(1, g));
  }
}
let _lastRoutedBoosted = null;
let _curElementBoosted = false;  // is the CURRENT element captured into the graph?
// The absolute pause gate. When it's true, no audio can start or continue in
// this document, so a play message arriving while paused gets rejected and the
// background loop holds that chunk until the user resumes. This
// makes pause truly absolute regardless of chunk-boundary timing races.
let isPausedHard = false;

// --- Keep-alive silence ---
// Chrome only keeps an AUDIO_PLAYBACK offscreen document alive while a media
// element, so an <audio> or <video>, is actively playing. A Web Audio oscillator
// doesn't count, which meant the old oscillator-based keep-alive let Chrome reap
// the document between chunks, and then the next play message hit a dead context
// and nothing was heard. The fix is to loop a muted, near-silent <audio> element
// for the
// document's whole life so a media element is always playing.
let _keepAlive = null;
// 1 second of 24kHz/16-bit mono PCM silence as a WAV data URI.
const _SILENCE_WAV = (() => {
  const sr = 24000, n = sr;                 // 1s of samples
  const bytes = new Uint8Array(44 + n * 2); // header + zeroed PCM
  const dv = new DataView(bytes.buffer);
  const wr = (o, s) => { for (let i = 0; i < s.length; i++) dv.setUint8(o + i, s.charCodeAt(i)); };
  wr(0, "RIFF"); dv.setUint32(4, 36 + n * 2, true); wr(8, "WAVE");
  wr(12, "fmt "); dv.setUint32(16, 16, true); dv.setUint16(20, 1, true);
  dv.setUint16(22, 1, true); dv.setUint32(24, sr, true);
  dv.setUint32(28, sr * 2, true); dv.setUint16(32, 2, true); dv.setUint16(34, 16, true);
  wr(36, "data"); dv.setUint32(40, n * 2, true);   // PCM body already zeroed
  let bin = "";
  for (let i = 0; i < bytes.length; i++) bin += String.fromCharCode(bytes[i]);
  return "data:audio/wav;base64," + btoa(bin);
})();

function startKeepAlive() {
  if (_keepAlive) return;
  try {
    const a = new Audio(_SILENCE_WAV);
    a.loop = true;
    // Chrome only counts a media element toward the offscreen AUDIO_PLAYBACK
    // keep-alive if it is (a) attached to the DOM and (b) not zero-volume. A
    // detached `new Audio()` is invisible to the lifetime check (querySelectorAll
    // returned 0), so the doc was reaped between chunks and play() on the next
    // chunk hit a dying context and there was no audio. So I append it and give
    // it a tiny non-zero volume on digital silence, which is inaudible but does
    // genuinely count as playing.
    a.volume = 0.01;
    document.body.appendChild(a);
    a.play().catch((e) => console.warn("[OFFSCREEN] keep-alive play rejected:", e && e.name));
    _keepAlive = a;
  } catch (e) {
    console.warn("[OFFSCREEN] keep-alive failed:", e && e.message);
  }
}
startKeepAlive();

// --- Service-worker keep-alive (persistent port) ---
// MV3 reaps an idle service worker at ~30s. The FIRST chunk's fetch can take far
// longer (cold model load ~16s + synthesis 20-40s), so the SW was dying
// mid-fetch. The loop logged "fetch resolved" and then the worker was terminated
// before it could send the audio to offscreen, so nothing ever played.
//
// Mechanism: hold a port open and ping it every 5s. background.js echoes each
// ping back; that round-trip within the idle window is what keeps the worker
// awake (a silent port alone does not). This offscreen document is never reaped
// (a media element is always playing via keep-alive), so it is the stable anchor
// for the port. On disconnect we just clear the ref and the interval reconnects
// on its next tick, never synchronously, which would thrash the worker.
let _swPort = null;
function connectKeepAlivePort() {
  if (_swPort) return;   // never stack connections
  try {
    _swPort = chrome.runtime.connect({ name: "kam-keepalive" });
    _swPort.onMessage.addListener(() => { /* ack from SW — channel is alive */ });
    _swPort.onDisconnect.addListener(() => { _swPort = null; });
  } catch (e) {
    _swPort = null;
  }
}
connectKeepAlivePort();
// Reconnect if dropped, otherwise ping. 5s comfortably beats the ~30s idle cap.
setInterval(() => {
  if (!_swPort) { connectKeepAlivePort(); return; }
  try { _swPort.postMessage({ t: Date.now() }); } catch (_) { _swPort = null; }
}, 5000);

// Sequence id of the chunk currently playing. background.js listens for a
// chunkComplete broadcast keyed by it. This is the single, reliable completion
// path, and there's no held-open response, since that hit Chrome's roughly 30s
// channel timeout and stalled the loop.
let respondSeq   = null;

// Broadcast completion for the current chunk's seq. Used by stop/resume to end
// the chunk the loop is waiting on. The per-play `ended` handler broadcasts
// directly (it captures its own seq), so this covers only the control paths.
function settleDone(reason) {
  const seq = respondSeq;
  respondSeq = null;
  if (seq !== null && seq !== undefined) {
    try { chrome.runtime.sendMessage({ action: "chunkComplete", seq, reason }).catch(() => {}); } catch (_) {}
  }
}

chrome.runtime.onMessage.addListener((request, sender, sendResponse) => {
  if (!request || request.target !== "offscreen") return false;

  switch (request.action) {

    // -- Set playback gain / boost (0..6, where 1.0 = 100%) -------------------
    case "setVolume": {
      const g = Math.max(0, Math.min(6, Number(request.volume)));
      if (!Number.isNaN(g)) {
        _playbackGain = g;
        _applyGain(g);
        try { chrome.storage.session.set({ playbackGain: g }).catch(() => {}); } catch (e) {}
      }
      sendResponse({ status: "ok", volume: _playbackGain });
      return false;
    }

    // -- Play a new chunk -----------------------------------------------------
    case "playAudioOffscreen": {
      // Absolute pause. If the user paused then I don't start this chunk, and I
      // tell the loop through the seq broadcast so it holds and retries on
      // resume.
      if (isPausedHard) {
        if (request.seq !== undefined && request.seq !== null) {
          try { chrome.runtime.sendMessage({ action: "chunkComplete", seq: request.seq, reason: "paused_hold" }).catch(() => {}); } catch (_) {}
        }
        sendResponse({ status: "received", reason: "paused_hold" });
        return false;
      }
      currentToken++;
      const myToken = currentToken;
      pausedAt = 0;
      // End the previous chunk's tracking, with no broadcast, since a new chunk
      // only arrives once the loop has seen the previous one complete.
      respondSeq = null;

      if (currentAudio) {
        try { currentAudio.pause(); } catch (_) {}
        currentAudio = null;
      }

      // Completion is reported via the seq-keyed chunkComplete broadcast fired
      // from the audio's `ended` event rather than a held-open response. Holding
      // the response open for a whole chunk hit Chrome's roughly 30s timeout
      // and stalled the loop. So we ack receipt immediately and broadcast the
      // real end later.
      respondSeq = (request.seq !== undefined) ? request.seq : null;

      const audio = decodeAudio(request.audio);
      if (!audio) {
        if (respondSeq !== null) { try { chrome.runtime.sendMessage({ action: "chunkComplete", seq: respondSeq, reason: "decode_failed" }).catch(() => {}); } catch (_) {} }
        sendResponse({ status: "received", reason: "decode_failed" });
        return false;
      }
      audio.playbackRate = request.speed || currentSpeed;
      currentAudio = audio;

      let finished = false;
      const mySeq = respondSeq;
      const finish = (reason) => {
        if (finished || currentToken !== myToken) return;
        finished = true;
        if (currentAudio === audio) currentAudio = null;
        if (mySeq !== null && mySeq !== undefined) {
          try { chrome.runtime.sendMessage({ action: "chunkComplete", seq: mySeq, reason }).catch(() => {}); } catch (_) {}
        }
      };

      audio.addEventListener("ended", () => finish("ended"), { once: true });
      audio.addEventListener("error", () => finish("error"), { once: true });
      // The boost graph reroutes the element's output into the AudioContext, so
      // a suspended context would be silent. Resume it before playing.
      if (_boostReady && _audioCtx && _audioCtx.state === "suspended") {
        _audioCtx.resume().catch(() => {});
      }
      audio.play()
        .catch((e) => {
          // AbortError is normal: a newer chunk paused this element before it
          // finished starting; that chunk's own dispatch advances the loop.
          if (e && e.name === "AbortError") return;
          if (currentToken !== myToken) return;
          console.warn("[OFFSCREEN] play rejected:", e && e.name);
          finish("play_rejected");
        });

      // Ack receipt immediately so the loop's send resolves at once; the real
      // end arrives via the chunkComplete broadcast above.
      sendResponse({ status: "received" });
      return false;
    }

    // -- Pause in place -------------------------------------------------------
    case "pauseAtPosition": {
      isPausedHard = true;   // gate all future play attempts until resume
      if (currentAudio && !currentAudio.paused) {
        pausedAt = currentAudio.currentTime;
        try { currentAudio.pause(); } catch (_) {}
        sendResponse({ status: "paused", position: pausedAt });
      } else {
        sendResponse({ status: "already_paused" });
      }
      return false;
    }

    // -- Resume from saved position -------------------------------------------
    case "resumeFromPosition": {
      isPausedHard = false;   // lift the gate; chunks may play again
      if (currentAudio && currentAudio.paused) {
        currentAudio.playbackRate = currentSpeed;
        currentAudio.play().catch(() => settleDone("resume_failed"));
        sendResponse({ status: "resumed" });
      } else {
        settleDone("nothing_to_resume");
        sendResponse({ status: "nothing_to_resume" });
      }
      return false;
    }

    // -- Hard stop ------------------------------------------------------------
    case "stopOffscreen": {
      currentToken++;
      pausedAt = 0;
      isPausedHard = false;   // stop also clears the pause gate
      if (currentAudio) {
        try { currentAudio.pause(); } catch (_) {}
        currentAudio = null;
      }
      settleDone("stopped");
      sendResponse({ status: "stopped" });
      return false;
    }

    // -- Speed change ---------------------------------------------------------
    case "setSpeed": {
      currentSpeed = request.speed || 1.0;
      if (currentAudio) currentAudio.playbackRate = currentSpeed;
      sendResponse({ status: "ok" });
      return false;
    }

    // -- Keep-alive -----------------------------------------------------------
    case "keepAlive": {
      sendResponse({ status: "ok" });
      return false;
    }

    default:
      return false;
  }
});

function decodeAudio(base64) {
  try {
    const binary = atob(base64);
    const bytes  = new Uint8Array(binary.length);
    const CHUNK  = 8192;
    for (let i = 0; i < binary.length; i += CHUNK) {
      const end = Math.min(i + CHUNK, binary.length);
      for (let j = i; j < end; j++) bytes[j] = binary.charCodeAt(j);
    }
    const url   = URL.createObjectURL(new Blob([bytes], { type: "audio/wav" }));
    const audio = new Audio(url);
    // Only capture the element into the boost graph when actually boosting
    // above 100%. At normal volume the element plays natively, which avoids
    // routing audio into an AudioContext that might be suspended, since
    // offscreen documents have no user
    // gesture), which was silencing playback. Native element volume is used at
    // or below 100%.
    let boosted = false;
    if (_playbackGain > 1.0) {
      boosted = _connectToBoost(audio);
    }
    _curElementBoosted = boosted;
    audio.volume = boosted ? 1.0 : Math.max(0, Math.min(1, _playbackGain));
    // Attach to the DOM. A detached Audio element plays unreliably in an offscreen
    // document; an element in the document body plays consistently and is counted
    // toward the doc's AUDIO_PLAYBACK lifetime.
    document.body.appendChild(audio);
    const cleanup = () => {
      URL.revokeObjectURL(url);
      if (audio.parentNode) audio.parentNode.removeChild(audio);
    };
    audio.addEventListener("ended", cleanup, { once: true });
    audio.addEventListener("error", cleanup, { once: true });
    return audio;
  } catch (e) {
    console.error("[OFFSCREEN] decode failed:", e);
    return null;
  }
}
