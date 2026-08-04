"""
audio_quality.py — objective audio checks at both ends of the pipeline.

Two problems that turned out to need the same toolkit.

The first is the reference clips, which decide the clone. XTTS averages every
clip in a voice folder into a single speaker embedding, so one clipped or noisy
or near-silent recording drags the whole voice down and there's no way to hear
which one did it. So I measure each clip and exclude the unusable ones before
computing latents, and print which and why. That's what keeps quality consistent
whichever voice profile you pick, since a profile recorded later on a laptop mic
gets held to the same standard as the first one.

The second is that XTTS hallucinates, and it does it in recognisable ways. The
autoregressive loop can fail to emit an end token and carry on into babble, or
loop a fragment, or stop early mid-word. All three show up in the waveform
without needing a transcription, since babble runs far longer than the text can
account for, looping repeats an envelope, and truncation ends at full volume.
Catching them here means I can re-synthesise a bad chunk before anyone hears it
instead of having Whisper score it badly a minute later.

All numpy. The original clip checker used audioop, which was deprecated in
Python 3.11 and removed in 3.13, so anything depending on it breaks on a current
Python.
"""
from __future__ import annotations

import wave
import contextlib
from dataclasses import dataclass, field
from typing import List, Optional

import numpy as np  # type: ignore


# Thresholds for the reference clips, tuned for XTTS-v2 zero-shot cloning.
TARGET_SR     = 22050     # what XTTS uses natively, higher is fine as it resamples
MIN_SR        = 16000     # below this there isn't enough detail to clone well
MIN_SECONDS   = 4.0
MAX_SECONDS   = 20.0
IDEAL_MIN     = 8.0
IDEAL_MAX     = 15.0
MAX_SILENCE   = 0.65      # share of 20ms windows under the silence floor. This
                          # counts natural pauses as well, so normal read-aloud
                          # speech sits around 40-50% and I only want to flag
                          # clips with obvious dead air in them.
SILENCE_FLOOR = 0.02      # window RMS (0 to 1) under this counts as silence
CLIP_CEILING  = 0.99      # a sample at or above this is clipped
MAX_CLIP_FRAC = 0.001     # more than 0.1% clipped samples and I won't use it
MIN_PEAK      = 0.10      # quieter than this is too weak to be a good reference
MIN_SNR_DB    = 12.0      # under this and you can hear the room in the recording


@dataclass
class ClipReport:
    """The measurements and verdict for one reference clip."""
    path:         str
    name:         str = ""
    ok:           bool = True          # can I use it as a reference
    verdict:      str = "PASS"         # PASS | WARN | FAIL
    reasons:      List[str] = field(default_factory=list)
    duration:     float = 0.0
    sample_rate:  int = 0
    channels:     int = 0
    bit_depth:    int = 0
    peak:         float = 0.0
    rms:          float = 0.0
    clip_frac:    float = 0.0
    silence_frac: float = 0.0
    snr_db:       Optional[float] = None

    def line(self) -> str:
        """Single-line summary for the console."""
        tag = {"PASS": "PASS", "WARN": "WARN", "FAIL": "FAIL"}[self.verdict]
        return (f"{tag}  {self.name or self.path}   {self.duration:.1f}s · "
                f"{self.sample_rate}Hz · peak {self.peak:.2f} · "
                f"{self.silence_frac * 100:.0f}% quiet"
                + (f" · SNR {self.snr_db:.0f}dB" if self.snr_db is not None else ""))


def _read_wav(path: str):
    """Return (mono float32 in -1..1, sample_rate, channels, bit_depth)."""
    with contextlib.closing(wave.open(path, "rb")) as w:
        channels = w.getnchannels()
        sr       = w.getframerate()
        width    = w.getsampwidth()
        frames   = w.readframes(w.getnframes())

    dtype = {1: np.int8, 2: np.int16, 4: np.int32}.get(width)
    if dtype is None:
        raise ValueError(f"unsupported bit depth: {width * 8}-bit")
    data = np.frombuffer(frames, dtype=dtype).astype(np.float32)
    if width == 1:
        data = (data - 128.0) / 128.0            # 8-bit WAV is unsigned
    else:
        data /= float(1 << (8 * width - 1))
    if channels > 1:
        usable = (len(data) // channels) * channels
        data = data[:usable].reshape(-1, channels).mean(axis=1)
    return data, sr, channels, width * 8


def analyse_clip(path: str, name: str = "") -> ClipReport:
    """Measure one reference clip and judge whether it should feed the clone."""
    rep = ClipReport(path=path, name=name or path.rsplit("/", 1)[-1].rsplit("\\", 1)[-1])
    try:
        audio, sr, channels, depth = _read_wav(path)
    except Exception as e:
        rep.ok = False
        rep.verdict = "FAIL"
        rep.reasons.append(f"could not read the file ({e})")
        return rep

    rep.sample_rate = sr
    rep.channels    = channels
    rep.bit_depth   = depth
    rep.duration    = len(audio) / float(sr) if sr else 0.0
    if audio.size == 0:
        rep.ok = False
        rep.verdict = "FAIL"
        rep.reasons.append("file contains no audio")
        return rep

    mag = np.abs(audio)
    rep.peak      = float(mag.max())
    rep.rms       = float(np.sqrt(np.mean(audio.astype(np.float64) ** 2)))
    rep.clip_frac = float(np.mean(mag >= CLIP_CEILING))

    # RMS over 20ms windows, since that's the resolution where speech and pauses
    # separate out.
    win = max(1, int(sr * 0.02))
    n_win = len(audio) // win
    if n_win:
        windows = audio[:n_win * win].reshape(n_win, win)
        win_rms = np.sqrt(np.mean(windows.astype(np.float64) ** 2, axis=1))
        quiet = win_rms < SILENCE_FLOOR
        rep.silence_frac = float(np.mean(quiet))
        # SNR compares the speech level against the noise floor. A noisy room
        # raises that floor even when the peaks look fine, and it gets baked
        # permanently into the clone, so I want to catch it first.
        #
        # I take the floor as the quietest tenth of the windows rather than
        # "windows below the silence threshold", because a uniformly noisy
        # recording (exactly the case worth flagging) has no window below the
        # threshold at all, so no SNR got computed and the clip went through
        # without comment.
        if n_win >= 10:
            floor  = float(np.percentile(win_rms, 10)) + 1e-9
            speech = float(np.percentile(win_rms, 90)) + 1e-9
            rep.snr_db = float(20.0 * np.log10(speech / floor))

    fails, warns = [], []
    if sr < MIN_SR:
        fails.append(f"sample rate {sr}Hz is too low (need at least {MIN_SR}Hz)")
    elif sr != TARGET_SR:
        warns.append(f"{sr}Hz (XTTS resamples to {TARGET_SR}Hz; harmless)")
    if channels != 1:
        warns.append(f"{channels} channels (downmixed to mono)")

    if rep.duration < MIN_SECONDS:
        fails.append(f"only {rep.duration:.1f}s (need at least {MIN_SECONDS:.0f}s)")
    elif rep.duration > MAX_SECONDS:
        fails.append(f"{rep.duration:.1f}s is too long (keep under {MAX_SECONDS:.0f}s)")
    elif not (IDEAL_MIN <= rep.duration <= IDEAL_MAX):
        warns.append(f"{rep.duration:.1f}s (ideal is {IDEAL_MIN:.0f}-{IDEAL_MAX:.0f}s)")

    if rep.clip_frac > MAX_CLIP_FRAC:
        fails.append(f"clipped: {rep.clip_frac * 100:.2f}% of samples are at full "
                     f"scale — the distortion becomes part of the voice")
    if rep.peak < MIN_PEAK:
        fails.append(f"too quiet (peak {rep.peak:.2f}) — record closer or louder")
    if rep.silence_frac > MAX_SILENCE:
        warns.append(f"{rep.silence_frac * 100:.0f}% of the clip is near-silent — "
                     f"trim the dead air")
    if rep.snr_db is not None and rep.snr_db < MIN_SNR_DB:
        warns.append(f"background noise is audible (SNR {rep.snr_db:.0f}dB, want "
                     f"{MIN_SNR_DB:.0f}dB+) — it will be cloned along with the voice")

    rep.reasons = fails + warns
    rep.verdict = "FAIL" if fails else ("WARN" if warns else "PASS")
    rep.ok = not fails
    return rep


def screen_clips(paths, min_keep: int = 1):
    """Measure every clip and split them into the ones I'll use and the rest.

    Returns (usable_paths, reports). If screening would throw out everything, or
    leave fewer than min_keep, then I keep them all instead, since a voice that
    clones badly still beats a voice that refuses to load and the reports still
    say exactly what needs fixing."""
    reports = [analyse_clip(p) for p in paths]
    usable = [r.path for r in reports if r.ok]
    if len(usable) < min_keep:
        return list(paths), reports
    return usable, reports


# Checks on what XTTS gives back, which is 24 kHz mono float audio.
SR_OUT = 24000

# Speech runs at roughly 65-75 ms per character at speed 1.0, so a chunk much
# longer than its text can account for is the classic runaway generation.
_MS_PER_CHAR       = 0.070
_RUNAWAY_FACTOR    = 1.9     # longer than this x expected = almost certainly babble
_TRUNCATED_FACTOR  = 0.35    # shorter than this x expected = cut off early
_MIN_SPEECH_FRAC   = 0.25    # at least this much of the clip must be above the floor
_TAIL_ENERGY_HOT   = 0.55    # ending this loud (vs peak) means it was cut mid-word
# For looping I need two measures to agree, and both have to be extreme.
# Correlation on its own isn't enough since ordinary speech is rhythmic, and a
# steady reading voice or a list read at an even pace correlates strongly with a
# shifted copy of itself with nothing actually wrong. A stuck generation repeats
# the same fragment at the same level as well, so the residual between the two
# copies collapses too, which rhythm alone never does.
_LOOP_CORR         = 0.97    # shape self-similarity
_LOOP_RESIDUAL     = 0.12    # mean |difference| relative to mean level
_LOOP_MIN_REPEATS  = 3       # the repeat must account for most of the chunk
_SPEECH_FLOOR      = 0.02    # |x| below this is not speech


@dataclass
class OutputCheck:
    """My verdict on one synthesised chunk before it goes to the browser."""
    ok:         bool = True
    problem:    Optional[str] = None    # runaway | looping | truncated | dead | ""
    detail:     str = ""
    duration_s: float = 0.0
    expected_s: float = 0.0
    speech_frac: float = 0.0
    tail_ratio: float = 0.0

    def as_dict(self) -> dict:
        return {"ok": self.ok, "problem": self.problem, "detail": self.detail,
                "duration_s": round(self.duration_s, 2),
                "expected_s": round(self.expected_s, 2),
                "speech_frac": round(self.speech_frac, 3),
                "tail_ratio": round(self.tail_ratio, 3)}


def _looks_looped(audio: np.ndarray) -> bool:
    """Spot a repeated fragment in the amplitude envelope.

    A stuck autoregressive loop puts out the same short phrase over and over.
    Two things are true of that and of nothing else: the envelope matches a
    shifted copy of itself in shape, which is the correlation, and it matches in
    level, which is the residual collapsing.

    Needing both is what keeps normal speech out of it. An even list or a
    metronomic delivery gives high correlation on its own, so correlation alone
    flags perfectly good audio. Requiring the repeat at least _LOOP_MIN_REPEATS
    times narrows it further, since a real loop fills the whole chunk while two
    similar-sounding phrases in a row don't.
    """
    if len(audio) < 2 * SR_OUT:      # under two seconds cannot show 3 repeats
        return False
    win = int(SR_OUT * 0.02)         # 20ms, which resolves syllables
    n = len(audio) // win
    if n < 60:
        return False
    env = np.abs(audio[:n * win]).reshape(n, win).max(axis=1).astype(np.float64)
    level = float(env.mean())
    if level <= 1e-6:
        return False
    centred = env - level

    # Lags long enough to be a phrase rather than a syllable, and short enough
    # that the fragment repeats at least _LOOP_MIN_REPEATS times in the chunk.
    lo = max(1, int(0.35 / 0.02))
    hi = n // _LOOP_MIN_REPEATS
    for lag in range(lo, max(lo + 1, hi)):
        a, b = centred[:-lag], centred[lag:]
        denom = (float(np.dot(a, a)) * float(np.dot(b, b))) ** 0.5
        if denom <= 1e-9:
            continue
        if float(np.dot(a, b)) / denom <= _LOOP_CORR:
            continue
        # The shape matches, but does the level match too? For a real repeat the
        # two copies are almost identical so the mean absolute difference against
        # the overall level is tiny, and rhythmic speech fails this easily.
        residual = float(np.mean(np.abs(env[:-lag] - env[lag:]))) / level
        if residual < _LOOP_RESIDUAL:
            return True
    return False


def check_output(wav, text_len: int, speed: float = 1.0) -> OutputCheck:
    """Check a synthesised waveform against the text it was supposed to speak.

    I run this before the audio gets post-processed and returned, so a failure
    can be retried at a steadier temperature instead of going out and then being
    scored badly by the listen-back pass a minute later."""
    audio = np.asarray(wav, dtype=np.float32)
    res = OutputCheck()
    if audio.size == 0:
        res.ok, res.problem, res.detail = False, "dead", "no audio was produced"
        return res

    res.duration_s = len(audio) / float(SR_OUT)
    res.expected_s = max(0.25, text_len * _MS_PER_CHAR / max(0.5, speed))

    mag = np.abs(audio)
    peak = float(mag.max())
    if peak < 1e-4:
        res.ok, res.problem = False, "dead"
        res.detail = "the chunk is silent"
        return res

    res.speech_frac = float(np.mean(mag > _SPEECH_FLOOR * peak))
    tail = mag[-int(SR_OUT * 0.05):] if len(mag) > SR_OUT * 0.05 else mag
    res.tail_ratio = float(tail.mean() / (peak + 1e-9))

    if res.duration_s > res.expected_s * _RUNAWAY_FACTOR:
        res.ok, res.problem = False, "runaway"
        res.detail = (f"{res.duration_s:.1f}s of audio for {text_len} characters "
                      f"(expected about {res.expected_s:.1f}s) — the model did not "
                      f"stop where the text ended")
        return res

    if _looks_looped(audio):
        res.ok, res.problem = False, "looping"
        res.detail = "the same fragment repeats — the generation got stuck"
        return res

    if res.speech_frac < _MIN_SPEECH_FRAC:
        res.ok, res.problem = False, "dead"
        res.detail = (f"only {res.speech_frac * 100:.0f}% of the chunk contains "
                      f"speech — mostly silence")
        return res

    if (res.duration_s < res.expected_s * _TRUNCATED_FACTOR
            and res.tail_ratio > _TAIL_ENERGY_HOT):
        res.ok, res.problem = False, "truncated"
        res.detail = (f"stopped after {res.duration_s:.1f}s of an expected "
                      f"{res.expected_s:.1f}s, still at full volume — cut off "
                      f"mid-word")
        return res

    return res
