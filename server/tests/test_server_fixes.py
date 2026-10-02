"""Regression tests for the fixes from the October server review.

Each section names the defect it guards against. The real learner.py, server.py
and scan_host.py are loaded from COPIES in a temp directory, so every path they
derive from __file__ (the database, good_settings.json, the pronunciation and
punctuation stores, voices/, the latent caches) lands in that temp directory.
Nothing here opens the live tts_quality.db, good_settings.json, either store or
voices/, and nothing is copied from them either: the learner builds an empty
database of its own.

Only torch, TTS, flask and flask_cors are stubbed, the same way test_pipeline.py
does it, and register_host is replaced so the boot path never touches the real
registration under %LOCALAPPDATA%.

To check that a section really catches its defect, point KAM_FIXES_SRC at a
folder holding the pre-fix server.py, learner.py and scan_host.py (the other
modules are taken from the real server folder when missing there). The sections
then fail rather than pass.
"""
import pathlib as _pl
SERVER_DIR = _pl.Path(__file__).resolve().parent.parent

import hashlib
import io
import json
import os
import shutil
import sqlite3
import struct
import sys
import tempfile
import threading
import time
import types

# server.py reconfigures its own streams at boot, but the parts run here print
# arrows and stars from inside handlers, so the console has to be UTF-8 first or
# a print would fail and look like a test failure.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

SRC = _pl.Path(os.environ.get("KAM_FIXES_SRC") or SERVER_DIR)
HERE = _pl.Path(tempfile.mkdtemp(prefix="kam_fixes_test_"))
for p in SERVER_DIR.glob("*.py"):
    shutil.copy(p, HERE / p.name)
for name in ("server.py", "learner.py", "scan_host.py"):
    if (SRC / name).exists():
        shutil.copy(SRC / name, HERE / name)
sys.path.insert(0, str(HERE))

PASS = FAIL = 0


def check(label, got, want):
    global PASS, FAIL
    if got == want:
        PASS += 1
        print(f"  ok   {label}")
    else:
        FAIL += 1
        print(f"  FAIL {label}\n         got:  {got!r}\n         want: {want!r}")


def section(title):
    """Run one block of checks. A block that raises counts as one failure and
    the rest still run, which is what lets the suite report cleanly against the
    pre-fix code, where some of the functions under test do not exist yet."""
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


def wav_bytes(seconds=6.0, sr=24000):
    """A real 16-bit mono WAV with a tone in it, so clip screening passes it."""
    import math
    n = int(sr * seconds)
    data = b"".join(struct.pack("<h", int(8000 * math.sin(i * 0.05))) for i in range(n))
    return (b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVE"
            + b"fmt " + struct.pack("<IHHIIHH", 16, 1, 1, sr, sr * 2, 2, 16)
            + b"data" + struct.pack("<I", len(data)) + data)


# --- The learner, from the temp copy ---
import learner as L

# scipy is loaded for real when it is installed, and before the torch stub
# below exists, since scipy probes sys.modules for torch and trips over a stub.
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

# --- Stubs for the heavy imports server.py makes ---
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


class _Flask:
    config = {}
    def __init__(self, *a, **k): pass
    def route(self, *a, **k): return lambda f: f
    def before_request(self, f): return f
    def run(self, *a, **k): pass


class _Req:
    """Stands in for flask.request. Each call sets the one it wants."""
    def __init__(self, json_body=None, args=None, form=None, files=None):
        self._j = json_body or {}
        self.args = args or {}
        self.form = form or {}
        self.files = files or {}
    @property
    def json(self): return self._j
    def get_json(self, *a, **k): return self._j


stub("flask", Flask=_Flask, request=_Req(),
     send_file=lambda *a, **k: ("FILE", k.get("mimetype")),
     jsonify=lambda *a, **k: (a[0] if a else k))

# The boot path imports register_host to repair the registration and to find
# the registered manifest. Both must stay inside the temp directory.
RH_DIR = HERE / "registered"
RH_DIR.mkdir()
stub("register_host", manifest_path=lambda: str(RH_DIR / "com.kam.tts.json"),
     ensure_registered=lambda **k: None)

src = (HERE / "server.py").read_text(encoding="utf-8")
body = src[:src.index('if __name__ == "__main__":')]
body = body.replace("if not _acquire_single_instance():",
                    "if False and not _acquire_single_instance():")
ns = {"__name__": "server_undertest", "__file__": str(HERE / "server.py")}
exec(compile(body, str(HERE / "server.py"), "exec"), ns)


def call(fn, json_body=None, args=None, form=None, files=None):
    """Run a route with a request, and return (body, status)."""
    ns["request"] = _Req(json_body, args, form, files)
    r = fn()
    return r if isinstance(r, tuple) else (r, 200)


VOICES = _pl.Path(ns["VOICES_DIR"])
(VOICES / "alice").mkdir(parents=True)
(VOICES / "alice" / "passage_01.wav").write_bytes(wav_bytes())
(VOICES / "bob").mkdir()
(VOICES / "bob" / "passage_01.wav").write_bytes(wav_bytes())
_pl.Path(ns["VOICE_SAMPLES_DIR"]).mkdir(exist_ok=True)
(_pl.Path(ns["VOICE_SAMPLES_DIR"]) / "passage_01.wav").write_bytes(wav_bytes())


@section("#1 a restart speaks in the saved voice, not the default")
def _():
    L.set_setting("active_voice", "alice")
    seen = {}

    def fake_load_model():
        seen["voice"] = ns["_ACTIVE_VOICE"]

    saved = {k: ns.get(k) for k in ("load_model", "_warm_model", "_maybe_selfbenchmark",
                                     "_apply_hardware_adaptation", "_scan", "_pos_prosody",
                                     "_idle_monitor")}
    ns.update({
        "load_model": fake_load_model,
        "_warm_model": lambda: None,
        "_maybe_selfbenchmark": lambda: None,
        "_apply_hardware_adaptation": lambda: None,
        "_scan": types.SimpleNamespace(cleanup_stale_dirs=lambda **k: 0),
        "_pos_prosody": types.SimpleNamespace(is_available=lambda: False),
        "_idle_monitor": lambda: None,
    })
    ns["_ACTIVE_VOICE"] = "default"
    try:
        ns["_run_startup"]()
    finally:
        ns.update(saved)
    check("the latents are loaded with the saved voice already active",
          seen.get("voice"), "alice")
    check("and it is still the active voice afterwards", ns["_ACTIVE_VOICE"], "alice")
    check("the learner agrees", L._ACTIVE_VOICE, "alice")


@section("#1 a saved voice that cannot be built falls back for the session only")
def _():
    saved = ns["_latents_for"]
    built = []

    def fake_latents(vid, fresh=False):
        built.append(vid)
        if vid == "bob":
            raise RuntimeError("could not read the clips")
        return ("gpt-" + vid, "spk-" + vid), 0

    ns["_latents_for"] = fake_latents
    try:
        L.set_setting("active_voice", "bob")
        ns["_ACTIVE_VOICE"] = "bob"
        ns["_load_active_latents"]()
        check("it tried the chosen voice first", built[:1], ["bob"])
        check("then built the default", ns["gpt_cond_latent"], "gpt-default")
        check("the session runs as default", ns["_ACTIVE_VOICE"], "default")
        check("the choice on disk is left for the next start to retry",
              L.get_setting("active_voice"), "bob")

        ns["_ACTIVE_VOICE"] = "alice"
        ns["_load_active_latents"]()
        check("a voice that builds is saved as the active one",
              L.get_setting("active_voice"), "alice")
    finally:
        ns["_latents_for"] = saved


@section("#2 every voice route refuses an id that escapes voices/")
def _():
    ok = ns["_checked_voice_id"]
    for good in ("default", "alice", "Alice Smith", "voice-2"):
        check(f"{good!r} is accepted", ok(good), good)
    victim = HERE / "victim"
    victim.mkdir()
    (victim / "keep.wav").write_bytes(wav_bytes(1))
    for bad in ("..", ".", "../escaped", "..\\escaped", "a/b", "C:\\Windows",
                str(victim), "alice.", "alice .", "x:y", "", "nul\x00"):
        check(f"{bad!r} is refused", ok(bad), None)

    os.startfile = lambda p: None          # /voices/open must never open a window here

    class _File:
        def read(self): return wav_bytes(1)

    routes = [
        ("/voices/check",       ns["check_voice_route"], "json", "voice_id"),
        ("/voices/record",      ns["record_clip"],       "form", "voice"),
        ("/voices/clips",       ns["list_clips"],        "args", "voice"),
        ("/voices/clip",        ns["get_clip"],          "args", "voice"),
        ("/voices/clip/delete", ns["delete_clip"],       "json", "voice_id"),
        ("/voices/select",      ns["select_voice"],      "json", "voice_id"),
        ("/voices/rename",      ns["rename_voice"],      "json", "voice_id"),
        ("/voices/data",        ns["voice_data"],        "args", "voice_id"),
        ("/voices/open",        ns["open_voice_folder"], "json", "voice_id"),
        ("/voices/delete",      ns["delete_voice"],      "json", "voice_id"),
    ]
    for bad in ("..", "../escaped", str(victim)):
        for path, fn, where, key in routes:
            payload = {key: bad, "confirm": True, "name": "renamed", "slot": "1"}
            kw = {"json_body": payload} if where == "json" else (
                 {"args": payload} if where == "args" else
                 {"form": payload, "files": {"audio": _File()}})
            _body, code = call(fn, **kw)
            check(f"{path} with {bad[:20]!r} is a 400", code, 400)
    check("the server folder is still there", (HERE / "server.py").exists(), True)
    check("the victim folder is still there", (victim / "keep.wav").exists(), True)
    check("nothing was written outside voices/", (HERE / "escaped").exists(), False)
    _body, code = call(ns["voice_data"], args={"voice_id": "alice"})
    check("a real voice still works", code, 200)


@section("#3 learned files are UTF-8, and older ANSI ones still read")
def _():
    L.learn_punctuation_correction("It was fine then", "It was fine — then → now")
    raw = L.PUNCT_PATH.read_bytes()
    check("the punctuation store is valid UTF-8 JSON",
          json.loads(raw.decode("utf-8")).get("It was fine then"),
          "It was fine — then → now")
    check("the server reads it back", ns["apply_punct_corrections"]("It was fine then"),
          "It was fine — then → now")

    # A file an older learner.py wrote in the ANSI codepage.
    legacy = {"Old line": "Old — line"}
    L.PUNCT_PATH.write_bytes(json.dumps(legacy, ensure_ascii=False).encode("cp1252"))
    os.utime(L.PUNCT_PATH, (time.time() + 5, time.time() + 5))
    check("the server still reads an ANSI-encoded store",
          ns["apply_punct_corrections"]("Old line"), "Old — line")

    _body, code = call(ns["set_pronunciation"], json_body={"abbr": "NAIVE", "spoken": "naïve"})
    check("POST /pronounce saved", code, 200)
    L._add_to_pronunciation_store("GPU", "gee pee you")
    store = json.loads(L.STORE_PATH.read_bytes().decode("utf-8"))
    check("the learner's write kept the accented entry intact", store.get("NAIVE"), "naïve")
    check("and added its own", store.get("GPU"), "gee pee you")


@section("#4 the baseline cache follows the voice")
def _():
    L.set_active_voice("default")
    L.BASELINE_PATH.write_text(json.dumps({"pitch_std": 50.0, "sample_count": 7}),
                               encoding="utf-8")
    L._forget_cached_baseline()
    check("default's baseline loads", (L._get_baseline() or {}).get("pitch_std"), 50.0)
    L.set_active_voice("carol")
    check("a voice with no baseline does not see default's", L._get_baseline(), None)
    L._update_baseline({"pitch_variance": 20.0, "energy_tail": 0.2, "speaking_rate": 3.0})
    carol = json.loads(L.baseline_path_for("carol").read_text(encoding="utf-8"))
    check("carol's first update starts her own count", carol.get("sample_count"), 1)
    check("and is not blended with default's pitch", carol.get("pitch_std"), 20.0)
    L.set_active_voice("default")


@section("#5 queued analysis is filed under the voice that made the chunk")
def _():
    L.set_active_voice("alice")
    cid = L.log_chunk("A sentence made in Alice's voice for the test.", "sentence", 30,
                      synth_params={"temperature": 0.3, "top_p": 0.9, "top_k": 40,
                                    "repetition_penalty": 4.0, "speed": 1.1},
                      profile_str="normal|medium|clean|plain")
    L.set_active_voice("bob")              # the user switches while it waits
    check("the chunk's voice is read off its row", L._chunk_voice(cid), "alice")
    L._record_param_observation(cid, 0.91, 0.95)
    conn = sqlite3.connect(str(L.DB_PATH))
    v = conn.execute("SELECT voice FROM param_observations ORDER BY id DESC LIMIT 1").fetchone()
    conn.close()
    check("the observation is alice's", v[0] if v else None, "alice")
    L._adjust_profile_temperature(["sentence|probe"], -0.02, voice="alice")
    store = L._load_good_settings()
    check("the temperature nudge lands on alice", "v:alice:prof:sentence|probe" in store, True)
    check("and not on bob", "v:bob:prof:sentence|probe" in store, False)
    L._update_baseline({"pitch_variance": 30.0, "energy_tail": 0.2, "speaking_rate": 3.0},
                       "alice")
    check("the baseline update goes to alice's file",
          L.baseline_path_for("alice").exists(), True)
    check("bob gets no baseline from it", L.baseline_path_for("bob").exists(), False)
    L.set_active_voice("default")


@section("#6 a failed transcription leaves no temp WAV behind")
def _():
    scratch = tempfile.mkdtemp(prefix="kam_whisper_tmp_", dir=HERE)
    saved_dir, saved_model = tempfile.tempdir, L._WHISPER_MODEL

    class _Broken:
        def transcribe(self, *a, **k):
            raise FileNotFoundError("ffmpeg not found")

    tempfile.tempdir = scratch
    L._WHISPER_MODEL = _Broken()
    try:
        try:
            L._transcribe_bytes(wav_bytes(1))
            raised = False
        except FileNotFoundError:
            raised = True
    finally:
        tempfile.tempdir, L._WHISPER_MODEL = saved_dir, saved_model
    check("the failure still reaches the worker", raised, True)
    check("and the temp WAV is gone", os.listdir(scratch), [])


@section("#7 skipping a chunk from the feed silences that chunk")
def _():
    produced = []

    def fake_synth(text, raw, position, chunk_no, dedup_key, read=None):
        produced.append(text)
        return "SYNTH"

    saved = ns["_synthesise_and_log"]
    ns["_synthesise_and_log"] = fake_synth
    try:
        line = "This paragraph is one the reader wants skipped every time."
        body, _ = call(ns["speak"], json_body={"text": line})
        check("before the skip it is synthesised", body, "SYNTH")
        cid = hashlib.sha1(produced[-1].strip().encode("utf-8")).hexdigest()[:16]

        _b, code = call(ns["chunk_verdict"], json_body={"chunk_id": cid, "verdict": "skip"})
        check("the skip verdict is accepted", code, 200)
        # The silence response is send_file's, which the stub returns as a pair.
        check("after the skip it is silent",
              call(ns["speak"], json_body={"text": line}), ("FILE", "audio/wav"))
        other, _ = call(ns["speak"], json_body={"text": "A different sentence entirely, still read."})
        check("other chunks are untouched", other, "SYNTH")
    finally:
        ns["_synthesise_and_log"] = saved

    before = [r for r in L.get_rules(active_only=True) if r["rule_type"] == "MONITOR"]
    _b, code = call(ns["chunk_verdict"], json_body={"chunk_id": "0123456789abcdef",
                                                    "verdict": "mostly_right"})
    after = [r for r in L.get_rules(active_only=True) if r["rule_type"] == "MONITOR"]
    check("mostly_right is still accepted", code, 200)
    check("but no longer files a MONITOR rule that can never fire", len(after), len(before))


@section("#8 the reload recovery really reloads")
def _():
    class _FreshTTS:
        def __init__(self, *a, **k): pass
        def to(self, d): return self

    saved = {k: ns.get(k) for k in ("TTS", "_load_active_latents", "tts")}
    ns["TTS"] = _FreshTTS
    ns["_load_active_latents"] = lambda: None
    ns["tts"] = "the model that just failed twice"
    try:
        ns["_reload_model"]()
        check("a new model is in place", isinstance(ns["tts"], _FreshTTS), True)
    finally:
        ns.update(saved)
    rec = src[src.index("recoveries = ("):src.index("recoveries = (") + 300]
    check("and it is what the last recovery step calls",
          '"Retry failed — reloading model", _reload_model)' in rec, True)


@section("#9 replaying a chunk keeps the user's verdict and scores")
def _():
    text = "The replayed sentence keeps what the user said about it."
    cid = L.log_chunk(text, "sentence", 30, synth_params={"temperature": 0.30},
                      profile_str="normal|medium|clean|plain")
    conn = sqlite3.connect(str(L.DB_PATH))
    conn.execute("UPDATE chunks SET user_feedback='negative', user_verdict='sounded_wrong', "
                 "quality_score=0.42, whisper_accuracy=0.8 WHERE id=?", (cid,))
    conn.commit()
    conn.close()
    L.log_chunk(text, "sentence", 30, synth_params={"temperature": 0.25},
                profile_str="normal|medium|clean|plain")
    conn = sqlite3.connect(str(L.DB_PATH))
    row = conn.execute("SELECT user_feedback, user_verdict, quality_score, whisper_accuracy, "
                       "used_temperature FROM chunks WHERE id=?", (cid,)).fetchone()
    n = conn.execute("SELECT COUNT(*) FROM chunks WHERE id=?", (cid,)).fetchone()[0]
    conn.close()
    check("still one row", n, 1)
    check("the verdict survives", row[0:2], ("negative", "sounded_wrong"))
    check("the scores survive", row[2:4], (0.42, 0.8))
    check("the synthesis columns are updated", row[4], 0.25)
    L.mark_session_solid(played=[cid])
    conn = sqlite3.connect(str(L.DB_PATH))
    fb = conn.execute("SELECT user_feedback FROM chunks WHERE id=?", (cid,)).fetchone()[0]
    conn.close()
    check("so the end of the session does not relabel it solid", fb, "negative")


@section("#10 a rule added mid-read is not lost to a stale cache")
def _():
    L.invalidate_rule_cache()
    real_get_rules = L.get_rules
    state = {"raced": False}

    def racing_get_rules(active_only=True):
        rows = real_get_rules(active_only=active_only)    # the old list
        if not state["raced"]:
            state["raced"] = True
            L._add_rule("FLAG", "zzz_added_mid_read", "warn", "", source="MANUAL")
        return rows

    L.get_rules = racing_get_rules
    try:
        L._active_rules()                  # the read that loses the race
    finally:
        L.get_rules = real_get_rules
    seen = any(r["pattern"] == "zzz_added_mid_read" for r in L._active_rules())
    check("the next read sees the new rule", seen, True)


@section("#11 learned files are written atomically and never silently emptied")
def _():
    target = HERE / "atomic_probe.json"
    target.write_text('{"old": true}', encoding="utf-8")
    try:
        L._write_json_atomic(target, {"x": object()})        # fails mid-dump
    except TypeError:
        pass
    check("a failed write leaves the old file whole",
          json.loads(target.read_text(encoding="utf-8")), {"old": True})
    check("and no temp file behind",
          [p.name for p in HERE.glob("atomic_probe.json.*.tmp")], [])

    gs = L._good_settings_path()
    gs.write_text('{"sentence": {"temperature": 0.3', encoding="utf-8")   # truncated
    L._good_settings_cache = None
    L._load_good_settings()
    kept = list(HERE.glob("good_settings.json.corrupt-*"))
    check("a damaged good_settings.json is set aside, not overwritten", len(kept), 1)
    check("with its contents intact", kept[0].read_text(encoding="utf-8") if kept else None,
          '{"sentence": {"temperature": 0.3')
    L.set_setting("probe", 1)
    check("and learning carries on in a fresh file",
          json.loads(gs.read_text(encoding="utf-8")).get("settings", {}).get("probe"), 1)

    # The autotune cycle must wait for the good-settings lock, or its stale copy
    # of the store overwrites whatever was saved while it ran.
    L.set_autotune(True)
    done = threading.Event()
    with L._good_settings_lock:
        t = threading.Thread(target=lambda: (L.run_autotune_cycle(), done.set()), daemon=True)
        t.start()
        time.sleep(0.4)
        check("the cycle waits while the store is being changed", done.is_set(), False)
    t.join(10)
    check("and runs once it is free", done.is_set(), True)
    L.set_autotune(False)


@section("#12 a voice that cannot be built is not saved as active")
def _():
    saved = {k: ns.get(k) for k in ("tts", "_latents_for", "gpt_cond_latent", "_ACTIVE_VOICE")}
    L.set_setting("active_voice", "alice")
    ns["_ACTIVE_VOICE"] = "alice"
    ns["gpt_cond_latent"] = "alice-latents"
    ns["tts"] = object()

    def fake_latents(vid, fresh=False):
        if vid == "bob":
            raise RuntimeError("no readable clips")
        return ("gpt-" + vid, "spk-" + vid), 1

    ns["_latents_for"] = fake_latents
    try:
        try:
            res = ns["switch_voice"]("bob")
        except Exception as e:
            res = {"ok": "raised " + type(e).__name__}
        check("the switch reports the failure", res.get("ok"), False)
        check("the active voice is unchanged", ns["_ACTIVE_VOICE"], "alice")
        check("so are the latents", ns["gpt_cond_latent"], "alice-latents")
        check("and nothing new was saved", L.get_setting("active_voice"), "alice")
        res = ns["switch_voice"]("default")
        check("a voice that builds switches", res.get("ok"), True)
        check("and is saved", L.get_setting("active_voice"), "default")
    finally:
        ns.update(saved)


@section("scan sessions expire and clean up without the dashboard")
def _():
    import scan_host as S
    d = tempfile.mkdtemp(prefix="kam_scan_fixes_", dir=HERE)
    (_pl.Path(d) / "page_000.jpg").write_bytes(b"\xff\xd8\xff photo")
    S._session = {"code": "EXP123", "token": "t", "dir": d, "pages": [{"path": d}],
                  "opened": time.time(), "touched": time.time() - S.SESSION_TTL - 5,
                  "ip": "192.168.0.2", "stop": threading.Event()}
    check("an expired session is closed", S._reap_expired(), True)
    check("its photos are gone", os.path.exists(d), False)
    check("and it is forgotten", S._session, None)

    d2 = tempfile.mkdtemp(prefix="kam_scan_fixes_", dir=HERE)
    sess = {"code": "LIVE12", "token": "t", "dir": d2, "pages": [],
            "opened": time.time(), "touched": time.time(), "ip": "192.168.0.2",
            "stop": threading.Event()}
    S._session = sess
    check("a live one is left alone", S._reap_expired(), False)
    saved_every = S.REAP_EVERY
    S.REAP_EVERY = 0.05
    try:
        sess["touched"] = time.time() - S.SESSION_TTL - 5
        th = threading.Thread(target=S._reaper, args=(sess,), daemon=True)
        th.start()
        th.join(5)
    finally:
        S.REAP_EVERY = saved_every
    check("the session's own reaper closes it once it expires", S._session, None)
    check("and deletes its photos", os.path.exists(d2), False)

    root = _pl.Path(tempfile.mkdtemp(prefix="kam_scan_root_", dir=HERE))
    old, young, other = root / "kam_scan_old", root / "kam_scan_young", root / "unrelated"
    for p in (old, young, other):
        p.mkdir()
        (p / "page_000.jpg").write_bytes(b"x")
    long_ago = time.time() - S.SESSION_TTL - 60
    os.utime(old, (long_ago, long_ago))
    os.utime(other, (long_ago, long_ago))
    check("boot cleanup removes one leftover", S.cleanup_stale_dirs(root=str(root)), 1)
    check("the old session folder went", old.exists(), False)
    check("a recent one is kept", young.exists(), True)
    check("other folders are never touched", other.exists(), True)


@section("the registered manifest is where the extension id is read from")
def _():
    i = src.index("_FALLBACK_EXTENSION_ID =")
    j = src.index("_EXTENSION_ORIGINS = _extension_origins()")
    legacy_dir = tempfile.mkdtemp(dir=HERE)
    (_pl.Path(legacy_dir) / "com.kam.tts.json").write_text(
        json.dumps({"allowed_origins": ["chrome-extension://legacylegacy/"]}), encoding="utf-8")
    (RH_DIR / "com.kam.tts.json").write_text(
        json.dumps({"allowed_origins": ["chrome-extension://registeredid/"]}), encoding="utf-8")
    old = os.environ.pop("KAM_EXTENSION_ID", None)
    try:
        bns = {"os": os, "json": json, "_SERVER_DIR0": legacy_dir, "print": lambda *a, **k: None}
        exec(src[i:j], bns)
        check("the manifest register_host.py installs is read first",
              bns["_extension_origins"](), ["chrome-extension://registeredid"])
        (RH_DIR / "com.kam.tts.json").unlink()
        check("the old spot beside server.py is still read second",
              bns["_extension_origins"](), ["chrome-extension://legacylegacy"])
    finally:
        if old is not None:
            os.environ["KAM_EXTENSION_ID"] = old


print()
print(f"{PASS} passed, {FAIL} failed")
shutil.rmtree(HERE, ignore_errors=True)   # best effort: the learner may hold a handle
sys.exit(1 if FAIL else 0)
