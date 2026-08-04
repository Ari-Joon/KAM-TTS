"""Compare old vs new gain staging on speech-like signals: level preserved,
distortion gone."""
import numpy as np, sys, pathlib

exec((pathlib.Path(__file__).resolve().parent / "test_pipeline.py")
     .read_text(encoding="utf-8").split(
    "# ---------------------------------------------------------------------------")[0])

SR = 24000
rng = np.random.default_rng(3)

def speechlike(seconds=2.0, transients=6):
    """Voiced hum + noise, with a few brief loud transients (plosives)."""
    n = int(SR * seconds)
    t = np.arange(n) / SR
    sig = (0.30 * np.sin(2*np.pi*120*t) + 0.15 * np.sin(2*np.pi*240*t)
           + 0.05 * rng.standard_normal(n))
    env = 0.6 + 0.4 * np.sin(2*np.pi*3*t)
    sig *= env
    for _ in range(transients):                     # short peaks, as in speech
        i = rng.integers(0, n - 400)
        sig[i:i+400] *= 3.0
    return (sig / np.max(np.abs(sig)) * 0.8).astype(np.float32)

def old_chain(a):
    a = a.copy()
    peak = float(np.max(np.abs(a)))
    if peak > 0:
        a = a / peak * 0.97
    return np.clip(a * 1.15, -1.0, 1.0)

def new_chain(a):
    a = a.copy()
    peak = float(np.max(np.abs(a)))
    if peak > 0:
        a = a / peak * S._PEAK_TARGET * S._MAKEUP_GAIN
        a = S._soft_limit(a)
    return a

def rms_db(a):  return 20 * np.log10(np.sqrt(np.mean(a.astype(np.float64)**2)) + 1e-12)
def clipped(a): return int(np.sum(np.abs(a) >= 0.999))

print(f"\n{'signal':10} {'chain':>6} {'RMS dBFS':>10} {'peak':>7} {'clipped samples':>17}")
print("-" * 56)
deltas = []
for k in range(5):
    sig = speechlike()
    o, n = old_chain(sig), new_chain(sig)
    deltas.append(rms_db(n) - rms_db(o))
    print(f"take {k+1:<5} {'old':>6} {rms_db(o):9.2f} {np.max(np.abs(o)):7.3f} {clipped(o):17d}")
    print(f"{'':10} {'new':>6} {rms_db(n):9.2f} {np.max(np.abs(n)):7.3f} {clipped(n):17d}")

print("-" * 56)
print(f"mean loudness change vs old chain: {np.mean(deltas):+.2f} dB "
      f"(target: within +/-1 dB)")
ok_level = abs(np.mean(deltas)) < 1.0
print(f"level preserved: {ok_level}")

# full process_audio never exceeds int16 range
worst = 0
for _ in range(20):
    out = S.process_audio(speechlike(0.5), silence_ms=30, text_len=60)
    worst = max(worst, int(np.max(np.abs(out.astype(np.int32)))))
print(f"process_audio worst |sample| over 20 chunks: {worst} (int16 max 32767)")
ok_range = worst <= 32767
print(f"never exceeds int16: {ok_range}")
_pass = int(ok_level) + int(ok_range)
print()
print(f"{_pass} passed, {2 - _pass} failed")
sys.exit(0 if _pass == 2 else 1)
