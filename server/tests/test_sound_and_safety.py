"""Regression tests for the November sound and safety changes.

Each section names what was wrong. The real server.py, learner.py,
audio_quality.py and speak_order.py are loaded from COPIES in a temp directory,
and the learner builds an empty database of its own there, so nothing here
reads or writes the live tts_quality.db, good_settings.json, either store or
hardware_profile.json.

server.py is loaded the way test_server_fixes.py loads it: torch, TTS, flask
and flask_cors stubbed, register_host replaced, the __main__ block sliced off.
Synthesis is exercised through the real generate_speech with a stand-in for the
model that hands back waveforms made here, so the retry ladder, the token
ceiling, the trim and the superseded checks run as they do in the server.

To check that a section catches its defect, point KAM_SOUND_SRC at a folder
holding the pre-change server.py, learner.py, audio_quality.py and
speak_order.py; the sections then fail.
"""
import pathlib as _pl
SERVER_DIR = _pl.Path(__file__).resolve().parent.parent

import contextlib
import dataclasses
import hashlib
import io
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
import types

import numpy as np

# The code under test prints arrows and stars, so the console has to be UTF-8
# or a print would fail and look like a test failure.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

SRC = _pl.Path(os.environ.get("KAM_SOUND_SRC") or SERVER_DIR)
HERE = _pl.Path(tempfile.mkdtemp(prefix="kam_sound_test_"))
for p in SERVER_DIR.glob("*.py"):
    shutil.copy(p, HERE / p.name)
for name in ("server.py", "learner.py", "audio_quality.py", "speak_order.py"):
    if (SRC / name).exists():
        shutil.copy(SRC / name, HERE / name)
sys.path.insert(0, str(HERE))

PASS = FAIL = 0
SR = 24000


def check(label, got, want):
    global PASS, FAIL
    if got == want:
        PASS += 1
        print(f"  ok   {label}")
    else:
        FAIL += 1
        print(f"  FAIL {label}\n         got:  {got!r}\n         want: {want!r}")


def section(title):
    """Run one block of checks; one that raises counts as a single failure."""
    def wrap(fn):
        global FAIL
        print(f"\n=== {title} ===")
        try:
            fn()
        except Exception as e:
            FAIL += 1
            print(f"  FAIL the section raised {type(e).__name__}: {e}")
        return fn
    return wrap


import learner as L
import audio_quality as AQ

# scipy for real when installed, and before the torch stub, which it trips over.
try:
    import scipy.io.wavfile          # noqa: F401
    import scipy.signal              # noqa: F401
except ImportError:
    _sc = types.ModuleType("scipy")
    _sc.io = types.ModuleType("scipy.io")
    _sc.io.wavfile = types.ModuleType("scipy.io.wavfile")
    _sc.io.wavfile.write = lambda *a, **k: None
    _sc.signal = types.ModuleType("scipy.signal")
    _sc.signal.savgol_filter = lambda a, **k: a
    sys.modules.update({"scipy": _sc, "scipy.io": _sc.io,
                        "scipy.io.wavfile": _sc.io.wavfile, "scipy.signal": _sc.signal})


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
# generate_speech enters this as a context manager, which a _Dummy cannot be.
torch.inference_mode = contextlib.nullcontext
torch.load = lambda *a, **k: {}
torch.save = lambda *a, **k: None
# scipy asks whether an array is a torch tensor by looking this up, and here it
# really filters and writes audio, so the stub has to be able to say no.
torch.Tensor = type("Tensor", (), {})
stub("TTS")
stub("TTS.api", TTS=_Dummy)
stub("flask_cors", CORS=lambda *a, **k: None)


class _Flask:
    config = {}
    def __init__(self, *a, **k): pass
    def route(self, *a, **k): return lambda f: f
    def before_request(self, f): return f
    def run(self, *a, **k): pass


class _Req:
    """Stands in for flask.request. Each call sets the one it wants."""
    def __init__(self, json_body=None, headers=None, method="POST", path="/"):
        self._j = json_body
        self.headers = headers or {}
        self.method, self.path = method, path
        self.args, self.form, self.files = {}, {}, {}
    @property
    def json(self): return self._j
    def get_json(self, *a, **k): return self._j


class _Resp:
    """What send_file returns: something a header can be set on."""
    def __init__(self, body, mimetype):
        self.body, self.mimetype, self.headers = body, mimetype, {}


stub("flask", Flask=_Flask, request=_Req(),
     send_file=lambda b, **k: _Resp(b, k.get("mimetype")),
     jsonify=lambda *a, **k: (a[0] if a else k))
stub("register_host", manifest_path=lambda: str(HERE / "no_manifest.json"),
     ensure_registered=lambda **k: None)

src = (HERE / "server.py").read_text(encoding="utf-8")
body = src[:src.index('if __name__ == "__main__":')]
body = body.replace("if not _acquire_single_instance():",
                    "if False and not _acquire_single_instance():")
ns = {"__name__": "server_undertest", "__file__": str(HERE / "server.py")}
exec(compile(body, str(HERE / "server.py"), "exec"), ns)

LIVE = ns["_LIVE_SETTINGS"]
LIVE.update({"temperature": 0.45, "speed": 1.0})


def call(fn, *args, json_body=None, **req):
    """Run a route with a request, and return (body, status)."""
    ns["request"] = _Req(json_body, **req)
    r = fn(*args)
    return r if isinstance(r, tuple) else (r, 200)


def store():
    return L._load_good_settings()


def put(key, entry):
    """Write one good_settings entry the way the learner's own writers do."""
    with L._good_settings_lock:
        s = dict(L._load_good_settings())
        s[key] = entry
        L._good_settings_cache = s
        L._write_json_atomic(L._good_settings_path(), s, indent=2)


def db(sql, args=()):
    conn = sqlite3.connect(str(L.DB_PATH))
    try:
        rows = conn.execute(sql, args).fetchall()
        conn.commit()
        return rows
    finally:
        conn.close()


def keys(stype, profile):
    return L._profile_keys_from_parts(stype, *profile.split("|"), False)


def logged(text, temp, stype="sentence", profile="normal|medium|clean|plain", dial=None):
    """Log a chunk the way /speak does, so it has a stored profile."""
    sp = {"temperature": temp, "top_p": 0.9, "top_k": 45,
          "repetition_penalty": 4.1, "speed": 1.0}
    if dial is not None:
        sp["temperature_dial"] = dial
    return L.log_chunk(text, stype, 30, synth_params=sp, profile_str=profile)


def speech(seconds, lead=0.0, tail=0.0, seed=3):
    """Speech-like audio with exact near-silence before and after it: uneven
    syllables on a voiced carrier, not a tone, because real speech is not."""
    rng = np.random.default_rng(seed)
    n = int(SR * seconds)
    t = np.arange(n) / SR
    carrier = (np.sin(2 * np.pi * 135 * t) + 0.4 * np.sin(2 * np.pi * 270 * t)
               + 0.15 * rng.standard_normal(n))
    env = np.zeros(n)
    pos = 0.0
    while pos < seconds:
        dur = min(rng.uniform(0.12, 0.34), seconds - pos)
        i0, i1 = int(pos * SR), min(n, int((pos + dur) * SR))
        if i1 > i0:
            env[i0:i1] = rng.uniform(0.35, 1.0) * np.sin(np.linspace(0, np.pi, i1 - i0))
        pos += dur + rng.uniform(0.0, 0.05)
    s = carrier * env
    s *= 0.6 / np.max(np.abs(s))
    quiet = lambda secs: 1e-4 * rng.standard_normal(int(SR * secs))
    return np.concatenate([quiet(lead), s, quiet(tail)]).astype(np.float32)


def edges_ms(pcm):
    """(lead, tail) silence of processed audio in ms, at the trim's own floor."""
    a = np.abs(np.asarray(pcm, dtype=np.float64))
    loud = np.flatnonzero(a > a.max() * 0.01)
    return (round(loud[0] / SR * 1000), round((len(a) - 1 - loud[-1]) / SR * 1000))


class FakeModel:
    """Stands in for XTTS: returns the waveforms it was given, in order, the
    last one again once they run out, and records how it was called. hook runs
    inside each inference with the call number, where a stop would land."""
    def __init__(self, *wavs, hook=None):
        self.wavs, self.calls, self.hook = list(wavs), [], hook
    def inference(self, **kw):
        self.calls.append(kw)
        if self.hook:
            self.hook(len(self.calls))
        return {"wav": self.wavs[min(len(self.calls), len(self.wavs)) - 1]}


def use_model(model):
    ns["tts"] = types.SimpleNamespace(synthesizer=types.SimpleNamespace(tts_model=model))
    ns["_model_state"] = "ready"
    return model


def rejected_rows():
    return db("SELECT COUNT(*) FROM param_observations WHERE rejected=1")[0][0]


# ---------------------------------------------------------------------------
@section("24. a request not addressed to this server is refused before anything else")
def _():
    tok = ns["KAM_TOKEN"]
    def ask(host, path="/speak", method="POST", token=True):
        h = {} if host is None else {"Host": host}
        if token:
            h["X-KAM-Token"] = tok
        ns["request"] = _Req(headers=h, method=method, path=path)
        r = ns["_require_token"]()
        return None if r is None else r[1]

    port = ns["KAM_PORT"]
    for host in (f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}", f"LocalHost:{port}"):
        check(f"{host} is this server", ask(host), None)
    check("a rebinding page's own hostname is refused", ask(f"evil.example:{port}"), 403)
    check("even with the right token", ask(f"evil.example:{port}", token=True), 403)
    check("and on /token, which needs no token", ask("evil.example", "/token", "GET", False), 403)
    check("and on /health", ask("evil.example", "/health", "GET", False), 403)
    check("and on a preflight", ask("evil.example", "/speak", "OPTIONS", False), 403)
    check("no Host at all is refused", ask(None, "/token", "GET", False), 403)
    check("the right name on another port is refused", ask(f"127.0.0.1:{port + 1}"), 403)
    check("a name that only starts like it is refused", ask(f"127.0.0.1:{port}.evil.example"), 403)
    check("the extension still gets the token", ask(f"127.0.0.1:{port}", "/token", "GET", False), None)
    check("and a missing token is still refused", ask(f"127.0.0.1:{port}", "/speak", "POST", False), 403)


@section("8. the temperature slider is a dial over what was learned")
def _():
    prof = "normal|medium|clean|plain"
    k = keys("sentence", prof)
    put(L._vk(f"prof:{k[0]}"), {"temperature": 0.30, "count": 5})
    check("before any server has registered there is no anchor", L.temperature_anchor(), None)
    check("so nothing is scaled", L.resolve_profile_temperature(k, 0.90), 0.30)

    # What __main__ does once startup is over: hand the learner the live dict.
    L.register_live_settings(LIVE)
    check("registering fixes the anchor where the slider stands", L.temperature_anchor(), 0.45)
    check("and logs it once",
          db("SELECT COUNT(*) FROM history WHERE event_type='MIGRATION' "
             "AND detail LIKE '%anchor fixed at 0.45%'")[0][0], 1)
    check("with the slider at the anchor nothing changes",
          L.resolve_profile_temperature(k, 0.45), 0.30)
    check("turning it up scales the learned value", L.resolve_profile_temperature(k, 0.60), 0.40)
    check("turning it down scales it too", L.resolve_profile_temperature(k, 0.225), 0.15)
    put(L._vk("question"), {"temperature": 0.32, "confirm_count": 4})
    check("the per-type fallback is scaled the same way",
          L.resolve_profile_temperature(keys("question", prof), 0.60), round(0.32 * 0.60 / 0.45, 3))
    check("a kind of text nothing was learned for gets the slider itself",
          L.resolve_profile_temperature(keys("caption", prof), 0.60), 0.60)
    put(L._vk("prof:exclamation"), {"temperature": 0.90, "count": 3})
    check("the result is kept under 0.95",
          L.resolve_profile_temperature(["exclamation"], 0.90), 0.95)
    put(L._vk("prof:imperative"), {"temperature": 0.06, "count": 3})
    check("and over 0.05", L.resolve_profile_temperature(["imperative"], 0.05), 0.05)

    L.register_live_settings({"temperature": 0.90})
    check("a later registration never moves the anchor", L.temperature_anchor(), 0.45)
    L.register_live_settings(LIVE)

    # Learning at a scaled slider stores the value with the dial taken out.
    LIVE["temperature"] = 0.60
    dial = L.temperature_dial()
    check("the dial is live over anchor", dial, round(0.60 / 0.45, 4))
    fp = "simple|long|commas|symbolic"
    cid = logged("A sentence the user loved hearing read aloud.", 0.40, "definition", fp, dial)
    kd = keys("definition", fp)
    L.confirm_chunk_quality(cid)
    L.confirm_chunk_quality(cid)
    e = store().get(L._vk(f"prof:{kd[0]}")) or {}
    check("a thumbs-up on a chunk made at 0.40 under the dial learns 0.30", e.get("temperature"), 0.30)
    check("the per-type entry learns the same 0.30",
          (store().get(L._vk("definition")) or {}).get("temperature"), 0.30)
    check("and it comes back out as 0.40 at that dial", L.resolve_profile_temperature(kd, 0.60), 0.40)
    check("or as 0.30 with the slider back at the anchor", L.resolve_profile_temperature(kd, 0.45), 0.30)

    cid2 = logged("Another sentence of the same kind, heard and fine.", 0.48, "definition", fp, dial)
    L.mark_session_solid(played=[cid2])
    check("solid pulls toward 0.36, the 0.48 with the dial out",
          (store().get(L._vk(f"prof:{kd[0]}")) or {}).get("temperature"),
          round(0.30 * 0.92 + 0.36 * 0.08, 3))

    # The slider moves between the chunk being made and the verdict on it.
    fp2 = "dense|long|commas|technical"
    cid3 = logged("Made while the slider was turned up, rated after.", 0.40, "definition", fp2, dial)
    LIVE["temperature"] = 0.45
    L.confirm_chunk_quality(cid3)
    check("the chunk's own dial is used, not the slider's position at the verdict",
          (store().get(L._vk(f"prof:{keys('definition', fp2)[0]}")) or {}).get("temperature"), 0.30)
    old = logged("A chunk from before the dial, with no factor on its row.", 0.27, "heading",
                 "simple|short|clean|plain")
    L.confirm_chunk_quality(old)
    check("a row with no dial was made with none",
          (store().get(L._vk(f"prof:{keys('heading', 'simple|short|clean|plain')[0]}")) or {})
          .get("temperature"), 0.27)

    # Steps are steps in learned units, whatever the dial says.
    LIVE["temperature"] = 0.60
    _key, new_t = L._adjust_profile_temperature(k, -0.03)
    check("a 0.03 step moves the learned value by 0.03", new_t, 0.27)
    check("which is heard through the dial", L.resolve_profile_temperature(k, 0.60), 0.36)
    kc = keys("caption", "normal|short|clean|plain")
    _key, new_t = L._adjust_profile_temperature(kc, +0.03, report=True)
    check("a new key starts from the slider in learned units, the anchor", new_t, 0.48)
    check("so 'more expressive' lands just above the slider",
          L.resolve_profile_temperature(kc, 0.60), 0.64)
    check("the per-type entry starts from the anchor too",
          L._adjust_pref_temperature("callout", -0.03), 0.42)

    d = L.diagnose_chunk(chunk_id=cid)
    t = d.get("temperature") or {}
    check("diagnose shows the temperature used and the dial it was made under",
          (t.get("used"), t.get("dial_when_made")), (0.40, dial))
    check("the learned value behind it", t.get("used_in_learned_units"), 0.30)
    check("and the dial as it stands now", (t.get("anchor"), t.get("live"), t.get("dial_now")),
          (0.45, 0.60, dial))
    check("the source line names both", "x dial" in (d.get("param_sources") or {}).get("temperature", ""), True)

    out, code = call(ns["get_settings"], method="GET", path="/settings")
    check("GET /settings still carries the live values", (code, out.get("temperature"), out.get("speed")),
          (200, 0.60, 1.0))
    check("with the anchor and the factor", (out.get("temperature_anchor"), out.get("temperature_dial")),
          (0.45, dial))
    ex = out.get("temperature_example") or {}
    check("and a worked example", ex.get("effective"),
          L.effective_temperature(ex.get("learned"), dial) if ex else "an example")
    check("the live dict itself is not given the extra keys", "temperature_anchor" in LIVE, False)
    LIVE["temperature"] = 0.45


@section("10. one thumbs-up cannot swing a value three hundred built")
def _():
    a = L._approval_alpha
    check("the first samples weigh the old 0.3", (a({}), a({"confirm_count": 2})), (0.3, 0.3))
    check("then 1/(n+1)", a({"count": 3}), 0.25)
    check("down to a floor of 0.05", (a({"confirm_count": 19}), a({"confirm_count": 310})), (0.05, 0.05))
    check("solid starts at the old 0.08", round(a({}, L._SOLID_ALPHA), 6), 0.08)
    check("and is gentler than a thumbs-up at every n",
          all(a({"count": n}, L._SOLID_ALPHA) < a({"count": n}) for n in range(0, 600)), True)

    L.set_active_voice("alpha_test")
    put(L._vk("mid_clause"), {"temperature": 0.293, "confirm_count": 310})
    L.record_good_settings("mid_clause", {"temperature": 0.15, "top_p": 0.8, "top_k": 50,
                                          "repetition_penalty": 4.5})
    check("a cold thumbs-up against 310 confirmations moves 0.293 to 0.286, not 0.250",
          store()[L._vk("mid_clause")]["temperature"], round(0.293 * 0.95 + 0.15 * 0.05, 3))
    put(L._vk("heading"), {"temperature": 0.30, "confirm_count": 1})
    L.record_good_settings("heading", {"temperature": 0.20})
    check("against one confirmation it still moves the old way",
          store()[L._vk("heading")]["temperature"], round(0.30 * 0.7 + 0.20 * 0.3, 3))

    kk = keys("mid_clause", "normal|medium|commas|plain")
    put(L._vk(f"prof:{kk[0]}"), {"temperature": 0.30, "count": 300})
    put(L._vk(f"prof:{kk[1]}"), {"temperature": 0.30, "count": 2})
    L._reinforce_profile_temperatures([(kk, 0.10)], L._THUMBS_UP_ALPHA, create=True)
    check("a profile key with 300 behind it moves by a twentieth of the gap",
          store()[L._vk(f"prof:{kk[0]}")]["temperature"], 0.29)
    check("a thin coarser key still moves by its damped share",
          store()[L._vk(f"prof:{kk[1]}")]["temperature"], round(0.30 - 0.20 * 0.15, 3))
    L._reinforce_profile_temperatures([(kk, 0.10)], L._SOLID_ALPHA, create=False)
    check("and solid on the heavy key moves it less than a thumbs-up did",
          0.29 - store()[L._vk(f"prof:{kk[0]}")]["temperature"] < 0.01, True)
    L.set_active_voice("default")


@section("9. speed is the live speed times the band, and nothing else")
def _():
    check("the tuner no longer has speed to tune", "speed" in L._AUTOTUNE_PARAMS, False)
    put("prof:dense|short|clean|plain", {"temperature": 0.31, "top_p": 0.91, "speed_mod": 1.39})
    put("prof:dense|medium|complex|technical", {"speed_mod": 1.37})
    put("v:other:prof:normal|long|clean|plain", {"top_p": 0.89, "speed_mod": 1.14})
    put("prof:sentence|normal", {"temperature": 0.31, "count": 9})
    put("_autotune_ckpt", {
        "dense|short|clean|plain::speed": {"profile": "dense|short|clean|plain", "param": "speed",
                                           "before": 1.38, "q_before": 0.89, "ts": 1.0},
        "dense|short|clean|plain::top_p": {"profile": "dense|short|clean|plain", "param": "top_p",
                                           "before": 0.9, "q_before": 0.89, "ts": 1.0}})
    db("DELETE FROM counters WHERE name=?", (L._SPEED_RETIRE_MARKER,))
    before_other = dict(store()["prof:sentence|normal"])
    res = L._retire_speed_mods()
    check("it reports the three trims it moved", res["profiles"],
          {"prof:dense|short|clean|plain": 1.39, "prof:dense|medium|complex|technical": 1.37,
           "v:other:prof:normal|long|clean|plain": 1.14})
    s = store()
    check("no profile entry carries a speed trim any more",
          [k for k, v in s.items() if isinstance(v, dict) and "speed_mod" in v], [])
    check("the rest of each entry is untouched", s["prof:dense|short|clean|plain"],
          {"temperature": 0.31, "top_p": 0.91})
    check("an entry with no trim is not rewritten", s["prof:sentence|normal"], before_other)
    check("the old values are kept where they can be put back",
          s[L._RETIRED_SPEED_KEY]["profiles"], res["profiles"])
    check("the open speed checkpoint is dropped and kept too",
          (list(s["_autotune_ckpt"]), list(s[L._RETIRED_SPEED_KEY]["checkpoints"])),
          (["dense|short|clean|plain::top_p"], ["_autotune_ckpt/dense|short|clean|plain::speed"]))
    check("it is in the history",
          db("SELECT COUNT(*) FROM history WHERE event_type='MIGRATION' "
             "AND detail LIKE 'removed 3 per-profile speed trim%'")[0][0], 1)
    check("and it runs once", L._retire_speed_mods(), None)
    check("what is on disk is what is in memory",
          json.loads((HERE / "good_settings.json").read_text(encoding="utf-8"))[L._RETIRED_SPEED_KEY],
          s[L._RETIRED_SPEED_KEY])
    check("the tuning panel does not list the retired record as a learned type",
          any(k.startswith("_") for k in L.get_learned_summary()["types"]), False)

    # A trim written since, by hand or by an old build, is not read by synthesis.
    text = "The scheduler wakes each worker in turn."
    model = use_model(FakeModel(speech(2.4, tail=0.15)))
    wav, ctx = ns["generate_speech"](text)
    put(f"prof:{ctx.profile_str}", {"speed_mod": 1.39})
    put(f"band:{ctx.profile['band']}", {"rate_mod": 1.1})
    wav, ctx = ns["generate_speech"](text)
    check("a stored trim is ignored: speed is live x band", ctx.used_params["speed"], 1.1)
    check("and that is the speed the model was given", model.calls[-1]["speed"], 1.1)
    put(f"band:{ctx.profile['band']}", {"rate_mod": 1.0})


@section("11. a clean short heading is not a runaway, and a real one is cut off early")
def _():
    # 1.3 s of speech for nine characters, with the padding XTTS adds: what
    # "Overview." really takes. The limit was 1.04 s of everything.
    ok = AQ.check_output(speech(1.3, lead=0.02, tail=0.15), 9, 1.15)
    check("a heading said at a natural pace passes", (ok.ok, ok.problem), (True, None))
    check("the padding is not counted as speech", round(ok.spoken_s, 1), 1.3)
    ran = AQ.check_output(speech(4.4, tail=0.2), 8, 1.15)
    check("one that ran on to 4.4 s is still caught", ran.problem, "runaway")
    check("a long chunk is judged as before",
          (AQ.check_output(speech(5.6), 80).ok, AQ.check_output(speech(18.0), 80).problem),
          (True, "runaway"))
    check("the limit is the old multiple plus half a second",
          round(AQ.runaway_limit_s(100, 1.0), 2), round(100 * 0.07 * 1.9 + 0.5, 2))

    for n in (8, 20, 60, 120, 190):
        want = 2.9 * n + 20
        check(f"the token ceiling for {n} characters is about 2.9n + 20",
              abs(AQ.token_ceiling(n, 1.0) - want) <= max(6, want * 0.03), True)
    check("it is always above what the check would reject",
          all(AQ.token_ceiling(n, s) / AQ._TOKENS_PER_S / s > AQ.runaway_limit_s(n, s)
              for n in range(3, 150) for s in (0.7, 1.0, 1.15, 1.6)), True)
    check("and never above the library's own limit", max(AQ.token_ceiling(n, 2.0) for n in range(3, 400)) <= 605, True)
    cap = AQ.token_ceiling(9, 1.0)
    full = int(cap * SR / AQ._TOKENS_PER_S)
    check("a waveform as long as its ceiling allows was stopped by it",
          (AQ.hit_token_ceiling(full, cap, 1.0), AQ.hit_token_ceiling(full // 2, cap, 1.0)), (True, False))
    padded = speech(1.3, tail=full / SR - 1.3)[:full]
    check("which makes it a runaway however it looks",
          (AQ.check_output(padded, 9, 1.0).ok, AQ.check_output(padded, 9, 1.0, capped=True).problem),
          (True, "runaway"))

    # Through the real synthesis loop.
    model = use_model(FakeModel(speech(1.3, lead=0.02, tail=0.15)))
    wav, ctx = ns["generate_speech"]("Overview.")
    check("the heading is made once and not rejected", (len(model.calls), ctx.rejected), (1, 0))
    kw = model.calls[0]
    check("generation is given a ceiling for its text and speed",
          kw.get("max_new_tokens"), AQ.token_ceiling(len(kw["text"]), kw["speed"]))

    seen = rejected_rows()
    model = use_model(FakeModel(speech(4.4, tail=0.2)))
    saved = L.resolve_profile_temperature
    try:
        L.resolve_profile_temperature = lambda *a, **k: 0.12
        wav, ctx = ns["generate_speech"]("Summary.")
        temps = [c["temperature"] for c in model.calls]
        check("a chunk that starts at 0.12 is never retried warmer", temps, [0.12, 0.12, 0.12])
        check("only its repetition penalty rises",
              [c["repetition_penalty"] for c in model.calls] ==
              sorted({c["repetition_penalty"] for c in model.calls}), True)
        check("it is sent after three attempts, marked as flawed",
              (ctx.rejected, (ctx.failure or {}).get("type")), (3, "runaway"))
        check("and every rejected attempt is kept as evidence", rejected_rows() - seen, 3)

        L.resolve_profile_temperature = lambda *a, **k: 0.45
        model = use_model(FakeModel(speech(4.4, tail=0.2), speech(4.4, tail=0.2),
                                    speech(1.2, tail=0.15)))
        wav, ctx = ns["generate_speech"]("Summary.")
        temps = [c["temperature"] for c in model.calls]
        check("a warm chunk still cools on each retry", temps == sorted(temps, reverse=True)
              and temps[0] == 0.45 and temps[-1] >= 0.15 and len(set(temps)) == 3, True)
        check("and the attempt that passed is the one sent", (ctx.rejected, ctx.failure), (2, None))
    finally:
        L.resolve_profile_temperature = saved
    check("the retry rule itself", (ns["_retry_temperature"](0.12, 0.12), ns["_retry_temperature"](0.2, 0.45),
                                    ns["_retry_temperature"](0.45, 0.45) < 0.45), (0.12, 0.15, True))


@section("12. a chunk that ends mid-sentence carries no dead air")
def _():
    raw = speech(2.0, lead=0.10, tail=0.18)
    raw_lead, raw_tail = edges_ms(raw)
    near = lambda got, want: abs(got - want) <= 3
    check("the test audio has the dead air the model produces",
          (100 <= raw_lead <= 120, 180 <= raw_tail <= 200), (True, True))
    P = ns["process_audio"]
    lead, tail = edges_ms(P(raw, silence_ms=4, text_len=40, sentence_type="mid_clause"))
    check("a mid-clause chunk keeps about 20 ms before the speech", near(lead, 20), True)
    check("and about 40 ms after it, plus its 4 ms gap", near(tail, 44), True)
    lead, tail = edges_ms(P(raw, silence_ms=8, text_len=40, sentence_type="parenthetical"))
    check("a parenthetical is trimmed the same way", (near(lead, 20), near(tail, 48)), (True, True))
    lead, tail = edges_ms(P(raw, silence_ms=35, text_len=40, sentence_type="sentence"))
    check("a sentence keeps its natural pause",
          (near(lead, raw_lead), near(tail, raw_tail + 35)), (True, True))
    check("and with no type given it is the same as before",
          edges_ms(P(raw, silence_ms=35, text_len=40)), (lead, tail))

    # Nothing above the floor is cut: the speech is as long after as before.
    def spoken(a):
        a = np.abs(np.asarray(a, dtype=np.float64))
        loud = np.flatnonzero(a > a.max() * 0.01)
        return int(loud[-1] - loud[0])
    kept = spoken(P(raw, silence_ms=4, text_len=40, sentence_type="mid_clause"))
    check("the trim removes silence only, never the end of a word", abs(kept - spoken(raw)) <= 8, True)
    out = P(raw, silence_ms=4, text_len=40, sentence_type="mid_clause")
    check("the cut ends in a fade, not a step", int(abs(int(out[-1 - int(SR * 0.004)]))) <= 3, True)

    head = speech(1.35, lead=0.02, tail=0.15)
    out = P(head, silence_ms=62, text_len=9, sentence_type="heading", speed=1.15)
    check("a one-word heading that passed the check is not cut short either",
          abs(spoken(out) - spoken(head)) <= 8, True)
    long = P(speech(6.0), silence_ms=35, text_len=9, sentence_type="heading", speed=1.0)
    check("while a real runaway is still cut to its limit",
          len(long) / SR <= AQ.runaway_limit_s(9, 1.0) + 0.05, True)


@section("13 and 15. the starting chunk is marked, and a replay hears the new settings")
def _():
    asked, made = [], []

    class Order:
        def acquire(self, epoch=None, index=None, head=False):
            asked.append((epoch, index, head))
            return True
        def release(self, epoch=None): pass
        def is_superseded(self, epoch): return False

    def fake_synth(text, raw, position, chunk_no, dedup_key, read=None):
        made.append(dedup_key)
        with ns["_dedup_lock"]:
            ns["_dedup_cache"][dedup_key] = (time.time(), b"RIFFold")
        return "SYNTH"

    saved = ns["_speak_order"], ns["_synthesise_and_log"]
    ns["_speak_order"], ns["_synthesise_and_log"] = Order(), fake_synth
    try:
        line = "The reader starts here and carries on down the page."
        req = {"text": line, "read_id": "r1", "index": 4, "read_total": 9, "epoch": 71}
        call(ns["speak"], json_body=dict(req, head=True))
        check("head: true reaches the turnstile", asked[-1], (71.0, 4, True))
        call(ns["speak"], json_body=dict(req, text="Its sibling, one chunk further on.", index=5))
        check("a sibling is not a head", asked[-1], (71.0, 5, False))

        n = len(made)
        r, _code = call(ns["speak"], json_body=req)
        check("a replay inside the window is served from the cache", (len(made), r.body.getvalue()),
              (n, b"RIFFold"))
        LIVE["speed"] = 1.3
        check("after a speed change it is synthesised again", call(ns["speak"], json_body=req)[0], "SYNTH")
        ns["_ACTIVE_VOICE"] = "someone_else"
        check("after a voice change too", call(ns["speak"], json_body=req)[0], "SYNTH")
        put(L._vk("band:normal"), {"rate_mod": 0.93})
        put(L._vk("band:simple"), {"rate_mod": 0.93})
        put(L._vk("band:dense"), {"rate_mod": 0.93})
        check("and after a too-fast report changed the pace", call(ns["speak"], json_body=req)[0], "SYNTH")
        check("each of those was a different cache entry", len(set(made[n - 1:])), 4)
        check("with nothing changed it is cached again", len(made) == n + 3
              and call(ns["speak"], json_body=req)[0] != "SYNTH", True)
    finally:
        ns["_speak_order"], ns["_synthesise_and_log"] = saved
        ns["_ACTIVE_VOICE"] = "default"
        LIVE["speed"] = 1.0
        for b in ("normal", "simple", "dense"):
            put(L._vk(f"band:{b}"), {"rate_mod": 1.0})
        ns["_dedup_cache"].clear()


@section("14. a stop reaches the chunk that is being synthesised")
def _():
    order = ns["_speak_order"]
    text = "Summary."
    cid = hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]

    # Stopped during the first attempt, which would otherwise be retried twice.
    order.acquire(100, 0, head=True)
    model = use_model(FakeModel(speech(4.4, tail=0.2), hook=lambda n: order.cancel(100)))
    seen = rejected_rows()
    try:
        ns["generate_speech"](text, None, 100)
        raised = False
    except ns["_Superseded"]:
        raised = True
    finally:
        order.release(100)
    check("synthesis gives up instead of retrying", (raised, len(model.calls)), (True, 1))
    check("the fragment is not scored or recorded as a rejection", rejected_rows(), seen)

    def run(epoch, model):
        read = {"id": "r14", "index": 0, "total": 3, "ts": 1.0, "epoch": epoch, "head": True}
        order.acquire(epoch, 0, head=True)
        try:
            ns["request"] = _Req({})
            r = ns["_synthesise_and_log"](text, text, None, 0, "key14", read)
        finally:
            order.release(epoch)
        return r if isinstance(r, tuple) else (r, 200)

    queued = L.analysis_backlog()["queued"]
    body, code = run(200, use_model(FakeModel(speech(1.2, tail=0.15), hook=lambda n: order.cancel(200))))
    check("the request gets the superseded reply", (body, code), ({"superseded": True}, 409))
    check("no chunk is logged", db("SELECT COUNT(*) FROM chunks WHERE id=?", (cid,))[0][0], 0)
    check("nothing is queued for listen-back", L.analysis_backlog()["queued"], queued)
    check("and the audio is not left in the dedup cache", "key14" in ns["_dedup_cache"], False)

    # Stopped in the instant after the last attempt returned.
    real_busy = L.mark_synthesis_busy
    def busy(flag):
        real_busy(flag)
        if not flag:
            order.cancel(300)
    L.mark_synthesis_busy = busy
    try:
        body, code = run(300, use_model(FakeModel(speech(1.2, tail=0.15))))
    finally:
        L.mark_synthesis_busy = real_busy
    check("a stop that lands just as synthesis finishes is honoured too", code, 409)
    check("with nothing logged then either",
          (db("SELECT COUNT(*) FROM chunks WHERE id=?", (cid,))[0][0], "key14" in ns["_dedup_cache"]),
          (0, False))

    resp, code = run(400, use_model(FakeModel(speech(1.2, tail=0.15))))
    check("a chunk nobody stopped is made, logged and cached as ever",
          (code, resp.headers.get("X-Chunk-Id"), "key14" in ns["_dedup_cache"],
           db("SELECT COUNT(*) FROM chunks WHERE id=?", (cid,))[0][0]), (200, cid, True, 1))
    check("a request with no epoch can never be superseded", order.is_superseded(None), False)
    check("and gets no stopping criterion", ns["_stop_when_superseded"](None), {})
    check("one with an epoch gets keyword arguments, or none where transformers is absent",
          set(ns["_stop_when_superseded"](400)) <= {"stopping_criteria"}, True)
    ns["_dedup_cache"].clear()


@section("16. standby suits the machine it is on")
def _():
    path = _pl.Path(ns["_hw_profile_path"]())
    ns["_IDLE_TIMEOUT_SEC"] = 999
    ns["_apply_hardware_adaptation"]()
    check("with no accelerator the model is never evicted by default", ns["_IDLE_TIMEOUT_SEC"], 0)

    cpu = ns["DEV"]
    ns["DEV"] = dataclasses.replace(cpu, torch_device="cuda", backend="cuda", name="Test GPU", vram_gb=24.0)
    try:
        facts = ns["_detect_machine_now"]()
        path.write_text(json.dumps(dict(facts, rtf=0.5, source="server", load_s=None, warm_s=None)),
                        encoding="utf-8")
        ns["_apply_hardware_adaptation"]()
        check("a GPU with no timings on file gets the 30 minute default", ns["_IDLE_TIMEOUT_SEC"], 1800)

        ns["_STARTUP_TIMINGS"].update({"load_s": 21.5, "latent_s": 0.1, "warm_s": 9.25})
        check("the measured startup timings are written to the profile", ns["_record_startup_timings"](), True)
        hp = json.loads(path.read_text(encoding="utf-8"))
        check("all three of them", (hp["load_s"], hp["latent_s"], hp["warm_s"]), (21.5, 0.1, 9.25))
        check("and the rest of the profile is as it was", (hp["rtf"], hp["gpu"]), (0.5, "Test GPU"))
        check("no temp file is left beside it",
              [f.name for f in HERE.iterdir() if f.name.startswith("hardware_profile.json.")], [])
        check("writing the same timings again changes nothing", ns["_record_startup_timings"](), False)
        ns["_apply_hardware_adaptation"]()
        check("so a slow wake now earns the long timeout", ns["_IDLE_TIMEOUT_SEC"], 7200)

        path.write_text(json.dumps(dict(facts, gpu="Some Other GPU", load_s=None)), encoding="utf-8")
        check("a profile from another machine is left alone", ns["_record_startup_timings"](), False)
    finally:
        ns["DEV"] = cpu
        ns["_STARTUP_TIMINGS"].update({"load_s": None, "latent_s": None, "warm_s": None})

    # Eviction waits for the synthesis in flight.
    ns["tts"], ns["_model_state"] = object(), "ready"
    ns["_inference_lock"].acquire()
    t = threading.Thread(target=ns["evict_model"])
    t.start()
    time.sleep(0.3)
    held = ns["_model_state"]
    ns["_inference_lock"].release()
    t.join(5)
    check("the model is not dropped while an inference holds the lock", held, "ready")
    check("and is once it lets go", (ns["_model_state"], ns["tts"]), ("standby", None))

    ns["tts"], ns["_model_state"] = object(), "ready"
    ns["_IDLE_TIMEOUT_SEC"] = 600
    ns["_touch_synth_activity"]()
    ns["evict_model"](idle_only=True)
    check("the idle monitor backs off when a request has just arrived", ns["_model_state"], "ready")
    ns["_last_synth_ts"] = time.time() - 601
    ns["evict_model"](idle_only=True)
    check("and evicts when the model really is idle", ns["_model_state"], "standby")
    ns["_IDLE_TIMEOUT_SEC"] = 0


@section("25. a private read leaves no text behind")
def _():
    secret = "The quokka ledger stayed hidden in Zanzibar all winter."
    words = ("quokka", "zanzibar", "ledger")
    leaked = lambda blob: [w for w in words if w in str(blob).lower()]
    seen, console0 = [], len(ns["_console_log"])
    real_put = L._analysis_queue.put_nowait
    L._analysis_queue.put_nowait = lambda item: (seen.append(item), real_put(item))[1]
    # Every chunk is listened back to here; the standby section's CPU defaults
    # had turned that down to one in four.
    ns["_ANALYSIS_ENABLED"], ns["_ANALYSIS_EVERY"] = True, 1
    use_model(FakeModel(speech(3.2, tail=0.15)))
    out = io.StringIO()
    req = {"text": secret, "private": True, "read_id": "p1", "index": 0, "read_total": 1,
           "read_ts": time.time(), "epoch": 500, "head": True}
    try:
        with contextlib.redirect_stdout(out):
            resp, code = call(ns["speak"], json_body=req)
            again, _c = call(ns["speak"], json_body=req)        # served from the dedup cache
    finally:
        L._analysis_queue.put_nowait = real_put
    cid = resp.headers.get("X-Chunk-Id")
    check("the chunk is spoken", (code, bool(cid)), (200, True))
    row = db("SELECT text, private, char_count, success FROM chunks WHERE id=?", (cid,))[0]
    check("its row has no text and says why", row[:2], ("", 1))
    check("but still counts as a chunk of that length", (row[2] > 40, row[3]), (True, 1))
    check("its id is the hash of the real spoken text, so feedback finds it",
          cid, hashlib.sha1(seen[-1][2].strip().encode("utf-8")).hexdigest()[:16])
    check("analysis gets the text in memory, marked private",
          (leaked(seen[-1][2]) != [], seen[-1][3]), (True, True))
    printed = out.getvalue()
    check("nothing printed carries the words", leaked(printed), [])
    check("nor does the console buffer the dashboard reads",
          leaked(list(ns["_console_log"])[-(len(ns["_console_log"]) - console0 or 1):] + [printed]), [])
    check("the lines say (private) instead", ("(private)" in printed, "[DEDUP] cached: (private)" in printed),
          (True, True))
    check("the history has none of it", leaked(db("SELECT detail FROM history")), [])
    feed = [r for r in L.get_chunk_feed(50) if r["chunk_id"] == cid][0]
    check("the feed marks the row private, with no text", (feed["private"], feed["chunk_text"]), (1, ""))
    check("and diagnose says so", (L.diagnose_chunk(chunk_id=cid)["private"], L.diagnose_chunk(chunk_id=cid)["text"]),
          (True, ""))

    # Feedback still works on it, by id.
    body, code = call(ns["chunk_verdict"], json_body={"chunk_id": cid, "verdict": "sounded_perfect"})
    check("a thumbs-up on a private chunk is accepted", code, 200)
    check("and marking it solid finds it", L.mark_session_solid(played=[cid])["missing"], 0)
    L.submit_report(chunk_text="", chunk_id=cid, issue="PRONUNCIATION", token="quokka",
                    expected="kwocka", action="ADD_TO_STORE", confidence="HIGH")
    check("a report the user files on it may keep what they typed",
          db("SELECT COUNT(*) FROM rules WHERE rule_type='PRONUNCIATION' AND pattern='quokka'")[0][0], 1)
    db("UPDATE rules SET active=0 WHERE pattern='quokka'")
    L.invalidate_rule_cache()

    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        call(ns["chunk_verdict"], json_body={"chunk_id": cid, "verdict": "skip"})
        call(ns["speak"], json_body=dict(req, epoch=501))
        db("UPDATE rules SET active=0 WHERE rule_type='SUPPRESS' AND pattern=?", (cid,))
        L.invalidate_rule_cache()
        ns["_speak_order"].cancel(502)
        ns["_dedup_cache"].clear()
        body, code = call(ns["speak"], json_body=dict(req, epoch=502))
    check("the skipped and superseded lines keep quiet too",
          (leaked(out.getvalue()), "skipped by the user: (private)" in out.getvalue(), code), ([], True, 409))

    # A failure on a private chunk.
    n_rules = db("SELECT COUNT(*) FROM rules")[0][0]
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        eid = L.log_error("quokka en-US:ZanzibarNeural ledger", error_type="RuntimeError",
                          stage="inference", message="index out of range: quokka ledger",
                          attempts=[{"attempt": 1, "stage": "inference", "type": "RuntimeError",
                                     "message": "srcIndex assert near quokka"}],
                          display_text="quokka en-US:ZanzibarNeural ledger", fatal=True, private=True)
    erow = db("SELECT text, private, error_type, error_detail FROM chunks WHERE id=?", (eid,))[0]
    check("a failed private chunk keeps its type and loses its text", erow[:3], ("", 1, "RuntimeError"))
    check("its stored detail has no message to quote the text", leaked(erow[3]), [])
    check("its history line has none",
          leaked(db("SELECT detail FROM history WHERE event_type='ERROR'")), [])
    check("no crash token is blacklisted from it", db("SELECT COUNT(*) FROM rules")[0][0], n_rules)
    check("and nothing is printed", leaked(out.getvalue()), [])

    # A private chunk that only worked on a retry keeps what failed, not its message.
    rid = L.log_chunk("quokka retry", "sentence", 30, private=True,
                      prosody={"retried": 1, "failure": {"attempt": 1, "stage": "inference",
                                                         "type": "RuntimeError",
                                                         "message": "could not encode: quokka"}})
    kept = json.loads(db("SELECT error_detail FROM chunks WHERE id=?", (rid,))[0][0])
    check("a recovered failure keeps its type and drops its message",
          (kept.get("type"), "message" in kept), ("RuntimeError", False))
    db("DELETE FROM chunks WHERE id=?", (rid,))

    # Read again without the flag, the same sentence is an ordinary chunk.
    ns["_dedup_cache"].clear()
    resp, code = call(ns["speak"], json_body={"text": secret})
    row = db("SELECT text, private FROM chunks WHERE id=?", (cid,))[0]
    check("the same text read openly is stored as usual", (resp.headers.get("X-Chunk-Id"), row[1],
                                                           leaked(row[0]) != []), (cid, 0, True))
    db("DELETE FROM chunks WHERE id=?", (cid,))
    ns["_dedup_cache"].clear()


@section("25. listen-back on a private chunk stores no words")
def _():
    # Whisper is not installed where the tests run, so the worker is given a
    # transcript that matches nothing, which is what makes it flag and learn.
    saved = (L._WHISPER_OK, L._whisper, L._WHISPER_MODEL, L._transcribe_bytes, L._LIBROSA_OK)
    L._WHISPER_OK, L._whisper, L._WHISPER_MODEL, L._LIBROSA_OK = True, object(), object(), False
    L._transcribe_bytes = lambda wav: {"text": "nothing like what was written", "segments": []}
    try:
        priv = L.log_chunk("The XQZ quokka ledger moved to Zanzibar yesterday.", "sentence", 30,
                           wav_bytes=b"RIFF", profile_str="normal|medium|clean|plain", private=True)
        openly = L.log_chunk("The XQV wombat ledger moved to Tasmania yesterday.", "sentence", 30,
                             wav_bytes=b"RIFF", profile_str="normal|medium|clean|plain")
        scored = lambda: db("SELECT COUNT(*) FROM chunks WHERE id IN (?,?) "
                            "AND whisper_accuracy IS NOT NULL", (priv, openly))[0][0]
        for _ in range(240):
            if scored() == 2:
                break
            time.sleep(0.25)
        time.sleep(0.5)     # the rule the open chunk teaches is written after its score
        check("both chunks were analysed", scored(), 2)
        pf = db("SELECT quality_flags, whisper_accuracy FROM chunks WHERE id=?", (priv,))[0]
        of = db("SELECT quality_flags FROM chunks WHERE id=?", (openly,))[0][0]
        check("the private chunk is scored", pf[1] is not None, True)
        check("its flags count the skipped words", "WHISPER_SKIP:" in pf[0] and "words" in pf[0], True)
        check("without naming one", [w for w in ("quokka", "zanzibar", "xqz", "ledger")
                                     if w in pf[0].lower()], [])
        check("an open chunk's flags still name them", "WHISPER_SKIP:" in of and "words" not in
              of.split("WHISPER_SKIP:")[1].split("|")[0], True)
        learned = [r[0] for r in db("SELECT pattern FROM rules WHERE rule_type='PRONUNCIATION' "
                                    "AND pattern IN ('XQZ','XQV')")]
        check("and no pronunciation is learned from the private one", learned, ["XQV"])
    finally:
        L._WHISPER_OK, L._whisper, L._WHISPER_MODEL, L._transcribe_bytes, L._LIBROSA_OK = saved


@section("7. a report that was only counted becomes the fix it asked for")
def _():
    def flag(pattern, hits=0):
        db("INSERT INTO rules (ts, rule_type, pattern, action, value, hits, source) "
           "VALUES (?, 'FLAG', ?, 'warn', '', ?, 'REPORT')", (time.time(), pattern, hits))
        return db("SELECT id FROM rules WHERE rule_type='FLAG' AND pattern=? AND active=1", (pattern,))[0][0]
    def reported(token, expected, issue="PRONUNCIATION", chunk="some chunk text"):
        db("INSERT INTO reports (ts, chunk_text, issue, token, expected, action) VALUES (?,?,?,?,?,?)",
           (time.time(), chunk, issue, token, expected, "FLAG_PATTERN"))
    def active(pattern, rtype="FLAG"):
        return db("SELECT COUNT(*) FROM rules WHERE rule_type=? AND pattern=? AND active=1",
                  (rtype, pattern))[0][0]

    db("UPDATE rules SET active=0 WHERE rule_type='FLAG'")
    ids = {p: flag(p, h) for p, h in (("CNF", 314), ("Google", 68), ("pi", 0), ("*", 0),
                                      ("In fact", 21), ("DNF", 9), ("AHH", 3))}
    reported("CNF", "C.N.F")
    reported("Google", "Google")
    reported("pi", "API")
    reported("*", "Star")
    reported("DNF", "D.N.F")
    reported("AHH", "", issue="HALLUCINATION")
    reported("", "", issue="PUNCT", chunk="In fact, the opposite is true.")
    L._add_rule("PRONUNCIATION", "DNF", "store_override", "dee en eff", source="MANUAL")
    L.invalidate_rule_cache()
    db("DELETE FROM counters WHERE name=?", (L._FLAG_FIX_MARKER,))

    check("it converts the one that qualifies", L._convert_reported_flags(), [("CNF", "C.N.F", 314)])
    check("the flag is switched off, not deleted",
          db("SELECT active FROM rules WHERE id=?", (ids["CNF"],))[0][0], 0)
    check("and the word is said the way the report asked",
          L.apply_learned_rules("Convert it to CNF first."), "Convert it to C.N.F first.")
    check("the pronunciation store agrees with the rule",
          json.loads((HERE / "pronunciation_store.json").read_text(encoding="utf-8")).get("CNF"), "c.n.f")
    check("a report that only repeated the word is left as a flag", active("Google"), 1)
    check("so is a token too short to be safe", active("pi"), 1)
    check("and a symbol", active("*"), 1)
    check("and a flag with no pronunciation report behind it", (active("In fact"), active("AHH")), (1, 1))
    check("a word fixed since keeps the newer fix and loses its flag",
          (active("DNF"), db("SELECT value FROM rules WHERE rule_type='PRONUNCIATION' AND pattern='DNF' "
                             "AND active=1")[0][0]), (0, "dee en eff"))
    check("it is in the history",
          db("SELECT COUNT(*) FROM history WHERE event_type='MIGRATION' AND detail LIKE 'flag \"CNF\"%'")[0][0], 1)
    check("and it runs once", L._convert_reported_flags(), None)

    # The dashboard's three calls.
    out, code = call(ns["flag_rules_route"], method="GET", path="/rules/flags")
    flags = out["flags"]
    check("the list is the active flags, most hits first",
          [(f["pattern"], f["hits"]) for f in flags][:3],
          [("Google", 68), ("In fact", 21), ("AHH", 3)])
    check("with the ones that never came up again after them",
          sorted(f["pattern"] for f in flags[3:]), ["*", "pi"])
    by = {f["pattern"]: f for f in flags}
    check("each carries the issue it was reported as",
          (by["pi"]["issue"], by["AHH"]["issue"], by["In fact"]["issue"]),
          ("PRONUNCIATION", "HALLUCINATION", "PUNCT"))
    check("and the reported sound as a hint, when there was one",
          (by["pi"]["expected_hint"], by["*"]["expected_hint"], by["Google"]["expected_hint"],
           by["AHH"]["expected_hint"]), ("API", "Star", "", ""))
    check("with the fields the dashboard reads",
          sorted(by["pi"]), ["expected_hint", "hits", "id", "issue", "pattern", "ts"])

    fix, dismiss = ns["flag_fix_route"], ns["flag_dismiss_route"]
    out, code = call(fix, ids["Google"], json_body={"say": "  "})
    check("an empty fix is refused with a message", (code, out["ok"], bool(out["message"])), (400, False, True))
    out, code = call(fix, ids["Google"], json_body={"say": "google"})
    check("so is one that only repeats the word", (code, out["ok"]), (400, False))
    out, code = call(fix, ids["Google"], json_body=["not", "an", "object"])
    check("and a body that is not an object", (code, out["ok"]), (400, False))
    check("none of which touched the flag", active("Google"), 1)
    out, code = call(fix, 987654, json_body={"say": "anything"})
    check("an id that is no flag is not found", (code, out["ok"]), (404, False))
    rule_id = db("SELECT id FROM rules WHERE rule_type='PRONUNCIATION' AND pattern='CNF'")[0][0]
    out, code = call(fix, rule_id, json_body={"say": "anything"})
    check("nor is the id of a rule of another kind", (code, active("CNF", "PRONUNCIATION")), (404, 1))

    out, code = call(fix, ids["Google"], json_body={"say": "Goo.gull"})
    check("a real fix is accepted and says what it did",
          (code, out["ok"], "Goo.gull" in out["message"]), (200, True, True))
    check("the flag is gone and the word is fixed",
          (active("Google"), L.apply_learned_rules("Ask Google about it.")), (0, "Ask Goo.gull about it."))
    out, code = call(fix, ids["Google"], json_body={"say": "Goo.gull"})
    check("fixing it twice finds no flag", code, 404)
    L._add_rule("FLAG", "DNF", "warn", "", source="REPORT")
    again = db("SELECT id FROM rules WHERE rule_type='FLAG' AND pattern='DNF' AND active=1")[0][0]
    out, code = call(fix, again, json_body={"say": "D.N.F"})
    check("a fix replaces an older pronunciation and says so",
          (code, "replacing" in out["message"], L.apply_learned_rules("Then DNF.")), (200, True, "Then D.N.F."))

    out, code = call(dismiss, ids["In fact"], json_body=None)
    check("dismiss switches the flag off", (code, out["ok"], active("In fact")), (200, True, 0))
    check("and teaches nothing", active("In fact", "PRONUNCIATION"), 0)
    check("dismissing it twice finds no flag", call(dismiss, ids["In fact"], json_body=None)[1], 404)
    out, code = call(ns["flag_rules_route"], method="GET", path="/rules/flags")
    check("the list is left with what still needs attention",
          sorted(f["pattern"] for f in out["flags"]), ["*", "AHH", "pi"])


print()
print(f"{PASS} passed, {FAIL} failed")
shutil.rmtree(HERE, ignore_errors=True)   # best effort: the learner may hold a handle
sys.exit(1 if FAIL else 0)
