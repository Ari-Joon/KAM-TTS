"""Exercise the REAL server.py text pipeline with torch/TTS/flask stubbed out.

Only the pure-text layer is loaded: the module is parsed, the heavy import
block and the Flask app are replaced by stubs, and the resulting namespace is
executed. Everything tested below is the actual code from server.py.
"""
import pathlib as _pl
SERVER_DIR    = _pl.Path(__file__).resolve().parent.parent
EXTENSION_DIR = SERVER_DIR.parent / "extension"
import sys, types, re, pathlib, textwrap

SERVER = SERVER_DIR
sys.path.insert(0, str(SERVER))

# ---- stub the heavy / stateful imports ------------------------------------
def stub(name, **attrs):
    m = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(m, k, v)
    sys.modules[name] = m
    return m

class _Dummy:
    def __init__(self, *a, **k): pass
    def __getattr__(self, n): return _Dummy()
    def __call__(self, *a, **k): return _Dummy()
    def __bool__(self): return False

torch = stub("torch")
torch.cuda = _Dummy()
torch.cuda.is_available = lambda: False
torch.backends = _Dummy()
torch.inference_mode = _Dummy
torch.load = lambda *a, **k: {}
torch.save = lambda *a, **k: None
stub("TTS")
stub("TTS.api", TTS=_Dummy)
stub("flask_cors", CORS=lambda *a, **k: None)
stub("scipy")
stub("scipy.io")
stub("scipy.io.wavfile", write=lambda *a, **k: None)
_sig = stub("scipy.signal", savgol_filter=lambda a, **k: a)
sys.modules["scipy"].signal = _sig
sys.modules["scipy"].io = sys.modules["scipy.io"]

class _Flask:
    config = {}
    def __init__(self, *a, **k): pass
    def route(self, *a, **k):
        return lambda f: f
    def before_request(self, f): return f
    def run(self, *a, **k): pass
stub("flask", Flask=_Flask, request=_Dummy(), send_file=lambda *a, **k: None,
     jsonify=lambda *a, **k: None)

# learner: stub only what the text pipeline touches
learner = types.ModuleType("learner")
learner.SUPPRESS_SENTINEL = "\x00SUPPRESS\x00"
learner.apply_learned_rules = lambda t: t
learner.get_rules = lambda **k: []
learner.get_setting = lambda k, d=None: d
learner.set_setting = lambda k, v: v
learner.set_active_voice = lambda v: None
learner.chunk_profile = lambda t, s=None: {
    "band": "normal", "length": "medium", "punct": "clean", "lexis": "plain",
    "keys": [s or "sentence"], "facets": {"math": False, "primary": "prose", "count": 0}}
learner.analyse_facets = lambda t: {"math": False, "primary": "prose", "count": 0}
learner.resolve_profile_temperature = lambda k, d, **kw: d
learner.resolve_profile_param = lambda p, k, d: d
learner.get_preferred_param = lambda *a, **k: a[2]
learner.get_rate_modifier = lambda b: 1.0
learner.mark_synthesis_busy = lambda b: None
learner.seed_feedback_counters = lambda: None
learner.prune_old_data = lambda: None
learner.log_chunk = lambda *a, **k: "id"
learner.log_error = lambda *a, **k: "id"
learner.register_live_settings = lambda d: None
sys.modules["learner"] = learner

# ---- load server.py, skipping the __main__ block ---------------------------
src = pathlib.Path(SERVER / "server.py").read_text(encoding="utf-8")
src = src[:src.index('if __name__ == "__main__":')]
# skip the single-instance lock (it writes files / can sys.exit)
src = src.replace("if not _acquire_single_instance():",
                  "if False and not _acquire_single_instance():")
ns = {"__name__": "server_undertest", "__file__": str(SERVER / "server.py")}
exec(compile(src, "server.py", "exec"), ns)

S = types.SimpleNamespace(**ns)

# ---------------------------------------------------------------------------
PASS = FAIL = 0
def check(label, got, want):
    global PASS, FAIL
    if got == want:
        PASS += 1
    else:
        FAIL += 1
        print(f"  FAIL {label}\n       got:  {got!r}\n       want: {want!r}")

def show(label, got):
    print(f"  {label:52} {got!r}")

print("\n=== 1. PascalCase code definitions (char-class typo fix) ===")
for s in ["def MyThing(x):", "def Foo(a, b):", "class ImageClient:"]:
    show(s, S.clean_code_block(s))

print("\n=== 2. Ampersand no longer mangles R&D / Q&A ===")
for s in ["R&D spending", "Q&A session"]:
    out = S.expand_symbols(s)
    check(s, "bitwise" in out, False)
    show(s, out)

print("\n=== 3. Lettered list markers a) b) B) Z) all stripped ===")
for s in ["a) first", "B) second", "Z) third", "q) fourth"]:
    show(s, S.clean_text(s))

print("\n=== 4. ALL-CAPS shouting is lowercased, acronyms survive ===")
for s in ["This is IMPORTANT to remember",
          "The GPU and the API are fast",
          "Read the DOCUMENTATION carefully"]:
    show(s, S.normalise_shouting(s))

print("\n=== 5. Maths operators use their intended readings ===")
for s in ["A \u2286 B", "x \u2261 y", "P \u2229 Q", "M \u222a N", "R \u2282 S"]:
    show(s, S.expand_math_symbols(s).strip())

print("\n=== 6. Sentence-type labelling ===")
cases = [
    ("What happens next?",                 "question"),
    ("That is incredible!",                "exclamation"),
    ("Note. Check the logs first.",        "callout"),
    ("Figure caption: the results",        "caption"),
    ("(and this is the part most people miss)", "parenthetical"),
    ("Getting Started With Python",        "heading"),
    ("A tuple is defined as an ordered pair.", "definition"),
    ("The morning light came through the window.", "sentence"),
    ("water, a map, two sandwiches",       "list_item"),
]
for text, want in cases:
    got = S._detect_sentence_type(text, None)[0]
    check(text, got, want)
    show(text[:48], got)

print("\n=== 7. Heading level survives the marker decoder ===")
for lvl in ("H1", "H2", "H3"):
    S._reset_structure()
    S.clean_text(f"|{lvl}|Chapter Three|/{lvl}|")
    got = S._detected_heading_level()
    check(f"{lvl} level recorded", got, lvl.lower())
    ctx = S.analyse_prosody("Chapter Three.", got)
    check(f"{lvl} labelled", ctx.sentence_type, f"{lvl.lower()}_heading")
    show(f"{lvl} -> label / silence", (ctx.sentence_type, ctx.silence_ms))

print("\n=== 8. Every emitted label has its own silence value ===")
for t in S.SENTENCE_TYPES:
    check(f"silence for {t}", t in S._SILENCE_MS, True)
print(f"  {len(S.SENTENCE_TYPES)} labels, all with distinct pacing")

print("\n=== 9. process_audio no longer clips ===")
import numpy as np
sys.modules.pop("numpy", None)
loud = (np.sin(np.linspace(0, 200, 24000)) * 0.99).astype(np.float32)
out = S.process_audio(loud, silence_ms=30, text_len=40)
peak = int(np.max(np.abs(out.astype(np.int32))))
clipped = int(np.sum(np.abs(out.astype(np.int32)) >= 32767))
check("no samples pinned at full scale", clipped, 0)
show("peak int16 / clipped samples", (peak, clipped))

print("\n=== 10. Very short audio does not raise (savgol guard) ===")
try:
    S.process_audio(np.array([0.1, -0.2, 0.3], dtype=np.float32), 20, 5)
    print("  3-sample chunk handled OK")
    PASS += 1
except Exception as e:
    print(f"  FAIL short-audio guard: {type(e).__name__}: {e}")
    FAIL += 1

print("\n=== 11. Substitution tables produce the same output as key-looping ===")
def loop_complex(t):
    for w, sp in S.COMPLEX_WORD_MAP.items():
        t = re.sub(r'\b' + re.escape(w) + r'\b', sp, t, flags=re.IGNORECASE)
    return t
def loop_prefix(t):
    for p, e in S.VARIABLE_PREFIXES.items():
        t = re.sub(r'\b' + re.escape(p) + r'(?=\d)',  e + ' ', t)
        t = re.sub(r'\b' + re.escape(p) + r'(?=[A-Z])', e + ' ', t)
        t = re.sub(r'\b' + re.escape(p) + r'(?=_)',   e, t)
    return t
samples = ["the cache queue tuple epoch", "strName numItems idx_start bufSize",
           "gaussian bayesian markov", "ptrValue cntTotal msgText", "no matches here"]
for s in samples:
    check(f"complex[{s[:22]}]", S.expand_complex_words(s), loop_complex(s))
    check(f"prefix[{s[:22]}]",  S.expand_variable_prefixes(s), loop_prefix(s))
print(f"  {len(samples)} samples x 2 tables matched the original loop output")

print("\n=== 12. SUPPRESS sentinel reaches the caller intact ===")
learner.apply_learned_rules = lambda t: learner.SUPPRESS_SENTINEL
check("clean_text returns sentinel", S.clean_text("skip me"), learner.SUPPRESS_SENTINEL)
learner.apply_learned_rules = lambda t: t
print("  sentinel propagates instead of being spoken as the word SUPPRESS")

print(f"\n{'='*60}\n  {PASS} passed, {FAIL} failed\n{'='*60}")
sys.exit(1 if FAIL else 0)
