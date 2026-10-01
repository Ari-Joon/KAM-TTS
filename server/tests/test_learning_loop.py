"""Regression tests for the report and reinforcement loop.

Each section names the defect from the October audit it guards against. The
real learner.py and server.py are loaded from COPIES in a temp directory, and
the learner builds an empty database of its own there, so nothing here reads
or writes the live tts_quality.db, good_settings.json or either store.

server.py is loaded the same way test_server_fixes.py loads it: torch, TTS,
flask and flask_cors stubbed, register_host replaced.

To check that a section catches its defect, point KAM_LOOP_SRC at a folder
holding the pre-fix learner.py and server.py; the sections then fail.
"""
import pathlib as _pl
SERVER_DIR = _pl.Path(__file__).resolve().parent.parent

import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
import types

# The code under test prints arrows and stars, so the console has to be UTF-8
# or a print would fail and look like a test failure.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

SRC = _pl.Path(os.environ.get("KAM_LOOP_SRC") or SERVER_DIR)
HERE = _pl.Path(tempfile.mkdtemp(prefix="kam_loop_test_"))
for p in SERVER_DIR.glob("*.py"):
    shutil.copy(p, HERE / p.name)
for name in ("server.py", "learner.py"):
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
    def __init__(self, json_body=None):
        self._j = json_body or {}
        self.args, self.form, self.files = {}, {}, {}
    @property
    def json(self): return self._j
    def get_json(self, *a, **k): return self._j


stub("flask", Flask=_Flask, request=_Req(),
     send_file=lambda *a, **k: ("FILE", k.get("mimetype")),
     jsonify=lambda *a, **k: (a[0] if a else k))
stub("register_host", manifest_path=lambda: str(HERE / "no_manifest.json"),
     ensure_registered=lambda **k: None)

src = (HERE / "server.py").read_text(encoding="utf-8")
body = src[:src.index('if __name__ == "__main__":')]
body = body.replace("if not _acquire_single_instance():",
                    "if False and not _acquire_single_instance():")
ns = {"__name__": "server_undertest", "__file__": str(HERE / "server.py")}
exec(compile(body, str(HERE / "server.py"), "exec"), ns)


def verdict(**payload):
    ns["request"] = _Req(payload)
    r = ns["chunk_verdict"]()
    return r if isinstance(r, tuple) else (r, 200)


def report(**kw):
    kw.setdefault("confidence", "HIGH")
    return L.submit_report(**kw)


def rules(rtype):
    return [r for r in L.get_rules(active_only=True) if r["rule_type"] == rtype]


def store():
    return L._load_good_settings()


PROFILE = "normal|medium|clean|plain"


def logged(text, temp=0.30, stype="sentence", profile=PROFILE):
    """Log a chunk the way /speak does, so it has a stored profile."""
    return L.log_chunk(text, stype, 30, synth_params={"temperature": temp, "top_p": 0.9,
                                                      "top_k": 45, "repetition_penalty": 4.1,
                                                      "speed": 1.1},
                       profile_str=profile)


def keys_of(cid):
    return L.lookup_chunk_profile(cid, None)["keys"]


L.register_live_settings({"temperature": 0.45, "speed": 1.0, "top_p": 0.9,
                          "top_k": 45, "repetition_penalty": 4.1})


@section("1. a punctuation report without the text and its fix makes no rule")
def _():
    text = "In the following demonstration, you'll learn how to:"
    before = len(rules("PUNCT"))
    r = report(chunk_text=text, issue="PUNCT", action="PUNCT")
    check("no PUNCT rule from the chunk prefix", len(rules("PUNCT")), before)
    check("the chunk is flagged instead", any(x["action"] == "needs_punctuation"
                                              for x in rules("FLAG")), True)
    check("and the reply says nothing was learned", "no rule" in r.get("message", ""), True)
    check("the chunk's text is untouched", L.apply_learned_rules(text), text)

    r = report(chunk_text="Lessons learned the hard way", issue="PUNCT", action="PUNCT",
               token="learned the hard way", expected="learned, the hard way")
    check("with the wrong text and its fix, a rule is made", len(rules("PUNCT")), before + 1)
    check("and it reads the way the user said",
          L.apply_learned_rules("Lessons learned the hard way stick."),
          "Lessons learned, the hard way stick.")
    check("the reply says what changed", "will now be read as" in r.get("message", ""), True)


@section("2. a hallucination report never strips a real word")
def _():
    before = len(rules("BLACKLIST"))
    r = report(chunk_text="|H2|One-hot encoding.|/H2|", issue="HALLUCINATION",
               action="BLACKLIST", token="BoW", expected="B.O.W")
    check("BoW with a pronunciation given is not blacklisted", len(rules("BLACKLIST")), before)
    check("the reply says why", "not blacklisted" in r.get("message", ""), True)
    report(chunk_text="The BoW model counts words.", issue="HALLUCINATION",
           action="BLACKLIST", token="BoW")
    check("a word in the chunk's own text is not blacklisted",
          len(rules("BLACKLIST")), before)
    report(chunk_text="The model counts words.", issue="HALLUCINATION",
           action="BLACKLIST", token="zorblax")
    check("an invented word is", any(x["pattern"] == "zorblax" for x in rules("BLACKLIST")), True)
    check("and only the whole word is removed",
          L.apply_learned_rules("zorblaxes and zorblax remain"), "zorblaxes and  remain")


@section("3. autotune speed is a trim, and today's speeds are kept")
def _():
    L.set_active_voice("speedtest")
    L.set_autotune(True)
    prof = "simple|medium|clean|plain"
    kept = "dense|short|clean|plain"
    with L._good_settings_lock:
        s = dict(L._load_good_settings())
        s[L._vk(f"prof:{kept}")] = {"speed_mod": 1.39}
        L._good_settings_cache = s
        L._write_json_atomic(L._good_settings_path(), s, indent=2)
    conn = sqlite3.connect(str(L.DB_PATH))
    # Slower chunks of this profile clearly scored better, at the absolute
    # speeds param_observations records (1.15 and 1.38, as on the real data).
    for i in range(40):
        slow = i % 2 == 0
        conn.execute("INSERT INTO param_observations (ts, profile, sentence_type, temperature, "
                     "top_p, top_k, rep_penalty, speed, quality, accuracy, voice) "
                     "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                     (time.time() - 1000 + i, prof, "sentence", 0.3, 0.9, 45, 4.1,
                      1.15 if slow else 1.38, (0.95 if slow else 0.80) + (i % 5) * 0.002,
                      0.9, "speedtest"))
    conn.commit()
    conn.close()
    heard_before = L.resolve_profile_param(kept, "speed_mod", 1.0)
    L._last_autotune_ts = 0
    L.run_autotune_cycle()
    mod = L.resolve_profile_param(prof, "speed_mod", 1.0)
    check("a new trim starts at 1.0 and steps once, not from the 1.38 median", mod, 0.99)
    check("an existing trim with no new evidence is untouched",
          L.resolve_profile_param(kept, "speed_mod", 1.0), heard_before)
    check("the tuner no longer writes temperature",
          "temperature" in (store().get(L._vk(f"prof:{prof}")) or {}), False)
    L.set_active_voice("default")


@section("6. autotune checkpoints close once the change has held")
def _():
    L.set_active_voice("ckpttest")
    prof = "normal|short|clean|plain"
    t0 = time.time() - 5000
    with L._good_settings_lock:
        s = dict(L._load_good_settings())
        s[L._vk("_autotune_ckpt")] = {
            f"{prof}::top_p": {"profile": prof, "param": "top_p", "before": 0.9,
                               "q_before": 0.80, "ts": t0},
            f"{prof}::temperature": {"profile": prof, "param": "temperature",
                                     "before": 0.3, "q_before": 0.80, "ts": t0},
        }
        L._good_settings_cache = s
        L._write_json_atomic(L._good_settings_path(), s, indent=2)
    conn = sqlite3.connect(str(L.DB_PATH))
    for i in range(14):
        conn.execute("INSERT INTO param_observations (ts, profile, sentence_type, top_p, "
                     "quality, voice) VALUES (?,?,?,?,?,?)",
                     (t0 + 10 + i, prof, "sentence", 0.91, 0.85, "ckpttest"))
    conn.commit()
    conn.close()
    L._last_autotune_ts = 0
    acts = L.run_autotune_cycle()
    ck = store().get(L._vk("_autotune_ckpt")) or {}
    check("a change that held is closed", f"{prof}::top_p" in ck, False)
    check("and the cycle says so", any(a.startswith("✓ kept") for a in acts), True)
    check("an old temperature checkpoint is dropped", f"{prof}::temperature" in ck, False)
    L.set_autotune(False)
    L.set_active_voice("default")


@section("4 and B. revert only undoes what the verdict did")
def _():
    def temp_of(cid):
        return (store().get(L._vk(f"prof:{keys_of(cid)[0]}")) or {}).get("temperature")

    # A chunk marked negative by a report, not a verdict.
    cid = logged("A chunk that only ever had a report filed on it.")
    report(chunk_id=cid, chunk_text="A chunk that only ever had a report filed on it.",
           issue="OTHER", action="FLAG_PATTERN", token="report")
    t_before, n_before = temp_of(cid), L.get_counter("negative_total")
    verdict(chunk_id=cid, verdict="revert")
    check("reverting a report-only mark leaves the temperature", temp_of(cid), t_before)
    check("and the counter", L.get_counter("negative_total"), n_before)

    # A real thumbs-down, then revert.
    cid = logged("A chunk the user marked as sounding wrong.")
    n0 = L.get_counter("negative_total")
    verdict(chunk_id=cid, verdict="sounded_wrong")
    stepped = temp_of(cid)
    check("the thumbs-down counted once", L.get_counter("negative_total"), n0 + 1)
    verdict(chunk_id=cid, verdict="revert")
    check("revert puts the temperature back", round(temp_of(cid) - stepped, 3), 0.03)
    check("and the count", L.get_counter("negative_total"), n0)

    # Recorded only: no step, and revert takes none back.
    cid = logged("One of many chunks marked wrong in one go.")
    t0 = temp_of(cid)
    body, code = verdict(chunk_id=cid, verdict="sounded_wrong", record_only=True)
    check("a recorded-only thumbs-down is accepted", code, 200)
    check("and takes no temperature step", temp_of(cid), t0)
    conn = sqlite3.connect(str(L.DB_PATH))
    row = conn.execute("SELECT user_feedback, user_verdict FROM chunks WHERE id=?", (cid,)).fetchone()
    conn.close()
    check("but the chunk is marked rejected", row, ("negative", "sounded_wrong_recorded"))
    verdict(chunk_id=cid, verdict="revert")
    check("revert does not invent a step to undo", temp_of(cid), t0)


@section("5. thumbs-up and solid reinforce the keys synthesis reads")
def _():
    # A fingerprint no other section touches, so the key starts empty.
    cid = logged("A sentence the user loved hearing read aloud.", temp=0.25,
                 stype="definition", profile="simple|long|commas|symbolic")
    k0 = keys_of(cid)[0]
    L.confirm_chunk_quality(cid)
    e = store().get(L._vk(f"prof:{k0}")) or {}
    check("a thumbs-up starts the specific profile key", e.get("temperature"), 0.25)
    L.confirm_chunk_quality(cid)
    check("two of them make it count for synthesis",
          L.resolve_profile_temperature(keys_of(cid), 0.45), 0.25)

    cid2 = logged("Another sentence of the same kind, heard and fine.", temp=0.35,
                  stype="definition", profile="simple|long|commas|symbolic")
    before = (store().get(L._vk(f"prof:{k0}")) or {})
    L.mark_session_solid(played=[cid2])
    after = store().get(L._vk(f"prof:{k0}")) or {}
    check("solid pulls it gently toward what was used",
          after.get("temperature"), round(0.25 * 0.92 + 0.35 * 0.08, 3))
    check("without adding evidence", after.get("count"), before.get("count"))


@section("7. a skipped-word report splits only before a safe phrase")
def _():
    text = "We measured the throughput, and then the latency dropped sharply."
    before = len(rules("SPLIT"))
    r = report(chunk_text=text, issue="SKIP", action="ADJUST_CHUNK")
    check("no split from the chunk prefix", len(rules("SPLIT")), before)
    check("the reply says why", "no split rule made" in r.get("message", ""), True)
    report(chunk_text=text, issue="SKIP", action="ADJUST_CHUNK", token="the")
    check("not before a lone short word", len(rules("SPLIT")), before)
    report(chunk_text=text, issue="CUTOFF", action="ADJUST_CHUNK", token="then the latency")
    check("a dropped phrase in the chunk gets a split",
          any(x["pattern"] == "then the latency" for x in rules("SPLIT")), True)


@section("8. a corrected pronunciation replaces the old one, and e.g. matches")
def _():
    report(chunk_text="Gemini Nano runs on device.", issue="PRONUNCIATION",
           action="ADD_TO_STORE", token="Gemini Nano", expected="Gem.E.ni NAH.NO")
    r = report(chunk_text="Gemini Nano runs on device.", issue="PRONUNCIATION",
               action="ADD_TO_STORE", token="Gemini Nano", expected="Gem,ini Na.No")
    vals = [x["value"] for x in rules("PRONUNCIATION") if x["pattern"] == "Gemini Nano"]
    check("one rule, holding the newer value", vals, ["Gem,ini Na.No"])
    check("and the reply says it replaced the old one", "replacing" in r.get("message", ""), True)
    L._add_rule("PRONUNCIATION", "Gemini Nano", "spell_abbreviation", "gee en", source="AUTO")
    vals = [x["value"] for x in rules("PRONUNCIATION") if x["pattern"] == "Gemini Nano"]
    check("an automatic rule never overwrites the user's", vals, ["Gem,ini Na.No"])
    report(chunk_text="Use e.g. a list.", issue="PRONUNCIATION", action="ADD_TO_STORE",
           token="e.g.", expected="for example")
    check("a token ending in a full stop matches",
          L.apply_learned_rules("Use e.g. a list."), "Use for example a list.")


@section("9. a report starts from today's temperature and is heard next time")
def _():
    cid = logged("A robotic sentence on a fingerprint nothing has learned yet.",
                 profile="dense|long|commas|technical")
    keys = keys_of(cid)
    # What synthesis would use for this chunk today, with the slider at 0.45.
    # Earlier sections taught the coarser keys, so it need not be 0.45 itself.
    start = L.resolve_profile_temperature(keys, 0.45)
    check("today's temperature is not the old fixed 0.33", start != 0.33, True)
    r = report(chunk_id=cid, chunk_text="x", issue="ROBOTIC", action="ADJUST_TEMP_UP")
    new_t = (store().get(L._vk(f"prof:{keys[0]}")) or {}).get("temperature")
    check("more expressive goes up from there, not from 0.33",
          new_t, round(start + 0.03, 3))
    check("and synthesis uses it on the next read",
          L.resolve_profile_temperature(keys, 0.45), new_t)

    with L._good_settings_lock:
        s = dict(L._load_good_settings())
        s[L._vk("band:dense")] = {"rate_mod": 1.15}
        L._good_settings_cache = s
        L._write_json_atomic(L._good_settings_path(), s, indent=2)
    r = report(chunk_id=cid, chunk_text="x", issue="TOO_SLOW", action="ADJUST_RATE_UP")
    check("at the limit the reply says nothing changed",
          "already at the fastest" in r.get("message", ""), True)
    entry = store().get(L._vk("band:dense")) or {}
    check("and no step is recorded", (entry.get("rate_mod"), "last_step_dir" in entry),
          (1.15, False))


@section("A. skipped acronyms keep their casing so the speller can act")
def _():
    heard = {"the", "model", "uses", "for", "training"}
    got = L._skipped_words("The BoW model uses CNF for training GPUs", lambda w: w in heard)
    check("CNF and BoW survive as written", [w for w in got if w in ("BoW", "CNF")],
          ["BoW", "CNF"])
    check("a plural acronym is not an acronym to spell",
          L._looks_like_acronym("GPUs"), False)
    check("nor is an ordinary capitalised word", L._looks_like_acronym("Hello"), False)
    L._add_rule("PRONUNCIATION", "BoW", "spell_abbreviation", "bee oh double-you",
                source="AUTO")
    check("a learned spelling matches as written",
          L.apply_learned_rules("The BoW model"), "The bee oh double-you model")
    check("and never the ordinary word", L.apply_learned_rules("a bow and arrow"),
          "a bow and arrow")


@section("C. every report reply carries a message")
def _():
    r = report(chunk_text="Some chunk of text.", issue="OTHER", action="FLAG_PATTERN",
               token="chunk", confidence="LOW")
    check("a pending report says nothing changes yet",
          r.get("message", "").startswith("Logged. Nothing changes until"), True)
    ns["request"] = _Req({"chunk_text": "x", "issue": "PRONUNCIATION",
                          "action": "ADD_TO_STORE"})
    body, code = ns["submit_report_route"]()
    check("a refused report is a 400 with a message", (code, bool(body.get("message"))),
          (400, True))


@section("10. the migration switches off the dead rules, and only those")
def _():
    conn = sqlite3.connect(str(L.DB_PATH))
    conn.execute("DELETE FROM counters WHERE name=?", (L._RETIRE_MARKER,))
    chunk = "Watch the following video for a brief demonstration of how t"
    conn.execute("INSERT INTO reports (ts, chunk_text, issue, token, action) VALUES (?,?,?,?,?)",
                 (time.time(), chunk, "PUNCT", "", "PUNCT"))
    conn.execute("INSERT INTO reports (ts, chunk_text, issue, token, action) VALUES (?,?,?,?,?)",
                 (time.time(), "BoW would not distinguish, between them", "SKIP", "", "ADJUST_CHUNK"))
    rows = [
        ("PUNCT", chunk[:30], "repunctuate", ".", "REPORT"),
        ("PUNCT", "however", "repunctuate", ",", "REPORT"),
        ("SPLIT", "BoW would not distinguish between them", "split_before", "", "REPORT"),
        ("SPLIT", "a phrase the user chose", "split_before", "", "REPORT"),
        ("MONITOR", "6ddadf0e2547c32b", "minor_issue", "", "REPORT"),
        ("MONITOR", "14e9ab10-859a-4b", "minor_issue", "", "REPORT"),
        ("BOUNDARY", "Select an icon to .", "review_split", "", "REPORT"),
        ("BOUNDARY", "2024),", "keep_with_previous", "", "REPORT"),
        ("PRONUNCIATION", "6ddadf0e2547c32c", "store_override", "x", "REPORT"),
    ]
    for r in rows:
        conn.execute("INSERT INTO rules (ts, rule_type, pattern, action, value, source) "
                     "VALUES (?,?,?,?,?,?)", (time.time(),) + r)
    conn.commit()
    conn.close()
    counts = L._retire_dead_report_rules()
    check("it switched off exactly the dead ones", counts,
          {"PUNCT": 1, "SPLIT": 1, "MONITOR": 2, "BOUNDARY": 1})
    conn = sqlite3.connect(str(L.DB_PATH))
    act = dict(conn.execute("SELECT pattern, active FROM rules WHERE pattern IN (?,?,?,?)",
                            ("however", "a phrase the user chose", "2024),",
                             "6ddadf0e2547c32c")).fetchall())
    hist = conn.execute("SELECT COUNT(*) FROM history WHERE event_type='MIGRATION'").fetchone()[0]
    conn.close()
    check("a deliberate punctuation rule stays on", act.get("however"), 1)
    check("a deliberate split stays on", act.get("a phrase the user chose"), 1)
    check("an exported boundary rule stays on", act.get("2024),"), 1)
    check("pronunciation rules are never touched", act.get("6ddadf0e2547c32c"), 1)
    check("it is logged", hist >= 1, True)
    check("and it runs once", L._retire_dead_report_rules(), None)


print()
print(f"{PASS} passed, {FAIL} failed")
shutil.rmtree(HERE, ignore_errors=True)   # best effort: the learner may hold a handle
sys.exit(1 if FAIL else 0)
