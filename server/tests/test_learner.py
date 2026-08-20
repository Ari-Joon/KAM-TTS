"""Import the real learner.py against a COPY of the database and exercise the
paths that matter: migrations, error logging, diagnosis, the rule cache, report
validation and the fingerprint round-trip.

It always works on a throwaway copy in a temp directory, never on the live
tts_quality.db, because these tests write rules, reports and observations. If a
database exists next to the server it gets copied so the migrations run against
realistic data; otherwise learner builds an empty one and the tests still pass.
"""
import sys, pathlib, sqlite3, json, shutil, tempfile

SERVER_DIR = pathlib.Path(__file__).resolve().parent.parent
HERE = pathlib.Path(tempfile.mkdtemp(prefix="kam_learner_test_"))
for name in ("learner.py", "alignment.py"):
    shutil.copy(SERVER_DIR / name, HERE / name)
for name in ("tts_quality.db", "good_settings.json"):
    if (SERVER_DIR / name).exists():
        shutil.copy(SERVER_DIR / name, HERE / name)

sys.path.insert(0, str(HERE))
import learner as L                      # runs _init_db + _run_migrations

PASS = FAIL = 0
def check(label, got, want):
    global PASS, FAIL
    if got == want:
        PASS += 1; print(f"  ok   {label}")
    else:
        FAIL += 1; print(f"  FAIL {label}\n         got:  {got!r}\n         want: {want!r}")

print("\n=== migrations added the new columns ===")
conn = sqlite3.connect(str(L.DB_PATH))
cols = {r[1] for r in conn.execute("PRAGMA table_info(chunks)")}
for c in ("error_type", "error_stage", "error_detail", "is_fragment",
          "pos_available", "user_verdict", "user_note", "applied_action"):
    check(f"chunks.{c} exists", c in cols, True)
conn.close()

print("\n=== report validation is case-insensitive now ===")
for issue in ("pronunciation", "Pronunciation", "PRONUNCIATION"):
    r = L.submit_report(chunk_text="A test chunk of text", issue=issue,
                        token="GPU", expected="gee pee you",
                        action="add_to_store", confidence="high")
    check(f"issue={issue!r} applied", bool(r.get("applied")), True)

print("\n=== failed chunks are content-addressed and explainable ===")
text = "Riff24Khz16BitMonoPcm output stream"
cid = L.log_error(text, error_type="RuntimeError", stage="inference",
                  message="CUDA error: device-side assert triggered srcIndex",
                  attempts=[{"attempt": 1, "stage": "inference",
                             "type": "RuntimeError", "message": "srcIndex assert"}])
import hashlib
want_id = hashlib.sha1(text.strip().encode()).hexdigest()[:16]
check("id is the content hash (findable)", cid, want_id)

d = L.diagnose_chunk(chunk_id=cid)
check("diagnose finds it", d["found"], True)
check("failure type recorded", d["failure"]["type"], "RuntimeError")
check("failure stage recorded", d["failure"]["stage"], "inference")
check("attempt trace kept", len(d["failure"]["attempts"]), 1)
print(f"       message: {d['failure']['message'][:60]!r}")

print("\n=== blacklisting only fires on real vocabulary crashes ===")
before = {r["pattern"] for r in L.get_rules(active_only=True)
          if r["rule_type"] == "BLACKLIST"}
L.log_error("The alpha-beta pruning ratio was 3:1 overall",
            error_type="RuntimeError", stage="inference",
            message="CUDA out of memory. Tried to allocate 2.00 GiB")
after = {r["pattern"] for r in L.get_rules(active_only=True)
         if r["rule_type"] == "BLACKLIST"}
check("OOM blacklists nothing", after - before, set())

L.log_error("The Riff24Khz16BitMonoPcm token", error_type="RuntimeError",
            stage="inference", message="device-side assert triggered")
after2 = {r["pattern"] for r in L.get_rules(active_only=True)
          if r["rule_type"] == "BLACKLIST"}
check("vocabulary crash does blacklist", "Riff24Khz16BitMonoPcm" in after2, True)

print("\n=== rule cache stays consistent with writes ===")
n0 = len(L._active_rules())
L._add_rule("FLAG", "zzz_cache_probe", "warn", "", source="MANUAL")
check("cache sees the new rule", len(L._active_rules()), n0 + 1)
rid = [r["id"] for r in L._active_rules() if r["pattern"] == "zzz_cache_probe"][0]
L.delete_rule(rid)
check("cache sees the deletion", len(L._active_rules()), n0)

print("\n=== fingerprint round-trip: what is written is what is read ===")
spoken = "The GPU ran sequel three point five queries."
display = "The GPU ran SQL 3.5 queries."
p = L.chunk_profile(spoken, "sentence")
pstr = f"{p['band']}|{p['length']}|{p['punct']}|{p['lexis']}"
cid2 = L.log_chunk(spoken, "sentence", 35, display_text=display,
                   synth_params={"temperature": 0.4}, profile_str=pstr)
conn = sqlite3.connect(str(L.DB_PATH))
stored = conn.execute("SELECT profile FROM chunks WHERE id=?", (cid2,)).fetchone()[0]
conn.close()
check("stored key == key used at synthesis", stored, pstr)
old_key = (lambda t: (lambda q: f"{q['band']}|{q['length']}|{q['punct']}|{q['lexis']}")
           (L.chunk_profile(t, "sentence")))(display)
print(f"       synthesis/stored: {pstr}")
print(f"       old (from display text): {old_key}   <- these used to differ")
check("the old scheme really did disagree", old_key != pstr, True)

print("\n=== analysis backlog is observable ===")
b = L.analysis_backlog()
check("backlog reports capacity", b["capacity"], 50)
print(f"       {b}")

print("\n=== voice isolation covers per-type params ===")
L.set_active_voice("default")
L.record_good_settings("sentence", {"temperature": 0.5, "repetition_penalty": 3.0,
                                    "top_k": 40, "top_p": 0.9})
base = L.get_preferred_param("sentence", "repetition_penalty", 4.1, 1.0, 10.0)
L.set_active_voice("someone_else")
other = L.get_preferred_param("sentence", "repetition_penalty", 4.1, 1.0, 10.0)
L.set_active_voice("default")
check("default voice uses its learned value", base != 4.1, True)
check("other voice does NOT inherit it", other, 4.1)

print("\n=== renaming a voice carries its learning with it ===")
# A voice's name is the key on every row and on every learned setting, so a
# rename that moved only the folder would leave a profile that looks unchanged
# and has quietly forgotten everything. The neighbour voice is here throughout
# because the real danger is not failing to move rows, it is moving somebody
# else's.
OLD, NEW, OTHER = "_kam_test_old", "_kam_test_new", "_kam_test_other"
_c = sqlite3.connect(str(L.DB_PATH))
with _c:
    for i in range(4):
        _c.execute("INSERT INTO chunks (id, text, voice) VALUES (?,?,?)",
                   (f"{OLD}-{i}", "a chunk", OLD))
    for i in range(7):
        _c.execute("INSERT INTO param_observations (profile, voice, quality) VALUES (?,?,?)",
                   ("band|len|punct|lex", OLD, 0.8))
    _c.execute("INSERT INTO chunks (id, text, voice) VALUES (?,?,?)",
               (f"{OTHER}-0", "not mine", OTHER))
    _c.execute("INSERT INTO param_observations (profile, voice, quality) VALUES (?,?,?)",
               ("band|len|punct|lex", OTHER, 0.5))
_c.close()

L.set_active_voice(OLD)
L.record_good_settings("sentence", {"temperature": 0.42, "repetition_penalty": 3.3,
                                    "top_k": 40, "top_p": 0.9})
L.set_active_voice(OTHER)
L.record_good_settings("sentence", {"temperature": 0.77, "repetition_penalty": 5.5,
                                    "top_k": 40, "top_p": 0.9})
L.set_active_voice("default")

before = L.voice_data_counts(OLD)
check("counts the chunks",       before["chunks"], 4)
check("counts the observations", before["observations"], 7)
check("counts the learned entries", before["settings"] >= 1, True)

moved = L.rename_voice_data(OLD, NEW)
check("moved every chunk",       moved["chunks"], 4)
check("moved every observation", moved["observations"], 7)
check("moved the learned entries", moved["settings"] >= 1, True)

after_old, after_new = L.voice_data_counts(OLD), L.voice_data_counts(NEW)
check("nothing left under the old name", (after_old["chunks"], after_old["observations"],
                                          after_old["settings"]), (0, 0, 0))
check("all of it under the new name",    (after_new["chunks"], after_new["observations"]), (4, 7))

# The learned value has to survive the move, not merely the row count.
L.set_active_voice(NEW)
check("and the tuned value came too",
      L.get_preferred_param("sentence", "repetition_penalty", 4.1, 1.0, 10.0) != 4.1, True)
L.set_active_voice("default")

neighbour = L.voice_data_counts(OTHER)
check("the neighbouring voice is untouched",
      (neighbour["chunks"], neighbour["observations"]), (1, 1))
check("and keeps its own learned entry", neighbour["settings"] >= 1, True)

print("\n=== forgetting a voice takes its own and nothing else ===")
gone = L.forget_voice_data(NEW)
check("removed every chunk",       gone["chunks"], 4)
check("removed every observation", gone["observations"], 7)
check("removed the learned entries", gone["settings"] >= 1, True)
emptied = L.voice_data_counts(NEW)
check("nothing left for it", (emptied["chunks"], emptied["observations"],
                              emptied["settings"]), (0, 0, 0))

still = L.voice_data_counts(OTHER)
check("the neighbour survived the delete",
      (still["chunks"], still["observations"]), (1, 1))
check("with its learned entry intact", still["settings"] >= 1, True)

L.forget_voice_data(OTHER)     # leave the copied database as we found it

print(f"\n{'='*62}\n  {PASS} passed, {FAIL} failed\n{'='*62}")
sys.exit(1 if FAIL else 0)
