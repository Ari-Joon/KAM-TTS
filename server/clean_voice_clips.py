"""
clean_voice_clips.py — prepare raw recordings for voice cloning.

Takes the clips you recorded and produces encoder-ready versions, so users don't
need Audacity or any external editor.

    python clean_voice_clips.py                    # cleans ./voice_samples
    python clean_voice_clips.py --in raw --out voice_samples
    python clean_voice_clips.py --dry-run          # report only, write nothing

What it does (all objective, all safe):
  1. Downmix to mono            — XTTS uses one channel.
  2. Trim leading/trailing silence — dead air wastes the reference budget; a
                                    clip that is half silence gives the encoder
                                    half the speech it could have had.
  3. Resample to 22050 Hz       — XTTS native rate.
  4. Peak-normalise to -3 dBFS  — consistent level across clips, with headroom
                                  so nothing clips.

What it deliberately does NOT do
--------------------------------
No noise reduction, EQ, compression, de-essing or "enhancement". Those reshape
the voice, and a clone learns whatever artifacts they leave behind. The goal is
your actual voice, cleanly framed — not a processed one. If a clip has
background noise, re-record it rather than trying to filter it out.

Originals are never modified: cleaned files go to a separate output folder.
Only the Python standard library is used.
"""
import os
import sys
import wave
import array

# Windows consoles still default to a legacy codepage (cp1252), where printing
# any non-ASCII character raises UnicodeEncodeError and kills the script. This
# has to run before anything prints, and cannot live in a shared import,
# because an import that itself printed would crash first.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# audioop was removed in Python 3.13. It's used only for downmixing, bit-depth
# conversion and resampling — all straightforward to do directly — so fall back
# to pure Python rather than adding a dependency.
try:
    import audioop
    _HAVE_AUDIOOP = True
except ImportError:
    audioop = None
    _HAVE_AUDIOOP = False

TARGET_SR      = 22050      # XTTS native sample rate
TARGET_PEAK    = 0.707      # about -3 dBFS: consistent, with headroom
SILENCE_FLOOR  = 0.02       # RMS (0..1) below this counts as silence
PAD_MS         = 120        # keep this much silence either side of speech
WIN_MS         = 20         # analysis window for silence detection


def _read_wav(path):
    """Return (samples as array('h'), sample_rate) downmixed to mono 16-bit."""
    with wave.open(path, "rb") as w:
        ch, width, sr, n = (w.getnchannels(), w.getsampwidth(),
                            w.getframerate(), w.getnframes())
        raw = w.readframes(n)

    if _HAVE_AUDIOOP:
        if width != 2:
            raw = audioop.lin2lin(raw, width, 2)
            width = 2
        if ch == 2:
            raw = audioop.tomono(raw, width, 0.5, 0.5)
        samples = array.array("h")
        samples.frombytes(raw)
        return samples, sr

    # Pure-Python path (Python 3.13+, where audioop was removed).
    if width == 2:
        interleaved = array.array("h")
        interleaved.frombytes(raw)
    elif width == 1:                      # unsigned 8-bit → signed 16-bit
        interleaved = array.array("h", ((b - 128) << 8 for b in raw))
    elif width == 4:                      # 32-bit → 16-bit
        src = array.array("i"); src.frombytes(raw)
        interleaved = array.array("h", (v >> 16 for v in src))
    else:
        raise ValueError(f"unsupported sample width: {width} bytes")

    if ch == 1:
        return interleaved, sr
    mono = array.array("h",
        (sum(interleaved[i:i + ch]) // ch for i in range(0, len(interleaved), ch)))
    return mono, sr


def _resample(samples, sr_from, sr_to):
    if sr_from == sr_to:
        return samples
    if _HAVE_AUDIOOP:
        raw, _ = audioop.ratecv(samples.tobytes(), 2, 1, sr_from, sr_to, None)
        out = array.array("h")
        out.frombytes(raw)
        return out
    # Linear interpolation — adequate for speech reference material.
    ratio = sr_to / float(sr_from)
    n_out = int(len(samples) * ratio)
    out = array.array("h", bytes(2 * n_out))
    for i in range(n_out):
        pos = i / ratio
        j = int(pos)
        frac = pos - j
        a = samples[j] if j < len(samples) else 0
        b = samples[j + 1] if j + 1 < len(samples) else a
        out[i] = int(a + (b - a) * frac)
    return out


def _trim_silence(samples, sr):
    """Trim leading/trailing silence, keeping a short natural pad.
    Returns (trimmed, removed_seconds)."""
    win = max(1, int(sr * WIN_MS / 1000))
    n = len(samples)
    if n == 0:
        return samples, 0.0

    def loud(i):
        seg = samples[i:i + win]
        if not seg:
            return False
        rms = (sum(v * v for v in seg) / len(seg)) ** 0.5 / 32768.0
        return rms >= SILENCE_FLOOR

    first = 0
    while first + win < n and not loud(first):
        first += win
    last = n - win
    while last > first and not loud(last):
        last -= win

    pad = int(sr * PAD_MS / 1000)
    start = max(0, first - pad)
    end   = min(n, last + win + pad)
    trimmed = samples[start:end]
    removed = (n - len(trimmed)) / float(sr)
    return trimmed, removed


def _normalise(samples, target_peak=TARGET_PEAK):
    """Scale so the loudest sample sits at target_peak. Returns (samples, gain)."""
    if not samples:
        return samples, 1.0
    peak = max(abs(v) for v in samples) / 32768.0
    if peak <= 0:
        return samples, 1.0
    gain = target_peak / peak
    # Never amplify hard: a very quiet clip is better re-recorded than boosted,
    # since boosting raises the noise floor with the voice.
    gain = min(gain, 4.0)
    out = array.array("h", (max(-32768, min(32767, int(v * gain))) for v in samples))
    return out, gain


def _write_wav(path, samples, sr):
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(samples.tobytes())


def clean_one(src, dst, dry_run=False):
    """Clean a single clip. Returns a summary dict."""
    samples, sr = _read_wav(src)
    before_s = len(samples) / float(sr)
    peak_before = (max(abs(v) for v in samples) / 32768.0) if samples else 0.0

    trimmed, removed_s = _trim_silence(samples, sr)
    resampled = _resample(trimmed, sr, TARGET_SR)
    final, gain = _normalise(resampled)
    after_s = len(final) / float(TARGET_SR)

    if not dry_run:
        _write_wav(dst, final, TARGET_SR)

    return {
        "name": os.path.basename(src),
        "before_s": before_s, "after_s": after_s,
        "removed_s": removed_s, "sr_from": sr,
        "peak_before": peak_before, "gain": gain,
        "clipped_before": peak_before >= 0.999,
    }


def main():
    args = sys.argv[1:]
    here = os.path.dirname(os.path.abspath(__file__))
    dry = "--dry-run" in args

    def opt(flag, default):
        if flag in args:
            i = args.index(flag)
            if i + 1 < len(args):
                return args[i + 1]
        return default

    in_dir  = os.path.abspath(opt("--in",  os.path.join(here, "voice_samples")))
    out_dir = os.path.abspath(opt("--out", os.path.join(here, "voice_samples_clean")))

    if not os.path.isdir(in_dir):
        print(f"Input folder not found: {in_dir}")
        sys.exit(2)
    clips = sorted(f for f in os.listdir(in_dir) if f.lower().endswith(".wav"))
    if not clips:
        print(f"No .wav files in {in_dir}")
        sys.exit(2)

    if not dry:
        os.makedirs(out_dir, exist_ok=True)

    print(f"Cleaning {len(clips)} clip(s)")
    print(f"  from: {in_dir}")
    print(f"  to:   {out_dir}{'  (DRY RUN — nothing written)' if dry else ''}")
    print("─" * 68)

    total_before = total_after = 0.0
    warnings = []
    for name in clips:
        src = os.path.join(in_dir, name)
        dst = os.path.join(out_dir, name)
        try:
            r = clean_one(src, dst, dry_run=dry)
        except Exception as e:
            print(f"  SKIP  {name}: {e}")
            continue
        total_before += r["before_s"]
        total_after  += r["after_s"]
        pct = (r["removed_s"] / r["before_s"] * 100) if r["before_s"] else 0
        print(f"  {name}")
        print(f"      {r['before_s']:.1f}s → {r['after_s']:.1f}s "
              f"(trimmed {r['removed_s']:.1f}s, {pct:.0f}% silence removed) · "
              f"{r['sr_from']}Hz → {TARGET_SR}Hz · gain ×{r['gain']:.2f}")
        if r["clipped_before"]:
            warnings.append(f"{name}: peaked at digital maximum before cleaning — "
                            "likely clipped. Listen for distortion; re-record if audible.")

    print("─" * 68)
    print(f"Total speech: {total_before:.1f}s → {total_after:.1f}s "
          f"({total_before - total_after:.1f}s of silence removed)")
    if warnings:
        print("\nWorth your ears:")
        for w in warnings:
            print(f"  - {w}")
    if not dry:
        print(f"\nCleaned clips written to: {out_dir}")
        print("Next: check them, then point the server at them.")
        print("  python check_voice_clips.py \"" + out_dir + "\"")


if __name__ == "__main__":
    main()
