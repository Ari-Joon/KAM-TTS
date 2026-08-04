"""Verify the three reinforcement fixes against a copy of the real database.

1. thumbs-up reinforces the parameters the chunk WAS MADE WITH, not the live ones
2. the learner never writes to the user's live settings
3. rejected attempts become usable negative evidence
"""
import sys, pathlib, sqlite3, json, time, hashlib, shutil, tempfile

# Always a throwaway copy, never the live database: these tests write chunks,
# observations and learned settings.
SERVER_DIR = pathlib.Path(__file__).resolve().parent.parent
HERE = pathlib.Path(tempfile.mkdtemp(prefix="kam_reinforce_test_"))
for _n in ("learner.py", "alignment.py"):
    shutil.copy(SERVER_DIR / _n, HERE / _n)
for _n in ("tts_quality.db", "good_settings.json"):
    if (SERVER_DIR / _n).exists():
        shutil.copy(SERVER_DIR / _n, HERE / _n)

sys.path.insert(0, str(HERE))
import learner as L

PASS = FAIL = 0
def check(label, got, want):
    global PASS, FAIL
    if got == want: PASS += 1; print(f"  ok   {label}")
    else:
        FAIL += 1; print(f"  FAIL {label}\n         got:  {got!r}\n         want: {want!r}")

L.set_active_voice("reinforce_test")          # isolate from real learned data

# ── 1. Positive reinforcement uses the chunk's own parameters ───────────────
print("\n=== 1. thumbs-up reinforces what the chunk actually used ===")

CHUNK_TEMP = 0.22          # what this chunk was synthesised with
LIVE_TEMP  = 0.61          # what the slider drifted to afterwards
live = {"temperature": LIVE_TEMP, "top_p": 0.9, "top_k": 45,
        "repetition_penalty": 4.1, "speed": 1.2}
L.register_live_settings(live)

cid = L.log_chunk("A calm declarative sentence for the test.", "sentence", 35,
                  synth_params={"temperature": CHUNK_TEMP, "top_p": 0.85,
                                "top_k": 40, "repetition_penalty": 4.6,
                                "speed": 1.1},
                  profile_str="normal|medium|clean|plain")
# Give it the acoustic metrics so the full (non-pending) path runs.
with L._db_lock:
    c = L._get_db()
    c.execute("UPDATE chunks SET pitch_variance=22.0, energy_tail=0.2, "
              "voice_consistency=0.9 WHERE id=?", (cid,))
    c.commit(); c.close()

res = L.confirm_chunk_quality(cid)
check("reinforcement ran", res.get("reinforced"), True)
got = res.get("reinforced_settings") or {}
check("reinforced the CHUNK's temperature", got.get("temperature"), CHUNK_TEMP)
check("did not reinforce the live slider", got.get("temperature") == LIVE_TEMP, False)
check("reinforced the chunk's repetition_penalty",
      got.get("repetition_penalty"), 4.6)
stored = L._load_good_settings().get(L._vk("sentence")) or {}
print(f"       chunk used {CHUNK_TEMP}, slider read {LIVE_TEMP}, "
      f"stored {stored.get('temperature')}")
check("stored value came from the chunk, not the slider",
      abs(stored.get("temperature", 9) - CHUNK_TEMP) < 0.05, True)

# ── 2. The learner never writes to the live settings ────────────────────────
print("\n=== 2. the user's live settings are never written by the learner ===")
before = dict(live)

# Drive the paths that used to mutate it.
L.mark_session_solid(played=[cid])
prof = L.lookup_chunk_profile(cid, None)
L._adjust_profile_temperature(prof["keys"], -0.02)
check("live settings unchanged after session digest + auto-tune", live, before)

import re as _re
src = pathlib.Path(HERE / "learner.py").read_text(encoding="utf-8")
writes = _re.findall(r"_live_settings_ref\s*\[[^\]]+\]\s*=", src)
check("no assignment into _live_settings_ref anywhere in learner.py",
      writes, [])

# Solid reinforcement should move toward what the chunks used, not the slider.
L.set_active_voice("solid_test")
ids = []
for i, t in enumerate((0.20, 0.24, 0.22)):
    cid2 = L.log_chunk(f"Solid session chunk number {i} of the test set.",
                       "sentence", 35,
                       synth_params={"temperature": t, "top_p": 0.9, "top_k": 45,
                                     "repetition_penalty": 4.1, "speed": 1.2},
                       profile_str="normal|medium|clean|plain")
    ids.append(cid2)
L.mark_session_solid(played=ids)
entry = L._load_good_settings().get(L._vk("sentence")) or {}
mean_used = (0.20 + 0.24 + 0.22) / 3
print(f"       chunks used ~{mean_used:.3f}, slider read {LIVE_TEMP}, "
      f"learned {entry.get('temperature')}")
check("solid reinforcement moved toward the chunks, away from the slider",
      abs(entry["temperature"] - mean_used) < abs(entry["temperature"] - LIVE_TEMP),
      True)

# ── 3. Rejected attempts become usable evidence ─────────────────────────────
print("\n=== 3. rejected attempts are recorded as negative evidence ===")
L.set_active_voice("reject_test")
PROF = "dense|long|complex|technical"

def obs_for(prof):
    with L._db_lock:
        c = L._get_db()
        rows = c.execute(
            "SELECT temperature, quality, COALESCE(rejected,0), problem "
            "FROM param_observations WHERE voice=? AND profile=?",
            ("reject_test", prof)).fetchall()
        c.close()
    return [tuple(r) for r in rows]

check("no evidence to begin with", len(obs_for(PROF)), 0)
L.record_rejected_attempt(PROF, "sentence",
                          {"temperature": 0.45, "top_p": 0.9, "top_k": 45,
                           "repetition_penalty": 4.1, "speed": 1.2}, "runaway")
rows = obs_for(PROF)
check("the rejected attempt was recorded", len(rows), 1)
check("recorded at the temperature that failed", rows[0][0], 0.45)
check("recorded as unusable quality", rows[0][1], 0.0)
check("flagged as rejected, not as a shipped chunk", rows[0][2], 1)
check("the failure mode is kept", rows[0][3], "runaway")

# The real point: this is what gives the estimator enough spread to act.
print("\n       — does it unblock the autotune estimator? —")
def spread_ok(prof):
    with L._db_lock:
        c = L._get_db()
        r = L._profile_quality_stats(prof, "temperature", c)
        c.close()
    return r

# A realistic read: 14 chunks, all at essentially the same temperature.
FLAT = "flat|medium|clean|plain"
for i in range(14):
    L.record_rejected_attempt.__self__ if False else None
    with L._db_lock:
        c = L._get_db()
        c.execute("INSERT INTO param_observations "
                  "(ts, profile, sentence_type, temperature, quality, voice, rejected) "
                  "VALUES (?,?,?,?,?,?,0)",
                  (time.time(), FLAT, "sentence", 0.33 + (i % 2) * 0.001,
                   0.80 + (i % 3) * 0.01, "reject_test"))
        c.commit(); c.close()
check("a normal read gives the estimator nothing to work with (no spread)",
      spread_ok(FLAT), None)

# Now the retry ladder does what a normal read never does: it moves the knob.
for temp, q in ((0.45, None), (0.45, None), (0.29, None)):
    L.record_rejected_attempt(FLAT, "sentence", {"temperature": temp},
                              "looping")
stats = spread_ok(FLAT)
check("with retry evidence the estimator can now judge temperature",
      stats is not None, True)
if stats:
    low_q, high_q, n = stats
    print(f"       low-temperature half mean quality  {low_q:.3f}")
    print(f"       high-temperature half mean quality {high_q:.3f}   (n={n})")
    check("and it correctly sees the high end as worse", high_q < low_q, True)

# ── stats stay interpretable ────────────────────────────────────────────────
print("\n=== displayed quality still means 'what the user heard' ===")
ins = L.ai_insights()
rej = ins.get("rejections")
check("rejections are reported separately", rej is not None, True)
if rej:
    print(f"       caught={rej['caught']} rate={rej['rate']} "
          f"by_problem={rej['by_problem']}")
    check("failure modes are broken down", "looping" in rej["by_problem"], True)

# Clean up the synthetic voices so the real DB copy isn't polluted further.
with L._db_lock:
    c = L._get_db()
    for v in ("reinforce_test", "solid_test", "reject_test"):
        c.execute("DELETE FROM param_observations WHERE voice=?", (v,))
        c.execute("DELETE FROM chunks WHERE voice=?", (v,))
    c.commit(); c.close()

print(f"\n{'='*64}\n  {PASS} passed, {FAIL} failed\n{'='*64}")
sys.exit(1 if FAIL else 0)
