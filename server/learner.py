"""
learner.py — the quality monitor and adaptive learning system
=============================================================
This sits alongside server.py and does six things:

  1. Chunk logging       every synthesis attempt goes into SQLite
  2. Quality analysis    Whisper for accuracy, librosa for prosody and voice
  3. Passive feedback    the structured reports from the popup UI
  4. Auto-actions        pronunciation store updates, chunk rules, blacklisting
  5. Baseline profiling  the characteristic fingerprint of your voice
  6. Report API          a rigid structured format with no free-text ambiguity

All the analysis runs on background threads so none of it costs playback any
latency. It also can't overfit, since every change maps to an explicit rule you
can look at and delete.
"""

import os
import re
import hashlib
import json
import time
import queue
import sqlite3
import threading
from pathlib import Path
from datetime import datetime

# ---
# Paths, since all of these files live next to server.py
# ---
_HERE         = Path(__file__).parent
DB_PATH       = _HERE / "tts_quality.db"
STORE_PATH    = _HERE / "pronunciation_store.json"
PUNCT_PATH    = _HERE / "punctuation_corrections.json"
BASELINE_PATH = _HERE / "voice_baseline.json"

# --- Voice isolation ---
# Every learned structure is namespaced by the active voice so feedback given
# on one clone never bleeds into another. "default" keeps the original bare
# keys/paths, so existing installs migrate for free. Only the pronunciation
# store is deliberately shared: how a WORD is said is voice-independent.
_ACTIVE_VOICE = "default"

def set_active_voice(voice_id):
    """Called by the server on startup and on every voice switch."""
    global _ACTIVE_VOICE
    _ACTIVE_VOICE = (voice_id or "default").strip() or "default"

def _vk(key):
    """Voice-namespace a good-settings store key ('default' stays bare)."""
    return key if _ACTIVE_VOICE == "default" else f"v:{_ACTIVE_VOICE}:{key}"

def _baseline_path():
    """The baseline file for one voice, since drift has to be measured against
    the active clone rather than whichever voice recorded a baseline first."""
    if _ACTIVE_VOICE == "default":
        return BASELINE_PATH
    return _HERE / f"voice_baseline_{_ACTIVE_VOICE}.json"

# ---
# Optional heavy dependencies, which degrade gracefully if they aren't installed
# ---
# Each optional dependency is bound to a name UNCONDITIONALLY (to None on
# failure) so static analysers never see a "possibly unbound" path. Runtime
# guards (_WHISPER_OK / _LIBROSA_OK) gate every actual use.
_whisper       = None
_WHISPER_MODEL = None
_WHISPER_DEVICE = "cpu"   # set to 'cuda' on first load when a GPU is available
# Probe whether whisper is installed WITHOUT importing it. The actual heavy
# import is deferred to the first analysis (saves ~2-3s on every boot).
import importlib.util as _ilu
import alignment as _alignment  # word-level alignment analysis (Part 3)
_WHISPER_OK = _ilu.find_spec("whisper") is not None

_librosa    = None
_np         = None
_LIBROSA_OK = False
try:
    import numpy as _np
except ImportError:
    _np = None
try:
    import librosa as _librosa  # type: ignore  # optional dep, guarded
    _LIBROSA_OK = _np is not None
except ImportError:
    _librosa = None

# ---
# Database
# ---
_db_lock = threading.Lock()

def _get_db():
    """Return a thread-local SQLite connection."""
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def _init_db():
    with _db_lock:
        conn = _get_db()
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS chunks (
            id           TEXT PRIMARY KEY,
            ts           REAL,
            text         TEXT,
            sentence_type TEXT,
            silence_ms   INTEGER,
            char_count   INTEGER,
            success      INTEGER DEFAULT 1,
            retried      INTEGER DEFAULT 0,
            skipped      INTEGER DEFAULT 0,
            duration_ms  INTEGER,
            -- Quality metrics (filled by background analysis)
            whisper_accuracy  REAL,
            pitch_variance    REAL,
            energy_tail       REAL,
            voice_consistency REAL,
            quality_score     REAL,
            quality_flags     TEXT
        );

        CREATE TABLE IF NOT EXISTS reports (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            ts           REAL,
            chunk_id     TEXT,
            chunk_text   TEXT,
            issue        TEXT,   -- PRONUNCIATION|SKIP|HALLUCINATION|PROSODY|SPEED|VOICE|OTHER
            token        TEXT,
            expected     TEXT,
            heard        TEXT,
            action       TEXT,   -- ADD_TO_STORE|ADJUST_CHUNK|BLACKLIST|FLAG_PATTERN|IGNORE
            confidence   TEXT,   -- HIGH|MEDIUM|LOW
            notes        TEXT,
            applied      INTEGER DEFAULT 0,
            apply_result TEXT
        );

        CREATE TABLE IF NOT EXISTS rules (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            ts           REAL,
            rule_type    TEXT,   -- SPLIT|MERGE|RECLASSIFY|BLACKLIST|PRONUNCIATION
            pattern      TEXT,
            action       TEXT,
            value        TEXT,
            hits         INTEGER DEFAULT 0,
            active       INTEGER DEFAULT 1,
            source       TEXT    -- REPORT|AUTO|BASELINE
        );

        CREATE TABLE IF NOT EXISTS baseline (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            ts           REAL,
            pitch_mean   REAL,
            pitch_std    REAL,
            energy_mean  REAL,
            energy_std   REAL,
            speaking_rate REAL,
            sample_count INTEGER
        );

        -- Closed-loop observation layer: one row per analysed chunk pairing its
        -- profile + the exact synth params used with the Whisper quality score
        -- that resulted. This is the cause→effect dataset the autonomous nudger
        -- learns from (which param values yield the best quality per profile).
        -- Pure observation; recording here changes nothing on its own.
        CREATE TABLE IF NOT EXISTS param_observations (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            ts           REAL,
            profile      TEXT,     -- band|length|punct|lexis
            sentence_type TEXT,
            temperature  REAL,
            top_p        REAL,
            top_k        INTEGER,
            rep_penalty  REAL,
            speed        REAL,
            quality      REAL,     -- Whisper-derived quality_score (0..1)
            accuracy     REAL      -- whisper_accuracy alone
        );

        -- Append-only learning history. One row per change the model made,
        -- whether automatic or from user feedback. This is the PERMANENT record
        -- (survives "clear", which only wipes per-session chunk analysis) and is
        -- the foundation for future per-user profiles. Kept deliberately simple
        -- and numeric so the Stats page can show totals + a scrollable log.
        CREATE TABLE IF NOT EXISTS history (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ts          REAL,
            event_type  TEXT,   -- BLACKLIST|PRONUNCIATION|SPLIT|PUNCT|SUPPRESS|RATE|TEMP|CONFIRM|REPORT
            detail      TEXT,   -- human-readable one-liner
            source      TEXT    -- 'auto' | 'user'
        );
        """)
        # Durable running totals that must survive a stats/console clear (which
        # wipes the chunks table). e.g. lifetime count of solid chunks digested.
        conn.execute("""
        CREATE TABLE IF NOT EXISTS counters (
            name   TEXT PRIMARY KEY,
            value  INTEGER DEFAULT 0
        );
        """)
        conn.commit()
        conn.close()

_init_db()

def _run_migrations():
    """One-shot cleanup migrations for older DB states.

    We deactivate rather than delete so the audit trail is preserved and
    users can re-enable by editing the DB if they genuinely want the rule.
    """
    with _db_lock:
        conn = _get_db()
        # Deactivate stale AUTO letter-spelled pronunciations (e.g. PYTHON → pee-why-tee-aitch-oh-en)
        # These predate the decision to let XTTS handle common abbreviations natively.
        # Heuristic: value contains at least two hyphens AND matches classic letter-spell pattern.
        n = conn.execute(
            """
            UPDATE rules SET active=0
            WHERE rule_type='PRONUNCIATION'
              AND source='AUTO'
              AND value LIKE '%-%-%'
              AND active=1
            """
        ).rowcount
        if n:
            print(f"[LEARNER] Migration: deactivated {n} stale auto letter-spelling rule(s)")
        # Ensure chunks.speaking_rate exists (added for thumbs-up good-settings
        # guidance). Older DBs created before this column need it backfilled.
        try:
            conn.execute("ALTER TABLE chunks ADD COLUMN speaking_rate REAL")
            print("[LEARNER] Migration: added chunks.speaking_rate column")
        except Exception:
            pass  # column already exists
        # Composite chunk profile (band|length|punct|lexis) for fingerprint-aware
        # learning. Stored so the AI view can show why a chunk was adjusted.
        try:
            conn.execute("ALTER TABLE chunks ADD COLUMN profile TEXT")
            print("[LEARNER] Migration: added chunks.profile column")
        except Exception:
            pass  # column already exists
        # The synth params actually used for this chunk, which are what the
        # closed-loop observation layer is built on (which param values gave which
        # Whisper quality, per profile).
        for _col in ("used_temperature REAL", "used_top_p REAL",
                     "used_top_k INTEGER", "used_rep_penalty REAL",
                     "used_speed REAL"):
            try:
                conn.execute(f"ALTER TABLE chunks ADD COLUMN {_col.split()[0]} {_col.split()[1]}")
            except Exception:
                pass
        # Prosody structure signals (spaCy layer): clause density, mid-thought
        # endings and emphasis density per chunk. Stored as observation metadata
        # so I can analyse quality against sentence structure. They're
        # deliberately not part of the profile fingerprint, since that would
        # fragment the evidence. user_feedback is in here because submit_report()
        # writes it and the stats queries read it, but it was never part of
        # CREATE TABLE, so on a fresh install its absence made every report fail.
        # error_type / error_stage / error_detail carry WHY a chunk failed, so a
        # failure can be explained after the fact instead of only in the console
        # scrollback. is_fragment / pos_available record whether the spaCy layer
        # ran and what it concluded, which is what makes a labelling regression
        # visible rather than merely suspected.
        for _col in ("clause_count INTEGER", "ends_mid INTEGER",
                     "emphasis_density REAL", "has_math INTEGER",
                     "voice TEXT", "sentence_count INTEGER",
                     # The verdict columns used to be created by an ALTER TABLE
                     # run inside the feedback request handler itself, on every
                     # single call. They belong here, once, at startup.
                     "user_feedback TEXT", "user_verdict TEXT",
                     "user_note TEXT", "applied_action TEXT",
                     "primary_facet TEXT", "facet_count INTEGER",
                     "synth_ms INTEGER", "rtf REAL",
                     "is_fragment INTEGER", "pos_available INTEGER",
                     "error_type TEXT", "error_stage TEXT", "error_detail TEXT",
                     # How many waveforms were discarded as hallucinated before
                     # one passed, and the verdict on the one finally sent.
                     "rejected INTEGER", "output_check TEXT"):
            try:
                conn.execute(f"ALTER TABLE chunks ADD COLUMN {_col.split()[0]} {_col.split()[1]}")
            except Exception:
                pass
        # Voice isolation: observations carry the voice they were made with, so
        # autotune evidence never mixes clones. Existing rows become 'default'.
        try:
            conn.execute("ALTER TABLE param_observations ADD COLUMN voice TEXT")
        except Exception:
            pass
        # Observations from waveforms that were REJECTED as hallucinated and
        # re-synthesised. They are real evidence (known-bad output at known
        # parameters) and belong in the same table, but must be distinguishable
        # so the UI can report them separately from chunks that shipped.
        for _col in ("rejected INTEGER DEFAULT 0", "problem TEXT"):
            try:
                conn.execute(
                    f"ALTER TABLE param_observations ADD COLUMN {_col}")
            except Exception:
                pass
        try:
            conn.execute("UPDATE chunks SET voice='default' WHERE voice IS NULL")
            conn.execute("UPDATE param_observations SET voice='default' WHERE voice IS NULL")
        except Exception:
            pass
        # Purge the old noisy AUTO FLAG rules, like "FLAG THIS, 118 hits". FLAG
        # doesn't mutate anything so these never improved the audio, they just
        # cluttered the
        # dashboard. New logic no longer creates them (it makes PRONUNCIATION
        # rules for genuine acronyms instead).
        try:
            n = conn.execute(
                "DELETE FROM rules WHERE source='AUTO' AND rule_type='FLAG'"
            ).rowcount
            if n:
                print(f"[LEARNER] Migration: purged {n} noisy AUTO FLAG rule(s)")
        except Exception:
            pass
        conn.commit()
        conn.close()

_run_migrations()

# ---
# Background analysis queue
# ---
_analysis_queue = queue.Queue(maxsize=50)

# --- Synthesis/analysis arbitration ---
# Whisper transcription is CPU-heavy and holds the GIL, starving the Flask
# synthesis thread (the ~45s-per-chunk stall). The server calls
# mark_synthesis_busy(True/False) around each /speak; the worker waits for idle
# (plus a short cooldown) before doing heavy work.
import time as _time
_synth_busy_until = 0.0
_synth_depth      = 0        # number of synthesis calls currently in flight
_synth_lock       = threading.Lock()

# Analysis work that never ran, and why. Whisper listen-back is what feeds the
# whole learning loop, so a chunk silently missing its analysis is a hole in the
# evidence, and the only trace of it used to be a `pass` on a full queue.
_analysis_dropped = 0
_analysis_queued  = 0
_analysis_done    = 0
_analysis_stats_lock = threading.Lock()


def analysis_backlog():
    """Queue depth and lifetime totals, so 'this chunk was never analysed' is an
    answerable question rather than a guess."""
    with _analysis_stats_lock:
        return {"queued": _analysis_queued, "analysed": _analysis_done,
                "dropped": _analysis_dropped, "pending": _analysis_queue.qsize(),
                "capacity": _analysis_queue.maxsize}

# Consecutive low-Whisper-accuracy chunks. Auto-tune only cools the live
# temperature after 2+ in a row, so a single interrupted chunk can't drift it.
_low_accuracy_streak = 0

# --- Word-matching helpers for Whisper comparison ---
_DIGIT_WORD = {'0':'zero','1':'one','2':'two','3':'three','4':'four',
               '5':'five','6':'six','7':'seven','8':'eight','9':'nine'}
_TEENS = {10:'ten',11:'eleven',12:'twelve',13:'thirteen',14:'fourteen',
          15:'fifteen',16:'sixteen',17:'seventeen',18:'eighteen',19:'nineteen'}
_TENS  = {2:'twenty',3:'thirty',4:'forty',5:'fifty',6:'sixty',
          7:'seventy',8:'eighty',9:'ninety'}

def _norm_word(w):
    """Lowercase and strip everything but letters, digits and apostrophes, so
    "logic," matches "logic" and "re-arranging" matches "rearranging"."""
    return re.sub(r"[^a-z0-9']", '', w.lower())

def _int_words(n):
    """Spoken words for a small integer: 42 → ['forty','two']."""
    if n < 10:  return [_DIGIT_WORD[str(n)]]
    if n < 20:  return [_TEENS[n]]
    if n < 100:
        out = [_TENS[n // 10]]
        if n % 10: out.append(_DIGIT_WORD[str(n % 10)])
        return out
    return [_DIGIT_WORD[d] for d in str(n)]

def _spoken_forms(token):
    """Spoken-word equivalents for a token containing digits, so the original
    "7.5.2" matches a transcript saying "seven point five two"."""
    out = set()
    if any(ch.isdigit() for ch in token):
        for part in re.split(r'[^0-9]+', token):
            if part:
                out.update(_int_words(int(part[:4])))
                out.update(_DIGIT_WORD[d] for d in part)
        out.add('point')
    return out

def _ed_le1(a, b):
    """True if edit distance between a and b is at most 1 (one substitution,
    insertion or deletion). Catches Whisper near-misses like cnf/cnff."""
    if a == b:
        return True
    la, lb = len(a), len(b)
    if abs(la - lb) > 1:
        return False
    if la == lb:
        return sum(1 for x, y in zip(a, b) if x != y) <= 1
    if la > lb:
        a, b, la, lb = b, a, lb, la
    i = j = diffs = 0
    while i < la and j < lb:
        if a[i] == b[j]:
            i += 1; j += 1
        else:
            diffs += 1
            if diffs > 1:
                return False
            j += 1
    return True


def mark_synthesis_busy(busy: bool):
    """The server calls this around synthesis. While any synthesis is in flight,
    and for a brief cooldown after the last one finishes, the analysis worker
    holds Whisper back so it never overlaps an active synthesis call.

    It counts the calls in flight rather than setting and clearing one deadline.
    With a deadline two overlapping /speak requests raced, because the first to
    finish cleared the flag to a 1 second cooldown while the second was still
    inside inference, which let Whisper start against a live synthesis. That's
    exactly the CPU and GIL contention the flag exists to prevent.

    A short cooldown after the last call keeps analysis off the device during
    synthesis itself while still letting per-chunk feedback run promptly. The old
    20 second exclusion window was only needed for the 20 to 30 second CPU
    transcription that used to stall synthesis, and keeping it just delayed all
    the feedback until playback had stopped."""
    global _synth_busy_until, _synth_depth
    with _synth_lock:
        if busy:
            _synth_depth += 1
        else:
            _synth_depth = max(0, _synth_depth - 1)
            if _synth_depth == 0:
                _synth_busy_until = _time.monotonic() + 1.0   # brief cooldown


def _synthesis_active():
    with _synth_lock:
        return _synth_depth > 0 or _time.monotonic() < _synth_busy_until


def _spell_abbreviation(word):
    """Spell a short all-caps acronym phonetically so XTTS says the letters
    rather than attempting (and skipping) it as a word. e.g. 'BFS' -> 'bee eff
    ess'. Kept deliberately simple and self-contained; the server has a richer
    renderer, but this is enough to make auto-learned acronym rules useful."""
    _LETTER = {
        'A':'ay','B':'bee','C':'see','D':'dee','E':'ee','F':'eff','G':'gee',
        'H':'aitch','I':'eye','J':'jay','K':'kay','L':'el','M':'em','N':'en',
        'O':'oh','P':'pee','Q':'cue','R':'ar','S':'ess','T':'tee','U':'you',
        'V':'vee','W':'double-you','X':'ex','Y':'why','Z':'zee',
    }
    try:
        return ' '.join(str(_LETTER.get(c, c)) for c in word.upper())
    except Exception:
        return None


def _analysis_worker():
    """
    Background thread: dequeues (chunk_id, wav_bytes, text) tuples,
    runs Whisper + librosa analysis, writes results to DB.

    Defers all heavy work while synthesis is active so it never competes with
    the TTS thread for CPU/GIL.
    """
    global _WHISPER_MODEL, _whisper, _analysis_done
    while True:
        try:
            # Wait out active synthesis before pulling work.
            while _synthesis_active():
                _time.sleep(0.25)
            item = _analysis_queue.get(timeout=5)
            if item is None:
                break
            chunk_id, wav_bytes, text = item

            metrics = {}

            # --- Whisper accuracy ---
            if _WHISPER_OK and wav_bytes:
                try:
                    # Lazy import on first analysis (deferred from boot).
                    if _whisper is None:
                        print("[LEARNER] Importing whisper (first analysis)...")
                        import whisper as _whisper_mod  # type: ignore
                        _whisper = _whisper_mod
                    if _WHISPER_MODEL is None:
                        # Whisper runs on CPU deliberately: sharing the GPU with
                        # XTTS made the two models contend for the same CUDA
                        # device and synthesis time ballooned (35s+ and growing
                        # per chunk). CPU keeps Whisper entirely off the synthesis
                        # GPU; the busy-guard below keeps it off the GIL during
                        # the synthesis call itself.
                        #
                        # (This used to assign _WHISPER_DEVICE without a global
                        # declaration, so it bound a function-local that shadowed
                        # the module constant and left the module value forever
                        # unset, which was only harmless because both said "cpu".)
                        print(f"[LEARNER] Loading Whisper tiny.en on {_WHISPER_DEVICE}...")
                        # tiny.en (English-only) is both faster and more accurate
                        # for English than multilingual tiny.
                        _WHISPER_MODEL = _whisper.load_model("tiny.en", device=_WHISPER_DEVICE)
                        print(f"[LEARNER] Whisper ready ({_WHISPER_DEVICE})")

                    import tempfile
                    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
                        tmp.write(wav_bytes)
                        tmp_path = tmp.name

                    # Final guard: if synthesis started after we dequeued, wait
                    # it out before transcribing (GPU transcribe is brief, so this
                    # rarely blocks anything now).
                    while _synthesis_active():
                        _time.sleep(0.25)
                    result = _WHISPER_MODEL.transcribe(
                        tmp_path, language="en",
                        fp16=(_WHISPER_DEVICE == "cuda"),
                        word_timestamps=True,   # per-word timing + confidence (Part 3)
                    )
                    os.unlink(tmp_path)

                    # Whisper's transcribe() is untyped; coerce explicitly so the
                    # type checker knows result["text"] is a str.
                    transcribed = str(result["text"]).strip().lower()

                    # --- Normalised word matching ---
                    # Raw-token comparison flagged constant false positives:
                    # the transcript kept punctuation ("logic," != "logic"),
                    # and numbers never matched their spoken forms ("7.5.2"
                    # vs "seven point five two"). Normalise both sides, add
                    # spoken-number equivalents, and allow edit distance 1 on
                    # longer words (Whisper's "cnff" for "CNF").
                    # Original tokens split on hyphens/slashes: Whisper hears
                    # "single-layer" as two words, so parts are the comparison
                    # unit. The joined form stays in the match set for the rare
                    # case Whisper joins them.
                    orig_words = []
                    orig_set   = set()
                    for t in text.split():
                        joined = _norm_word(t)
                        if joined:
                            orig_set.add(joined)
                        orig_set |= _spoken_forms(t)
                        for part in re.split(r'[-/]', t):
                            p = _norm_word(part)
                            if p:
                                orig_words.append(p)
                                orig_set.add(p)
                    trans_words = [w for w in (_norm_word(t) for t in transcribed.split()) if w]
                    trans_set   = set(trans_words)

                    _long_orig  = [w for w in orig_set if len(w) >= 5]
                    _long_trans = [w for w in trans_set if len(w) >= 5]
                    def _in_orig(w):
                        return w in orig_set or (len(w) >= 5 and any(_ed_le1(w, o) for o in _long_orig))
                    def _in_trans(w):
                        return w in trans_set or (len(w) >= 5 and any(_ed_le1(w, o) for o in _long_trans))

                    if orig_words:
                        matches  = sum(1 for w in trans_words if _in_orig(w))
                        accuracy = min(1.0, matches / len(orig_words))
                    else:
                        accuracy = 1.0

                    metrics["whisper_accuracy"] = round(accuracy, 3)
                    metrics["whisper_text"]     = transcribed

                    # Skipped words: in the original but truly absent from the
                    # transcript even after normalised and fuzzy matching.
                    skipped_words = [w for w in orig_words if len(w) > 3 and not _in_trans(w)]
                    if skipped_words:
                        metrics["quality_flags"] = metrics.get("quality_flags", "") + \
                            f"|WHISPER_SKIP:{','.join(skipped_words[:3])}"

                    # Hallucinations: transcript words with no normalised or
                    # fuzzy counterpart in the original, minus Whisper's fillers.
                    _FILLERS = {'the','a','an','and','or','of','to','in','is','it',
                                'that','this','was','for','on','are','with','as'}
                    hallucinated = [w for w in trans_words
                                    if len(w) > 3 and w not in _FILLERS and not _in_orig(w)]
                    if len(hallucinated) > 2:
                        metrics["quality_flags"] = metrics.get("quality_flags", "") + \
                            f"|HALLUCINATION:{len(hallucinated)}words"
                        print(f"[LEARNER] Hallucination detected: {hallucinated[:3]}")

                    # --- Word-level alignment (Part 3) ---
                    # Whisper's word timestamps localise problems: which words
                    # were low confidence or rushed, not just a chunk score.
                    try:
                        _align = _alignment.analyse(result)
                        if _align.available:
                            metrics["align_confidence"] = _align.mean_confidence
                            _af = _alignment.summary_flags(_align)
                            if _af:
                                metrics["quality_flags"] = metrics.get("quality_flags", "") + "|" + _af
                            if _align.low_conf_words or _align.rushed_words:
                                print(f"[LEARNER] alignment: conf={_align.mean_confidence} "
                                      f"low={_align.low_conf_words} rushed={_align.rushed_words}")
                    except Exception as _ae:
                        print(f"[LEARNER] alignment skipped: {_ae}")

                except Exception as e:
                    print(f"[LEARNER] Whisper error: {e}")

            # --- Librosa prosody analysis ---
            if _LIBROSA_OK and wav_bytes:
                # _LIBROSA_OK guarantees both are imported; assert narrows the
                # Optional types for static analysis (no runtime cost).
                assert _librosa is not None and _np is not None
                try:
                    import io as _io_l
                    audio, sr = _librosa.load(_io_l.BytesIO(wav_bytes), sr=24000, mono=True)

                    # Pitch variance
                    f0, voiced, _ = _librosa.pyin(audio, fmin=50, fmax=400, sr=sr)
                    voiced_f0 = f0[voiced] if voiced is not None else f0
                    voiced_f0 = voiced_f0[~_np.isnan(voiced_f0)] if len(voiced_f0) else _np.array([])
                    pitch_variance = float(_np.std(voiced_f0)) if len(voiced_f0) > 5 else 0.0
                    metrics["pitch_variance"] = round(pitch_variance, 2)

                    # Energy tail, which detects words cut off at the end
                    rms    = _librosa.feature.rms(y=audio)[0]
                    if len(rms) > 10:
                        tail_energy = float(_np.mean(rms[-len(rms)//5:]))
                        peak_energy = float(_np.max(rms))
                        tail_ratio  = tail_energy / (peak_energy + 1e-8)
                        metrics["energy_tail"] = round(tail_ratio, 3)
                        if tail_ratio < 0.05:
                            metrics["quality_flags"] = metrics.get("quality_flags", "") + "|CUTOFF"

                    # Speaking rate (syllables/sec approximation)
                    onset_frames = _librosa.onset.onset_detect(y=audio, sr=sr)
                    duration     = len(audio) / sr
                    if duration > 0.5:
                        speaking_rate = len(onset_frames) / duration
                        metrics["speaking_rate"] = round(speaking_rate, 2)

                except Exception as e:
                    print(f"[LEARNER] librosa error: {e}")

            # --- Voice consistency vs baseline ---
            baseline = _get_baseline()
            if baseline and "pitch_variance" in metrics:
                expected_std = baseline.get("pitch_std", 20)
                actual_std   = metrics["pitch_variance"]
                # Consistency: how close to baseline variance
                if expected_std > 0:
                    ratio       = actual_std / expected_std
                    consistency = 1.0 - min(1.0, abs(1.0 - ratio))
                    metrics["voice_consistency"] = round(consistency, 3)
                    if consistency < 0.5:
                        metrics["quality_flags"] = metrics.get("quality_flags", "") + "|VOICE_DRIFT"

            # --- Composite quality score ---
            scores = []
            if "whisper_accuracy"   in metrics: scores.append(metrics["whisper_accuracy"])
            if "voice_consistency"  in metrics: scores.append(metrics["voice_consistency"])
            if "energy_tail"        in metrics:
                # Good tail ratio is 0.1-0.4
                tail = metrics["energy_tail"]
                tail_score = 1.0 if 0.1 <= tail <= 0.4 else max(0, 1.0 - abs(tail - 0.25))
                scores.append(tail_score)
            if "pitch_variance" in metrics:
                # Good variance is 10-60Hz
                pv = metrics["pitch_variance"]
                pv_score = 1.0 if 10 <= pv <= 60 else (0.5 if pv > 0 else 0.2)
                scores.append(pv_score)

            quality_score = round(sum(scores) / len(scores), 3) if scores else None

            # --- Write to DB ---
            with _db_lock:
                conn = _get_db()
                conn.execute("""
                    UPDATE chunks SET
                        whisper_accuracy  = ?,
                        pitch_variance    = ?,
                        energy_tail       = ?,
                        voice_consistency = ?,
                        quality_score     = ?,
                        quality_flags     = ?,
                        speaking_rate     = ?
                    WHERE id = ?
                """, (
                    metrics.get("whisper_accuracy"),
                    metrics.get("pitch_variance"),
                    metrics.get("energy_tail"),
                    metrics.get("voice_consistency"),
                    quality_score,
                    metrics.get("quality_flags", "").lstrip("|"),
                    metrics.get("speaking_rate"),
                    chunk_id
                ))
                conn.commit()
                conn.close()

            # Closed-loop observation: pair this chunk's profile and used params
            #    with the quality Whisper just measured. Pure recording. ────────
            if quality_score is not None:
                try:
                    _record_param_observation(chunk_id, quality_score,
                                              metrics.get("whisper_accuracy"))
                    _maybe_run_autotune()
                except Exception as e:
                    print(f"[LEARNER] observation record skipped: {e}")

            # --- Auto-flag low quality ---
            if quality_score is not None and quality_score < 0.6:
                flags = metrics.get("quality_flags", "")
                print(f"[LEARNER] ⚠ Low quality ({quality_score:.2f}) flags={flags or 'none'}")

                # --- Auto-tune temperature from Whisper feedback ---
                # This nudges the PROFILE of the chunk that scored badly, not the
                # user's live temperature slider.
                #
                # It used to assign straight into _live_settings_ref, which made
                # the learner a writer of the user's own setting. Three things
                # went wrong with that. The change was global, so one bad chunk
                # re-tuned every kind of text. It silently overrode a value the
                # user had deliberately chosen. And because mark_session_solid
                # then blends every per-type learned temperature TOWARD the live
                # value, an automatic nudge laundered itself into the permanent
                # learned settings at the end of the session.
                #
                # Writing to the profile store instead keeps the user's slider
                # theirs, targets the fingerprint that actually failed, and goes
                # through the same evidence-counted path as a user report, so it
                # needs corroboration before synthesis acts on it.
                #
                # Guard retained: a single low-accuracy chunk is usually a FALSE
                # POSITIVE (e.g. playback stopped mid-chunk, so Whisper analysed
                # truncated audio). Two consecutive low readings are required;
                # any good chunk resets the streak.
                wa = metrics.get("whisper_accuracy", 1.0)
                pv = metrics.get("pitch_variance", 20)
                global _low_accuracy_streak
                if wa < 0.80:
                    _low_accuracy_streak += 1
                    if _low_accuracy_streak >= 2:
                        try:
                            prof = lookup_chunk_profile(chunk_id, text)
                            _k, new_t = _adjust_profile_temperature(prof["keys"], -0.02)
                            print(f"[LEARNER] Auto-tune ↓ temp={new_t} for profile "
                                  f"[{prof['band']}/{prof['length']}/{prof['punct']}/"
                                  f"{prof['lexis']}] (Whisper {wa:.0%}, "
                                  f"streak {_low_accuracy_streak})")
                            log_history("TEMP", f"auto-tune: {prof['sentence_type']} "
                                                f"profile temp -0.02 → {new_t} "
                                                f"(Whisper {wa:.0%})", "auto")
                        except Exception as e:
                            print(f"[LEARNER] auto-tune adjust skipped: {e}")
                else:
                    _low_accuracy_streak = 0
                    if pv is not None and pv < 6 and quality_score > 0.55:
                        # Accurate but flat/robotic → warm this profile slightly.
                        try:
                            prof = lookup_chunk_profile(chunk_id, text)
                            _k, new_t = _adjust_profile_temperature(prof["keys"], +0.02)
                            print(f"[LEARNER] Auto-tune ↑ temp={new_t} for profile "
                                  f"[{prof['band']}/{prof['length']}/{prof['punct']}/"
                                  f"{prof['lexis']}] (flat pitch {pv})")
                            log_history("TEMP", f"auto-tune: {prof['sentence_type']} "
                                                f"profile temp +0.02 → {new_t} "
                                                f"(flat pitch)", "auto")
                        except Exception as e:
                            print(f"[LEARNER] auto-tune adjust skipped: {e}")

            # --- Auto-correct skipped abbreviations + hallucinations ---
            if quality_score is not None and quality_score < 0.6:
                flags_str = metrics.get("quality_flags", "")
                import re as _re2
                # Flag skipped abbreviations for user review
                # Only flag genuine abbreviations (2-5 chars, not common English words)
                # XTTS handles common English words fine, so a Whisper mismatch
                # on one is expected
                _COMMON_WORDS = {
                    'THE','AND','FOR','ARE','BUT','NOT','YOU','ALL','CAN','HER',
                    'WAS','ONE','OUR','OUT','DAY','GET','HAS','HIM','HIS','HOW',
                    'ITS','MAY','NEW','NOW','OLD','SEE','TWO','WAY','WHO','BOY',
                    'DID','ITS','LET','PUT','SAY','SHE','TOO','USE','ADD','AGO',
                    'ANY','BIG','END','FEW','GOT','HAD','HIT','HOT','HOW','JOB',
                    'KEY','LAW','MAP','MAN','MEN','NET','OFF','PAY','RUN','SET',
                    'SIX','TAX','TEN','TOP','TRY','WAR','WON','YES','YET',
                    # Domain words that appear capitalised in headings
                    'VIDEO','AUDIO','IMAGE','IMAGES','FORMS','WORDS','MODEL','MODELS',
                    'IMPORTANT','TESTING','MICROSOFT','AZURE','BOUNDING','WIDTH',
                    'HEIGHT','LEFT','RIGHT','CLASSIFICATION','CONTENTS','EXAMPLE',
                    'IMAGEBASED','MULTIMODAL','COGNITIVE','SERVICES','COGNITIVESERVICES',
                }
                for match in _re2.findall(r'WHISPER_SKIP:([^|]+)', flags_str):
                    for word in match.split(','):
                        w_orig = word.strip()
                        word   = w_orig.upper()
                        # Only act on GENUINE abbreviations: the word must be
                        # all-caps in the original text, like BFS or API or SQL,
                        # and short. A Whisper skip on an ordinary lowercase word
                        # is almost always a transcription error rather than a
                        # real synthesis problem, so flagging it is pure noise,
                        # and that's what produced "FLAG THIS, 118 hits". Rather
                        # than a FLAG that mutates nothing, I add a PRONUNCIATION
                        # rule that
                        # actually spells the abbreviation out, which is the only
                        # action that improves future audio.
                        if (len(word) >= 2 and len(word) <= 5
                                and word.isalpha()
                                and w_orig.isupper()          # genuinely an acronym in source
                                and word not in _COMMON_WORDS):
                            spoken = _spell_abbreviation(word)
                            if spoken and spoken.lower() != word.lower():
                                _add_rule("PRONUNCIATION", word, "spell_abbreviation",
                                          spoken, source="AUTO")
                                print(f"[LEARNER] ★ learned abbreviation pronunciation: {word} → {spoken}")

                # Flag hallucinations for dashboard review
                if "HALLUCINATION" in flags_str:
                    print("[LEARNER] Hallucination pattern in chunk - flagged for review")

            # --- Update baseline ---
            if quality_score and quality_score > 0.75 and "pitch_variance" in metrics:
                _update_baseline(metrics)
                if _live_settings_ref is not None and quality_score > 0.85:
                    print(f"[LEARNER] ✓ High quality ({quality_score:.2f}) — temp stable at {_live_settings_ref.get('temperature',0.33):.3f}")

            with _analysis_stats_lock:
                _analysis_done += 1

        except queue.Empty:
            continue    # nothing waiting; task_done() would be wrong here
        except Exception as e:
            print(f"[LEARNER] Analysis worker error: {e}")
            try:
                _analysis_queue.task_done()
            except ValueError:
                pass
        else:
            _analysis_queue.task_done()

_worker_thread = threading.Thread(target=_analysis_worker, daemon=True)
_worker_thread.start()

# ---
# Baseline management
# ---
_baseline_cache = None
_baseline_lock  = threading.Lock()

def _get_baseline():
    global _baseline_cache
    if _baseline_cache:
        return _baseline_cache
    if _baseline_path().exists():
        try:
            with open(_baseline_path()) as f:
                _baseline_cache = json.load(f)
            return _baseline_cache
        except Exception:
            pass
    return None

def _update_baseline(metrics):
    """Rolling average update to the baseline, so it can't overfit to one chunk."""
    global _baseline_cache
    with _baseline_lock:
        existing = _get_baseline() or {}
        n = existing.get("sample_count", 0)

        # Exponential moving average, so recent chunks count for more
        alpha = 0.1  # 10% weight to new sample, 90% to history
        def _ema(old, new):
            if old is None: return new
            return round(old * (1 - alpha) + new * alpha, 3)

        updated = {
            "pitch_mean":    _ema(existing.get("pitch_mean"), metrics.get("pitch_variance", 0)),
            "pitch_std":     _ema(existing.get("pitch_std"),  metrics.get("pitch_variance", 0)),
            "energy_mean":   _ema(existing.get("energy_mean"), metrics.get("energy_tail", 0)),
            "energy_std":    _ema(existing.get("energy_std"),  metrics.get("energy_tail", 0)),
            "speaking_rate": _ema(existing.get("speaking_rate"), metrics.get("speaking_rate", 0)),
            "sample_count":  n + 1,
            "updated":       datetime.now().isoformat(),
        }
        _baseline_cache = updated
        try:
            with open(_baseline_path(), "w") as f:
                json.dump(updated, f, indent=2)
        except Exception as e:
            print(f"[LEARNER] Baseline save error: {e}")

        # Mirror into the baseline table so the DB is a true source of truth.
        # The JSON file stays the live read cache; the table gives a history.
        try:
            with _db_lock:
                conn = _get_db()
                conn.execute(
                    """INSERT INTO baseline
                       (ts, pitch_mean, pitch_std, energy_mean, energy_std, speaking_rate, sample_count)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (time.time(),
                     updated["pitch_mean"], updated["pitch_std"],
                     updated["energy_mean"], updated["energy_std"],
                     updated["speaking_rate"], updated["sample_count"])
                )
                conn.commit()
                conn.close()
        except Exception as e:
            print(f"[LEARNER] Baseline DB mirror error: {e}")

# ---
# The public API, which server.py calls
# ---

def log_chunk(text, sentence_type, silence_ms, wav_bytes=None, chunk_id=None,
              display_text=None, synth_params=None, prosody=None, has_math=False,
              profile_str=None):
    """
    Called by server.py after every successful synthesis.
    Logs the chunk and queues background quality analysis.
    Returns the chunk_id for reference.

    The chunk_id comes deterministically from a content hash of the chunk text
    rather than a random uuid. That means re-synthesising the same sentence,
    whether through replay or a prefetch race or scrubbing back, maps to the same
    row and INSERT OR REPLACE updates it in place instead of adding a duplicate.
    So the stored chunk count reflects the distinct chunks in the document and
    one Read Page gives a stable count matching the popup's reading length.

    display_text, if it's given, is the human-readable original text with correct
    grammar and no pronunciation substitution, and that's what gets stored and
    shown in the dashboard and reports. The id still comes from the spoken
    `text`, so dedup and replay mapping don't change, and Whisper analysis still
    uses `text` too.

    profile_str is the fingerprint the server already resolved for this chunk
    when it chose the synthesis parameters, so pass it in. This function used to
    recompute the fingerprint from `display_text`, but the parameters were
    resolved from the spoken `text`, and the two strings differ by exactly the
    cleaning that changes the features a fingerprint is built from. So the stored
    key disagreed with the key synthesis reads back, and observations piled up
    under one address while resolve_profile_param() looked up another, which
    meant the closed loop never actually closed. I only recompute now as a
    fallback for callers with no profile to hand.
    """
    if chunk_id is None:
        # Stable content-addressed id: same spoken text -> same id -> dedupes.
        chunk_id = hashlib.sha1((text or "").strip().encode("utf-8")).hexdigest()[:16]

    # What the user sees: the original grammar, not the spoken form.
    stored_text = display_text if display_text else text

    _profile_str = profile_str
    if _profile_str is None:
        try:
            _p = chunk_profile(text, sentence_type)
            _profile_str = f"{_p['band']}|{_p['length']}|{_p['punct']}|{_p['lexis']}"
        except Exception:
            _profile_str = None

    sp = synth_params or {}
    pz = prosody or {}
    with _db_lock:
        conn = _get_db()
        try:
            conn.execute("""
                INSERT OR REPLACE INTO chunks
                    (id, ts, text, sentence_type, silence_ms, char_count, success, profile,
                     used_temperature, used_top_p, used_top_k, used_rep_penalty, used_speed,
                     clause_count, ends_mid, emphasis_density, has_math, voice, sentence_count,
                     primary_facet, facet_count, synth_ms, duration_ms, rtf, retried,
                     is_fragment, pos_available, error_detail, rejected, output_check)
                VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (chunk_id, time.time(), stored_text, sentence_type, silence_ms, len(stored_text), _profile_str,
                  sp.get("temperature"), sp.get("top_p"), sp.get("top_k"),
                  sp.get("repetition_penalty"), sp.get("speed"),
                  pz.get("clause_count"), (1 if pz.get("ends_mid") else 0) if pz else None,
                  pz.get("emphasis_density"), 1 if has_math else 0, _ACTIVE_VOICE,
                  pz.get("sentence_count"),
                  pz.get("primary_facet"), pz.get("facet_count"),
                  pz.get("synth_ms"), pz.get("audio_ms"), pz.get("rtf"),
                  pz.get("retried", 0),
                  1 if pz.get("is_fragment") else 0,
                  1 if pz.get("pos_available") else 0,
                  # A chunk that succeeded only after a retry keeps the record of
                  # what failed first, since that's the signal for "this shape of
                  # text is unstable on this machine".
                  json.dumps(pz["failure"]) if pz.get("failure") else None,
                  pz.get("rejected", 0),
                  json.dumps(pz["output_check"]) if pz.get("output_check") else None))
        except Exception as e:
            # Schema mismatch (e.g. a migration didn't apply on this DB) must never
            # break synthesis. Fall back to the minimal, always-present columns.
            print(f"[LEARNER] log_chunk full insert failed ({e}); using minimal insert")
            conn.execute("""
                INSERT OR REPLACE INTO chunks
                    (id, ts, text, sentence_type, silence_ms, char_count, success)
                VALUES (?, ?, ?, ?, ?, ?, 1)
            """, (chunk_id, time.time(), stored_text, sentence_type, silence_ms, len(stored_text)))
        conn.commit()
        conn.close()

    # Queue background analysis (non-blocking). Whisper compares against the
    # SPOKEN text, so pass `text`, not the display text.
    if wav_bytes is not None:
        global _analysis_queued, _analysis_dropped
        try:
            _analysis_queue.put_nowait((chunk_id, wav_bytes, text))
            with _analysis_stats_lock:
                _analysis_queued += 1
        except queue.Full:
            # This still never blocks synthesis, but now a drop gets counted and
            # announced. Dropping silently meant a chunk could go unanalysed
            # forever with no record, which looked identical to "analysis ran
            # and found nothing wrong" when reading the dashboard back.
            with _analysis_stats_lock:
                _analysis_dropped += 1
                n = _analysis_dropped
            if n == 1 or n % 25 == 0:
                print(f"[LEARNER] analysis queue full — {n} chunk(s) not analysed "
                      f"(synthesis is outrunning listen-back; lower analysis_every "
                      f"or leave it, learning just gathers evidence more slowly)")

    return chunk_id


def log_error(text, error_type="ERROR", stage="inference", message="",
              attempts=None, display_text=None, fatal=True):
    """Record a synthesis failure with enough detail to diagnose it later.

    This used to write a row with a random uuid, sentence_type='ERROR' and
    nothing else, so no error message, no stage and no parameters. That made a
    failed chunk impossible to find, since its id matched nothing the client
    held, and impossible to explain, since the reason only lived in the console
    scrollback. The row is now content-addressed like every successful chunk, so
    the failure sits beside the successful attempts at the same text and
    GET /diagnose/<id> can report exactly what went wrong.

    Returns the chunk_id so the caller can hand it to the client.
    """
    chunk_id = hashlib.sha1((text or "").strip().encode("utf-8")).hexdigest()[:16]
    stored_text = display_text or text
    detail = json.dumps({
        "stage": stage,
        "type": error_type,
        "message": (message or "")[:500],
        "attempts": attempts or [],
        "ts": time.time(),
    })
    with _db_lock:
        conn = _get_db()
        try:
            conn.execute("""
                INSERT OR REPLACE INTO chunks
                    (id, ts, text, sentence_type, silence_ms, char_count,
                     success, voice, error_type, error_stage, error_detail, retried)
                VALUES (?, ?, ?, 'ERROR', 0, ?, 0, ?, ?, ?, ?, ?)
            """, (chunk_id, time.time(), stored_text, len(stored_text or ""),
                  _ACTIVE_VOICE, error_type, stage, detail, len(attempts or [])))
        except Exception as e:
            print(f"[LEARNER] log_error full insert failed ({e}); using minimal insert")
            conn.execute("""
                INSERT OR REPLACE INTO chunks
                    (id, ts, text, sentence_type, silence_ms, char_count, success)
                VALUES (?, ?, ?, 'ERROR', 0, ?, 0)
            """, (chunk_id, time.time(), stored_text, len(stored_text or "")))
        conn.commit()
        conn.close()

    log_history("ERROR", f"{error_type} at {stage}: {(message or '')[:120]}", "auto")

    # Auto-blacklisting is destructive, since it permanently strips tokens from
    # all future speech, so it now runs only for the vocabulary crashes it was
    # written for and only when the chunk really did fail for good. It used to
    # run on every error including transient CUDA OOM, where the token had
    # nothing to do with the failure and real words were blacklisted as a result.
    if fatal:
        _auto_detect_crash_pattern(text, error_type, message)
    return chunk_id


def submit_report(chunk_text, issue, token=None, expected=None, heard=None,
                  action="IGNORE", confidence="MEDIUM", notes=None, chunk_id=None):
    """
    Structured report submission from the popup UI.

    issue:      PRONUNCIATION | SKIP | HALLUCINATION | PROSODY | SPEED | VOICE | OTHER
    action:     ADD_TO_STORE | ADJUST_CHUNK | BLACKLIST | FLAG_PATTERN | IGNORE
    confidence: HIGH | MEDIUM | LOW

    Returns {"ok": False, "error": "..."} on validation failure so the UI can
    surface it. Otherwise returns {"report_id": N, "applied": bool, "result": "..."}
    """
    VALID_ISSUES   = {"PRONUNCIATION","HALLUCINATION","SKIP","TOO_FAST","TOO_SLOW",
                      "ROBOTIC","VOICE","PUNCT","CUTOFF","SUPPRESS","OTHER",
                      "CHUNK_BOUNDARY","EQUATION",
                      "PROSODY","SPEED"}  # last two kept for backward compat
    VALID_ACTIONS  = {"ADD_TO_STORE","ADJUST_CHUNK","BLACKLIST","FLAG_PATTERN",
                      "ADJUST_RATE_DOWN","ADJUST_RATE_UP","ADJUST_TEMP_UP",
                      "ADJUST_TEMP_DOWN","SUPPRESS","PUNCT","REMERGE","IGNORE",
                      "MATH_OVERRIDE"}
    VALID_CONF     = {"HIGH","MEDIUM","LOW"}

    # Normalise the case before validating. These three lines used to test the
    # raw value against the upper-case vocabularies, so a client sending
    # "pronunciation" or "Pronunciation", which is the natural thing to send, had
    # its report quietly downgraded to issue=OTHER and action=IGNORE and nothing
    # was
    # ever learned from it.
    issue      = (issue      or "").strip().upper()
    action     = (action     or "").strip().upper()
    confidence = (confidence or "").strip().upper()
    issue      = issue      if issue      in VALID_ISSUES  else "OTHER"
    action     = action     if action     in VALID_ACTIONS else "IGNORE"
    confidence = confidence if confidence in VALID_CONF    else "MEDIUM"

    # Normalise the inputs, so whitespace-only fields count as empty
    token    = (token or "").strip()
    expected = (expected or "").strip()
    heard    = (heard or "").strip()
    chunk_text = (chunk_text or "").strip()

    # Validation gate, rejecting no-op or empty submissions before I save them
    # ADD_TO_STORE: needs BOTH token and expected, and they must not be identical
    if action == "ADD_TO_STORE":
        if not token or not expected:
            return {"ok": False, "error": "Pronunciation reports need both the token and the 'should sound like' value."}
        if token.lower() == expected.lower():
            return {"ok": False, "error": f"Token and 'should sound like' are identical ({token!r}). Nothing to change."}

    # BLACKLIST / FLAG_PATTERN: need at least a token OR chunk_text
    if action in ("BLACKLIST", "FLAG_PATTERN") and not token and not chunk_text:
        return {"ok": False, "error": f"{action} reports need a token or a chunk text to flag."}

    # ADJUST_CHUNK: needs chunk_text of some length
    if action == "ADJUST_CHUNK" and len(chunk_text) < 5:
        return {"ok": False, "error": "Chunk-split reports need the chunk text (at least 5 characters)."}

    with _db_lock:
        conn = _get_db()
        conn.execute("""
            INSERT INTO reports
                (ts, chunk_id, chunk_text, issue, token, expected, heard, action, confidence, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (time.time(), chunk_id, chunk_text, issue, token, expected, heard, action, confidence, notes))
        report_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        # A genuine issue report is a negative verdict on that chunk, so I mark
        # it and the reported-with-issues statistics count it. The thumbs-up
        # ping arrives through this same route with notes='user_approved' and is
        # a positive verdict instead. The chunk id is a content hash, so it can
        # be re-derived from the text when the report doesn't carry one.
        _cid = chunk_id
        if not _cid and chunk_text:
            _cid = hashlib.sha1(chunk_text.strip().encode("utf-8")).hexdigest()[:16]
        if _cid:
            verdict = "positive" if notes == "user_approved" else "negative"
            conn.execute("UPDATE chunks SET user_feedback=? WHERE id=?", (verdict, _cid))
        conn.commit()
        conn.close()

    # Confidence drives WHEN a report acts:
    #   HIGH   → apply immediately.
    #   MEDIUM → apply now for safe store/blacklist actions; otherwise apply once
    #            2 matching reports agree (accumulated evidence).
    #   LOW    → never instant; apply once 3 matching reports agree.
    # This way lower-confidence reports are weighed rather than ignored: repeated
    # agreement is the model "deciding" the issue is real.
    result = {"report_id": report_id, "applied": False}
    if confidence == "HIGH" or (confidence == "MEDIUM" and action in ("ADD_TO_STORE", "BLACKLIST")):
        result = _apply_report(report_id, chunk_text, issue, token, expected, heard, action, chunk_id)
    else:
        need = 2 if confidence == "MEDIUM" else 3
        agree = _count_matching_reports(issue, token, chunk_text)
        if agree >= need:
            result = _apply_report(report_id, chunk_text, issue, token, expected, heard, action, chunk_id)
            result["applied_via"] = f"accumulated {agree} matching reports"
            log_history("REPORT", f"{issue} applied after {agree} matching reports (confidence {confidence})", "user")
        else:
            result["pending"] = True
            result["agreement"] = agree
            result["needed"] = need
            result["reason"] = f"{confidence} confidence: logged. Will apply after {need} matching reports ({agree} so far)."

    return result


def _count_matching_reports(issue, token, chunk_text):
    """Count prior reports that agree on this issue and target (token, or chunk
    text prefix when no token). Used to let MEDIUM/LOW reports accumulate into an
    applied action once enough agree."""
    try:
        with _db_lock:
            conn = _get_db()
            if token:
                n = conn.execute(
                    "SELECT COUNT(*) FROM reports WHERE issue=? AND token=?",
                    (issue, token)
                ).fetchone()[0]
            else:
                prefix = (chunk_text or "")[:40]
                n = conn.execute(
                    "SELECT COUNT(*) FROM reports WHERE issue=? AND substr(chunk_text,1,40)=?",
                    (issue, prefix)
                ).fetchone()[0]
            conn.close()
        return int(n or 0)
    except Exception:
        return 0


def _apply_report(report_id, chunk_text, issue, token, expected, heard, action, chunk_id=None):
    """Execute the action specified in a report."""
    result_msg = "no_action"

    try:
        if action == "ADD_TO_STORE" and token and expected:
            # Add to pronunciation store
            _add_to_pronunciation_store(token.upper(), expected.lower().strip())
            result_msg = f"added {token} → {expected} to pronunciation store"
            _add_rule("PRONUNCIATION", token, "store_override", expected, source="REPORT")

        elif action == "ADD_TO_STORE" and token:
            # Token given but no target pronunciation: flag it so the report
            # still registers and the word is tracked, rather than no_action.
            _add_rule("FLAG", token, "needs_pronunciation", "", source="REPORT")
            result_msg = f"flagged {token}: add a 'should sound like' value to teach the pronunciation"

        elif action == "BLACKLIST":
            if token:
                # A named word the voice invented → block it.
                _add_rule("BLACKLIST", token, "strip", "", source="REPORT")
                result_msg = f"blacklisted token: {token}"
            else:
                # Hallucination with no specific word named: lower temperature
                # for this chunk's full PROFILE (sentence type + complexity +
                # length + punctuation + lexis) so the fix targets the actual
                # failure fingerprint, with graceful fallback to coarser buckets.
                prof = lookup_chunk_profile(chunk_id, chunk_text)
                stype = prof["sentence_type"]
                key, new_t = _adjust_profile_temperature(prof["keys"], -0.03)
                # Keep the legacy per-type value moving too for back-compat.
                _adjust_pref_temperature(stype, -0.03)
                result_msg = f"lowered temperature to {new_t} for profile [{prof['band']}/{prof['length']}/{prof['punct']}/{prof['lexis']}] to reduce hallucination"
                log_history("TEMP", f"{stype} profile temp -0.03 → {new_t} ({prof['band']},{prof['length']},{prof['punct']},{prof['lexis']}; hallucination)", "user")

        elif action == "ADJUST_CHUNK" and chunk_text:
            # Add a splitting/merging rule for this text pattern
            # Extract the key phrase (first 50 chars, alphanumeric)
            pattern = re.sub(r'[^a-zA-Z0-9 ]', '', chunk_text[:50]).strip()
            _add_rule("SPLIT", pattern, "split_before", "", source="REPORT")
            result_msg = f"chunk rule added for pattern: {pattern[:30]}"

        elif action == "FLAG_PATTERN" and (token or chunk_text):
            pattern = token or chunk_text[:30]
            _add_rule("FLAG", pattern, "warn", "", source="REPORT")
            result_msg = f"pattern flagged: {pattern[:30]}"

        elif action in ("ADJUST_RATE_DOWN", "ADJUST_RATE_UP"):
            # Too fast / too slow → adjust the rate modifier for THIS CHUNK'S
            # complexity band only. A dense technical chunk reported too fast
            # slows future dense chunks; simple chunks keep the user's pace.
            # Band comes from the stored fingerprint (what synthesis used), not
            # from re-classifying the display text the report carries.
            band = lookup_chunk_profile(chunk_id, chunk_text)["band"]
            step = -0.04 if action == "ADJUST_RATE_DOWN" else 0.04
            new_mod, damped = _adjust_band_rate(band, step)
            direction = "slower" if step < 0 else "faster"
            result_msg = f"{BAND_LABEL[band]} chunks will now play slightly {direction} (modifier {new_mod})"
            if damped:
                result_msg += ". Step halved because the previous report went the other way"
            log_history("RATE", f"{BAND_LABEL[band]} band {direction}: modifier {new_mod} (too {'fast' if step<0 else 'slow'})", "user")

        elif action in ("ADJUST_TEMP_DOWN", "ADJUST_TEMP_UP"):
            # Robotic (up = more variation) / unstable (down = steadier).
            # Targets the chunk's TRUE sentence type as classified at synthesis,
            # looked up from the chunk log, so a robotic question adjusts
            # questions rather than everything.
            step = 0.03 if action == "ADJUST_TEMP_UP" else -0.03
            prof = lookup_chunk_profile(chunk_id, chunk_text)
            stype = prof["sentence_type"]
            key, new_t = _adjust_profile_temperature(prof["keys"], step)
            _adjust_pref_temperature(stype, step)   # keep legacy value in sync
            result_msg = f"temperature now {new_t} for profile [{prof['band']}/{prof['length']}/{prof['punct']}/{prof['lexis']}]"
            log_history("TEMP", f"{stype} profile temp {('+' if step>0 else '')}{step} → {new_t} ({prof['band']},{prof['length']},{prof['punct']},{prof['lexis']})", "user")

        elif action == "MATH_OVERRIDE" and (token or chunk_text):
            # An equation was read wrongly. Store the literal expression and how
            # it SHOULD be spoken, applied before the symbol expander on every
            # future occurrence. Equations are also the highest-hallucination
            # chunk class, so nudge this profile steadier at the same time.
            expr = (token or "").strip() or chunk_text[:60].strip()
            if expected:
                _add_rule("MATH", expr, "speak_as", expected.strip(), source="REPORT")
                result_msg = f"equation rule: '{expr[:28]}' will now be read as '{expected.strip()[:34]}'"
            else:
                _add_rule("FLAG", expr, "needs_math_reading", "", source="REPORT")
                result_msg = (f"flagged equation '{expr[:28]}' — add how it should be "
                              f"read aloud to teach it")
            try:
                prof = lookup_chunk_profile(chunk_id, chunk_text)
                _key, new_t = _adjust_profile_temperature(prof["keys"], -0.02)
                log_history("TEMP", f"equation report: {prof['sentence_type']} "
                                    f"profile temp -0.02 → {new_t}", "user")
            except Exception:
                pass

        elif action == "PUNCT" and (token or chunk_text):
            anchor = token or chunk_text[:30]
            _add_rule("PUNCT", anchor, "repunctuate", expected or ".", source="REPORT")
            result_msg = f"punctuation rule added at: {anchor[:30]}"

        elif action == "SUPPRESS" and chunk_text:
            pattern = re.sub(r'[^a-zA-Z0-9 ]', '', chunk_text[:50]).strip()
            _add_rule("SUPPRESS", pattern, "silence", "", source="REPORT")
            result_msg = f"suppress rule added: {pattern[:30]}"

        elif action == "REMERGE" and (token or chunk_text):
            # Chunk-boundary correction: the user reports that a fragment was
            # wrongly split off from its sentence. token holds the fragment that
            # should rejoin the PREVIOUS chunk (e.g. "2024),"). The chunker reads
            # BOUNDARY rules and keeps the fragment attached on future reads.
            fragment = (token or "").strip()
            if fragment:
                _add_rule("BOUNDARY", fragment, "keep_with_previous", "", source="REPORT")
                result_msg = f"boundary rule added: '{fragment[:40]}' will stay with the previous chunk"
            else:
                # No fragment named: flag the chunk so the split point is tracked.
                anchor = chunk_text[:40]
                _add_rule("BOUNDARY", anchor, "review_split", "", source="REPORT")
                result_msg = f"split flagged for review: {anchor[:30]}"
            log_history("SPLIT", f"Boundary correction: {result_msg}", "user")

        # Catch-all: if no branch matched (e.g. an action lacking the data it
        # needs and IGNORE), still register the report as a flagged pattern so it
        # is never silently dropped as "no_action".
        if result_msg == "no_action" and action != "IGNORE" and (token or chunk_text):
            anchor = token or chunk_text[:30]
            _add_rule("FLAG", anchor, "review", "", source="REPORT")
            result_msg = f"logged and flagged for review: {anchor[:30]}"

        # Mark report as applied
        with _db_lock:
            conn = _get_db()
            conn.execute("UPDATE reports SET applied=1, apply_result=? WHERE id=?",
                        (result_msg, report_id))
            conn.commit()
            conn.close()

        print(f"[LEARNER] Report #{report_id} applied: {result_msg}")

    except Exception as e:
        result_msg = f"error: {e}"
        print(f"[LEARNER] Report apply error: {e}")

    return {"report_id": report_id, "applied": True, "result": result_msg}


def _add_to_pronunciation_store(token, spoken):
    """Thread-safe pronunciation store update."""
    try:
        store = {}
        if STORE_PATH.exists():
            with open(STORE_PATH) as f:
                store = json.load(f)
        store[token.upper()] = spoken.lower().strip()
        with open(STORE_PATH, "w") as f:
            json.dump(store, f, indent=2)
        print(f"[LEARNER] Pronunciation store updated: {token} → {spoken}")
    except Exception as e:
        print(f"[LEARNER] Store update error: {e}")


def bump_counter(name, by=1):
    """Increment a durable running total like 'solid_total'. These survive a
    stats clear, and it's best-effort so it never breaks the caller."""
    try:
        with _db_lock:
            conn = _get_db()
            conn.execute(
                "INSERT INTO counters (name, value) VALUES (?, ?) "
                "ON CONFLICT(name) DO UPDATE SET value = MAX(0, value + ?)",
                (name, max(0, by), by))
            conn.commit()
            conn.close()
    except Exception as e:
        print(f"[LEARNER] counter bump error: {e}")


def get_counter(name):
    """Read a durable counter's value (0 if unset)."""
    try:
        with _db_lock:
            conn = _get_db()
            row = conn.execute("SELECT value FROM counters WHERE name=?", (name,)).fetchone()
            conn.close()
        return row[0] if row else 0
    except Exception:
        return 0


def clear_counter(name):
    """Reset a durable counter to zero (for the dedicated clear control)."""
    try:
        with _db_lock:
            conn = _get_db()
            conn.execute("INSERT INTO counters (name, value) VALUES (?, 0) "
                         "ON CONFLICT(name) DO UPDATE SET value = 0", (name,))
            conn.commit()
            conn.close()
    except Exception as e:
        print(f"[LEARNER] counter clear error: {e}")


def set_counter(name, value):
    """Set a durable counter to an explicit value."""
    try:
        with _db_lock:
            conn = _get_db()
            conn.execute("INSERT INTO counters (name, value) VALUES (?, ?) "
                         "ON CONFLICT(name) DO UPDATE SET value = ?", (name, value, value))
            conn.commit()
            conn.close()
    except Exception as e:
        print(f"[LEARNER] counter set error: {e}")


def seed_feedback_counters():
    """One-time backfill: if the durable feedback counters have never been set
    (existing install upgrading to durable counting), initialise them from the
    current chunk-row counts so prior verdicts are preserved rather than zeroed.
    A 'seeded' marker prevents re-seeding on every startup."""
    try:
        with _db_lock:
            conn = _get_db()
            already = conn.execute("SELECT value FROM counters WHERE name='feedback_seeded'").fetchone()
            if already:
                conn.close(); return
            pos   = conn.execute("SELECT COUNT(*) FROM chunks WHERE user_feedback='positive'").fetchone()[0]
            neg   = conn.execute("SELECT COUNT(*) FROM chunks WHERE user_feedback='negative'").fetchone()[0]
            solid = conn.execute("SELECT COUNT(*) FROM chunks WHERE user_feedback='solid'").fetchone()[0]
            for name, val in (("perfect_total", pos), ("negative_total", neg),
                              ("solid_total", solid), ("feedback_seeded", 1)):
                # Only seed totals that aren't already present (don't clobber a
                # solid_total that the session digester already created).
                row = conn.execute("SELECT value FROM counters WHERE name=?", (name,)).fetchone()
                if row is None:
                    conn.execute("INSERT INTO counters (name, value) VALUES (?, ?)", (name, val))
            conn.commit(); conn.close()
            print(f"[LEARNER] feedback counters seeded: perfect={pos} negative={neg} solid={solid}")
    except Exception as e:
        print(f"[LEARNER] feedback seed skipped: {e}")


def log_history(event_type, detail, source="auto"):
    """Append one permanent record of a change the model made. The console and
    stats resets never clear this, since it's the long-term learning log behind
    the Stats page and any future profiles. It's best-effort, because logging
    history can't be allowed to break synthesis or feedback, so I swallow any
    errors."""
    try:
        with _db_lock:
            conn = _get_db()
            conn.execute(
                "INSERT INTO history (ts, event_type, detail, source) VALUES (?,?,?,?)",
                (time.time(), event_type, detail, source),
            )
            conn.commit()
            conn.close()
    except Exception as e:
        print(f"[LEARNER] history log error: {e}")


def clear_history():
    """Wipe the permanent learning log. Only the dedicated clear control on the
    Stats page uses this, never the per-session console clear."""
    try:
        with _db_lock:
            conn = _get_db()
            conn.execute("DELETE FROM history")
            conn.commit()
            conn.close()
        print("[LEARNER] Permanent learning history cleared by user")
    except Exception as e:
        print(f"[LEARNER] history clear error: {e}")


def get_history(limit=200):
    """Newest-first list of history rows for the Stats page."""
    try:
        with _db_lock:
            conn = _get_db()
            rows = conn.execute(
                "SELECT ts, event_type, detail, source FROM history "
                "ORDER BY ts DESC LIMIT ?", (limit,)
            ).fetchall()
            conn.close()
        return [dict(r) for r in rows]
    except Exception:
        return []


def get_history_summary():
    """Compact numeric totals by event type + source, for the Stats header.
    Includes the total count of chunks digested as 'solid' (heard, no verdict),
    so the Stats Model Learning History panel can surface it permanently."""
    try:
        with _db_lock:
            conn = _get_db()
            total = conn.execute("SELECT COUNT(*) FROM history").fetchone()[0]
            by_src = dict(conn.execute(
                "SELECT source, COUNT(*) FROM history GROUP BY source").fetchall())
            by_type = dict(conn.execute(
                "SELECT event_type, COUNT(*) FROM history GROUP BY event_type").fetchall())
            conn.close()
        # Durable lifetime total (survives stats clears), not the chunk-table
        # count which resets whenever the live feed is cleared.
        solid = get_counter("solid_total")
        # Total folds in solid chunks per the dashboard's "Total Chunks" stat,
        # so it intentionally exceeds from-you + automatic (which are events).
        return {"total": total + solid, "events_total": total,
                "by_source": by_src, "by_type": by_type, "solid": solid}
    except Exception:
        return {"total": 0, "by_source": {}, "by_type": {}, "solid": 0}


def _add_rule(rule_type, pattern, action, value, source="AUTO"):
    with _db_lock:
        conn = _get_db()
        # Don't duplicate existing rules
        existing = conn.execute(
            "SELECT id FROM rules WHERE rule_type=? AND pattern=? AND active=1",
            (rule_type, pattern)
        ).fetchone()
        is_new = not existing
        if is_new:
            conn.execute("""
                INSERT INTO rules (ts, rule_type, pattern, action, value, source)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (time.time(), rule_type, pattern, action, value, source))
            conn.commit()
        conn.close()
    if is_new:
        invalidate_rule_cache()
    # Record genuinely new rules in the permanent history.
    if is_new:
        src = "user" if source == "REPORT" else "auto"
        detail = f'{rule_type} "{pattern}"' + (f' → {value}' if value else "")
        log_history(rule_type, detail, src)


def unconfirm_chunk_quality(chunk_id):
    """Partially reverse a confirm_chunk_quality reinforcement after the user
    reverts a perfect mark. The EMA-blended acoustic baseline can't be perfectly
    un-blended (the pre-blend value is lost), but we decrement sample_count so the
    evidence tally stays honest and this chunk no longer counts as an exemplar."""
    global _baseline_cache
    with _baseline_lock:
        ex = _get_baseline() or {}
        n = ex.get("sample_count", 0)
        if n > 0:
            updated = dict(ex)
            updated["sample_count"] = n - 1
            updated["updated"] = datetime.now().isoformat()
            _baseline_cache = updated
            try:
                with open(_baseline_path(), "w") as f:
                    json.dump(updated, f, indent=2)
            except Exception as e:
                print(f"[LEARNER] baseline save error (unconfirm): {e}")
    print(f"[LEARNER] ↩ baseline sample decremented for reverted chunk {chunk_id[:8]}")
    return {"unconfirmed": True}


def _settings_that_produced(row_mapping):
    """The synth parameters a chunk was actually generated with.

    Reinforcement has to point at the operating point that earned the praise.
    This used to hand record_good_settings a copy of the current live settings,
    which is a completely different thing, because temperature drifts during a
    read. So a thumbs-up on the third chunk of a page reinforced whatever the
    slider happened to say by the time it was clicked rather than what that chunk
    used. Every parameter has been stored per chunk since log_chunk started
    writing the used_* columns, so I just read them back.

    Returns None for rows that pre-date those columns, and the caller then falls
    back to the live settings, which is at least the old behaviour and not
    nothing."""
    params = {
        "temperature":        row_mapping.get("used_temperature"),
        "top_p":              row_mapping.get("used_top_p"),
        "top_k":              row_mapping.get("used_top_k"),
        "repetition_penalty": row_mapping.get("used_rep_penalty"),
        "speed":              row_mapping.get("used_speed"),
    }
    return params if params["temperature"] is not None else None


def confirm_chunk_quality(chunk_id):
    """Reinforce the voice baseline from a chunk the user has confirmed is good.

    I only read columns that exist on chunks, so pitch_variance, energy_tail and
    voice_consistency, but not speaking_rate which only lives in the baseline.
    The EMA alpha is 0.25, stronger than the passive 0.10, since this is learning
    from a success."""
    with _db_lock:
        conn = _get_db()
        row = conn.execute(
            "SELECT sentence_type, pitch_variance, energy_tail, voice_consistency, "
            "used_temperature, used_top_p, used_top_k, used_rep_penalty, used_speed "
            "FROM chunks WHERE id=?", (chunk_id,)).fetchone()
        conn.close()
    if not row:
        return {"reinforced": False, "reason": "chunk_not_found"}
    r = dict(row)
    stype       = r["sentence_type"]
    pitch_var   = r["pitch_variance"]
    energy_tail = r["energy_tail"]
    voice_cons  = r["voice_consistency"]

    # The settings this chunk was made with, not the ones in force right now.
    good = _settings_that_produced(r)
    if good is None and _live_settings_ref:
        good = dict(_live_settings_ref)
        print(f"[LEARNER] chunk {chunk_id[:8]} has no stored parameters "
              f"(pre-dates per-chunk recording) — reinforcing live settings")

    if pitch_var is None or energy_tail is None:
        # Prosody analysis hasn't finished yet, so we cannot reinforce the
        # acoustic baseline. We CAN still record the inference settings for
        # thumbs-up guidance, since temperature steering doesn't depend on the
        # prosody metrics. This makes an immediate post-playback thumbs-up count.
        if good:
            try:
                record_good_settings(stype, good)
            except Exception as e:
                print(f"[LEARNER] good_settings (pending) error: {e}")
        return {"reinforced": False, "reason": "metrics_pending",
                "settings_recorded": bool(good)}
    global _baseline_cache
    with _baseline_lock:
        ex = _get_baseline() or {}
        a = 0.25
        def ema(o, n):
            if n is None:
                return o
            return n if o is None else round(o*(1-a)+n*a, 3)
        updated = dict(ex)
        updated["pitch_std"]   = ema(ex.get("pitch_std"),  pitch_var)
        updated["energy_mean"] = ema(ex.get("energy_mean"), energy_tail)
        if voice_cons is not None:
            updated["voice_consistency"] = ema(ex.get("voice_consistency"), voice_cons)
        updated["sample_count"] = ex.get("sample_count", 0) + 1
        updated["updated"]      = datetime.now().isoformat()
        _baseline_cache = updated
        try:
            with open(_baseline_path(), "w") as f:
                json.dump(updated, f, indent=2)
        except Exception as e:
            print(f"[LEARNER] baseline save error: {e}")
    print(f"[LEARNER] ✓ baseline reinforced from confirmed chunk {chunk_id[:8]} (type={stype})")

    # Thumbs-up guidance: remember the inference settings that produced this
    # approved chunk, keyed by sentence_type, so /speak can bias future chunks
    # of the same kind toward this known-good operating point.
    if good:
        record_good_settings(stype, good)

    return {"reinforced": True, "sentence_type": stype,
            "reinforced_settings": good, "new_baseline": updated}


def learn_punctuation_correction(original, corrected):
    """Store a user punctuation correction + extract reusable PUNCT rules."""
    original  = (original or "").strip()
    corrected = (corrected or "").strip()
    if not corrected or corrected == original:
        return {"ok": False, "edits": 0}
    try:
        store = {}
        if PUNCT_PATH.exists():
            with open(PUNCT_PATH) as f:
                store = json.load(f)
        store[original] = corrected
        with open(PUNCT_PATH, "w") as f:
            json.dump(store, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"[LEARNER] punct store error: {e}")
    import difflib
    ow, cw = original.split(), corrected.split()
    edits = 0
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(a=ow, b=cw, autojunk=False).get_opcodes():
        if tag == "equal":
            continue
        before = ow[i1-1] if i1 > 0 else ""
        if before:
            _add_rule("PUNCT", before, "repunctuate", " ".join(cw[j1:j2])[:40], source="REPORT")
        edits += 1
    print(f"[LEARNER] ✏ learned punctuation correction ({edits} edits)")
    return {"ok": True, "edits": edits}


# ---
# The good-settings store, which drives the thumbs-up guidance
# ---
# When the user confirms a chunk sounded perfect we remember the inference
# settings that produced it, keyed by sentence_type. /speak then biases the
# live temperature toward the confirmed-good value for the type it is about to
# synthesise, so future chunks of the same kind trend toward the calibre the
# user already approved. This is the closest thing to "learning" available
# without retraining XTTS: we steer the knobs we DO control toward known-good
# operating points.
_good_settings_cache = None
_good_settings_lock  = threading.Lock()


def _good_settings_path():
    # STORE_PATH may be a str or Path depending on import order; normalise.
    from pathlib import Path as _P
    base = _P(str(STORE_PATH)).parent
    return base / "good_settings.json"


def _load_good_settings():
    global _good_settings_cache
    if _good_settings_cache is not None:
        return _good_settings_cache
    try:
        p = _good_settings_path()
        if p.exists():
            with open(p) as f:
                _good_settings_cache = json.load(f)
        else:
            _good_settings_cache = {}
    except Exception:
        _good_settings_cache = {}
    return _good_settings_cache


def record_good_settings(sentence_type, settings):
    """Remember the inference settings of a confirmed-perfect chunk, keyed by
    sentence_type. EMA-blends with any prior good point so one outlier cannot
    dominate. Called from confirm_chunk_quality."""
    if not settings:
        return
    stype = (sentence_type or "sentence").lower()
    with _good_settings_lock:
        store = dict(_load_good_settings())
        prev  = store.get(_vk(stype)) or {}
        a = 0.3  # weight on the newest confirmed-good sample
        def ema(o, n):
            if n is None:
                return o
            return round(n if o is None else o * (1 - a) + n * a, 3)
        merged = {
            "temperature":        ema(prev.get("temperature"),        settings.get("temperature")),
            "repetition_penalty": ema(prev.get("repetition_penalty"), settings.get("repetition_penalty")),
            "top_k":              settings.get("top_k", prev.get("top_k")),
            "top_p":              ema(prev.get("top_p"),               settings.get("top_p")),
            # I deliberately don't store speaking_rate here. It was a librosa
            # words-per-second metric of about 5.x sharing a field name with the
            # XTTS
            # speed multiplier (~1.x) and silently overrode the user's speed.
            # Rate learning now lives in the band:<name> rate_mod entries.
            "confirm_count":      (prev.get("confirm_count", 0) + 1),
            "updated":            datetime.now().isoformat(),
        }
        store[_vk(stype)] = merged
        global _good_settings_cache
        _good_settings_cache = store
        try:
            with open(_good_settings_path(), "w") as f:
                json.dump(store, f, indent=2)
        except Exception as e:
            print(f"[LEARNER] good_settings save error: {e}")
    print(f"[LEARNER] ★ recorded good settings for '{stype}' "
          f"(temp={merged['temperature']}, n={merged['confirm_count']})")
    _before = prev.get("temperature")
    _bstr = "—" if _before is None else f"{_before:.3f}"
    log_history("CONFIRM",
                f"👍 perfect '{stype}' · temp {_bstr}→{merged['temperature']:.3f} (n={merged['confirm_count']})",
                "user")
    return merged


def _guess_sentence_type(text):
    """A fallback guess at the sentence type, used only when a chunk was never
    logged, since the normal path looks up the server's real classification
    through _lookup_chunk_type. It's a compact mirror of the main categories."""
    t = (text or "").strip()
    if not t:
        return "sentence"
    if t.endswith("?"):
        return "question"
    if re.search(r'\b(is defined as|means|refers to|is known as|is called|stands for)\b', t, re.IGNORECASE):
        return "definition"
    words = t.split()
    if len(words) <= 8 and t[-1] not in '.!?' and sum(1 for w in words if w and w[0].isupper()) >= len(words) * 0.6:
        return "heading"
    if t[-1] not in '.!?;:,':
        return "mid_clause"
    return "sentence"


_last_autotune_ts = 0
def _maybe_run_autotune():
    """Throttled trigger for the autonomous cycle: at most once every 5 minutes,
    and only when auto_tune is enabled. Called from the analysis thread."""
    global _last_autotune_ts
    if not autotune_enabled():
        return
    now = time.time()
    if now - _last_autotune_ts < 120:   # check at most every 2 min (was 5)
        return
    _last_autotune_ts = now
    try:
        acts = run_autotune_cycle()
        if acts:
            print(f"[LEARNER] auto-tune cycle: {len(acts)} change(s)")
    except Exception as e:
        print(f"[LEARNER] auto-tune cycle error: {e}")


def record_rejected_attempt(profile_str, sentence_type, params, problem):
    """Record a synthesis attempt that was thrown away as hallucinated.

    This is the highest-value evidence the system produces and until now I was
    throwing it away. When the output check rejects a waveform at temperature
    0.45 and the retry passes at 0.29, that's a controlled comparison: identical
    text, identical voice, identical everything except the one parameter, and a
    known outcome on both sides. Every other row in param_observations is purely
    observational, recorded from a chunk that happened to ship, and they all
    cluster tightly around whatever settings were in force at the time.

    Two things follow from writing the failures down.

    First, autotune finally sees negative examples at known parameter values
    instead of only ever seeing the chunks that survived.

    Second, and this is what actually unblocks it, _profile_quality_stats refuses
    to judge a parameter that barely varied across its observations, because a
    near-constant parameter can't be credited with a quality difference. Ordinary
    reads never move temperature, so that guard rejected almost every profile.
    The retry ladder deliberately steps temperature down, so these rows give the
    estimator the spread it needs.

    I record quality as 0.0. That isn't a penalty or a heuristic weight, the
    output was genuinely unusable, and the whole point is that the number is
    honest.
    """
    if not profile_str:
        return
    p = params or {}
    try:
        with _db_lock:
            conn = _get_db()
            conn.execute(
                "INSERT INTO param_observations "
                "(ts, profile, sentence_type, temperature, top_p, top_k, "
                " rep_penalty, speed, quality, accuracy, voice, rejected, problem) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,1,?)",
                (time.time(), profile_str, sentence_type,
                 p.get("temperature"), p.get("top_p"), p.get("top_k"),
                 p.get("repetition_penalty"), p.get("speed"),
                 0.0, None, _ACTIVE_VOICE, problem or "unknown"))
            conn.commit()
            conn.close()
    except Exception as e:
        print(f"[LEARNER] rejected-attempt record skipped: {e}")


def _record_param_observation(chunk_id, quality, accuracy):
    """Log one row of (profile, params, quality), which is the closed-loop
    dataset. This only records, and it reads the params and profile already
    stored on the chunk."""
    with _db_lock:
        conn = _get_db()
        row = conn.execute(
            "SELECT profile, sentence_type, used_temperature, used_top_p, "
            "used_top_k, used_rep_penalty, used_speed FROM chunks WHERE id=?",
            (chunk_id,)).fetchone()
        if row and row[0]:
            conn.execute(
                "INSERT INTO param_observations "
                "(ts, profile, sentence_type, temperature, top_p, top_k, "
                " rep_penalty, speed, quality, accuracy, voice) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (time.time(), row[0], row[1], row[2], row[3], row[4],
                 row[5], row[6], quality, accuracy, _ACTIVE_VOICE))
            conn.commit()
        conn.close()


# --- Autonomous self-tuning (OFF by default) ---
# Turned on only via good_settings 'auto_tune.enabled'. Even then it is
# deliberately gentle: tiny clamped steps, a minimum evidence requirement, and a
# rollback tripwire that reverts any change a profile's quality got worse after.
_AUTOTUNE_MIN_SAMPLES = 12   # per profile before any autonomous nudge
_AUTOTUNE_MARGIN      = 0.03 # quality gap required to act on a param preference
# Per-param step sizes live in _AUTOTUNE_PARAMS below, one per parameter.

def autotune_enabled():
    try:
        return bool((_load_good_settings().get("auto_tune") or {}).get("enabled"))
    except Exception:
        return False

def set_setting(key, value):
    """Persist a simple key-value setting in good_settings under 'settings'."""
    with _good_settings_lock:
        store = dict(_load_good_settings())
        s = dict(store.get("settings") or {})
        s[key] = value
        store["settings"] = s
        global _good_settings_cache
        _good_settings_cache = store
        try:
            with open(_good_settings_path(), "w") as f:
                json.dump(store, f, indent=2)
        except Exception as e:
            print(f"[LEARNER] set_setting save error: {e}")
    return value


def get_setting(key, default=None):
    """Read a simple key-value setting from good_settings."""
    try:
        return (_load_good_settings().get("settings") or {}).get(key, default)
    except Exception:
        return default


def set_autotune(on):
    """Persist the autonomous self-tuning on/off flag."""
    with _good_settings_lock:
        store = dict(_load_good_settings())
        at = dict(store.get("auto_tune") or {})
        at["enabled"] = bool(on)
        store["auto_tune"] = at
        global _good_settings_cache
        _good_settings_cache = store
        try:
            with open(_good_settings_path(), "w") as f:
                json.dump(store, f, indent=2)
        except Exception as e:
            print(f"[LEARNER] autotune flag save error: {e}")
    log_history("TEMP", f"auto-tune {'enabled' if on else 'disabled'} by user", "user")
    return bool(on)

def autotune_info():
    """Status + observation coverage for the UI: how many profiles have enough
    data to be self-tunable, and total observations recorded."""
    with _db_lock:
        conn = _get_db()
        total = conn.execute("SELECT COUNT(*) FROM param_observations WHERE voice=?", (_ACTIVE_VOICE,)).fetchone()[0]
        ready = conn.execute(
            "SELECT COUNT(*) FROM (SELECT profile FROM param_observations "
            "WHERE voice=? GROUP BY profile HAVING COUNT(*) >= ?)",
            (_ACTIVE_VOICE, _AUTOTUNE_MIN_SAMPLES)).fetchone()[0]
        conn.close()
    return {"enabled": autotune_enabled(), "observations": total,
            "profiles_ready": ready, "min_samples": _AUTOTUNE_MIN_SAMPLES}



# --- Storage retention ---
# Raw chunk rows and parameter observations accumulate on every read (~440
# bytes per chunk, so roughly 30–300 MB/year depending on use). Left unbounded
# the database would grow forever on a machine used daily.
#
# Pruning is safe because the LEARNING does not live in these rows: learned
# temperatures and rate modifiers are aggregated into good_settings.json, and
# rules/reports are stored separately. Chunks and observations are raw evidence
# and autotune uses the recent evidence, so old rows contribute nothing once
# their profile has enough samples.
#
# I never prune good_settings.json, which holds all the learned values, or the
# rules, or the reports, which are your own corrections, or the voice baselines.
_MAX_CHUNKS       = 5000    # roughly 2 MB, far more than the feed or insights need
_MAX_OBSERVATIONS = 20000   # autotune needs ~12 per profile; this holds hundreds

def prune_old_data(max_chunks=_MAX_CHUNKS, max_observations=_MAX_OBSERVATIONS,
                   vacuum_threshold=1000):
    """Keep the newest rows and drop the rest. Returns what was removed.

    VACUUM only runs after a substantial prune, because it rewrites the whole
    file and is far too slow to do routinely."""
    removed = {"chunks": 0, "observations": 0, "vacuumed": False}
    try:
        with _db_lock:
            conn = _get_db()
            n = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
            if n > max_chunks:
                cur = conn.execute(
                    "DELETE FROM chunks WHERE id NOT IN "
                    "(SELECT id FROM chunks ORDER BY ts DESC LIMIT ?)", (max_chunks,))
                removed["chunks"] = cur.rowcount or 0
            n = conn.execute("SELECT COUNT(*) FROM param_observations").fetchone()[0]
            if n > max_observations:
                cur = conn.execute(
                    "DELETE FROM param_observations WHERE id NOT IN "
                    "(SELECT id FROM param_observations ORDER BY id DESC LIMIT ?)",
                    (max_observations,))
                removed["observations"] = cur.rowcount or 0
            conn.commit()
            total = removed["chunks"] + removed["observations"]
            if total >= vacuum_threshold:
                conn.execute("VACUUM")       # reclaim the freed pages
                removed["vacuumed"] = True
            conn.close()
        if removed["chunks"] or removed["observations"]:
            print(f"[LEARNER] Retention: pruned {removed['chunks']} chunks, "
                  f"{removed['observations']} observations"
                  + (" (file compacted)" if removed["vacuumed"] else "")
                  + " — learned settings and rules untouched")
    except Exception as e:
        print(f"[LEARNER] retention prune skipped: {e}")
    return removed


def storage_info():
    """Database size and row counts, for the UI."""
    info = {"bytes": 0, "chunks": 0, "observations": 0,
            "max_chunks": _MAX_CHUNKS, "max_observations": _MAX_OBSERVATIONS}
    try:
        info["bytes"] = os.path.getsize(DB_PATH)
    except Exception:
        pass
    try:
        with _db_lock:
            conn = _get_db()
            info["chunks"] = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
            info["observations"] = conn.execute(
                "SELECT COUNT(*) FROM param_observations").fetchone()[0]
            conn.close()
    except Exception:
        pass
    return info


def diagnose_chunk(chunk_id=None, chunk_text=None):
    """Everything I know about one chunk, put together in a single answer.

    Answering "why did this chunk sound wrong, or fail?" used to mean joining the
    console scrollback, the chunk row, the observation rows and the rules table
    by hand, since each of them holds a different part of the story:

      what was asked for   the stored display text and its character count
      how it was labelled  sentence_type, profile fingerprint, facets, and
                           whether the spaCy layer ran and what it concluded
      what was done to it  the exact synth params used, and importantly where
                           they came from, whether that's a learned per-profile
                           value, a legacy per-type value or the live defaults
      what came out        synthesis and audio ms, real-time factor, retry count
      how it sounded       Whisper accuracy plus the quality flags derived from
                           the transcript (WHISPER_SKIP, HALLUCINATION, LOWCONF,
                           RUSHED, CUTOFF, VOICE_DRIFT), pitch and energy tail
      what went wrong      error type, stage and the full per-attempt trace
      what touched it      the learned rules whose pattern matches this text
      peer comparison      the average quality of the same fingerprint, so I can
                           tell a bad chunk apart from a bad fingerprint

    Takes an id or the raw text, since ids are content hashes so the text is
    enough on its own. Read-only.
    """
    cid = chunk_id
    if not cid and chunk_text:
        cid = hashlib.sha1(chunk_text.strip().encode("utf-8")).hexdigest()[:16]
    if not cid:
        return {"found": False, "error": "chunk_id or chunk_text required"}

    out = {"found": False, "chunk_id": cid}
    with _db_lock:
        conn = _get_db()
        try:
            row = conn.execute("SELECT * FROM chunks WHERE id=?", (cid,)).fetchone()
            if not row:
                return {"found": False, "chunk_id": cid,
                        "hint": "No record. The chunk may predate the current "
                                "retention window, or its text differs from what "
                                "was synthesised (ids hash the SPOKEN text)."}
            r = dict(row)
            out["found"] = True
            out["text"] = r.get("text")
            out["voice"] = r.get("voice")
            out["ts"] = r.get("ts")

            out["labelling"] = {
                "sentence_type":  r.get("sentence_type"),
                "profile":        r.get("profile"),
                "primary_facet":  r.get("primary_facet"),
                "facet_count":    r.get("facet_count"),
                "has_math":       r.get("has_math"),
                "clause_count":   r.get("clause_count"),
                "ends_mid":       r.get("ends_mid"),
                "emphasis_density": r.get("emphasis_density"),
                "sentence_count": r.get("sentence_count"),
                "is_fragment":    r.get("is_fragment"),
                "pos_available":  r.get("pos_available"),
                "silence_ms":     r.get("silence_ms"),
                "char_count":     r.get("char_count"),
            }
            # A chunk holding more than one sentence means the upstream splitter
            # missed a boundary, which is a common cause of odd pacing and is
            # invisible if you only look at the audio.
            if (r.get("sentence_count") or 0) > 1:
                out["labelling"]["warning"] = (
                    f"{r['sentence_count']} sentences in one chunk — the splitter "
                    f"missed a boundary, so prosody was planned for the whole run.")

            out["synthesis"] = {
                "temperature":        r.get("used_temperature"),
                "top_p":              r.get("used_top_p"),
                "top_k":              r.get("used_top_k"),
                "repetition_penalty": r.get("used_rep_penalty"),
                "speed":              r.get("used_speed"),
                "synth_ms":           r.get("synth_ms"),
                "audio_ms":           r.get("duration_ms"),
                "rtf":                r.get("rtf"),
                "retried":            r.get("retried"),
                "rejected":           r.get("rejected"),
            }
            # Waveforms thrown away as hallucinated before one passed. This is
            # usually the whole answer to "why did this chunk sound odd", since
            # it took several attempts and the last one went out regardless.
            if r.get("rejected"):
                try:
                    oc = json.loads(r.get("output_check") or "{}")
                except Exception:
                    oc = {}
                out["synthesis"]["rejected_detail"] = {
                    "discarded": r["rejected"],
                    "final_check": oc,
                    "note": ("Each discarded attempt was re-synthesised at a "
                             "lower temperature. A chunk needing several tries "
                             "is a sign its text or its profile is unstable."),
                }
            # Where each parameter actually came from, so "the model ignored my
            # setting" is checkable rather than arguable.
            prof = r.get("profile")
            sources = {}
            if prof:
                entry = _load_good_settings().get(_vk(f"prof:{prof}")) or {}
                for param, store_key in (("temperature", "temperature"),
                                         ("top_p", "top_p"),
                                         ("top_k", "top_k"),
                                         ("repetition_penalty", "repetition_penalty"),
                                         ("speed", "speed_mod")):
                    sources[param] = ("learned for this profile"
                                      if entry.get(store_key) is not None
                                      else "live/default settings")
            out["param_sources"] = sources

            out["quality"] = {
                "score":             r.get("quality_score"),
                "whisper_accuracy":  r.get("whisper_accuracy"),
                "flags":             r.get("quality_flags"),
                "pitch_variance":    r.get("pitch_variance"),
                "energy_tail":       r.get("energy_tail"),
                "voice_consistency": r.get("voice_consistency"),
                "speaking_rate":     r.get("speaking_rate"),
                "user_feedback":     r.get("user_feedback"),
                "user_verdict":      r.get("user_verdict"),
            }
            if r.get("quality_score") is None:
                out["quality"]["note"] = (
                    "Not analysed. Either sampling skipped it (analysis_every), "
                    "analysis is disabled on this machine, or the queue was full "
                    "— see /analysis/backlog.")

            out["failure"] = None
            if r.get("success") == 0 or r.get("error_type"):
                detail = {}
                try:
                    detail = json.loads(r.get("error_detail") or "{}")
                except Exception:
                    pass
                out["failure"] = {
                    "type":     r.get("error_type"),
                    "stage":    r.get("error_stage"),
                    "message":  detail.get("message"),
                    "attempts": detail.get("attempts", []),
                }

            # How this chunk's fingerprint performs in general, so I can tell
            # whether this chunk is unusual or the whole fingerprint is weak.
            if prof:
                peer = conn.execute(
                    "SELECT COUNT(*), AVG(quality) FROM param_observations "
                    "WHERE voice=? AND profile=? AND quality IS NOT NULL",
                    (r.get("voice") or _ACTIVE_VOICE, prof)).fetchone()
                if peer and peer[0]:
                    out["profile_peers"] = {
                        "n": peer[0],
                        "mean_quality": round(peer[1], 3) if peer[1] is not None else None,
                        "ready_to_autotune": peer[0] >= _AUTOTUNE_MIN_SAMPLES,
                    }
        finally:
            conn.close()

    # Which learned rules match this text, which is usually the explanation for
    # a chunk that reads differently from what's on the page.
    text_for_rules = out.get("text") or chunk_text or ""
    matched = []
    for rule in get_rules(active_only=True):
        pat = rule.get("pattern") or ""
        if not pat:
            continue
        try:
            if re.search(r"\b" + re.escape(pat) + r"\b", text_for_rules, re.IGNORECASE):
                matched.append({"id": rule["id"], "type": rule["rule_type"],
                                "pattern": pat, "action": rule.get("action"),
                                "value": rule.get("value"), "hits": rule.get("hits")})
        except re.error:
            continue
    out["matching_rules"] = matched[:20]
    out["analysis_backlog"] = analysis_backlog()
    return out


def ai_insights():
    """Deep quality insight for the AI page. All read-only and all scoped to the
    active voice.

      profiles         per-fingerprint quality with a trend arrow, comparing the
                       recent half of its observations against the earlier half
      math_split       equation and symbol chunks against plain ones, which
                       directly measures whether the maths expander is winning
      splitter         how often a chunk held 2 or more sentences, meaning a
                       missed boundary, with recent examples to act on
      report_outcomes  for the latest reports, the quality of the reported
                       chunk's fingerprint before and after the report, which
                       closes the loop on whether reporting actually helped
      voices           per-voice quality comparison, only once 2 or more have
                       enough data to compare
    """
    out = {"profiles": [], "math_split": None, "splitter": None,
           "report_outcomes": [], "voices": [],
           "perf_by_facet": [], "perf_overall": None, "retries": None}
    with _db_lock:
        conn = _get_db()
        V = _ACTIVE_VOICE
        try:
            # --- Per-fingerprint quality + trend ---
            # Quality here means the quality of what the user HEARD, so rejected
            # attempts are left out of the average since they never reached the
            # ear. I surface them beside it as a rejection rate instead, which
            # keeps both numbers meaning exactly one thing. (The autotune
            # estimator does use them; see _profile_quality_stats.)
            rows = conn.execute(
                "SELECT profile, COUNT(*) n, AVG(quality) q FROM param_observations "
                "WHERE voice=? AND quality IS NOT NULL AND profile IS NOT NULL "
                "AND COALESCE(rejected, 0) = 0 "
                "GROUP BY profile HAVING n >= 6 ORDER BY n DESC LIMIT 10",
                (V,)).fetchall()
            for prof, n, q in rows:
                half = conn.execute(
                    "SELECT AVG(quality) FROM (SELECT quality FROM param_observations "
                    "WHERE voice=? AND profile=? AND quality IS NOT NULL "
                    "AND COALESCE(rejected, 0) = 0 "
                    "ORDER BY id DESC LIMIT ?)", (V, prof, max(3, n // 2))).fetchone()[0]
                trend = None
                if half is not None and q is not None:
                    d = half - q
                    trend = "up" if d > 0.02 else ("down" if d < -0.02 else "flat")
                rej = conn.execute(
                    "SELECT COUNT(*) FROM param_observations "
                    "WHERE voice=? AND profile=? AND rejected = 1",
                    (V, prof)).fetchone()[0]
                out["profiles"].append({"profile": prof, "n": n,
                                        "quality": round(q, 3) if q is not None else None,
                                        "recent": round(half, 3) if half is not None else None,
                                        "trend": trend,
                                        "rejected": rej,
                                        "reject_rate": (round(rej / (n + rej), 3)
                                                        if (n + rej) else 0.0)})
            # --- Maths vs plain ---
            m = conn.execute(
                "SELECT AVG(CASE WHEN has_math=1 THEN quality_score END), "
                "       SUM(CASE WHEN has_math=1 THEN 1 ELSE 0 END), "
                "       AVG(CASE WHEN has_math=0 OR has_math IS NULL THEN quality_score END), "
                "       SUM(CASE WHEN has_math=0 OR has_math IS NULL THEN 1 ELSE 0 END) "
                "FROM chunks WHERE voice=? AND quality_score IS NOT NULL", (V,)).fetchone()
            if m and m[1] and m[1] >= 3:
                out["math_split"] = {"math_q": round(m[0], 3), "math_n": m[1],
                                     "plain_q": round(m[2], 3) if m[2] is not None else None,
                                     "plain_n": m[3]}
            # --- Splitter health ---
            sp = conn.execute(
                "SELECT COUNT(*), SUM(CASE WHEN sentence_count > 1 THEN 1 ELSE 0 END) "
                "FROM chunks WHERE voice=? AND sentence_count IS NOT NULL", (V,)).fetchone()
            if sp and sp[0]:
                ex = [r[0] for r in conn.execute(
                    "SELECT text FROM chunks WHERE voice=? AND sentence_count > 1 "
                    "ORDER BY ts DESC LIMIT 3", (V,)).fetchall()]
                out["splitter"] = {"total": sp[0], "multi": sp[1] or 0,
                                   "pct": round(100.0 * (sp[1] or 0) / sp[0], 1),
                                   "examples": ex}
            # --- Report outcomes (loop closure) ---
            reps = conn.execute(
                "SELECT r.ts, r.issue, c.profile FROM reports r "
                "JOIN chunks c ON c.id = r.chunk_id "
                "WHERE c.profile IS NOT NULL ORDER BY r.ts DESC LIMIT 3").fetchall()
            for rts, issue, prof in reps:
                before = conn.execute(
                    "SELECT AVG(quality), COUNT(*) FROM param_observations "
                    "WHERE voice=? AND profile=? AND quality IS NOT NULL AND ts < ?",
                    (V, prof, rts)).fetchone()
                after = conn.execute(
                    "SELECT AVG(quality), COUNT(*) FROM param_observations "
                    "WHERE voice=? AND profile=? AND quality IS NOT NULL AND ts >= ?",
                    (V, prof, rts)).fetchone()
                out["report_outcomes"].append({
                    "issue": issue, "profile": prof,
                    "before": round(before[0], 3) if before and before[0] is not None else None,
                    "after":  round(after[0], 3) if after and after[0] is not None else None,
                    "n_before": before[1] if before else 0,
                    "n_after":  after[1] if after else 0,
                })
            # --- Synthesis cost by chunk shape ---
            # Which content is expensive to speak on THIS machine. RTF < 1 means
            # the chunk was produced faster than it plays, so buffering holds.
            perf = conn.execute(
                "SELECT primary_facet, COUNT(*), AVG(rtf), AVG(synth_ms) "
                "FROM chunks WHERE voice=? AND rtf IS NOT NULL "
                "GROUP BY primary_facet HAVING COUNT(*) >= 3 "
                "ORDER BY AVG(rtf) DESC", (V,)).fetchall()
            if perf:
                out["perf_by_facet"] = [
                    {"facet": (f or "prose"), "n": n,
                     "rtf": round(r, 2), "ms": int(ms or 0)}
                    for f, n, r, ms in perf]
            slow = conn.execute(
                "SELECT AVG(rtf), COUNT(*) FROM chunks WHERE voice=? AND rtf IS NOT NULL",
                (V,)).fetchone()
            if slow and slow[1]:
                out["perf_overall"] = {"rtf": round(slow[0], 2), "n": slow[1]}
            # Retries are a strong instability signal. The column existed but
            # nothing ever populated it before.
            rt = conn.execute(
                "SELECT COUNT(*), SUM(CASE WHEN retried>0 THEN 1 ELSE 0 END) "
                "FROM chunks WHERE voice=?", (V,)).fetchone()
            if rt and rt[0]:
                out["retries"] = {"total": rt[0], "retried": rt[1] or 0,
                                  "pct": round(100.0*(rt[1] or 0)/rt[0], 1)}

            # --- Hallucination rate, and where it happens ---
            # Which fingerprints the model actually loses the thread on. This is
            # the most directly actionable number in the panel: a profile with a
            # high reject rate is one whose temperature is too high for it.
            rej_rows = conn.execute(
                "SELECT problem, COUNT(*) FROM param_observations "
                "WHERE voice=? AND rejected = 1 GROUP BY problem "
                "ORDER BY COUNT(*) DESC", (V,)).fetchall()
            if rej_rows:
                total_obs = conn.execute(
                    "SELECT COUNT(*) FROM param_observations WHERE voice=?",
                    (V,)).fetchone()[0] or 1
                worst = conn.execute(
                    "SELECT profile, COUNT(*) c FROM param_observations "
                    "WHERE voice=? AND rejected = 1 AND profile IS NOT NULL "
                    "GROUP BY profile ORDER BY c DESC LIMIT 3", (V,)).fetchall()
                caught = sum(r[1] for r in rej_rows)
                out["rejections"] = {
                    "caught": caught,
                    "rate": round(caught / total_obs, 3),
                    "by_problem": {(r[0] or "unknown"): r[1] for r in rej_rows},
                    "worst_profiles": [{"profile": p, "n": c} for p, c in worst],
                    "note": ("Each was re-synthesised at a lower temperature "
                             "rather than played."),
                }

            # --- Voice comparison (only meaningful with 2+ voices of data) ---
            vs = conn.execute(
                "SELECT voice, COUNT(*), AVG(quality) FROM param_observations "
                "WHERE quality IS NOT NULL GROUP BY voice HAVING COUNT(*) >= 5").fetchall()
            if len(vs) >= 2:
                out["voices"] = [{"voice": v or "default", "n": n, "quality": round(q, 3)}
                                 for v, n, q in vs]
        finally:
            conn.close()
    return out


def autotune_report():
    """Rich analysis for the AI tab: observation coverage, per-profile mean
    quality + sample counts, recent autonomous changes with rationale, overall
    quality trend, and how many tuning checkpoints are open. Read-only."""
    out = {"enabled": autotune_enabled(), "min_samples": _AUTOTUNE_MIN_SAMPLES,
           "throttle_sec": 120, "params_tunable": list(_AUTOTUNE_PARAMS.keys())}
    with _db_lock:
        conn = _get_db()
        try:
            out["observations"] = conn.execute("SELECT COUNT(*) FROM param_observations WHERE voice=?", (_ACTIVE_VOICE,)).fetchone()[0]
            # Per-profile: count + mean quality, richest first.
            rows = conn.execute(
                "SELECT profile, COUNT(*) n, AVG(quality) q FROM param_observations "
                "WHERE voice=? AND quality IS NOT NULL "
                "GROUP BY profile ORDER BY n DESC LIMIT 12",
                (_ACTIVE_VOICE,)).fetchall()
            out["profiles"] = [
                {"profile": r[0], "samples": r[1], "mean_quality": round(r[2], 3) if r[2] is not None else None,
                 "ready": r[1] >= _AUTOTUNE_MIN_SAMPLES} for r in rows]
            out["profiles_ready"] = sum(1 for p in out["profiles"] if p["ready"])
            # Overall quality: recent 50 vs prior 50 → trend.
            recent = conn.execute("SELECT AVG(quality) FROM (SELECT quality FROM param_observations "
                "WHERE quality IS NOT NULL AND voice=? ORDER BY id DESC LIMIT 50)",
                (_ACTIVE_VOICE,)).fetchone()[0]
            prior = conn.execute("SELECT AVG(quality) FROM (SELECT quality FROM param_observations "
                "WHERE quality IS NOT NULL AND voice=? ORDER BY id DESC LIMIT 50 OFFSET 50)",
                (_ACTIVE_VOICE,)).fetchone()[0]
            out["quality_recent"] = round(recent, 3) if recent is not None else None
            out["quality_prior"]  = round(prior, 3) if prior is not None else None
            out["quality_trend"]  = (round(recent - prior, 3)
                                     if (recent is not None and prior is not None) else None)
        except Exception as e:
            out["error"] = str(e)
        conn.close()
    # Recent autonomous changes (from history log, auto-sourced TEMP entries).
    try:
        hist = get_history(limit=60) or []
        out["recent_changes"] = [
            {"detail": h.get("detail"), "ts": h.get("ts")}
            for h in hist if h.get("source") == "auto" and "auto-tune" in (h.get("detail") or "")
        ][:8]
    except Exception:
        out["recent_changes"] = []
    # Open tuning checkpoints (changes being watched by the tripwire).
    try:
        ck = (_load_good_settings().get(_vk("_autotune_ckpt")) or {})
        out["open_checkpoints"] = len(ck)
    except Exception:
        out["open_checkpoints"] = 0
    return out


# Params the autotuner may adjust, each with (min, max, step, integer?). A param
# is only ever nudged when its own observation split shows a clear quality margin
# for a profile, so params that don't affect quality are left untouched.
_AUTOTUNE_PARAMS = {
    "temperature": (0.05, 0.95, 0.01, False),
    "top_p":       (0.50, 0.99, 0.01, False),
    "rep_penalty": (1.0, 10.0, 0.05, False),
    "top_k":       (5,   100,  1,    True),
    "speed":       (0.7,  1.6,  0.01, False),
}
# Map param name → the profile-entry key it's stored under (what synthesis reads).
_AUTOTUNE_STORE_KEY = {
    "temperature": "temperature", "top_p": "top_p", "rep_penalty": "repetition_penalty",
    "top_k": "top_k", "speed": "speed_mod",
}

def _profile_quality_stats(profile, param, conn):
    """Split a profile's observations into low and high halves of a param and
    return (low_mean_q, high_mean_q, n), which is the evidence for whether
    raising or lowering that param improves measured quality for a fingerprint.

    Returns None when there isn't enough data, or when the param barely varies.
    If a param was near-constant across the observations then its low and high
    split is arbitrary, and it would wrongly get the credit for another param's
    effect on quality. Requiring real spread is what stops multi-param tuning
    confounding itself.

    Rejected attempts, recorded at quality 0.0 by record_rejected_attempt, are
    deliberately included. They're the only observations in the table that came
    from changing a parameter on purpose and seeing what happened, and they're
    what gives most profiles enough spread to clear the guard above at all."""
    rows = conn.execute(
        f"SELECT {param}, quality FROM param_observations "
        f"WHERE voice=? AND profile=? AND {param} IS NOT NULL AND quality IS NOT NULL",
        (_ACTIVE_VOICE, profile)).fetchall()
    if len(rows) < _AUTOTUNE_MIN_SAMPLES:
        return None
    vals = sorted(rows, key=lambda r: r[0])
    lo_val, hi_val = vals[0][0], vals[-1][0]
    span = hi_val - lo_val
    # Require the param to span a meaningful range relative to its own scale;
    # a param that never moved can't be credited with a quality difference.
    rng = _AUTOTUNE_PARAMS.get(param)
    if rng:
        full = rng[1] - rng[0]
        if full > 0 and (span / full) < 0.05:   # varied <5% of its range → skip
            return None
    mid = len(vals) // 2
    low  = [q for _, q in vals[:mid]]
    high = [q for _, q in vals[mid:]]
    if not low or not high:
        return None
    return (sum(low)/len(low), sum(high)/len(high), len(rows))

def run_autotune_cycle():
    """One safe self-tuning pass over the profiles that have enough data.

    For each tunable param independently, if its high or low half clearly beats
    the other by at least the margin, I nudge the stored preference one tiny step
    that way. I record a checkpoint per profile and param so the next cycle can
    roll back if quality fell after the change. A param showing no quality margin
    is never touched, so params only enter the loop once the data proves they
    matter. Does nothing unless auto_tune is enabled. Returns a summary list."""
    if not autotune_enabled():
        return []
    actions = []
    with _db_lock:
        conn = _get_db()
        profiles = [r[0] for r in conn.execute(
            "SELECT profile, COUNT(*) c FROM param_observations WHERE voice=? "
            "GROUP BY profile HAVING c >= ?",
            (_ACTIVE_VOICE, _AUTOTUNE_MIN_SAMPLES)).fetchall()]
        store = dict(_load_good_settings())
        ckpts = dict(store.get(_vk("_autotune_ckpt")) or {})

        def _recent_q(prof):
            return conn.execute(
                "SELECT AVG(quality) FROM (SELECT quality FROM param_observations "
                "WHERE voice=? AND profile=? ORDER BY id DESC LIMIT 6)",
                (_ACTIVE_VOICE, prof)).fetchone()[0]

        # Tripwire, reverting any profile/param checkpoint whose quality dropped.
        for ckey, ck in list(ckpts.items()):
            prof = ck.get("profile"); param = ck.get("param")
            if not prof or not param:
                ckpts.pop(ckey, None); continue
            recent = _recent_q(prof)
            if recent is not None and ck.get("q_before") is not None \
               and recent < ck["q_before"] - 0.02:
                skey = _AUTOTUNE_STORE_KEY[param]
                pentry = dict(store.get(_vk(f"prof:{prof}")) or {})
                pentry[skey] = ck.get("before")
                store[_vk(f"prof:{prof}")] = pentry
                actions.append(f"↩ rolled back {prof}/{param} (q {recent:.2f} < {ck['q_before']:.2f})")
                ckpts.pop(ckey, None)

        # Forward pass, going per profile and per param, nudging only on clear
        # evidence.
        for prof in profiles:
            for param, (lo, hi, step, is_int) in _AUTOTUNE_PARAMS.items():
                ckey = f"{prof}::{param}"
                if ckey in ckpts:
                    continue  # one open checkpoint per (profile,param)
                stats = _profile_quality_stats(prof, param, conn)
                if not stats:
                    continue
                low_q, high_q, n = stats
                skey = _AUTOTUNE_STORE_KEY[param]
                entry = dict(store.get(_vk(f"prof:{prof}")) or {})
                cur = entry.get(skey)
                if cur is None:
                    # No stored preference yet, so seed it from the median of
                    # what's actually been used for this param and profile.
                    med = conn.execute(
                        f"SELECT {param} FROM param_observations "
                        f"WHERE voice=? AND profile=? "
                        f"AND {param} IS NOT NULL ORDER BY {param} "
                        f"LIMIT 1 OFFSET (SELECT COUNT(*) FROM param_observations "
                        f"WHERE voice=? AND profile=? AND {param} IS NOT NULL)/2",
                        (_ACTIVE_VOICE, prof, _ACTIVE_VOICE, prof)).fetchone()
                    if not med or med[0] is None:
                        continue
                    cur = med[0]
                if high_q - low_q >= _AUTOTUNE_MARGIN:
                    new = cur + step
                elif low_q - high_q >= _AUTOTUNE_MARGIN:
                    new = cur - step
                else:
                    continue  # no quality difference → leave this param alone
                new = max(lo, min(hi, new))
                new = int(round(new)) if is_int else round(new, 3)
                if new == cur:
                    continue
                entry[skey] = new
                store[_vk(f"prof:{prof}")] = entry
                ckpts[ckey] = {"profile": prof, "param": param, "before": cur,
                               "q_before": _recent_q(prof), "ts": time.time()}
                actions.append(f"{prof}: {param} {cur}→{new} (q {low_q:.2f}/{high_q:.2f})")

        store[_vk("_autotune_ckpt")] = ckpts
        global _good_settings_cache
        _good_settings_cache = store
        try:
            with open(_good_settings_path(), "w") as f:
                json.dump(store, f, indent=2)
        except Exception as e:
            print(f"[LEARNER] autotune save error: {e}")
        conn.close()
    for a in actions:
        log_history("TEMP", f"auto-tune: {a}", "auto")
    return actions


def _adjust_profile_temperature(profile_keys, step, base_default=0.33):
    """Nudge the preferred temperature for the MOST SPECIFIC profile key, and
    also (with a damped step) the coarser keys so broad learning still forms.
    Stores under 'prof:<key>' with an evidence count. Returns (key, new_t)."""
    if not profile_keys:
        return (None, None)
    with _good_settings_lock:
        store = dict(_load_good_settings())
        specific = profile_keys[0]
        results = {}
        # Most-specific key gets the full step; each coarser level gets half the
        # previous, so specific patterns move most but broad buckets still learn.
        s = step
        for i, key in enumerate(profile_keys):
            pk = _vk(f"prof:{key}")
            entry = dict(store.get(pk) or {})
            base = entry.get("temperature")
            if base is None:
                base = base_default
            new_t = round(max(0.05, min(0.95, base + s)), 3)
            entry["temperature"] = new_t
            entry["count"] = entry.get("count", 0) + 1
            entry["updated"] = datetime.now().isoformat()
            store[pk] = entry
            results[key] = new_t
            s = s * 0.5   # damp for coarser levels
        global _good_settings_cache
        _good_settings_cache = store
        try:
            with open(_good_settings_path(), "w") as f:
                json.dump(store, f, indent=2)
        except Exception as e:
            print(f"[LEARNER] profile temp save error: {e}")
    return (specific, results.get(specific))


def resolve_profile_temperature(profile_keys, current_temp, min_count=2):
    """Return the learned temperature for a chunk profile.

    I walk the key hierarchy from most specific down to coarsest and take the
    first one with enough evidence behind it, meaning count >= min_count, and
    fall back to current_temp if none of them qualify. That's what makes this
    precise when the data is rich and still robust when it's thin, since a
    rarely-seen fingerprint just defers to a broader learned bucket."""
    if not profile_keys:
        return current_temp
    store = _load_good_settings()
    for key in profile_keys:
        entry = store.get(_vk(f"prof:{key}"))
        if entry and entry.get("temperature") is not None and entry.get("count", 0) >= min_count:
            return entry["temperature"]
    # Legacy fallback: the plain sentence_type entry (pre-profile learning).
    stype = profile_keys[-1].split("|")[0]
    legacy = store.get(_vk(stype))
    if legacy and legacy.get("temperature") is not None:
        return legacy["temperature"]
    return current_temp


def resolve_profile_param(profile_str, store_key, default):
    """Return an autotuned per-profile param (e.g. top_p, repetition_penalty,
    top_k, speed_mod) from the coarse 'prof:<profile>' entry the autotuner writes.
    Falls back to `default` when the param hasn't been learned for this profile.
    profile_str is the 'band|length|punct|lexis' string stored on each chunk."""
    if not profile_str:
        return default
    entry = _load_good_settings().get(_vk(f"prof:{profile_str}"))
    if entry and entry.get(store_key) is not None:
        return entry[store_key]
    return default


def _adjust_pref_temperature(sentence_type, step):
    """Nudge the stored per-sentence-type preferred temperature, clamped."""
    stype = (sentence_type or "sentence").lower()
    with _good_settings_lock:
        store = dict(_load_good_settings())
        entry = dict(store.get(_vk(stype)) or {})
        base = entry.get("temperature")
        if base is None:
            base = 0.33
        new_t = round(max(0.05, min(0.95, base + step)), 3)
        entry["temperature"] = new_t
        entry["updated"] = datetime.now().isoformat()
        store[_vk(stype)] = entry
        global _good_settings_cache
        _good_settings_cache = store
        try:
            with open(_good_settings_path(), "w") as f:
                json.dump(store, f, indent=2)
        except Exception as e:
            print(f"[LEARNER] temp save error: {e}")
    return new_t


def complexity_band(text):
    """Classify a chunk into a complexity band: 'simple', 'normal' or 'dense'.

    Used to make speaking-rate learning COMPLEXITY-AWARE: a too-fast report on a
    dense technical chunk should slow future dense chunks, not every chunk.
    Deterministic, cheap, and computed from measurable features only:
      avg word length, share of long words (8+ chars), punctuation density,
      digit density, and chunk length."""
    t = (text or "").strip()
    words = re.findall(r"[A-Za-z0-9']+", t)
    if not words:
        return "normal"
    n          = len(words)
    avg_len    = sum(len(w) for w in words) / n
    long_ratio = sum(1 for w in words if len(w) >= 8) / n
    punct10    = sum(t.count(c) for c in ',;:()') / max(1.0, n / 10.0)
    digit_r    = sum(ch.isdigit() for ch in t) / max(1, len(t))
    score = 0
    if avg_len    >= 5.6:  score += 1
    if avg_len    >= 6.4:  score += 1
    if long_ratio >= 0.22: score += 1
    if long_ratio >= 0.35: score += 1
    if punct10    >= 1.5:  score += 1
    if digit_r    >= 0.04: score += 1
    if n          >= 24:   score += 1
    if score >= 3:
        return "dense"
    if score == 0 and n <= 14 and avg_len < 5.2:
        return "simple"
    return "normal"


# Plain-language band names used in report confirmations and the tuning panel.
BAND_LABEL = {"simple": "Short simple", "normal": "Typical", "dense": "Dense technical"}


def _length_bucket(n_words):
    """Chunk length bucket by word count: short / medium / long."""
    if n_words <= 8:  return "short"
    if n_words <= 20: return "medium"
    return "long"


def _punct_profile(text):
    """The punctuation shape, which affects pacing and pausing:
      clean    little internal punctuation
      commas   comma-heavy, so enumerations and appositives
      complex  colons, semicolons, dashes or parentheticals present
    'complex' beats 'commas' since those marks demand stronger pauses."""
    t = text or ""
    words = max(1, len(re.findall(r"[A-Za-z0-9']+", t)))
    _complex_marks = [';', ':', '\u2014', '\u2013', '(', ')']
    if any(c in t for c in _complex_marks) or " - " in t:
        return "complex"
    comma_density = t.count(",") / (words / 10.0)   # commas per 10 words
    if comma_density >= 1.5:
        return "commas"
    return "clean"


def _lexical_profile(text):
    """Lexical difficulty, which affects pronunciation and rushing:
      plain      ordinary words
      technical  a lot of long or rare words
      symbolic   noticeable digits, acronyms or symbols, so numbers, all-caps
                 tokens and maths"""
    t = (text or "").strip()
    words = re.findall(r"[A-Za-z0-9']+", t)
    if not words:
        return "plain"
    n = len(words)
    acronyms = sum(1 for w in words if len(w) >= 2 and w.isupper())
    digits   = sum(ch.isdigit() for ch in t)
    symbols  = sum(t.count(c) for c in "+*/=^%<>~≈≠×÷√")
    long_ratio = sum(1 for w in words if len(w) >= 9) / n
    if (digits / max(1, len(t))) >= 0.05 or acronyms >= 2 or symbols >= 2:
        return "symbolic"
    if long_ratio >= 0.28:
        return "technical"
    return "plain"


# --- Content facets ---
# A chunk is rarely just one thing: a definition can contain an equation, a list
# item can be a citation. These are ORTHOGONAL traits, detected independently,
# unlike sentence_type which is a single classification.
#
# Only `math` enters the learning fingerprint. That is a deliberate trade: every
# dimension added to a key multiplies the number of buckets and thins the
# evidence in each, and autotune needs ~12 samples per bucket before it acts.
# Equations earn the split because they measurably behave differently (higher
# hallucination, distinct failure mode). The rest are recorded for analysis.
_FACET_MATH = re.compile(
    r"\\[A-Za-z]+"                      # LaTeX command
    r"|[_^]\{"                          # braced sub/superscript
    r"|\b[A-Za-z]_[A-Za-z0-9]"          # X_t
    r"|[×÷−≤≥≠≈∑∏√∫±∂∞∇∝≡∈∪∩⊂]"        # unicode operators
    r"|\d\s*[+\-*/=]\s*\d"             # inline arithmetic
    r"|[⁰-⁹ⁿⁱ²³][^\w]|[₀-₉]"            # unicode super/subscripts
)
_FACET_LIST = re.compile(r"^\s*(?:[-•*–]|\(?\d{1,2}[.)]|[a-z][.)])\s+", re.M)
# A DEFINITION is a chunk that DEFINES a term, not any chunk containing the verb
# "to be". The old pattern matched a bare \b(is|are)\b, which appears in a large
# fraction of all English prose, so "The sky is blue today" was labelled a
# definition, and because `definition` sits high in the primary-facet priority
# order below, most ordinary sentences ended up filed under it. Every
# definitional phrase now has to appear in its full multi-word form.
_FACET_DEFN = re.compile(
    r"\b(?:is|are)\s+(?:defined|known|referred\s+to|called|termed)\s+(?:as|to)\b"
    r"|\b(?:means|denotes|signifies|refers\s+to|stands\s+for|is\s+short\s+for)\b"
    r"|\bis\s+(?:a|an|the)\s+(?:type|kind|form|class|category|measure)\s+of\b"
    # "Term: the thing it is" / "Term - the thing it is". Scoped case-SENSITIVE
    # via (?-i:...) so the leading capital still has to be a capital; with the
    # module-level re.I this form would otherwise match any "word: something".
    r"|(?-i:^\s*[A-Z][\w \-]{2,40}\s*[:—–]\s+)"
    r"|(?-i:^\s*[A-Z][\w ]{2,40}\s+[-–—]\s+)", re.M | re.I)
# CODE requires a syntactic marker, not an English word that happens to be a
# keyword somewhere. "class", "import", "function", "const" and "var" are all
# ordinary English ("the class met on Tuesday", "the function of the heart"), so
# the old bare keyword alternation labelled plain prose as code. Keywords now
# count only where they are unambiguously syntax.
_FACET_CODE = re.compile(
    r"[A-Za-z_]\w*\(\s*\)"                       # zero-arg call: foo()
    r"|\b(?:def|class)\s+[A-Za-z_]\w*\s*[(:]"    # def name( / class Name:
    r"|^\s*(?:import|from)\s+[A-Za-z_][\w.]*"    # import statement at line start
    r"|\b(?:const|let|var)\s+[A-Za-z_]\w*\s*="   # JS declaration with assignment
    r"|\bfunction\s+[A-Za-z_]\w*\s*\("           # function name(
    r"|[{;]\s*$"                                 # line ends in a brace/semicolon
    r"|\b[a-z_]\w*\.[a-z_]\w*\s*\(",             # obj.method(
    re.M)
# QUOTE needed only two quote-ish characters six apart, and an ASCII apostrophe
# counted as one \u2014 so "It doesn't matter what you're doing" matched on its two
# apostrophes. A straight single quote is no longer a delimiter; a real
# quotation in page text uses double or curly quotes.
_FACET_QUOTE = re.compile(
    r"[\u201c\u201d\"][^\u201c\u201d\"]{6,}?[\u201c\u201d\"]"
    r"|\u2018[^\u2019]{6,}?\u2019"
    r"|\b(?:said|writes|wrote|argues|argued|notes|noted|claims|observes)\b")
_FACET_CITE = re.compile(r"\(\s*[A-Z][A-Za-z]+(?:\s+(?:et al\.?|and\s+[A-Z][A-Za-z]+))?,?\s*\d{4}\s*\)"
                         r"|\[\d{1,3}\]")

def analyse_facets(text):
    """Detect every content trait present in a chunk. A chunk may have several.

    Returns {math, list, definition, code, quote, citation, primary, count}
    where `primary` names the most consequential facet for prosody."""
    t = text or ""
    f = {
        "math":       bool(_FACET_MATH.search(t)),
        "list":       bool(_FACET_LIST.search(t)),
        "definition": bool(_FACET_DEFN.search(t)),
        "code":       bool(_FACET_CODE.search(t)),
        "quote":      bool(_FACET_QUOTE.search(t)),
        "citation":   bool(_FACET_CITE.search(t)),
    }
    # Priority reflects how strongly each trait should shape delivery.
    for name in ("math", "code", "list", "definition", "quote", "citation"):
        if f[name]:
            f["primary"] = name
            break
    else:
        f["primary"] = "prose"
    f["count"] = sum(1 for k in ("math","list","definition","code","quote","citation") if f[k])
    return f


def chunk_profile(text, sentence_type=None):
    """Multi-dimensional chunk fingerprint for precise, non-oscillating learning.

    Returns a dict of independent dimensions plus `keys`: a hierarchy from the
    most specific composite key down to the coarsest, so a report can adjust the
    most specific profile that has enough evidence and gracefully fall back to
    broader buckets when data is thin. Every dimension is deterministic and
    cheap (no model calls beyond what's already computed)."""
    t = (text or "").strip()
    words = re.findall(r"[A-Za-z0-9']+", t)
    n = len(words)
    stype  = (sentence_type or "sentence").lower()
    band   = complexity_band(t)
    length = _length_bucket(n)
    punct  = _punct_profile(t)
    lexis  = _lexical_profile(t)
    facets = analyse_facets(t)
    # Hierarchy: most specific → coarsest. Learning tries each in order.
    base = [
        f"{stype}|{band}|{length}|{punct}|{lexis}",   # full fingerprint
        f"{stype}|{band}|{length}|{punct}",           # drop lexis
        f"{stype}|{band}|{punct}",                    # structure + punctuation
        f"{stype}|{band}",                            # type + complexity
        f"{stype}",                                   # sentence type only (legacy)
    ]
    # Equation chunks get a dedicated layer ABOVE the shared one. Two benefits:
    # existing prose keys stay byte-identical (nothing to migrate), and a maths
    # chunk inherits general learning until it has enough of its own evidence.
    if facets["math"]:
        keys = [f"math|{k}" for k in base[:3]] + ["math"] + base
    else:
        keys = base
    return {
        "sentence_type": stype, "band": band, "length": length,
        "punct": punct, "lexis": lexis, "n_words": n, "keys": keys,
        "facets": facets, "primary": facets["primary"],
    }


def get_rate_modifier(band):
    """Return the learned rate modifier for a complexity band (default 1.0).
    Final synthesis speed = user's live speed x this modifier, so the user's
    chosen pace is always the anchor and learning only shades it per band.
    Clamped to 0.85-1.15 so learning can never run away."""
    store = _load_good_settings()
    entry = store.get(_vk(f"band:{band}")) or {}
    mod = entry.get("rate_mod", 1.0)
    try:
        mod = float(mod)
    except (TypeError, ValueError):
        return 1.0
    return max(0.85, min(1.15, mod))


def _adjust_band_rate(band, step):
    """Nudge a complexity band's rate modifier and return the new value.

    Anti-oscillation: if this step reverses the previous step's direction for the
    same band within 10 minutes (the user flip-flopping too fast / too slow),
    the step size is halved so the value converges instead of bouncing."""
    key = _vk(f"band:{band}")
    now = time.time()
    with _good_settings_lock:
        store = dict(_load_good_settings())
        entry = dict(store.get(key) or {})
        base  = entry.get("rate_mod", 1.0)
        try:
            base = float(base)
        except (TypeError, ValueError):
            base = 1.0
        last_dir = entry.get("last_step_dir", 0)
        last_ts  = entry.get("last_step_ts", 0)
        damped = False
        if last_dir and (step > 0) != (last_dir > 0) and (now - last_ts) < 600:
            step *= 0.5
            damped = True
        new_mod = round(max(0.85, min(1.15, base + step)), 3)
        entry["rate_mod"]      = new_mod
        entry["last_step_dir"] = 1 if step > 0 else -1
        entry["last_step_ts"]  = now
        entry["updated"]       = datetime.now().isoformat()
        store[key] = entry
        global _good_settings_cache
        _good_settings_cache = store
        try:
            with open(_good_settings_path(), "w") as f:
                json.dump(store, f, indent=2)
        except Exception as e:
            print(f"[LEARNER] band rate save error: {e}")
    print(f"[LEARNER] rate_mod[{band}] → {new_mod}" + (" (damped: direction flip)" if damped else ""))
    return new_mod, damped


def _lookup_chunk_type(chunk_id, chunk_text):
    """Resolve the TRUE sentence type for a reported chunk.

    The server's 10-type prosody detector classifies every chunk at synthesis
    and log_chunk stores it. Chunk ids are content hashes of the text, so even
    when a report arrives without an id we can re-derive it from the text and
    read the stored type. Falls back to a light heuristic only if the chunk was
    never logged."""
    cid = chunk_id
    if not cid and chunk_text:
        cid = hashlib.sha1(chunk_text.strip().encode("utf-8")).hexdigest()[:16]
    if cid:
        try:
            with _db_lock:
                conn = _get_db()
                row = conn.execute(
                    "SELECT sentence_type FROM chunks WHERE id=?", (cid,)
                ).fetchone()
                conn.close()
            if row and row[0]:
                return row[0]
        except Exception:
            pass
    return _guess_sentence_type(chunk_text)


def _profile_keys_from_parts(stype, band, length, punct, lexis, is_math):
    """Rebuild chunk_profile()'s key hierarchy from stored dimensions.

    Kept next to chunk_profile so the two cannot drift: this is the same list,
    constructed from values that were computed once at synthesis rather than
    recomputed from a different string later."""
    base = [
        f"{stype}|{band}|{length}|{punct}|{lexis}",
        f"{stype}|{band}|{length}|{punct}",
        f"{stype}|{band}|{punct}",
        f"{stype}|{band}",
        f"{stype}",
    ]
    return [f"math|{k}" for k in base[:3]] + ["math"] + base if is_math else base


def lookup_chunk_profile(chunk_id, chunk_text):
    """The fingerprint a chunk was actually synthesised under.

    Reports arrive carrying the display text, which is the human-readable version
    shown in the dashboard, but the parameters were chosen from the fingerprint
    of the spoken text. Recomputing the profile from the display text, which is
    what every report path used to do, therefore adjusted a different bucket from
    the one synthesis reads back, so a correction could be applied perfectly and
    still change nothing you could hear.

    log_chunk stores the real profile so I read it back, and I only fall back to
    computing it from the text when the chunk was never logged."""
    cid = chunk_id
    if not cid and chunk_text:
        cid = hashlib.sha1(chunk_text.strip().encode("utf-8")).hexdigest()[:16]
    if cid:
        try:
            with _db_lock:
                conn = _get_db()
                row = conn.execute(
                    "SELECT sentence_type, profile, has_math FROM chunks WHERE id=?",
                    (cid,)).fetchone()
                conn.close()
            if row and row[1] and row[1].count("|") == 3:
                stype = (row[0] or "sentence").lower()
                band, length, punct, lexis = row[1].split("|")
                return {
                    "sentence_type": stype, "band": band, "length": length,
                    "punct": punct, "lexis": lexis,
                    "keys": _profile_keys_from_parts(stype, band, length, punct,
                                                     lexis, bool(row[2])),
                    "from_stored": True,
                }
        except Exception as e:
            print(f"[LEARNER] stored profile lookup failed ({e}); recomputing")
    prof = chunk_profile(chunk_text, _lookup_chunk_type(chunk_id, chunk_text))
    prof["from_stored"] = False
    return prof


def get_learned_summary():
    """Everything the model has learned that affects synthesis, for the tuning
    panel: per-band rate modifiers and per-type temperatures."""
    store = _load_good_settings()
    bands = {}
    for b in ("simple", "normal", "dense"):
        bands[b] = {"label": BAND_LABEL[b], "rate_mod": get_rate_modifier(b)}
    types = {}
    _pfx = "" if _ACTIVE_VOICE == "default" else f"v:{_ACTIVE_VOICE}:"
    for k, v in store.items():
        if not isinstance(v, dict):
            continue
        # Only this voice's entries; strip the namespace for display. Default
        # must also EXCLUDE other voices' prefixed keys.
        if _pfx:
            if not k.startswith(_pfx):
                continue
            k = k[len(_pfx):]
        elif k.startswith("v:"):
            continue
        if k.startswith("band:") or k.startswith("_"):
            continue
        if v.get("temperature") is not None:
            types[k] = {"temperature": v["temperature"],
                        "confirm_count": v.get("confirm_count", 0),
                        "solid_count": v.get("solid_count", 0)}
    return {"bands": bands, "types": types}


def cleanup_auto_flag_rules():
    """Delete every AUTO-source FLAG rule.

    FLAG doesn't mutate anything, so these never improved the audio, they just
    cluttered the dashboard with the "FLAG THIS, 118 hits" noise. This lives here
    rather than in the route so the rule cache gets invalidated along with the
    write, which is exactly the kind of thing that gets forgotten when a route
    reaches into another module's database internals."""
    with _db_lock:
        conn = _get_db()
        n = conn.execute(
            "DELETE FROM rules WHERE source='AUTO' AND rule_type='FLAG'").rowcount
        conn.commit()
        conn.close()
    invalidate_rule_cache()
    print(f"[LEARNER] Cleaned up {n} noisy AUTO FLAG rules")
    return {"ok": True, "deleted": n}


def purge_flag_blacklists():
    """Deactivate the BLACKLIST rules created by the auto path, now fixed, that
    blacklisted WHISPER_SKIP words, meaning words from the original text. Those
    rules actively strip real words out of synthesis. I'm conservative about it:
    the rules are deactivated rather than deleted, and I return the patterns I
    touched so any that were genuinely intended can be re-enabled."""
    with _db_lock:
        conn = _get_db()
        rows = conn.execute("""
            SELECT id, pattern FROM rules
            WHERE rule_type='BLACKLIST' AND active=1
              AND action='strip' AND (value IS NULL OR value='')
              AND source IN ('AUTO', 'REPORT')
        """).fetchall()
        for rid, _ in rows:
            conn.execute("UPDATE rules SET active=0 WHERE id=?", (rid,))
        conn.commit()
        conn.close()
    invalidate_rule_cache()
    patterns = [r[1] for r in rows]
    if patterns:
        print(f"[LEARNER] purged {len(patterns)} flag-derived blacklists: {patterns[:10]}")
        log_history("BLACKLIST", f"Purged {len(patterns)} auto blacklists that were stripping real words", "auto")
    return {"ok": True, "purged": len(patterns), "patterns": patterns}


def maintenance_compact(days=90):
    """Prune database rows older than the retention window and reclaim space.

    It deletes applied reports and history entries older than `days`, and keeps
    every rule, every baseline, all the chunks, which are content-addressed so
    they dedupe on re-reads, and any unapplied reports, since those might still
    accumulate into an action. It ends with a VACUUM so the file really shrinks."""
    cutoff = time.time() - days * 86400
    deleted = {"reports": 0, "history": 0}
    with _db_lock:
        conn = _get_db()
        cur = conn.execute(
            "DELETE FROM reports WHERE ts < ? AND applied = 1", (cutoff,))
        deleted["reports"] = cur.rowcount
        cur = conn.execute(
            "DELETE FROM history WHERE ts < ?", (cutoff,))
        deleted["history"] = cur.rowcount
        conn.commit()
        conn.execute("VACUUM")
        conn.close()
    print(f"[LEARNER] compact: removed {deleted['reports']} old reports, "
          f"{deleted['history']} history rows (kept last {days} days)")
    return {"ok": True, "days": days, **deleted}


def reset_learned_rates():
    """Clear all rate learning: band modifiers back to 1.0 and legacy per-type
    speaking_rate values removed (some stored corrupt words-per-second figures).
    Temperatures and pronunciation learning are untouched."""
    removed = 0
    with _good_settings_lock:
        store = dict(_load_good_settings())
        for k in list(store.keys()):
            if k.startswith("band:"):
                del store[k]; removed += 1
            elif isinstance(store[k], dict) and "speaking_rate" in store[k]:
                store[k] = {kk: vv for kk, vv in store[k].items() if kk != "speaking_rate"}
                removed += 1
        global _good_settings_cache
        _good_settings_cache = store
        try:
            with open(_good_settings_path(), "w") as f:
                json.dump(store, f, indent=2)
        except Exception as e:
            print(f"[LEARNER] rate reset save error: {e}")
    log_history("RATE", "All learned speaking rates reset to default", "user")
    return {"ok": True, "cleared": removed}


def get_preferred_param(sentence_type, param, current_value, lo, hi, integer=False):
    """Nudge any per-type learned parameter toward its confirmed-good value for
    this sentence_type, or return current_value unchanged if none is stored.
    Generalises preferred-value learning to rep_penalty / top_k so dense and
    simple chunks can carry their own learned synthesis profile. Gentle 25%
    nudge keeps live user changes dominant; clamped to [lo, hi]."""
    store = _load_good_settings()
    # _vk() namespaces the key by active voice. This lookup was the one place
    # that read the bare key, so a second voice's learned rep_penalty / top_k
    # came from whatever the default voice had learned, which is the one thing
    # voice isolation is supposed to prevent.
    pref  = store.get(_vk((sentence_type or "sentence").lower()))
    if not pref or pref.get(param) is None:
        return current_value
    target = pref[param]
    nudged = current_value + 0.25 * (target - current_value)
    nudged = max(lo, min(hi, nudged))
    return int(round(nudged)) if integer else round(nudged, 3)


def add_rule_manual(rule_type, pattern, action, value):
    """Public wrapper for the /report/rules POST endpoint.

    Adds a user-authored rule with source='MANUAL' (these rank above AUTO
    rules). Returns a result dict the API serialises straight to JSON.
    """
    rule_type = (rule_type or "FLAG").strip().upper()
    pattern   = (pattern or "").strip()
    if not pattern:
        return {"ok": False, "error": "pattern is required"}
    valid = {"FLAG", "PRONUNCIATION", "BLACKLIST", "SPLIT", "MONITOR", "SUPPRESS", "MATH"}
    if rule_type not in valid:
        return {"ok": False, "error": f"unknown rule_type: {rule_type}"}
    _add_rule(rule_type, pattern, (action or "").strip(),
              (value or "").strip(), source="MANUAL")
    return {"ok": True, "rule_type": rule_type, "pattern": pattern}


# Failures whose cause really is a token outside the XTTS vocabulary. Anything
# else (out of memory, a device-side assert from a driver hiccup, a reshape on
# an over-long input) has nothing to do with the words in the chunk.
_VOCAB_CRASH_MARKERS = (
    "srcindex", "indexing.cu", "index out of range", "device-side assert",
    "embedding", "out of bounds",
)


def _auto_detect_crash_pattern(text, error_type, message=""):
    """
    Detect and blacklist the token patterns that genuinely crash XTTS.

    Blacklisting permanently strips a token from all future speech, so the bar
    for doing it automatically has to be high. This only fires when the error
    looks like a vocabulary or index crash, which is the failure mode these
    patterns describe. It used to run after every error, so a transient CUDA
    out-of-memory, which the very next retry recovered from anyway, would
    blacklist every hyphenated or colon-separated token in the chunk and quietly
    delete real words from every later reading.
    """
    blob = f"{error_type} {message}".lower()
    if not any(marker in blob for marker in _VOCAB_CRASH_MARKERS):
        print(f"[LEARNER] {error_type} is not a vocabulary crash — "
              f"no tokens blacklisted")
        return

    # Mixed alphanumeric tokens (Riff24Khz16BitMonoPcm pattern)
    for tok in re.findall(r'[A-Za-z]+\d+[A-Za-z0-9]{3,}', text):
        _add_rule("BLACKLIST", tok, "strip", "", source="AUTO")
        print(f"[LEARNER] Auto-blacklisted crash token: {tok}")

    # Colon identifiers (en-US-Brian:DragonHDLatestNeural pattern)
    for tok in re.findall(r'[A-Za-z0-9._-]+:[A-Za-z0-9._-]+', text):
        _add_rule("BLACKLIST", tok, "strip", "", source="AUTO")
        print(f"[LEARNER] Auto-blacklisted colon token: {tok}")


# --- Active-rule cache ---
# apply_learned_rules() runs on every chunk and used to start by re-reading the
# whole rules table from SQLite, so a connect, query and close for every chunk,
# on a table that only changes when someone reports something. I cache the
# active set
# and explicitly invalidated on every write path (_add_rule, delete_rule, the
# bulk purges), so the cache can never serve a stale rule set.
_rules_cache      = None
_rules_cache_lock = threading.Lock()


def invalidate_rule_cache():
    """Drop the cached active-rule list. Called after any change to `rules`."""
    global _rules_cache
    with _rules_cache_lock:
        _rules_cache = None


def _active_rules():
    """Cached list of active rules (the hot path's view of the table)."""
    global _rules_cache
    with _rules_cache_lock:
        if _rules_cache is not None:
            return _rules_cache
    fresh = get_rules(active_only=True)
    with _rules_cache_lock:
        _rules_cache = fresh
    return fresh


def get_rules(active_only=True) -> list:
    """Return all rules.

    Sort order: hits DESC, then newest first.
    Rules that actually fire on synthesis float to the top of the dashboard,
    so the user can see at a glance which ones are earning their keep.

    Passing active_only=False is used by the rules tab to show the full audit
    trail (including deactivated rules), which callers filter client-side.
    """
    with _db_lock:
        conn = _get_db()
        if active_only:
            rows = conn.execute(
                "SELECT * FROM rules WHERE active=1 ORDER BY hits DESC, ts DESC"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM rules ORDER BY hits DESC, ts DESC"
            ).fetchall()
        conn.close()
    return [dict(r) for r in rows]


def get_reports(limit=50):
    with _db_lock:
        conn = _get_db()
        rows = conn.execute(
            "SELECT * FROM reports ORDER BY ts DESC LIMIT ?", (limit,)
        ).fetchall()
        conn.close()
    return [dict(r) for r in rows]


def mark_session_solid(since_ts=None, played=None):
    """Digest the chunks that were actually heard this session.

    The input I want is `played`, a list of the exact chunks that finished
    playing, since the extension only includes fully-played ones and never the
    3 or 4 prefetched ahead. Each resolves to its content-hash id and, if it has
    no verdict yet, gets marked 'solid'. That works for a completed read and for
    an early cancel alike, since only what was heard gets digested.

    `since_ts` is an older fallback using a timestamp window, kept for
    compatibility.

    Learning uses three classes:
      perfect, from a thumbs up   strong reinforcement via record_good_settings
      reported issues             a targeted adjustment away, via the report
      solid                       gentle reinforcement here, a small EMA pull
                                  toward the settings that produced acceptable
                                  output, which never outweighs an explicit
                                  verdict."""
    marked = 0
    stype_counts = {}
    # Per sentence type, the temperatures those solid chunks were ACTUALLY
    # synthesised with. Reinforcement below moves toward the mean of these
    # rather than toward whatever the live slider currently reads.
    stype_temps = {}

    def _digest(cid, stype, used_temp):
        nonlocal marked
        key = (stype or "sentence").lower()
        stype_counts[key] = stype_counts.get(key, 0) + 1
        if used_temp is not None:
            stype_temps.setdefault(key, []).append(used_temp)
        marked += 1

    with _db_lock:
        conn = _get_db()
        if played:
            # `played` is a list of real server chunk ids (the extension
            # captured each from the X-Chunk-Id header). Mark solid if the chunk
            # currently has no verdict.
            for cid in played:
                row = conn.execute(
                    "SELECT sentence_type, user_feedback, used_temperature "
                    "FROM chunks WHERE id=?", (cid,)).fetchone()
                if not row:
                    continue
                stype, fb, used_temp = row[0], row[1], row[2]
                if fb:   # already perfect, negative or solid, so leave it
                    continue
                conn.execute("UPDATE chunks SET user_feedback='solid' WHERE id=?", (cid,))
                _digest(cid, stype, used_temp)
        elif since_ts is not None:
            rows = conn.execute("""
                SELECT id, sentence_type, used_temperature FROM chunks
                WHERE ts >= ? AND success = 1 AND skipped = 0
                  AND (user_feedback IS NULL OR user_feedback = '')
            """, (since_ts,)).fetchall()
            for cid, stype, used_temp in rows:
                conn.execute("UPDATE chunks SET user_feedback='solid' WHERE id=?", (cid,))
                _digest(cid, stype, used_temp)
        conn.commit()
        conn.close()

    # Gentle reinforcement: pull each affected type's stored temperature a
    # little toward the temperature those chunks were ACTUALLY produced with.
    #
    # This used to pull toward _LIVE_SETTINGS["temperature"], which made the
    # whole mechanism circular. The live value is not evidence of anything: it
    # is the current slider position, and until this change the Whisper worker
    # was writing to it automatically. So an automatic global nudge became the
    # target that every per-type learned temperature was dragged toward at the
    # end of every session, so the model reinforced its own drift and called it
    # user approval. Averaging what the solid chunks really used keeps the pull
    # anchored to audio the user actually accepted.
    deltas = []   # (stype, before, after) for the legible history event
    if marked:
        alpha = 0.08
        fallback_t = (_live_settings_ref or {}).get("temperature")
        with _good_settings_lock:
            store = dict(_load_good_settings())
            for stype in stype_counts:
                temps = stype_temps.get(stype) or []
                # Mean of what actually produced these chunks; the live value is
                # only a fallback for rows predating per-chunk parameters.
                target = (sum(temps) / len(temps)) if temps else fallback_t
                if target is None:
                    continue
                entry = dict(store.get(_vk(stype)) or {})
                prev = entry.get("temperature")
                new_t = round(
                    target if prev is None else prev * (1 - alpha) + target * alpha, 3)
                entry["temperature"] = new_t
                entry["solid_count"] = entry.get("solid_count", 0) + stype_counts[stype]
                entry["updated"] = datetime.now().isoformat()
                store[_vk(stype)] = entry
                deltas.append((stype, prev, new_t))
            global _good_settings_cache
            _good_settings_cache = store
            try:
                with open(_good_settings_path(), "w") as f:
                    json.dump(store, f, indent=2)
            except Exception as e:
                print(f"[LEARNER] solid reinforce save error: {e}")
        print(f"[LEARNER] session closed: {marked} chunks marked solid "
              f"({', '.join(f'{k}:{v}' for k, v in stype_counts.items())})")
        if marked:
            bump_counter("solid_total", marked)   # durable lifetime total
        # Legible history (#2): show the actual temperature movement per type so
        # the change can be tracked and cross-checked against reported/perfect.
        if deltas:
            moves = ", ".join(
                f"{s} {('—' if b is None else f'{b:.3f}')}→{a:.3f}" for s, b, a in deltas)
            log_history("CONFIRM",
                        f"{marked} chunks solid · temp {moves}", "auto")
        else:
            log_history("CONFIRM", f"{marked} chunks solid", "auto")
    return {"ok": True, "solid": marked}


def get_stats():
    """Summary statistics for the report dashboard."""
    with _db_lock:
        conn = _get_db()
        total      = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        success    = conn.execute("SELECT COUNT(*) FROM chunks WHERE success=1").fetchone()[0]
        errors     = conn.execute("SELECT COUNT(*) FROM chunks WHERE success=0").fetchone()[0]
        skipped    = conn.execute("SELECT COUNT(*) FROM chunks WHERE skipped=1").fetchone()[0]
        avg_qual   = conn.execute("SELECT AVG(quality_score) FROM chunks WHERE quality_score IS NOT NULL").fetchone()[0]
        reports_n  = conn.execute("SELECT COUNT(*) FROM reports").fetchone()[0]
        rules_n    = conn.execute("SELECT COUNT(*) FROM rules WHERE active=1").fetchone()[0]
        blacklist_n= conn.execute("SELECT COUNT(*) FROM rules WHERE rule_type='BLACKLIST' AND active=1").fetchone()[0]
        recent     = conn.execute("""
            SELECT text, quality_score, quality_flags
            FROM chunks
            WHERE quality_score IS NOT NULL
            ORDER BY ts DESC LIMIT 5
        """).fetchall()
        conn.close()

    baseline = _get_baseline()

    with _db_lock:
        conn = _get_db()
        high_q    = conn.execute("SELECT COUNT(*) FROM chunks WHERE quality_score >= 0.85").fetchone()[0]
        low_q     = conn.execute("SELECT COUNT(*) FROM chunks WHERE quality_score < 0.6 AND quality_score IS NOT NULL").fetchone()[0]
        flagged   = conn.execute("SELECT COUNT(*) FROM chunks WHERE quality_flags IS NOT NULL AND quality_flags != ''").fetchone()[0]
        analysed  = conn.execute("SELECT COUNT(*) FROM chunks WHERE quality_score IS NOT NULL").fetchone()[0]
        # The sentence-type distribution, which makes labelling quality
        # observable. A healthy spread confirms the classifier is assigning
        # meaningful types, whereas
        # everything collapsing to 'sentence'/'unknown' signals a labelling gap.
        type_rows = conn.execute("""
            SELECT sentence_type, COUNT(*) AS n FROM chunks
            WHERE sentence_type IS NOT NULL AND sentence_type != ''
            GROUP BY sentence_type ORDER BY n DESC
        """).fetchall()
        # User verdicts: thumbs up / down on chunks, and what the negative
        # reports actually were, grouped by issue type.
        fb_pos_live   = conn.execute("SELECT COUNT(*) FROM chunks WHERE user_feedback='positive'").fetchone()[0]
        fb_neg_live   = conn.execute("SELECT COUNT(*) FROM chunks WHERE user_feedback='negative'").fetchone()[0]
        fb_solid_live = conn.execute("SELECT COUNT(*) FROM chunks WHERE user_feedback='solid'").fetchone()[0]
        issue_rows = conn.execute("""
            SELECT issue, COUNT(*) AS n,
                   SUM(CASE WHEN applied=1 THEN 1 ELSE 0 END) AS applied_n
            FROM reports
            WHERE issue IS NOT NULL AND issue != '' AND issue != 'IGNORE'
            GROUP BY issue ORDER BY n DESC
        """).fetchall()
        conn.close()

    by_issue = [
        {"issue": r[0], "count": r[1], "applied": r[2] or 0}
        for r in issue_rows
    ]
    type_dist = [{"type": r[0], "count": r[1]} for r in type_rows]

    # Durable lifetime feedback totals (survive console clears), so the Reports
    # page and the Stats history panel always agree. Live session counts are
    # also exposed (suffixed _live) for any view that needs post-clear counts.
    fb_pos   = get_counter("perfect_total")
    fb_neg   = get_counter("negative_total")
    fb_solid = get_counter("solid_total")

    return {
        "chunks": {
            "total":    total,
            "success":  success,
            "errors":   errors,
            "skipped":  skipped,
            "analysed": analysed,
            "high_quality": high_q,
            "low_quality":  low_q,
            "flagged":      flagged,
            "avg_quality": round(avg_qual, 3) if avg_qual else None,
        },
        "feedback": {
            "positive": fb_pos,
            "negative": fb_neg,
            "solid":    fb_solid,
            "positive_live": fb_pos_live,
            "negative_live": fb_neg_live,
            "solid_live":    fb_solid_live,
            "by_issue": by_issue,
        },
        "reports":   reports_n,
        "type_distribution": type_dist,
        "rules":     rules_n,
        "blacklist": blacklist_n,
        "baseline":  baseline,
        "recent_quality": [dict(r) for r in recent],
        "whisper_available": _WHISPER_OK,
        "librosa_available": _LIBROSA_OK,
    }


def delete_rule(rule_id):
    with _db_lock:
        conn = _get_db()
        conn.execute("UPDATE rules SET active=0 WHERE id=?", (rule_id,))
        conn.commit()
        conn.close()
    invalidate_rule_cache()


# Sentinel returned by apply_learned_rules when a SUPPRESS rule matches the
# whole chunk, and server.py spots it and emits silence rather than synthesising.
SUPPRESS_SENTINEL = "\x00SUPPRESS\x00"

# A MONITOR rule that accumulates this many hits is auto-escalated to a SPLIT
# rule: if the user keeps flagging chunks containing the same phrase as
# "minor issue", the most likely fix is that the phrase needs its own chunk.
_MONITOR_ESCALATE_HITS = 3


def apply_learned_rules(text):
    """
    Apply all the learned rules to the text before synthesis.

    server.py's clean_text calls this. It returns the modified text, or
    SUPPRESS_SENTINEL if a SUPPRESS rule matches the whole chunk.

    The rule types it handles are:
      BLACKLIST      strip the matching tokens out of the text
      PRONUNCIATION  swap the token for its spoken form
      PUNCT          repunctuate after an anchor word, from the user's own
                     punctuation corrections. These are actually applied now,
                     they used to be ignored.
      SPLIT          insert a sentence break before the phrase so the chunker
                     splits there
      MONITOR        track how often a phrase flagged as a "minor issue" comes
                     back, and escalate it to a SPLIT after enough hits
      SUPPRESS       the user asked to skip this content, so return the sentinel
                     and the server emits silence instead of synthesising
      FLAG           mutates nothing, it just logs a hit so the dashboard can
                     show it

    Every successful match bumps the rule's hits counter, so the dashboard shows
    which rules are firing and MONITOR rules know when to escalate.
    """
    rules = _active_rules()     # cached; invalidated on any rule write
    fired_ids = []  # ids of rules that actually changed or matched text

    for rule in rules:
        rtype = rule["rule_type"]
        pat   = rule["pattern"]
        if not pat:
            continue

        if rtype == "BLACKLIST":
            regex = re.escape(pat)
            new_text, n = re.subn(regex, "", text, flags=re.IGNORECASE)
            if n > 0:
                text = new_text
                fired_ids.append(rule["id"])

        elif rtype == "PRONUNCIATION":
            spoken = (rule.get("value") or "").strip()
            # Skip no-op rules (pattern == value, case-insensitive)
            if not spoken or spoken.lower() == pat.lower():
                continue
            regex = r"\b" + re.escape(pat) + r"\b"
            new_text, n = re.subn(regex, spoken, text, flags=re.IGNORECASE)
            if n > 0:
                text = new_text
                fired_ids.append(rule["id"])

        elif rtype == "PUNCT":
            # Repunctuate after an anchor word. value holds the replacement text
            # that should follow the anchor (stored by learn_punctuation_correction).
            repl = (rule.get("value") or "").strip()
            if not repl:
                continue
            regex = r"\b" + re.escape(pat) + r"\b"
            # Insert the corrected fragment right after the first anchor match.
            new_text, n = re.subn(regex, pat + " " + repl, text,
                                  flags=re.IGNORECASE, count=1)
            if n > 0:
                # Collapse any doubled spaces/punctuation the insert may create.
                new_text = re.sub(r"\s{2,}", " ", new_text)
                new_text = re.sub(r"\s+([,.;:!?])", r"\1", new_text)
                if new_text != text:
                    text = new_text
                    fired_ids.append(rule["id"])

        elif rtype == "SPLIT":
            regex = r"(?<!\. )\b" + re.escape(pat) + r"\b"
            new_text, n = re.subn(regex, ". " + pat, text, flags=re.IGNORECASE, count=1)
            if n > 0:
                text = new_text
                fired_ids.append(rule["id"])

        elif rtype == "MONITOR":
            # Track recurrence. When the same flagged phrase keeps appearing the
            # rule's hit count grows; past a threshold we auto-add a SPLIT so the
            # phrase gets isolated into its own chunk on future reads.
            regex = r"\b" + re.escape(pat) + r"\b"
            if re.search(regex, text, flags=re.IGNORECASE):
                fired_ids.append(rule["id"])
                if (rule.get("hits") or 0) + 1 >= _MONITOR_ESCALATE_HITS:
                    _add_rule("SPLIT", pat, "auto_escalated_from_monitor", "",
                              source="AUTO")
                    print(f"[LEARNER] ⤴ MONITOR '{pat}' escalated to SPLIT "
                          f"after {_MONITOR_ESCALATE_HITS} hits")

        elif rtype == "SUPPRESS":
            # User explicitly skipped this content. If the suppressed phrase is
            # essentially the whole chunk, skip synthesis entirely.
            regex = r"\b" + re.escape(pat) + r"\b"
            if re.search(regex, text, flags=re.IGNORECASE):
                fired_ids.append(rule["id"])
                stripped_pat   = re.sub(r"\W+", "", pat).lower()
                stripped_text  = re.sub(r"\W+", "", text).lower()
                # Whole-chunk match (allowing minor punctuation diff) → suppress.
                if stripped_pat and stripped_pat == stripped_text:
                    _increment_rule_hits(fired_ids)
                    return SUPPRESS_SENTINEL

        elif rtype == "FLAG":
            regex = r"\b" + re.escape(pat) + r"\b"
            if re.search(regex, text, flags=re.IGNORECASE):
                fired_ids.append(rule["id"])

    if fired_ids:
        _increment_rule_hits(fired_ids)

    return text.strip()


def _increment_rule_hits(rule_ids):
    """Increment the hits counter for every rule id in the list."""
    if not rule_ids:
        return
    try:
        with _db_lock:
            conn = _get_db()
            conn.executemany(
                "UPDATE rules SET hits = hits + 1 WHERE id = ?",
                [(rid,) for rid in rule_ids]
            )
            conn.commit()
            conn.close()
    except Exception as e:
        print(f"[LEARNER] hits update error: {e}")
    # Keep the cached copies in step rather than invalidating the whole cache on
    # every chunk. MONITOR escalation reads `hits` off the cached rule, so a
    # frozen count there would stop it ever reaching its threshold.
    hit = set(rule_ids)
    with _rules_cache_lock:
        if _rules_cache:
            for r in _rules_cache:
                if r["id"] in hit:
                    r["hits"] = (r.get("hits") or 0) + 1


# Reference to the server's _LIVE_SETTINGS.
#
# It is read-only. Nothing in this module is allowed to assign into it.
#
# That dict holds the user's own settings, meaning what the Speech Tuning sliders
# show and what they last chose. I write what I learn to good_settings.json
# (per profile and per sentence type), which synthesis layers ON TOP of the live
# values. Keeping the two separate is what makes the user's choice stable and
# the learning inspectable and revertible.
#
# This module used to assign into it from the Whisper analysis worker, which
# meant an automatic adjustment silently moved a control the user owned, applied
# globally instead of to the failing profile, and then got laundered into the
# permanent per-type values by mark_session_solid. It is now used only as a
# default of last resort, for chunks logged before per-chunk parameters existed.
_live_settings_ref = None

def register_live_settings(settings_dict):
    """server.py calls this on startup to share the live settings dict, which is
    read-only in this module. See the note above."""
    global _live_settings_ref
    _live_settings_ref = settings_dict

print("[LEARNER] Quality monitor initialised — DB:", DB_PATH)
print(f"[LEARNER] Whisper: {'available' if _WHISPER_OK else 'not installed (pip install openai-whisper)'}")
print(f"[LEARNER] librosa: {'available' if _LIBROSA_OK else 'not installed (pip install librosa)'}")

def get_chunk_feed(limit=20):
    """Return recent chunks with quality metrics for the dashboard feed.
    Each chunk carries a global chronological seq (1 = oldest, total = newest)
    so the UI can label "chunk 24/108" consistently everywhere."""
    with _db_lock:
        conn = _get_db()
        total = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        rows = conn.execute("""
            SELECT id as chunk_id, text as chunk_text, sentence_type, silence_ms,
                   char_count, success, skipped, quality_score, quality_flags,
                   whisper_accuracy, pitch_variance, energy_tail, voice_consistency,
                   has_math, primary_facet, facet_count, ts
            FROM chunks ORDER BY ts DESC LIMIT ?
        """, (limit,)).fetchall()
        conn.close()
    out = []
    for i, r in enumerate(rows):
        d = dict(r)
        d["seq"]   = total - i   # newest row (i=0) = total
        d["total"] = total
        out.append(d)
    return out

def get_chunks_filtered(limit=100, offset=0, filter_type="all"):
    """Paginated, filtered chunk list for the browser tab."""
    where = {
        "all":     "1=1",
        "high":    "quality_score >= 0.85",
        "low":     "quality_score < 0.6 AND quality_score IS NOT NULL",
        "flagged": "quality_flags IS NOT NULL AND quality_flags != ''",
        "error":   "success = 0",
        "skipped": "skipped = 1",
    }.get(filter_type, "1=1")

    with _db_lock:
        conn = _get_db()
        total = conn.execute(f"SELECT COUNT(*) FROM chunks WHERE {where}").fetchone()[0]
        grand_total = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        # Global chronological rank by ts: how many chunks are <= this one's ts.
        # Gives a stable creation-order number independent of the active filter.
        #
        # The rank used to be a separate COUNT(*) query for every row, so 100
        # extra full scans of the chunks table to render one page. It's now
        # a single correlated subquery evaluated alongside the page itself.
        rows = conn.execute(f"""
            SELECT id as chunk_id, text as chunk_text, sentence_type,
                   char_count, success, skipped, quality_score,
                   quality_flags, whisper_accuracy, pitch_variance,
                   voice_consistency, energy_tail, ts, user_feedback, profile,
                   error_type, error_stage, retried, primary_facet,
                   (SELECT COUNT(*) FROM chunks c2 WHERE c2.ts <= chunks.ts) AS seq
            FROM chunks WHERE {where}
            ORDER BY ts DESC LIMIT ? OFFSET ?
        """, (limit, offset)).fetchall()
        ranked = []
        for r in rows:
            d = dict(r)
            d["total"] = grand_total      # 1 = oldest ever, grand_total = newest
            ranked.append(d)
        conn.close()

    return {"total": total, "offset": offset, "chunks": ranked}

def reset_stats():
    """Clear only the per-session chunk analysis, which resets the dashboard's
    live stats and feed to zero.

    It deliberately preserves the learned rules, the reports, the permanent
    learning history and the voice baseline. "Clear" is a per-session action and
    must never erase what the model has learned or your confirmed voice profile.
    The baseline used to get wiped here, which reset voice learning on every
    clear, and that was wrong."""
    with _db_lock:
        conn = _get_db()
        conn.execute("DELETE FROM chunks")
        conn.commit()
        conn.close()
    print("[LEARNER] Per-session chunk history cleared "
          "(rules, history & voice baseline preserved)")
