// Tests the pure parts of the voice recorder: WAV encoding, the auto-trim
// search, and the waveform peaks. The mic and Web Audio parts need a browser,
// but these are the ones that can be quietly wrong, since a bad header is
// rejected by the server and a bad trim silently cuts a word off.
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const SRC  = path.join(HERE, "..", "..", "extension", "recorder.js");

// The module is written for a page, so it is evaluated here with only the
// functions this test needs pulled back out.
const src = fs.readFileSync(SRC, "utf8");
const { recAutoTrim, recSlice, recPeaks, recWav, recSampleRate } =
  new Function(`${src}
    return { recAutoTrim, recSlice, recPeaks, recWav, recSampleRate };`)();

const SR = recSampleRate();
let pass = 0, fail = 0;

function check(label, got, want) {
  if (got === want) { pass++; console.log(`  ok   ${label}`); }
  else { fail++; console.log(`  FAIL ${label}\n         got  ${got}\n         want ${want}`); }
}
function near(label, got, want, tol) {
  if (Math.abs(got - want) <= tol) { pass++; console.log(`  ok   ${label} (${got})`); }
  else { fail++; console.log(`  FAIL ${label}\n         got  ${got}\n         want ${want} +/- ${tol}`); }
}

// --- Building test signals ---

function silence(secs) { return new Float32Array(Math.round(SR * secs)); }

function tone(secs, amp = 0.5) {
  const n = Math.round(SR * secs), a = new Float32Array(n);
  for (let i = 0; i < n; i++) a[i] = amp * Math.sin(2 * Math.PI * 140 * i / SR);
  return a;
}

// Real rooms are never digitally silent, so the "quiet" parts carry room tone.
// A trimmer that only finds exact zeroes would do nothing on a real recording.
function roomTone(secs, amp = 0.004) {
  const n = Math.round(SR * secs), a = new Float32Array(n);
  for (let i = 0; i < n; i++) a[i] = (Math.random() * 2 - 1) * amp;
  return a;
}

function concat(...parts) {
  const total = parts.reduce((s, p) => s + p.length, 0);
  const out = new Float32Array(total);
  let o = 0;
  for (const p of parts) { out.set(p, o); o += p.length; }
  return out;
}

console.log("\n=== auto-trim finds the speech inside the silence ===");
{
  // One second of room tone, two of speech, one more of room tone: the shape of
  // every take, where the lead-in is a breath and the tail is the mouse.
  const sig = concat(roomTone(1.0), tone(2.0), roomTone(1.0));
  const { start, end } = recAutoTrim(sig);
  // 80 ms of padding is deliberate, so the bounds sit just outside the speech.
  near("start lands just before the speech", start / SR, 1.0, 0.13);
  near("end lands just after the speech",    end   / SR, 3.0, 0.13);
  check("start is before end", start < end, true);
  check("keeps the whole spoken part", (end - start) / SR > 1.9, true);
}

console.log("\n=== a trailing click is cut, which is the whole point ===");
{
  // A click is short and loud, exactly what the mouse makes on the stop button.
  const click = new Float32Array(Math.round(SR * 0.01)).fill(0.9);
  const sig   = concat(roomTone(0.4), tone(2.0), roomTone(0.5), click, roomTone(0.1));
  const full  = sig.length / SR;
  const { end } = recAutoTrim(sig);
  // Without trimming the click rides along in the reference clip. The detector
  // should stop at the speech, well before it.
  check("end stops before the click", end / SR < full - 0.4, true);
}

console.log("\n=== degrades safely ===");
{
  const quiet = recAutoTrim(silence(2.0));
  check("digital silence keeps everything", quiet.start === 0 && quiet.end === SR * 2, true);
  const flat = recAutoTrim(roomTone(2.0));
  check("room tone only does not invert the range", flat.start < flat.end, true);
  const tiny = recAutoTrim(new Float32Array(10));
  check("a signal too short to window keeps everything", tiny.end, 10);
  const loud = recAutoTrim(tone(1.5));
  check("speech with no silence keeps everything", loud.start === 0 && loud.end === 1.5 * SR, true);
}

console.log("\n=== slicing is clamped ===");
{
  const s = tone(1.0);
  check("negative start clamps to 0",     recSlice(s, -500, 100).length, 100);
  check("end past the buffer clamps",     recSlice(s, 0, s.length + 999).length, s.length);
  check("normal slice is exact",          recSlice(s, 100, 400).length, 300);
}

console.log("\n=== the WAV the server will receive ===");
{
  const samples = tone(1.0, 0.5);
  const blob = recWav(samples);
  const buf  = Buffer.from(await blob.arrayBuffer());
  const str  = (o, n) => buf.toString("ascii", o, o + n);

  // These four are exactly what the server checks before saving.
  check("starts RIFF",           str(0, 4),  "RIFF");
  check("is WAVE",               str(8, 4),  "WAVE");
  check("has a fmt chunk",       str(12, 4), "fmt ");
  check("has a data chunk",      str(36, 4), "data");
  check("uncompressed PCM",      buf.readUInt16LE(20), 1);
  check("mono",                  buf.readUInt16LE(22), 1);
  check("24 kHz",                buf.readUInt32LE(24), SR);
  check("16-bit",                buf.readUInt16LE(34), 16);
  check("byte rate agrees",      buf.readUInt32LE(28), SR * 2);
  check("block align agrees",    buf.readUInt16LE(32), 2);
  check("data size matches",     buf.readUInt32LE(40), samples.length * 2);
  check("riff size matches",     buf.readUInt32LE(4), 36 + samples.length * 2);
  check("total length is right", buf.length, 44 + samples.length * 2);

  // Quantisation should be the only loss, so a round trip has to come back
  // within one 16-bit step.
  let worst = 0;
  for (let i = 0; i < samples.length; i++) {
    const back = buf.readInt16LE(44 + i * 2) / 0x7fff;
    worst = Math.max(worst, Math.abs(back - samples[i]));
  }
  check("round-trips within one quantisation step", worst < 1 / 32767 + 1e-6, true);
}

console.log("\n=== full-scale audio does not wrap around ===");
{
  // Wrapping is the classic sign error here, and it turns a loud peak into a
  // loud peak of the opposite sign, which sounds like a hard click.
  const edge = Float32Array.from([1, -1, 1.5, -1.5, 0]);
  const buf  = Buffer.from(await recWav(edge).arrayBuffer());
  const v    = i => buf.readInt16LE(44 + i * 2);
  check("+1 hits the positive limit",  v(0), 32767);
  check("-1 hits the negative limit",  v(1), -32768);
  check("above +1 is clamped",         v(2), 32767);
  check("below -1 is clamped",         v(3), -32768);
  check("zero stays zero",             v(4), 0);
}

console.log("\n=== waveform peaks ===");
{
  const sig = concat(silence(0.5), tone(0.5, 0.8));
  const p   = recPeaks(sig, 100);
  check("one value per column", p.length, 100);
  check("silent half reads as flat", p[10] < 0.01, true);
  near("loud half reads near its amplitude", p[90], 0.8, 0.05);
}

console.log(`\n${pass} passed, ${fail} failed`);
process.exit(fail ? 1 : 0);
