// --- Microphone capture for voice profiles ---
// Recording the reference clips used to mean leaving the dashboard, finding
// recording software, working out mono versus stereo and which sample rate, and
// saving the files into the right folder by hand. That is a lot to ask before
// anything works, and it is the one step where the project assumed you knew
// what a WAV file is. So the browser does it instead.
//
// The mic gives me whatever the device wants to give me, usually 48 kHz stereo
// compressed to Opus. XTTS wants plain PCM, so I decode what was captured,
// resample it to 24 kHz mono, and write a real WAV here in the page. The server
// then only has to save bytes, which keeps it out of audio decoding entirely.

const REC_SR   = 24000;   // XTTS resamples anyway, so this only has to be enough
const REC_BITS = 16;

let _stream   = null;     // live mic stream, kept open between takes
let _recorder = null;
let _parts    = [];
let _meterCtx = null;
let _analyser = null;
let _startTs  = 0;

// --- Permission and setup ---

// I hold the stream open across takes rather than asking per passage, since
// re-prompting after every clip would be sixteen interruptions.
async function recInit() {
  if (_stream) return { ok: true };
  if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
    return { ok: false, error: "This browser will not give the page a microphone." };
  }
  try {
    _stream = await navigator.mediaDevices.getUserMedia({
      audio: {
        channelCount: 1,
        // Cloning wants the room as it really sounds. Chrome's cleanup is tuned
        // for calls, and noise suppression in particular eats the quiet detail
        // that makes a voice sound like that person.
        echoCancellation:  false,
        noiseSuppression:  false,
        autoGainControl:   false,
      },
    });
  } catch (e) {
    const name = (e && e.name) || "";
    if (name === "NotAllowedError")
      return { ok: false, error: "Microphone blocked. Allow it for this page and try again." };
    if (name === "NotFoundError")
      return { ok: false, error: "No microphone found. Plug one in and try again." };
    return { ok: false, error: `Could not open the microphone (${name || e}).` };
  }
  // A separate context purely for the level meter, so the meter cannot affect
  // what gets recorded.
  _meterCtx = new AudioContext();
  const src  = _meterCtx.createMediaStreamSource(_stream);
  _analyser  = _meterCtx.createAnalyser();
  _analyser.fftSize = 1024;
  src.connect(_analyser);
  return { ok: true };
}

function recRelease() {
  try { if (_stream) _stream.getTracks().forEach(t => t.stop()); } catch (_) {}
  try { if (_meterCtx) _meterCtx.close(); } catch (_) {}
  _stream = null; _meterCtx = null; _analyser = null; _recorder = null; _parts = [];
}

function recHasMic() { return !!_stream; }

// --- Level meter ---

// Peak rather than RMS, because the meter is there to answer "is my voice
// getting in, and am I too loud", and peak is what clipping cares about.
function recLevel() {
  if (!_analyser) return 0;
  const buf = new Float32Array(_analyser.fftSize);
  _analyser.getFloatTimeDomainData(buf);
  let peak = 0;
  for (let i = 0; i < buf.length; i++) {
    const v = Math.abs(buf[i]);
    if (v > peak) peak = v;
  }
  return peak;
}

// --- Recording ---

function recStart() {
  if (!_stream) return false;
  _parts = [];
  _recorder = new MediaRecorder(_stream);
  _recorder.ondataavailable = e => { if (e.data && e.data.size) _parts.push(e.data); };
  _recorder.start();
  _startTs = Date.now();
  return true;
}

function recElapsed() { return _startTs ? (Date.now() - _startTs) / 1000 : 0; }

function recIsRecording() { return !!_recorder && _recorder.state === "recording"; }

// Resolves with the raw samples rather than a finished WAV, because the take
// still has to be trimmed. Encoding happens at save time on whatever range the
// user settled on.
function recStop() {
  return new Promise((resolve, reject) => {
    if (!_recorder || _recorder.state === "inactive") { reject(new Error("not recording")); return; }
    _recorder.onstop = async () => {
      try {
        const captured = new Blob(_parts, { type: _recorder.mimeType || "audio/webm" });
        _parts = [];
        resolve(await _toSamples(captured));
      } catch (e) { reject(e); }
    };
    _recorder.stop();
    _startTs = 0;
  });
}

// --- Trimming ---

// Nearly every take starts with a breath and ends with the click of the mouse
// going back to the stop button, and both are in the clip that gets cloned. So
// I suggest bounds by walking in from each end until the audio rises above a
// floor taken from the take itself, rather than a fixed threshold that would be
// wrong in a different room.
//
// This only ever suggests. The handles are the user's, since a hard cut on a
// quiet first word is worse than a little extra silence.
function recAutoTrim(samples, sr = REC_SR) {
  const win  = Math.max(1, Math.floor(sr * 0.02));       // 20 ms, about one phoneme
  const n    = Math.floor(samples.length / win);
  if (n < 3) return { start: 0, end: samples.length };

  const energy = new Float32Array(n);
  for (let i = 0; i < n; i++) {
    let peak = 0;
    for (let j = i * win; j < (i + 1) * win; j++) {
      const v = Math.abs(samples[j]);
      if (v > peak) peak = v;
    }
    energy[i] = peak;
  }
  // Taking the reference from a high percentile rather than the maximum, since
  // the click of the mouse hitting stop is louder than anything I actually said.
  // Measuring against the loudest moment therefore measures against the click,
  // which drags the floor up and leaves the click sitting inside the clip: the
  // exact thing this is here to remove.
  const sorted = Float32Array.from(energy).sort();
  const ref    = sorted[Math.floor(n * 0.9)] || sorted[n - 1];
  if (ref <= 0) return { start: 0, end: samples.length };
  const floor = ref * 0.08;

  // A click is loud but lasts a few milliseconds, while speech keeps going, so
  // a boundary only counts where the level stays up across a run of windows.
  // That difference is what separates the two, not loudness.
  const run  = Math.max(2, Math.round(0.10 * sr / win));   // about 100 ms
  const need = Math.ceil(run * 0.6);                       // allow brief stops
  const holds = (from, step) => {
    let hits = 0;
    for (let k = 0; k < run; k++) {
      const i = from + k * step;
      if (i < 0 || i >= n) break;
      if (energy[i] >= floor) hits++;
    }
    return hits >= need;
  };

  let a = 0,     b = n - 1;
  while (a < n && !holds(a,  1)) a++;
  if (a >= n) return { start: 0, end: samples.length };    // nothing sustained
  while (b > a && !holds(b, -1)) b--;

  // Leave a little air either side so the first consonant and the final
  // syllable are not clipped, which sounds far worse than a short silence.
  const pad = Math.round(sr * 0.08);
  return {
    start: Math.max(0, a * win - pad),
    end:   Math.min(samples.length, (b + 1) * win + pad),
  };
}

function recSlice(samples, start, end) {
  return samples.slice(Math.max(0, start | 0), Math.min(samples.length, end | 0));
}

// Peak per pixel column, which is what makes a waveform readable: averaging
// hides the transients that show where words actually start.
function recPeaks(samples, columns) {
  const out  = new Float32Array(columns);
  const step = samples.length / columns;
  for (let c = 0; c < columns; c++) {
    const a = Math.floor(c * step), b = Math.min(samples.length, Math.floor((c + 1) * step));
    let peak = 0;
    for (let i = a; i < b; i++) {
      const v = Math.abs(samples[i]);
      if (v > peak) peak = v;
    }
    out[c] = peak;
  }
  return out;
}

function recWav(samples, sr = REC_SR) { return _encodeWav(samples, sr); }

function recSampleRate() { return REC_SR; }

// --- Converting what was captured into plain samples ---

async function _toSamples(blob) {
  const bytes = await blob.arrayBuffer();
  // decodeAudioData understands whatever MediaRecorder produced, which saves me
  // writing an Opus decoder or making the server depend on ffmpeg.
  const tmp     = new AudioContext();
  const decoded = await tmp.decodeAudioData(bytes);
  tmp.close();

  // Rendering through an offline context at the target rate does the resample
  // and the stereo downmix in one go, since Web Audio mixes to the destination
  // channel count for me.
  const frames  = Math.max(1, Math.ceil(decoded.duration * REC_SR));
  const offline = new OfflineAudioContext(1, frames, REC_SR);
  const src     = offline.createBufferSource();
  src.buffer    = decoded;
  src.connect(offline.destination);
  src.start();
  const rendered = await offline.startRendering();
  // A copy, since the rendered buffer belongs to a context that is about to go.
  return new Float32Array(rendered.getChannelData(0));
}

function _encodeWav(samples, sampleRate) {
  const n    = samples.length;
  const buf  = new ArrayBuffer(44 + n * 2);
  const view = new DataView(buf);
  const put  = (off, s) => { for (let i = 0; i < s.length; i++) view.setUint8(off + i, s.charCodeAt(i)); };

  put(0, "RIFF");
  view.setUint32(4, 36 + n * 2, true);          // size of everything after this field
  put(8, "WAVE");
  put(12, "fmt ");
  view.setUint32(16, 16, true);                 // fmt chunk length
  view.setUint16(20, 1, true);                  // 1 = uncompressed PCM
  view.setUint16(22, 1, true);                  // mono
  view.setUint32(24, sampleRate, true);
  view.setUint32(28, sampleRate * 2, true);     // bytes per second
  view.setUint16(32, 2, true);                  // bytes per frame
  view.setUint16(34, REC_BITS, true);
  put(36, "data");
  view.setUint32(40, n * 2, true);

  // Negative and positive have different ranges in two's complement, so I scale
  // each side by its own limit rather than clipping one of them early.
  let off = 44;
  for (let i = 0; i < n; i++) {
    const s = Math.max(-1, Math.min(1, samples[i]));
    view.setInt16(off, s < 0 ? s * 0x8000 : s * 0x7fff, true);
    off += 2;
  }
  return new Blob([buf], { type: "audio/wav" });
}
