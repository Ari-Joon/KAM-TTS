"""Test device.py, audio_quality.py and benchmark.py — the real modules, with
torch stubbed so several machine shapes can be simulated on one box."""
import pathlib as _pl
SERVER_DIR    = _pl.Path(__file__).resolve().parent.parent
EXTENSION_DIR = SERVER_DIR.parent / "extension"
import sys, types, pathlib, wave, struct, math, tempfile, os
import numpy as np

SERVER = SERVER_DIR
sys.path.insert(0, str(SERVER))

PASS = FAIL = 0
def check(label, got, want):
    global PASS, FAIL
    if got == want:
        PASS += 1; print(f"  ok   {label}")
    else:
        FAIL += 1; print(f"  FAIL {label}\n         got:  {got!r}\n         want: {want!r}")

# ── fake torch, reshaped per scenario ────────────────────────────────────────
def make_torch(*, cuda=False, hip=None, cap=(8, 9), mps=False, xpu=False,
               fail_on=None, vram_gb=24.0):
    t = types.ModuleType("torch")
    t.__version__ = "2.6.0"
    class V: pass
    t.version = V(); t.version.cuda = None if hip else "12.8"; t.version.hip = hip
    class Cuda:
        @staticmethod
        def is_available(): return cuda
        @staticmethod
        def device_count(): return 1 if cuda else 0
        @staticmethod
        def get_device_name(i): return "AMD Radeon RX 7900" if hip else "NVIDIA RTX 5090"
        @staticmethod
        def mem_get_info(): return (int(vram_gb*.5*1024**3), int(vram_gb*1024**3))
        @staticmethod
        def get_device_capability(i): return cap
        @staticmethod
        def empty_cache(): pass
    t.cuda = Cuda
    class MPSB:
        @staticmethod
        def is_available(): return mps
        @staticmethod
        def is_built(): return mps
    class Backends:
        class cuda:
            class matmul: allow_tf32 = False
        class cudnn: allow_tf32 = False; benchmark = True
    t.backends = Backends; t.backends.mps = MPSB
    if xpu:
        class Xpu:
            @staticmethod
            def is_available(): return True
            @staticmethod
            def device_count(): return 1
            @staticmethod
            def get_device_name(i): return "Intel Arc A770"
            @staticmethod
            def empty_cache(): pass
        t.xpu = Xpu
    t.set_float32_matmul_precision = lambda s: None
    t._threads = None
    def set_num_threads(n): t._threads = n
    t.set_num_threads = set_num_threads
    t.device = lambda s: s
    class Tensor:
        """Just enough tensor for device.verify()'s a @ a.t() round-trip."""
        def t(self): return self
        def __matmul__(self, other): return self
        def sum(self): return self
        def item(self): return 1.0
    def randn(*shape, device=None):
        if fail_on and device == fail_on:
            raise RuntimeError(f"{device} kernel not implemented")
        return Tensor()
    t.randn = randn
    t.tanh = lambda a: a
    return t

def resolve_with(torch_mod, env=None):
    for k in ("device",):
        sys.modules.pop(k, None)
    sys.modules["torch"] = torch_mod
    old = {}
    for k, v in (env or {}).items():
        old[k] = os.environ.get(k); os.environ[k] = v
    try:
        import importlib, device as D
        importlib.reload(D)
        return D.resolve(), D
    finally:
        for k, v in old.items():
            if v is None: os.environ.pop(k, None)
            else: os.environ[k] = v

print("\n=== device selection across machine types ===")
# NVIDIA Ampere+
d, D = resolve_with(make_torch(cuda=True, cap=(8, 9)))
check("NVIDIA -> cuda", (d.backend, d.torch_device), ("cuda", "cuda"))
check("NVIDIA SM8.9 enables TF32", d.supports_tf32, True)
check("cudnn.benchmark forced off", sys.modules["torch"].backends.cudnn.benchmark, False)

# Older NVIDIA (Pascal, SM 6.1) — TF32 must NOT be claimed
d, _ = resolve_with(make_torch(cuda=True, cap=(6, 1)))
check("Pascal does not claim TF32", d.supports_tf32, False)

# AMD ROCm — reports as cuda but is not cuda
d, _ = resolve_with(make_torch(cuda=True, hip="6.2"))
check("ROCm -> backend rocm", d.backend, "rocm")
check("ROCm still uses the cuda device string", d.torch_device, "cuda")
check("ROCm does not enable TF32", d.supports_tf32, False)
check("ROCm is explained in notes",
      any("ROCm" in n for n in d.notes), True)

# Apple Silicon
d, _ = resolve_with(make_torch(mps=True))
check("Apple Silicon -> mps", d.backend, "mps")
check("MPS sets the CPU fallback env var",
      os.environ.get("PYTORCH_ENABLE_MPS_FALLBACK"), "1")

# Intel Arc
d, _ = resolve_with(make_torch(xpu=True))
check("Intel -> xpu", d.backend, "xpu")

# No accelerator at all
d, _ = resolve_with(make_torch())
check("no GPU -> cpu", d.backend, "cpu")
check("CPU threads were capped", sys.modules["torch"]._threads is not None, True)

# A backend that claims available but fails real work must be demoted
d, _ = resolve_with(make_torch(mps=True, fail_on="mps"))
check("broken MPS falls back to CPU", d.backend, "cpu")
check("fallback is recorded", d.fell_back, True)
check("fallback reason is explained",
      any("failed a test operation" in n for n in d.notes), True)

# Explicit override
d, _ = resolve_with(make_torch(cuda=True), env={"KAM_DEVICE": "cpu"})
check("KAM_DEVICE=cpu is honoured", d.backend, "cpu")
d, _ = resolve_with(make_torch(), env={"KAM_DEVICE": "cuda"})
check("impossible KAM_DEVICE is ignored, not fatal", d.backend, "cpu")

print("\n=== reference-clip gate ===")
import audio_quality as AQ

def write_wav(path, seconds=10.0, sr=22050, peak=0.5, noise=0.0,
              silence_tail=0.0, clip=False):
    n = int(sr * seconds)
    t = np.arange(n) / sr
    sig = 0.6*np.sin(2*np.pi*130*t) + 0.3*np.sin(2*np.pi*260*t)
    sig *= (0.6 + 0.4*np.sin(2*np.pi*2.5*t))          # syllable envelope
    sig = sig / np.max(np.abs(sig)) * peak
    if noise: sig += np.random.randn(n) * noise
    if silence_tail:
        k = int(sr*silence_tail); sig[-k:] = 0.0
    if clip: sig = np.clip(sig*3.0, -1.0, 1.0)
    data = np.clip(sig, -1, 1)
    with wave.open(path, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(sr)
        w.writeframes((data*32767).astype("<i2").tobytes())

tmp = tempfile.mkdtemp()
good  = os.path.join(tmp, "good.wav");   write_wav(good)
quiet = os.path.join(tmp, "quiet.wav");  write_wav(quiet, peak=0.03)
short = os.path.join(tmp, "short.wav");  write_wav(short, seconds=1.5)
clipd = os.path.join(tmp, "clipped.wav");write_wav(clipd, clip=True)
noisy = os.path.join(tmp, "noisy.wav");  write_wav(noisy, peak=0.5, noise=0.05)

check("good clip passes",    AQ.analyse_clip(good).ok,  True)
check("too quiet fails",     AQ.analyse_clip(quiet).ok, False)
check("too short fails",     AQ.analyse_clip(short).ok, False)
check("clipped fails",       AQ.analyse_clip(clipd).ok, False)
for p, why in ((quiet, "quiet"), (short, "short"), (clipd, "clipped")):
    r = AQ.analyse_clip(p)
    print(f"       {why:8} -> {r.verdict}: {r.reasons[0][:64]}")
nr = AQ.analyse_clip(noisy)
gr = AQ.analyse_clip(good)
_snr = f"{nr.snr_db:.0f}dB" if nr.snr_db is not None else "n/a"
print(f"       noisy    -> {nr.verdict}, SNR {_snr}")
check("noisy clip gets an SNR at all", nr.snr_db is not None, True)
check("clean clip gets an SNR at all", gr.snr_db is not None, True)
check("noisy clip measures worse than the clean one",
      nr.snr_db < gr.snr_db, True)
check("audible room noise is reported to the user",
      any("noise" in x for x in nr.reasons), True)

usable, reports = AQ.screen_clips([good, quiet, short, clipd])
check("screening keeps only the good clip", usable, [good])
usable, _ = AQ.screen_clips([quiet, short, clipd])
check("never rejects EVERY clip (server must still start)", len(usable), 3)

print("\n=== hallucination detection on synthesised output ===")
SR = AQ.SR_OUT
_rng = np.random.default_rng(11)

def speech(seconds, peak=0.6, tail_silence=0.25, seed=None):
    """Speech-like: irregular syllable envelope, varying loudness, jitter —
    deliberately NOT a periodic tone, because real speech is not."""
    rng = np.random.default_rng(seed) if seed is not None else _rng
    n = int(SR*seconds); t = np.arange(n)/SR
    carrier = (np.sin(2*np.pi*135*t) + 0.4*np.sin(2*np.pi*270*t)
               + 0.15*rng.standard_normal(n))
    # Syllables at an uneven rate with uneven loudness — the thing that
    # distinguishes real delivery from a stuck loop.
    env = np.zeros(n)
    pos = 0.0
    while pos < seconds:
        dur = rng.uniform(0.12, 0.34)
        amp = rng.uniform(0.35, 1.0)
        i0, i1 = int(pos*SR), min(n, int((pos+dur)*SR))
        if i1 > i0:
            env[i0:i1] = amp * np.sin(np.linspace(0, np.pi, i1-i0))
        pos += dur + rng.uniform(0.0, 0.09)
    s = carrier * env
    m = np.max(np.abs(s))
    if m > 0: s *= peak/m
    k = int(SR*tail_silence)
    if 0 < k < n: s[-k:] *= np.linspace(1, 0, k)
    return s.astype(np.float32)

TEXT_LEN = 80                      # ~5.6s expected at 0.070 s/char
ok = AQ.check_output(speech(5.6), TEXT_LEN)
check("normal chunk passes", (ok.ok, ok.problem), (True, None))

runaway = AQ.check_output(speech(18.0), TEXT_LEN)
check("runaway detected", runaway.problem, "runaway")
print(f"       {runaway.detail}")

# A true stuck loop: the SAME generated fragment, repeated verbatim.
frag = speech(1.1, tail_silence=0.0, seed=5)
loop = AQ.check_output(np.tile(frag, 6), 200)
check("looping detected", loop.problem, "looping")
print(f"       {loop.detail}")

# Truncation: stops far too early, still at full volume mid-word.
cut = speech(1.2, tail_silence=0.0, seed=7)
cut[-int(SR*0.08):] = 0.6 * np.sin(
    np.linspace(0, 2*np.pi*30, int(SR*0.08))).astype(np.float32)
check("truncation detected", AQ.check_output(cut, TEXT_LEN).problem, "truncated")
check("silence detected",
      AQ.check_output(np.zeros(SR*3, dtype=np.float32), TEXT_LEN).problem, "dead")
check("empty output detected",
      AQ.check_output(np.array([], dtype=np.float32), TEXT_LEN).problem, "dead")

# No false positives across many ordinary chunks, including deliberately
# metronomic delivery — an evenly-read list is the case that naively-tuned
# loop detection destroys.
false_pos = trials = 0
for secs, chars in ((2.0, 28), (4.0, 57), (6.0, 85), (9.0, 128), (12.0, 170)):
    for seed in range(12):
        trials += 1
        r = AQ.check_output(speech(secs, seed=seed), chars)
        if not r.ok:
            false_pos += 1
            print(f"       false positive at {secs}s/{chars}c seed {seed}: "
                  f"{r.problem} — {r.detail}")
check(f"no false positives over {trials} normal chunks", false_pos, 0)

def metronomic(seconds, period=0.5, seed=3):
    """Perfectly even TIMING — a list read at a steady pace — but each beat
    differs in level and pitch, as any real voice does. High correlation,
    genuinely not a loop. This is the case a correlation-only detector ruins."""
    rng = np.random.default_rng(seed)
    n = int(SR*seconds)
    out = np.zeros(n)
    beat = int(SR*period)
    for i0 in range(0, n, beat):
        i1 = min(n, i0 + int(beat*0.8))
        if i1 <= i0:
            continue
        k = i1 - i0
        f = rng.uniform(120, 190)                    # pitch varies per beat
        amp = rng.uniform(0.4, 1.0)                  # level varies per beat
        tt = np.arange(k)/SR
        out[i0:i1] = amp*np.sin(2*np.pi*f*tt)*np.sin(np.linspace(0, np.pi, k))
    return (out/np.max(np.abs(out))*0.6).astype(np.float32)

mets = [AQ.check_output(metronomic(s, seed=sd), int(s/0.070))
        for s in (4.0, 6.0, 9.0) for sd in (1, 2, 3)]
bad = [m for m in mets if not m.ok]
for m in bad:
    print(f"       flagged metronomic reading as {m.problem}: {m.detail}")
check(f"steady-paced reading is not called looping ({len(mets)} cases)",
      len(bad), 0)

print("\n=== benchmark module ===")
import benchmark as B
calls = []
perf = B.measure_inference(lambda text: (calls.append(text), 4.0)[1],
                           emit=lambda l: None,
                           sentences=["one", "two"], warmup=False)
check("timed every sentence", len(perf["runs"]), 2)
check("reported an average RTF", perf["rtf"] is not None, True)
for rtf, want in ((0.3, "comfortable"), (0.8, "good"), (1.5, "marginal"), (3.0, "poor")):
    check(f"RTF {rtf} -> {want}", B.rtf_band(rtf)[0], want)
check("unmeasured RTF is 'unknown'", B.rtf_band(None)[0], "unknown")

print(f"\n{'='*62}\n  {PASS} passed, {FAIL} failed\n{'='*62}")
sys.exit(1 if FAIL else 0)
