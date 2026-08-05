// --- Voice recorder screen ---
// Wires the recording overlay to the mic and to the server. The aim is that
// building a voice never leaves the dashboard, so this covers choosing the
// profile, picking or writing the passage, recording, trimming, listening back
// and saving, with the WAV still available to anyone who wants the file itself.
//
// Uploads go straight to the server rather than through the service worker,
// since audio cannot cross chrome.runtime.sendMessage without being turned into
// something much larger first. The page is the extension origin, so CORS is
// already happy; it just needs the token.

const REC_MIN_CLIPS = 6;      // fewer than this and the clone starts to wobble

let _recToken    = null;
let _recPassages = [];        // the standard passages from the server
let _recClips    = [];        // what is already recorded for this voice
let _recSlot     = 1;         // which passage is on screen
let _recTake     = null;      // Float32Array of the take being reviewed
let _recTrim     = { start: 0, end: 0 };
let _recMeterRaf = 0;
let _recTimerId  = 0;
let _recPlayCtx  = null;
let _recPlaySrc  = null;
let _recPlayRaf  = 0;
let _recSavedUrl = null;      // object URL for playing a clip already on disk

const _$ = id => document.getElementById(id);

// --- Talking to the server ---

async function recToken() {
  if (_recToken) return _recToken;
  const r = await fetch("http://127.0.0.1:5050/token");
  if (!r.ok) throw new Error("server not running");
  _recToken = (await r.json()).token;
  return _recToken;
}

async function recFetch(path, opts = {}) {
  const t = await recToken();
  const headers = Object.assign({}, opts.headers || {}, { "X-KAM-Token": t });
  return fetch(`http://127.0.0.1:5050${path}`, Object.assign({}, opts, { headers }));
}

// --- Opening and closing ---

async function recOpen() {
  const overlay = _$("rec-overlay");
  overlay.classList.add("open");
  _$("rec-verdict").textContent = "";
  try {
    await recLoadVoices();
    await recLoadPassages();
    await recLoadClips();
    recShowSlot(_recSlot);
  } catch (e) {
    recSay(`Could not reach the server. Start it with the power button first.`, "bad");
  }
  // Asked for on opening rather than on the first take, so a blocked mic is
  // discovered before someone has read a paragraph aloud for nothing.
  const got = await recInit();
  if (!got.ok) recSay(got.error, "bad");
  else recStartMeter();
}

function recClose() {
  recStopMeter();
  recStopPlayback();
  recRelease();
  if (_recSavedUrl) { URL.revokeObjectURL(_recSavedUrl); _recSavedUrl = null; }
  _$("rec-overlay").classList.remove("open");
}

// --- Loading state ---

async function recLoadVoices() {
  const d   = await (await recFetch("/voices")).json();
  const sel = _$("rec-voice");
  sel.innerHTML = "";
  (d.voices || []).forEach(v => {
    const o = document.createElement("option");
    o.value = v.voice_id;
    o.textContent = `${v.voice_id} (${v.clips} clip${v.clips === 1 ? "" : "s"})`;
    sel.appendChild(o);
  });
  sel.value = d.active || "default";
}

async function recLoadPassages() {
  const d = await (await recFetch("/voices/passages")).json();
  _recPassages = d.passages || [];
}

async function recLoadClips() {
  const d = await (await recFetch(`/voices/clips?voice=${encodeURIComponent(recVoice())}`)).json();
  _recClips = d.clips || [];
  recRenderList();
  recRenderProgress();
}

function recVoice() { return _$("rec-voice").value || "default"; }

function recClipForSlot(slot) { return _recClips.find(c => c.slot === slot) || null; }

// --- The passage list ---

function recRenderList() {
  const list = _$("rec-list");
  list.innerHTML = "";

  const row = (slot, label, clip) => {
    const b = document.createElement("button");
    b.className = "rec-item" + (slot === _recSlot ? " active" : "");
    b.type = "button";
    const dot = document.createElement("span");
    dot.className = "rec-dot" + (clip ? (clip.usable ? " good" : " warn") : "");
    b.appendChild(dot);
    const s = document.createElement("span");
    s.textContent = label;
    s.style.cssText = "overflow:hidden;text-overflow:ellipsis;white-space:nowrap";
    b.appendChild(s);
    // The label is truncated with an ellipsis and the state is carried by a
    // coloured dot, so both are spelled out here for anyone not reading pixels.
    b.title = label;
    b.setAttribute("aria-label",
      `${label}. ${clip ? (clip.usable ? "recorded" : "recorded, needs another take")
                        : "not recorded yet"}`);
    b.onclick = () => recShowSlot(slot);
    list.appendChild(b);
  };

  _recPassages.forEach(p => row(p.n, `${p.n}. ${p.title}`, recClipForSlot(p.n)));

  // Anything recorded above the standard passages was written by the user, so
  // it is listed by its own text rather than a title it never had.
  const extras = _recClips
    .filter(c => c.slot && c.slot > _recPassages.length)
    .sort((a, b) => a.slot - b.slot);
  if (extras.length) {
    const h = document.createElement("div");
    h.textContent = "YOUR OWN";
    h.style.cssText = "font-size:9px;color:var(--dim);letter-spacing:1px;margin:8px 0 4px 7px";
    list.appendChild(h);
    extras.forEach(c => row(c.slot, (c.text || "(no text)").slice(0, 34), c));
  }

  const add = document.createElement("button");
  add.className = "rec-item";
  add.type = "button";
  add.style.color = "var(--indigo)";
  add.textContent = "＋ Write my own passage";
  add.onclick = () => recShowSlot(0);
  list.appendChild(add);
}

function recRenderProgress() {
  const usable = _recClips.filter(c => c.usable).length;
  const total  = _recClips.length;
  const p      = _$("rec-progress");
  if (!total) {
    p.textContent = `No clips yet. ${REC_MIN_CLIPS} good ones make a solid voice.`;
  } else if (usable < REC_MIN_CLIPS) {
    p.textContent = `${usable} of ${REC_MIN_CLIPS} good clips` +
      (total > usable ? ` (${total - usable} need re-recording)` : "");
  } else {
    p.textContent = `${usable} good clips. Ready to build.`;
  }
  _$("rec-use").disabled = usable < 1;
}

// --- Showing one passage ---

function recShowSlot(slot) {
  _recSlot = slot;
  recDiscardTake();

  const box = _$("rec-passage-text");
  if (slot === 0) {
    _$("rec-passage-title").textContent = "Your own passage — type or paste anything to read";
    box.textContent = "";
    box.focus();
  } else {
    const p = _recPassages.find(x => x.n === slot);
    const c = recClipForSlot(slot);
    _$("rec-passage-title").textContent = p
      ? `Passage ${p.n} — ${p.title}`
      : `Your own passage ${slot}`;
    // A saved clip's own text wins, since that is what was actually read.
    box.textContent = (c && c.text) || (p && p.text) || "";
  }
  recRenderList();
  recShowSavedClip(recClipForSlot(slot));
}

// Reports the stored measurements for a clip already on disk, so re-opening a
// passage says the same thing about it as when it was recorded.
function recShowSavedClip(clip) {
  const hasSaved = !!clip;
  _$("rec-play").disabled   = !hasSaved;
  _$("rec-dl").disabled     = !hasSaved;
  _$("rec-delete").disabled = !hasSaved;
  if (!clip) { recSay("", ""); return; }
  const bits = [`${clip.duration}s`];
  if (clip.snr_db != null) bits.push(`${clip.snr_db} dB signal to noise`);
  if (clip.usable) recSay(`Saved. ${bits.join(" · ")}`, "good");
  else recSay(`Saved, but weak: ${(clip.reasons || []).join("; ")}`, "warn");
}

function recSay(msg, kind) {
  const el = _$("rec-verdict");
  el.textContent = msg;
  el.style.color = kind === "good" ? "var(--green)"
                 : kind === "warn" ? "var(--amber)"
                 : kind === "bad"  ? "var(--red)"
                 : "var(--dim)";
}

// --- Level meter ---

function recStartMeter() {
  cancelAnimationFrame(_recMeterRaf);
  const fill = _$("rec-meter-fill");
  const tick = () => {
    const lvl = recLevel();
    fill.style.width = `${Math.min(100, lvl * 130)}%`;
    fill.className = lvl > 0.97 ? "over" : lvl > 0.8 ? "hot" : "";
    _recMeterRaf = requestAnimationFrame(tick);
  };
  tick();
}

function recStopMeter() {
  cancelAnimationFrame(_recMeterRaf);
  _recMeterRaf = 0;
  const fill = _$("rec-meter-fill");
  if (fill) fill.style.width = "0%";
}

// --- Recording ---

async function recToggle() {
  if (recIsRecording()) { await recFinish(); return; }
  if (!recHasMic()) {
    const got = await recInit();
    if (!got.ok) { recSay(got.error, "bad"); return; }
    recStartMeter();
  }
  recDiscardTake();
  if (!recStart()) { recSay("Could not start recording.", "bad"); return; }
  _$("rec-btn").classList.add("recording");
  _$("rec-btn").textContent = "■";
  _$("rec-btn").title = "Stop";
  recSay("", "");
  _recTimerId = setInterval(() => {
    _$("rec-timer").textContent = `Recording… ${recElapsed().toFixed(1)}s`;
  }, 100);
}

async function recFinish() {
  clearInterval(_recTimerId);
  const btn = _$("rec-btn");
  btn.classList.remove("recording");
  btn.textContent = "●";
  btn.title = "Start recording";
  _$("rec-timer").textContent = "Working…";
  try {
    _recTake = await recStop();
  } catch (e) {
    recSay(`Recording failed (${e.message}).`, "bad");
    _$("rec-timer").textContent = "Ready when you are.";
    return;
  }
  // Suggested bounds, not applied ones. The trailing click of the mouse is the
  // reason this exists, and it is always inside the last fraction of a second.
  _recTrim = recAutoTrim(_recTake);
  recEnableTrim(true);
  recDrawWave();
  _$("rec-timer").textContent = `Take: ${(_recTake.length / recSampleRate()).toFixed(1)}s`;
  recSay("Trim the ends if you need to, then save.", "");
}

function recDiscardTake() {
  _recTake = null;
  _recTrim = { start: 0, end: 0 };
  recStopPlayback();
  recEnableTrim(false);
  recDrawWave();
  _$("rec-timer").textContent = "Ready when you are.";
}

function recEnableTrim(on) {
  _$("rec-trim").classList.toggle("ready", on);
  ["rec-auto", "rec-reset", "rec-save"].forEach(id => { _$(id).disabled = !on; });
  _$("rec-trim-info").textContent = on
    ? "Drag the handles to cut the breath at the start and the click at the end."
    : "Record a take and you can trim it here before saving.";
}

// --- Waveform ---

function recDrawWave() {
  const cv  = _$("rec-wave");
  const box = _$("rec-trim");
  const w   = box.clientWidth || 600;
  const h   = 76;
  const dpr = window.devicePixelRatio || 1;
  cv.width  = w * dpr;
  cv.height = h * dpr;
  const g = cv.getContext("2d");
  g.setTransform(dpr, 0, 0, dpr, 0, 0);
  g.clearRect(0, 0, w, h);

  const css = getComputedStyle(document.documentElement);
  const dim = css.getPropertyValue("--dim").trim() || "#666";
  const txt = css.getPropertyValue("--text").trim() || "#ddd";

  if (!_recTake) {
    g.fillStyle = dim;
    g.fillRect(0, h / 2, w, 1);
    return;
  }

  const peaks = recPeaks(_recTake, w);
  const a = Math.round((_recTrim.start / _recTake.length) * w);
  const b = Math.round((_recTrim.end   / _recTake.length) * w);

  for (let x = 0; x < w; x++) {
    const bar = Math.max(1, peaks[x] * (h * 0.92));
    // Outside the trim is drawn faintly, so what is being thrown away stays
    // visible rather than disappearing.
    g.fillStyle = (x >= a && x <= b) ? txt : dim;
    g.globalAlpha = (x >= a && x <= b) ? 0.95 : 0.28;
    g.fillRect(x, (h - bar) / 2, 1, bar);
  }
  g.globalAlpha = 1;

  _$("rec-handle-a").style.left = `${a}px`;
  _$("rec-handle-b").style.left = `${b}px`;
  const secs = (_recTrim.end - _recTrim.start) / recSampleRate();
  _$("rec-trim-info").textContent =
    `Keeping ${secs.toFixed(1)}s of ${(_recTake.length / recSampleRate()).toFixed(1)}s`;
}

// --- Dragging the handles ---

function recBindHandles() {
  const box = _$("rec-trim");
  let dragging = null;

  const posToSample = clientX => {
    const r = box.getBoundingClientRect();
    const f = Math.max(0, Math.min(1, (clientX - r.left) / r.width));
    return Math.round(f * _recTake.length);
  };

  ["rec-handle-a", "rec-handle-b"].forEach(id => {
    _$(id).addEventListener("pointerdown", e => {
      if (!_recTake) return;
      dragging = id;
      _$(id).setPointerCapture(e.pointerId);
      e.preventDefault();
    });
  });

  window.addEventListener("pointermove", e => {
    if (!dragging || !_recTake) return;
    const s   = posToSample(e.clientX);
    // Keep a floor of a tenth of a second so the handles cannot cross over and
    // leave an empty or reversed selection.
    const gap = Math.round(recSampleRate() * 0.1);
    if (dragging === "rec-handle-a") _recTrim.start = Math.min(s, _recTrim.end - gap);
    else                             _recTrim.end   = Math.max(s, _recTrim.start + gap);
    _recTrim.start = Math.max(0, _recTrim.start);
    _recTrim.end   = Math.min(_recTake.length, _recTrim.end);
    recDrawWave();
  });

  window.addEventListener("pointerup", () => { dragging = null; });
}

// --- Playback ---

// Plays the trimmed range of the current take, which is the only way to be sure
// the cut landed where it sounded like it did.
function recPlayTake() {
  if (!_recTake) return;
  recStopPlayback();
  const slice = recSlice(_recTake, _recTrim.start, _recTrim.end);
  if (!slice.length) return;
  _recPlayCtx = new AudioContext();
  const buf = _recPlayCtx.createBuffer(1, slice.length, recSampleRate());
  buf.copyToChannel(slice, 0);
  _recPlaySrc = _recPlayCtx.createBufferSource();
  _recPlaySrc.buffer = buf;
  _recPlaySrc.connect(_recPlayCtx.destination);
  _recPlaySrc.start();

  const head = _$("rec-playhead");
  head.style.display = "block";
  const box   = _$("rec-trim");
  const t0    = _recPlayCtx.currentTime;
  const total = slice.length / recSampleRate();
  const tick  = () => {
    if (!_recPlayCtx) return;
    const done = (_recPlayCtx.currentTime - t0) / total;
    if (done >= 1) { recStopPlayback(); return; }
    const w = box.clientWidth;
    const a = (_recTrim.start / _recTake.length) * w;
    const b = (_recTrim.end   / _recTake.length) * w;
    head.style.left = `${a + (b - a) * done}px`;
    _recPlayRaf = requestAnimationFrame(tick);
  };
  tick();
}

function recStopPlayback() {
  cancelAnimationFrame(_recPlayRaf);
  try { if (_recPlaySrc) _recPlaySrc.stop(); } catch (_) {}
  try { if (_recPlayCtx) _recPlayCtx.close(); } catch (_) {}
  _recPlaySrc = null; _recPlayCtx = null;
  const head = _$("rec-playhead");
  if (head) head.style.display = "none";
}

// Playing a clip that is already saved needs the token, and an audio element
// cannot send headers, so it is fetched and handed over as a blob instead.
async function recPlaySaved() {
  const clip = recClipForSlot(_recSlot);
  if (!clip) return;
  const r = await recFetch(
    `/voices/clip?voice=${encodeURIComponent(recVoice())}&name=${encodeURIComponent(clip.name)}`);
  if (!r.ok) { recSay("Could not load that clip.", "bad"); return; }
  if (_recSavedUrl) URL.revokeObjectURL(_recSavedUrl);
  _recSavedUrl = URL.createObjectURL(await r.blob());
  new Audio(_recSavedUrl).play();
}

// --- Saving ---

async function recSaveTake() {
  if (!_recTake) return;
  const slice = recSlice(_recTake, _recTrim.start, _recTrim.end);
  const secs  = slice.length / recSampleRate();
  if (secs < 2) { recSay("That is too short to clone from. Aim for 8 to 15 seconds.", "bad"); return; }

  const text = _$("rec-passage-text").innerText.trim();
  const fd   = new FormData();
  fd.append("voice", recVoice());
  fd.append("slot",  String(_recSlot));
  fd.append("text",  text);
  fd.append("audio", recWav(slice), "take.wav");

  _$("rec-save").disabled = true;
  recSay("Saving…", "");
  try {
    const r = await recFetch("/voices/record", { method: "POST", body: fd });
    const d = await r.json();
    if (!d.ok) { recSay(d.error || "Save failed.", "bad"); _$("rec-save").disabled = false; return; }
    _recSlot = d.slot;               // a written passage is told which slot it took
    recDiscardTake();
    await recLoadClips();
    await recLoadVoices();
    recShowSlot(_recSlot);
  } catch (e) {
    recSay(`Save failed (${e.message}).`, "bad");
    _$("rec-save").disabled = false;
  }
}

// The WAV files are still the real artefact, so anyone who wants one can have
// it without going near the folder it lives in.
async function recDownload() {
  const clip = recClipForSlot(_recSlot);
  if (!clip) return;
  const r = await recFetch(
    `/voices/clip?voice=${encodeURIComponent(recVoice())}&name=${encodeURIComponent(clip.name)}`);
  if (!r.ok) return;
  const url = URL.createObjectURL(await r.blob());
  const a   = document.createElement("a");
  a.href = url;
  a.download = `${recVoice()}_${clip.name}`;
  a.click();
  setTimeout(() => URL.revokeObjectURL(url), 4000);
}

async function recDeleteClip() {
  const clip = recClipForSlot(_recSlot);
  if (!clip) return;
  await recFetch("/voices/clip/delete", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ voice_id: recVoice(), name: clip.name }),
  });
  await recLoadClips();
  await recLoadVoices();
  recShowSlot(_recSlot);
}

async function recUseVoice() {
  recSay("Building the voice… this takes a few seconds.", "");
  _$("rec-use").disabled = true;
  try {
    const r = await recFetch("/voices/select", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ voice_id: recVoice() }),
    });
    const d = await r.json();
    recSay(d.ok ? `Now using '${d.voice_id}' (${d.applied}).` : (d.error || "Could not switch."),
           d.ok ? "good" : "bad");
  } catch (e) {
    recSay(`Could not switch (${e.message}).`, "bad");
  }
  _$("rec-use").disabled = false;
}

async function recNewVoice() {
  const name = prompt("Name for the new voice profile (letters and numbers):");
  if (!name) return;
  const r = await recFetch("/voices/create", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name }),
  });
  const d = await r.json();
  if (!d.ok) { recSay(d.error || "Could not create that voice.", "bad"); return; }
  await recLoadVoices();
  _$("rec-voice").value = d.voice_id;
  await recLoadClips();
  recShowSlot(1);
}

// --- Wiring ---

function recBindUi() {
  _$("rec-open").onclick     = recOpen;
  _$("rec-close").onclick    = recClose;
  _$("rec-btn").onclick      = recToggle;
  _$("rec-auto").onclick     = () => { if (_recTake) { _recTrim = recAutoTrim(_recTake); recDrawWave(); } };
  _$("rec-reset").onclick    = () => { if (_recTake) { _recTrim = { start: 0, end: _recTake.length }; recDrawWave(); } };
  _$("rec-save").onclick     = recSaveTake;
  _$("rec-play").onclick     = () => (_recTake ? recPlayTake() : recPlaySaved());
  _$("rec-dl").onclick       = recDownload;
  _$("rec-delete").onclick   = recDeleteClip;
  _$("rec-use").onclick      = recUseVoice;
  _$("rec-newvoice").onclick = recNewVoice;
  _$("rec-voice").onchange   = async () => { await recLoadClips(); recShowSlot(_recSlot); };

  // A take under review is not saved yet, so closing on Escape would throw it
  // away silently. Space toggling the recorder would fight the passage editor.
  _$("rec-overlay").addEventListener("click", e => {
    if (e.target === _$("rec-overlay") && !_recTake) recClose();
  });
  document.addEventListener("keydown", e => {
    if (e.key === "Escape" && _$("rec-overlay").classList.contains("open") && !_recTake) recClose();
  });

  recBindHandles();
  window.addEventListener("resize", () => { if (_$("rec-overlay").classList.contains("open")) recDrawWave(); });
}

if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", recBindUi);
else recBindUi();
