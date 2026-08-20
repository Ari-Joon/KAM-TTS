# -*- coding: utf-8 -*-
"""
server.py — KAM TTS synthesis server (Flask + XTTS-v2)

This is the local engine behind the Chrome extension. It binds to 127.0.0.1 only
and every request carries a per-install token, which lives in kam_token.txt and
is handed out by the /token endpoint.

What happens to one chunk of page text
--------------------------------------
    raw text
      → apply_math_rules        equation readings I've been taught by reports
      → expand_math_symbols     ∑ ≤ ∂ X_t x² turned into spoken words
      → pronunciation, code and punctuation normalisation
      → analyse_prosody         sentence type, pause length, POS/clause signals
      → resolve learned params  per chunk fingerprint and per active voice
      → XTTS inference          under _inference_lock, since it isn't thread safe
      → Whisper listen-back     scores the result and feeds the learning loop
      → log_chunk               text, params, facets, quality → tts_quality.db

The main pieces
---------------
  Voice profiles   Folders of reference clips under voices/<id>/, with latents
                   cached per voice so switching swaps them live. Learning is
                   isolated per voice (see learner.py) and only pronunciations
                   are shared between them.
  Standby          I evict the model after an idle timeout to free VRAM, then
                   re-warm it on the next request since the kernels die with it.
  Hardware adapt   hardware_profile.json, either measured or auto-detected on
                   first boot, tunes standby, prefetch depth and Whisper
                   analysis to the machine. An explicit user setting always wins.
  Reports          Corrections become rules, and equation corrections become
                   MATH rules that run ahead of the generic expander.

Threading notes
---------------
  * XTTS isn't thread safe, so _inference_lock serialises all inference.
  * cudnn.benchmark stays off, because autotuning re-benchmarks for every input
    shape and stalls variable-length speech synthesis.
  * Startup only runs from __main__ with use_reloader=False, otherwise
    Werkzeug's reloader imports the module twice and loads the model twice.
"""

# The very first thing I do is write a breadcrumb to disk, before touching stdout
# or importing anything heavy. When the power-button host launches this, stdout
# is a pipe, and some console operations can block a GUI-spawned child before any
# output appears at all. Writing a plain file proves the process started and
# can't block on its own.
import os as _os_boot0
import sys as _sys_boot0

# Console encoding, before anything prints. The native-messaging host sets
# PYTHONIOENCODING=utf-8 for the child it launches, but running `python server.py`
# by hand on Windows inherits the legacy console codepage, and then the first
# banner with a box-drawing character in it raises UnicodeEncodeError and kills
# the server before it even starts.
try:
    _sys_boot0.stdout.reconfigure(encoding="utf-8", errors="replace")
    _sys_boot0.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

_SERVER_DIR0 = _os_boot0.path.dirname(_os_boot0.path.abspath(__file__))
_READY_SENTINEL = _os_boot0.path.join(_SERVER_DIR0, ".kam_ready")
_STAGE_FILE = _os_boot0.path.join(_SERVER_DIR0, ".kam_stage")
def _stage(msg):
    try:
        with open(_STAGE_FILE, "w") as _sf:
            _sf.write(msg)
    except Exception:
        pass
for _f in (_READY_SENTINEL, _STAGE_FILE):
    try:
        if _os_boot0.path.exists(_f):
            _os_boot0.remove(_f)
    except Exception:
        pass
_stage("process-started")

# Single-instance lock, done atomically so it can't race. Just checking the port
# doesn't work because during the 14 second model load the port isn't bound yet,
# so two launches close together both pass the check and both load, which is the
# duplicate server I kept seeing. Instead I grab an exclusive lock file with
# O_CREAT|O_EXCL, since only one process can create it and any other exits
# straight away having loaded nothing. The lock gets removed on exit.
import os as _os_lock
import sys as _sys_lock
import atexit as _atexit_lock
_LOCK_FILE = _os_lock.path.join(_SERVER_DIR0, ".kam_lock")

def _pid_alive(pid):
    """True if a process with this PID is running, on Windows or POSIX."""
    if pid <= 0:
        return False
    if _os_lock.name == "nt":
        try:
            import ctypes as _ct
            _k32 = getattr(_ct, "windll").kernel32   # windll exists only on Windows
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            h = _k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not h:
                return False
            code = _ct.c_ulong()
            ok = _k32.GetExitCodeProcess(h, _ct.byref(code))
            _k32.CloseHandle(h)
            # 259 == STILL_ACTIVE
            return bool(ok) and code.value == 259
        except Exception:
            return True   # if I can't tell, assume alive so I don't double-launch
    else:
        try:
            _os_lock.kill(pid, 0)
            return True
        except OSError:
            return False

def _acquire_single_instance():
    """Atomic single-instance lock. I only treat the lock as stale if the PID that
    wrote it is no longer alive, rather than going by whether port 5050 is bound.
    The old port-based check raced, since during the 14 second model load the port
    isn't bound yet, so a second launch decided the lock was stale, took it and
    loaded as well. Checking PID liveness can't race like that."""
    try:
        fd = _os_lock.open(_LOCK_FILE, _os_lock.O_CREAT | _os_lock.O_EXCL | _os_lock.O_WRONLY)
        _os_lock.write(fd, str(_os_lock.getpid()).encode())
        _os_lock.close(fd)
        return True
    except FileExistsError:
        # Read the holder PID; if that process is alive, we are a duplicate.
        holder = -1
        try:
            with open(_LOCK_FILE, "r") as _lf:
                holder = int((_lf.read() or "-1").strip() or "-1")
        except Exception:
            holder = -1
        if _pid_alive(holder):
            return False          # a live instance owns the lock → refuse
        # Holder is dead → reclaim the stale lock.
        try:
            _os_lock.remove(_LOCK_FILE)
            fd = _os_lock.open(_LOCK_FILE, _os_lock.O_CREAT | _os_lock.O_EXCL | _os_lock.O_WRONLY)
            _os_lock.write(fd, str(_os_lock.getpid()).encode())
            _os_lock.close(fd)
            return True
        except Exception:
            return False

if not _acquire_single_instance():
    _stage("duplicate-aborted")
    print("[BOOT] Another server instance is already starting/running — exiting duplicate.", flush=True)
    _sys_lock.exit(0)

def _release_lock():
    try:
        _os_lock.remove(_LOCK_FILE)
    except Exception:
        pass
_atexit_lock.register(_release_lock)

# The launcher's shell redirection handles output capture, so I don't touch
# sys.stdout here. The breadcrumb files are the signals that survive buffering.

_stage("flask-import")

# Print IMMEDIATELY, before the heavy imports below, so when launched by the
# power-button host the dashboard shows the process is alive.
print("[BOOT] server.py starting — importing Flask…", flush=True)

from flask import Flask, request, send_file, jsonify
import learner as _learner
import pos_prosody as _pos_prosody  # POS-informed prosody (NLP layer)
import device as _device            # cross-platform backend selection
import audio_quality as _aq         # reference-clip gate + output validation
import benchmark as _bench          # shared, streamable speed measurement
from flask_cors import CORS  # type: ignore
print("[BOOT] importing TTS + torch (this is the slow one)…", flush=True)
# Speed up the transformers import: skip the optional integrations and the
# slow advisory scans that walk the models directory at import time. These must
# be set before transformers or TTS is imported. They don't change behaviour,
# they just skip work I don't use, which cuts cold-start import time a lot.
import os as _os_boot
_os_boot.environ.setdefault("TRANSFORMERS_NO_ADVISORY_WARNINGS", "1")
_os_boot.environ.setdefault("TRANSFORMERS_VERBOSITY", "error")
_os_boot.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
_os_boot.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
_os_boot.environ.setdefault("DO_NOT_TRACK", "1")
from TTS.api import TTS  # type: ignore
import torch  # type: ignore
print("[BOOT] torch + TTS imported OK", flush=True)
_stage("torch-tts-imported")
import io
import os
import re
import json
import struct
import scipy.io.wavfile as wav  # type: ignore
import numpy as np  # type: ignore
from scipy import signal  # type: ignore

import hashlib, threading as _threading
import collections as _collections

# --- XTTS isn't thread safe, so only one inference at a time ---
_inference_lock = _threading.Lock()

# --- Standby / idle-eviction state ---
# The XTTS model holds several GB of VRAM whether or not it's synthesising.
# After a configurable idle period with no SYNTHESIS activity, we evict it and
# drop to STANDBY (server stays up, listening). The next synth request wakes it.
# States: "cold" (never loaded) | "ready" | "standby" | "waking".
_model_state       = "cold"
_last_synth_ts     = 0.0            # only real synthesis updates this, not health pings
_standby_lock      = _threading.Lock()
_IDLE_TIMEOUT_SEC  = 0              # 0 = always on (never idle); else seconds
# Allowed idle-timeout choices exposed to the UI (label → seconds).
_IDLE_CHOICES = {
    "5m": 300, "10m": 600, "15m": 900, "30m": 1800,
    "60m": 3600, "2h": 7200, "always": 0,
}

# --- Live inference settings, adjustable through the /settings endpoint ---
_LIVE_SETTINGS = {
    "temperature":        0.33,   # 0.1=robotic, 0.9=expressive
    "speed":              1.20,   # playback rate multiplier
    "repetition_penalty": 4.1,
    "top_k":              45,
    "top_p":              0.90,
}

# Immutable copy of the server's FACTORY default inference settings, so the
# popup's "Reset to original" button can always restore exactly what KAM shipped
# with, even after the user has saved a default of their own.
_FACTORY_SETTINGS = dict(_LIVE_SETTINGS)
_DEFAULT_SETTINGS = dict(_LIVE_SETTINGS)   # current default (may be user-saved)

# User-saved default settings persist here and override the factory baseline at
# startup, so a saved preference survives restarts.
_USER_DEFAULTS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                   "user_defaults.json")

def _load_user_defaults():
    """Apply a persisted user default (if any) as the live + default baseline."""
    global _DEFAULT_SETTINGS
    try:
        if os.path.exists(_USER_DEFAULTS_PATH):
            with open(_USER_DEFAULTS_PATH) as f:
                saved = json.load(f)
            clean = {k: saved[k] for k in _FACTORY_SETTINGS if k in saved}
            if clean:
                _DEFAULT_SETTINGS = {**_FACTORY_SETTINGS, **clean}
                _LIVE_SETTINGS.update(_DEFAULT_SETTINGS)
                print(f"[STARTUP] User default settings loaded: {clean}")
    except Exception as e:
        print(f"[STARTUP] user_defaults load skipped: {e}")

_load_user_defaults()


# --- Console log buffer, streamed to the player.html dashboard ---
_console_log    = _collections.deque(maxlen=500)
_console_lock   = _threading.Lock()
_console_cursor = 0

def _clog(line):
    global _console_cursor
    with _console_lock:
        _console_log.append(line)
        _console_cursor += 1

_real_print = print
def print(*args, **kwargs):
    import io as _sio
    buf = _sio.StringIO()
    kwargs2 = {k:v for k,v in kwargs.items() if k not in ('file',)}
    _real_print(*args, file=buf, **kwargs2)
    line = buf.getvalue().rstrip('\n')
    if line:
        _clog(line)
    _real_print(*args, **kwargs)


# --- Console colours (ANSI) ---
class C:
    RESET   = "\033[0m"
    BOLD    = "\033[1m"
    DIM     = "\033[2m"
    # Chunk types
    SENT    = "\033[36m"    # cyan for sentence
    MID     = "\033[34m"    # blue for mid_clause
    HEAD    = "\033[35m"    # magenta for heading
    LIST    = "\033[33m"    # yellow for list_item
    DEF     = "\033[94m"    # bright blue for definition
    QUES    = "\033[96m"    # bright cyan for question
    # Status
    OK      = "\033[32m"    # green
    WARN    = "\033[33m"    # yellow
    ERR     = "\033[31m"    # red
    SKIP    = "\033[90m"    # dark grey
    DEDUP   = "\033[90m"    # dark grey
    RETRY   = "\033[33m"    # yellow
    PRONOUNCE = "\033[95m"  # bright magenta
    SEP     = "\033[90m"    # dark grey


import sys as _sys, threading as _threading_stderr

class _CUDAFilter:
    """Suppress repetitive CUDA assertion spam, 3 lines then silence."""
    def __init__(self, real):
        self._real = real; self._n = 0; self._lock = _threading_stderr.Lock()
    def write(self, msg):
        if 'Assertion `srcIndex' in msg or ('Indexing.cu' in msg and 'block:' in msg):
            with self._lock:
                self._n += 1
                if self._n <= 3: self._real.write(msg)
                elif self._n == 4: self._real.write('  [CUDA] ... assertion spam suppressed\n')
        else:
            with self._lock: self._n = 0
            self._real.write(msg)
    def flush(self): self._real.flush()
    def __getattr__(self, a): return getattr(self._real, a)

_sys.stderr = _CUDAFilter(_sys.stderr)

app = Flask(__name__)
# Only the KAM extension is allowed to call this API. CORS limits what pages can
# read, but a malicious page could still fire no-cors POSTs at localhost, so
# every request has to carry a token as well. The token is generated per install
# on the first run and kept in kam_token.txt, which is gitignored, so no shared
# secret ships in the source. Clients fetch it from /token, and CORS restricts
# that to the extension origin, so a page can fire requests at localhost but
# can't read the token back.
# Overrides: KAM_TOKEN env var (custom setups), KAM_EXTENSION_ID env var.
_FALLBACK_EXTENSION_ID = "mdhbimlofbadmgombcdmnmnebgglalob"


def _extension_origins():
    """Which chrome-extension origins may call this API.

    Chrome normally gives an unpacked extension a different ID on every machine,
    since it hashes the folder it was loaded from. That meant a hardcoded ID
    only worked on the install it was written on, and anyone else cloning this
    got a server that started fine, a power button that worked, and then every
    request blocked by CORS with nothing obvious to point at. The worst kind of
    first-run failure: silent, and in the wrong place.

    So manifest.json now pins a public key, which makes Chrome derive the ID
    from that instead of the path. Everyone gets the same ID, and the fallback
    below is correct for every install rather than just mine. The other two
    routes stay because they cost nothing and cover the cases the key does not:
    someone who edits the key out, or runs several builds side by side.

    Order of preference:
      1. KAM_EXTENSION_ID, which may list several IDs separated by commas.
      2. Whatever register_host.py wrote into the native-host manifest.
      3. The pinned ID, which is what almost everyone will actually be on.

    Note this is defence in depth rather than the lock itself. Every request
    also has to carry the per-install token, so a wrong origin here costs a
    confusing failure, not a security hole.
    """
    env = (os.environ.get("KAM_EXTENSION_ID") or "").strip()
    if env:
        ids = [e.strip() for e in env.split(",") if e.strip()]
        print(f"[BOOT] Extension origin(s) from KAM_EXTENSION_ID: {', '.join(ids)}")
        return [f"chrome-extension://{i}" for i in ids]

    manifest = os.path.join(_SERVER_DIR0, "com.kam.tts.json")
    try:
        with open(manifest) as f:
            allowed = json.load(f).get("allowed_origins") or []
        # Chrome writes these with a trailing slash; CORS wants them without.
        origins = [o.rstrip("/") for o in allowed
                   if isinstance(o, str) and o.startswith("chrome-extension://")]
        if origins:
            print(f"[BOOT] Extension origin(s) from the native-host manifest: "
                  f"{', '.join(origins)}")
            return origins
    except FileNotFoundError:
        pass          # normal before register_host.py has been run
    except Exception as e:
        print(f"[BOOT] Could not read the native-host manifest ({e})")

    print(f"[BOOT] Extension origin: the pinned ID "
          f"{_FALLBACK_EXTENSION_ID}. If you changed the key in manifest.json, "
          f"set KAM_EXTENSION_ID to your own ID.")
    return [f"chrome-extension://{_FALLBACK_EXTENSION_ID}"]


_EXTENSION_ORIGINS = _extension_origins()
app.config["MAX_CONTENT_LENGTH"] = 1 * 1024 * 1024   # 1MB request cap

def _load_or_create_token():
    """Per-install API token: env override > kam_token.txt > generate + save."""
    env = os.environ.get("KAM_TOKEN")
    if env:
        return env.strip()
    tpath = os.path.join(_SERVER_DIR0, "kam_token.txt")
    try:
        with open(tpath) as f:
            tok = f.read().strip()
            if tok:
                return tok
    except OSError:
        pass
    import secrets
    tok = "kam-" + secrets.token_urlsafe(24)
    try:
        with open(tpath, "w") as f:
            f.write(tok)
        print("[BOOT] Generated per-install API token (kam_token.txt)")
    except OSError as e:
        print(f"[BOOT] Could not persist token ({e}) — using in-memory token")
    return tok

KAM_TOKEN = _load_or_create_token()
CORS(app, resources={r"/*": {"origins": _EXTENSION_ORIGINS}}, supports_credentials=False)

@app.route("/token", methods=["GET"])
def get_token():
    """Hands the per-install token to the extension. CORS restricts READING
    the response to the extension origin, so this stays local-only."""
    return jsonify({"token": KAM_TOKEN})

@app.before_request
def _require_token():
    # OPTIONS preflights carry no custom headers by design; /health is a
    # harmless status ping and /token is how clients bootstrap their auth.
    if request.method == "OPTIONS" or request.path in ("/health", "/token"):
        return None
    if request.headers.get("X-KAM-Token") != KAM_TOKEN:
        print(f"  [WARN] rejected request to {request.path} (missing or wrong X-KAM-Token)")
        return jsonify({"error": "unauthorised"}), 403
    return None

_dedup_cache  = {}
_dedup_lock   = _threading.Lock()
_DEDUP_WINDOW = 15.0

import time as _time

# --- Compute backend ---
# One line used to decide this: `"cuda" if torch.cuda.is_available() else "cpu"`.
# That's right on an NVIDIA machine and wrong on most others, since AMD ROCm
# reports itself as "cuda" but has no TF32 knobs, Apple Silicon was never used
# at all, and a CPU-only machine ran with PyTorch's default thread count while
# the listen-back worker competed for the same cores.
#
# device.resolve() detects every backend present, picks the best one (honouring
# a KAM_DEVICE override), PROVES it can run a real operation before committing
# to it, and applies the per-backend tuning that actually helps. See device.py.
DEV = _device.resolve()
device = DEV.torch_device          # kept as a plain string: used all over below
_DEVICE_READY_LOGGED = False


def _log_device():
    """Print the backend decision once, in the startup log the dashboard shows."""
    global _DEVICE_READY_LOGGED
    if _DEVICE_READY_LOGGED:
        return
    _DEVICE_READY_LOGGED = True
    print(f"[DEVICE] {DEV.summary()}")
    print(f"[DEVICE] torch {DEV.torch_version}"
          + (f" · {DEV.label} build {DEV.build}" if DEV.build else "")
          + (f" · compute {DEV.capability}" if DEV.capability else ""))
    if len(DEV.available) > 1:
        print(f"[DEVICE] backends available: {', '.join(DEV.available)} "
              f"(override with the KAM_DEVICE environment variable)")
    for note in DEV.notes:
        print(f"[DEVICE] {note}")
    if not DEV.is_accelerated:
        print("[DEVICE] No GPU in use — synthesis will be slower than playback. "
              "KAM will still read, with pauses to buffer.")

_SERVER_DIR  = os.path.dirname(os.path.abspath(__file__))
# Voice references: XTTS clones from one or more short clips. We keep them in a
# folder and let get_conditioning_latents average across all of them (a cleaner
# speaker embedding than a single concatenated file with splice artifacts). For
# backward compatibility, a lone my_voice.wav next to the server still works.
VOICE_SAMPLES_DIR = os.path.join(_SERVER_DIR, "voice_samples")
VOICE_SAMPLE      = os.path.join(_SERVER_DIR, "my_voice.wav")  # legacy single-file
# Named voice profiles: each is a folder of reference clips under voices/<id>/.
# "default" is the original voice_samples/ folder, so existing installs need no
# migration. The active voice persists via the learner settings store.
VOICES_DIR = os.path.join(_SERVER_DIR, "voices")
_ACTIVE_VOICE = "default"   # loaded from settings at startup


class NoVoiceClipsError(FileNotFoundError):
    """Raised at startup when there is no reference audio to clone from.

    Its own class because this is the one failure every new install hits, and it
    is not a bug: reference clips are personal, so they are gitignored and can
    never ship. Distinguishing it lets __main__ print instructions rather than a
    traceback, while a genuine missing-file bug still gets one."""

def _voice_dir(voice_id):
    return VOICE_SAMPLES_DIR if voice_id == "default" else os.path.join(VOICES_DIR, voice_id)

def _list_voices():
    """All voice profiles with clip counts; 'default' always listed first."""
    out = [{"voice_id": "default",
            "clips": len(_discover_voice_samples("default")),
            "path": VOICE_SAMPLES_DIR}]
    try:
        if os.path.isdir(VOICES_DIR):
            for d in sorted(os.listdir(VOICES_DIR)):
                p = os.path.join(VOICES_DIR, d)
                if os.path.isdir(p):
                    n = len([f for f in os.listdir(p) if f.lower().endswith(".wav")])
                    out.append({"voice_id": d, "clips": n, "path": p})
    except OSError:
        pass
    for v in out:
        v["active"] = (v["voice_id"] == _ACTIVE_VOICE)
    return out

def _discover_voice_samples(voice_id=None):
    """Return the reference clips for a voice (default: the active one).
    'default' prefers voice_samples/*.wav, falling back to my_voice.wav."""
    vid = voice_id or _ACTIVE_VOICE
    d = _voice_dir(vid)
    clips = []
    try:
        if os.path.isdir(d):
            clips = sorted(os.path.join(d, f) for f in os.listdir(d)
                           if f.lower().endswith(".wav"))
    except OSError:
        clips = []
    if not clips and vid == "default" and os.path.exists(VOICE_SAMPLE):
        clips = [VOICE_SAMPLE]
    return clips


# Screening results for the active voice, so /voices and the dashboard can show
# WHY a profile sounds the way it does without re-reading every wav.
_CLIP_SCREENING = {}     # voice_id -> {"kept": [...], "rejected": [...], ...}


def _screen_voice_clips(clips, voice_id):
    """Measure every reference clip and drop the ones that would damage the clone.

    XTTS averages every reference clip into one speaker embedding, so a single
    bad recording, whether it's clipped or near-silent or four seconds of room
    tone, contaminates the whole voice, and there's no way to hear which clip did
    it. Since they're averaged, removing a bad one can only improve things: the
    embedding gets cleaner and the remaining clips aren't affected.

    This is what keeps quality even across profiles. The 'default' voice was
    usually recorded carefully, whereas a profile added later on a laptop mic
    wasn't, and it used to load without comment and just sound worse.

    I never reject everything. If no clip passes I use them all and print the
    reasons instead, since a mediocre voice beats a server that won't start."""
    usable, reports = _aq.screen_clips(clips, min_keep=1)
    kept = [r for r in reports if r.path in usable]
    dropped = [r for r in reports if r.path not in usable]

    print(f"[VOICE] Screening {len(reports)} reference clip(s) for '{voice_id}':")
    for r in reports:
        marker = "  " if r.path in usable else "  ✗"
        print(f"[VOICE] {marker}{r.line()}")
        for reason in r.reasons:
            print(f"[VOICE]      - {reason}")

    if dropped:
        print(f"[VOICE] Excluding {len(dropped)} clip(s) from the voice: "
              f"{', '.join(r.name for r in dropped)}")
        print("[VOICE] XTTS averages every clip into one embedding, so a bad "
              "clip degrades the whole voice. Fix or re-record them and press "
              "Use again.")
    elif all(r.verdict == "PASS" for r in reports):
        print(f"[VOICE] All {len(reports)} clip(s) passed — good reference material.")

    if len(usable) < 4:
        print(f"[VOICE] Only {len(usable)} usable clip(s). 8-12 covering different "
              f"sentence types clone noticeably better than a handful.")

    _CLIP_SCREENING[voice_id] = {
        "kept": [r.name for r in kept],
        "rejected": [{"name": r.name, "reasons": r.reasons} for r in dropped],
        "warnings": [{"name": r.name, "reasons": r.reasons}
                     for r in kept if r.verdict == "WARN"],
        "n_total": len(reports), "n_used": len(usable),
    }
    return usable

def LATENT_CACHE_PATH():
    """Per-voice latent cache so switching voices is instant after first use."""
    if _ACTIVE_VOICE == "default":
        return os.path.join(_SERVER_DIR, "voice_latents.pt")
    return os.path.join(_SERVER_DIR, f"voice_latents_{_ACTIVE_VOICE}.pt")

# NOTE: there is deliberately no warmup FLAG file. Kernel caches are
# per-process, so a saved "already warmed" marker did real harm: it made a fresh
# process skip warmup and pay the full 40 second kernel-compile cost on the first
# real chunk. So _warm_model() runs on every start now.

# Annotated Any: None until load_model() populates them; annotation stops the
# static checker inferring a None-only type at the post-load access sites.
from typing import Any as _Any, Optional
tts: _Any               = None
gpt_cond_latent: _Any   = None
speaker_embedding: _Any = None

# --- Voice profiles ---
# The 12 standard recording passages. Each targets a structure KAM reads daily;
# together they give the encoder full prosodic coverage. Shown in the extension
# when creating a new voice so every profile is recorded to the same recipe.
VOICE_PASSAGES = [
    ("Plain declarative", "The morning light came through the window slowly, and the room felt calm and quiet. I sat down with a cup of coffee and started to read, in no particular hurry to be anywhere."),
    ("Questions", "So what actually happens next? How do we know which approach is the right one, and who gets to decide? These are the questions worth sitting with before we rush toward an answer."),
    ("Numbers, acronyms, technical", "The API returned a 404 error at 3:15 in the afternoon. We were running version 2.7 on the server, and the CPU load had climbed to nearly 80 percent before it finally settled."),
    ("Long, flowing, embedded clauses", "Although the plan seemed straightforward at first, the details, which nobody had fully considered, turned out to matter far more than expected, and by the end we had rewritten almost everything."),
    ("Lists and comma pacing", "We packed the essentials: water, a map, two sandwiches, a spare jacket, and the small torch from the kitchen drawer. It wasn't much, but it was enough for a day on the hills."),
    ("Warm and expressive", "Honestly, it was one of the best days I can remember. The weather was perfect, everyone was in good spirits, and for a few hours nothing else in the world seemed to matter."),
    ("Calm explanatory", "The idea is simple enough. Each part of the system does one job, does it well, and hands the result to the next part. Nothing is wasted, and every step has a clear purpose."),
    ("Measured closing", "In the end, what matters most is not how quickly you finish, but whether you understood the thing you set out to learn. Take your time, stay curious, and the rest tends to follow."),
    ("Exclamations and emphasis", "That's incredible! I honestly didn't expect it to work on the first try. Look at that — every single test passed, and the whole thing runs faster than before!"),
    ("Quoted speech and dialogue", "She looked at the results and said, 'This changes everything.' I asked her what she meant, and she replied, 'We've been solving the wrong problem all along.'"),
    ("Parenthetical asides", "The main approach (and this is the part most people miss) relies on careful preparation. The results — surprising as they were — held up under every condition we tested."),
    ("Short fragments and headings", "Chapter three. Getting started. First, the basics. A quick note before we begin: none of this requires prior experience. Ready? Let's go."),
    # 13-16 close the remaining prosodic gaps: sustained low pitch, contrastive
    # stress, equations read aloud, and steady enumeration under load.
    ("Equations read aloud", "Let x sub t plus one equal x sub t, so the forecast for the next step is simply the value we observed last. The sum of all residuals, divided by n minus one, gives the variance."),
    ("Contrast and correction", "It isn't the speed that matters — it's the accuracy. Not the first answer, but the right one. I didn't say it was impossible; I said it was difficult."),
    ("Serious and measured", "The results were not what anyone had hoped for. Three of the four trials failed outright, and the fourth produced data we still cannot explain. We need to be honest about that."),
    ("Sustained enumeration", "There are seven steps. Load the data. Clean the missing values. Split into training and test sets. Fit the baseline. Measure the error. Compare against the benchmark. Then, finally, write up what you found."),
]

def _restore_active_voice():
    """Load the persisted voice choice; fall back to default if its folder is gone."""
    global _ACTIVE_VOICE
    try:
        vid = _learner.get_setting("active_voice", "default") or "default"
        _ACTIVE_VOICE = vid if _discover_voice_samples(vid) else "default"
    except Exception:
        _ACTIVE_VOICE = "default"
    _learner.set_active_voice(_ACTIVE_VOICE)

def switch_voice(voice_id):
    """Switch the active voice. If the model is loaded, swap latents live under
    the inference lock (instant when this voice's cache exists; otherwise a
    one-off recompute of a few seconds). If the model is cold/standby the choice
    simply applies at the next wake."""
    global _ACTIVE_VOICE, gpt_cond_latent, speaker_embedding
    clips = _discover_voice_samples(voice_id)
    if not clips:
        return {"ok": False, "error": f"No .wav clips found for voice '{voice_id}'. "
                                      f"Record some passages for it in the dashboard first."}
    _ACTIVE_VOICE = voice_id
    _learner.set_setting("active_voice", voice_id)
    _learner.set_active_voice(voice_id)   # isolate all learning to this voice
    if tts is None:
        return {"ok": True, "voice_id": voice_id, "applied": "on next model load"}
    with _inference_lock:
        cached = _load_cached_latents()
        if cached is not None:
            gpt_cond_latent, speaker_embedding = cached
            print(f"[VOICE] Switched to '{voice_id}' ({len(clips)} clips, cached latents)")
            return {"ok": True, "voice_id": voice_id, "applied": "instant (cached)"}
        t0 = _time.time()
        clips = _screen_voice_clips(clips, voice_id)
        g, sp = tts.synthesizer.tts_model.get_conditioning_latents(
            audio_path=clips, gpt_cond_len=30, max_ref_length=60)
        gpt_cond_latent, speaker_embedding = g.contiguous(), sp.contiguous()
        _save_cached_latents(gpt_cond_latent, speaker_embedding)
        print(f"[VOICE] Switched to '{voice_id}' ({len(clips)} clips, "
              f"latents computed in {_time.time()-t0:.1f}s)")
        return {"ok": True, "voice_id": voice_id,
                "applied": f"latents computed ({_time.time()-t0:.1f}s)"}


def _voice_mtime():
    """Newest mtime across all reference clips (plus their count), so the latent
    cache invalidates when any clip is added, removed, or re-recorded."""
    clips = _discover_voice_samples()
    if not clips:
        return None
    try:
        newest = max(os.path.getmtime(c) for c in clips)
        return (newest, len(clips))   # count change also invalidates
    except OSError:
        return None


def _load_cached_latents():
    """Restore conditioning latents from disk; None if missing/stale/corrupt."""
    if not os.path.exists(LATENT_CACHE_PATH()):
        return None
    try:
        vm = _voice_mtime()
        cache_mtime = os.path.getmtime(LATENT_CACHE_PATH())
        if vm:
            newest, count = vm
            if cache_mtime < newest:
                print("[STARTUP] Latent cache stale (a clip is newer) - recomputing")
                return None
        payload = torch.load(LATENT_CACHE_PATH(), map_location=device, weights_only=True)
        # If the number of reference clips changed, the averaged embedding is
        # no longer valid, so recompute it.
        if vm and payload.get("clip_count") not in (None, vm[1]):
            print("[STARTUP] Latent cache stale (clip count changed) - recomputing")
            return None
        gpt = payload["gpt_cond_latent"].to(device).contiguous()
        spk = payload["speaker_embedding"].to(device).contiguous()
        print("[STARTUP] Voice latents loaded from cache (~instant)")
        return gpt, spk
    except Exception as e:
        print(f"[STARTUP] Latent cache load failed ({e}) - recomputing")
        return None


def _save_cached_latents(gpt, spk):
    try:
        _vm = _voice_mtime()
        torch.save({"gpt_cond_latent": gpt.detach().cpu(),
                    "speaker_embedding": spk.detach().cpu(),
                    "clip_count": (_vm[1] if _vm else None)}, LATENT_CACHE_PATH())
        print("[STARTUP] Voice latents cached to disk")
    except Exception as e:
        print(f"[STARTUP] Could not save latent cache: {e}")


def load_model():
    """Load XTTS v2 and either restore or compute the voice latents.

    The first boot computes latents, which takes 3 to 5 seconds, then caches
    them, and later boots restore them in about 50ms. If DeepSpeed is installed
    I enable it, since it gives a large speedup on the GPT autoregressive loop.

    This is idempotent: if the model is already loaded in this process I return
    straight away. That stops a second full load, and a duplicate startup banner,
    if the module gets re-entered, which happens when Werkzeug imports it by name
    to serve the app after it has already run as __main__."""
    global tts, gpt_cond_latent, speaker_embedding
    if tts is not None:
        return
    t0 = _time.time()

    # Probe for DeepSpeed without importing it (cheap, avoids unused-import).
    import importlib.util as _ds_probe
    # DeepSpeed's inference engine is CUDA only, not ROCm or MPS or XPU or CPU.
    use_ds = (DEV.backend == "cuda" and _ds_probe.find_spec("deepspeed") is not None)

    tts = TTS("tts_models/multilingual/multi-dataset/xtts_v2").to(device)
    print(f"[STARTUP] Model weights loaded in {_time.time()-t0:.1f}s"
          + (" (DeepSpeed available)" if use_ds else ""))

    if use_ds:
        try:
            m = tts.synthesizer.tts_model
            if hasattr(m, "gpt") and hasattr(m.gpt, "init_deepspeed"):
                m.gpt.init_deepspeed(use_deepspeed=True)
                print("[STARTUP] DeepSpeed inference engine enabled")
        except Exception as e:
            print(f"[STARTUP] DeepSpeed enable skipped: {e}")

    cached = None if os.environ.get("KAM_FRESH_LATENTS") == "1" else _load_cached_latents()
    if cached is not None:
        gpt_cond_latent, speaker_embedding = cached
    else:
        print("[STARTUP] Computing voice latents (first boot / cache miss)...")
        t1 = _time.time()
        _clips = _discover_voice_samples()
        if not _clips:
            raise NoVoiceClipsError(
                "No voice reference clips found. Add .wav files to "
                f"{VOICE_SAMPLES_DIR}/ or place a my_voice.wav next to the server.")
        _clips = _screen_voice_clips(_clips, _ACTIVE_VOICE)
        print(f"[STARTUP] Cloning from {len(_clips)} reference clip(s)")
        gpt_cond_latent, speaker_embedding = tts.synthesizer.tts_model.get_conditioning_latents(
            audio_path=_clips,                 # XTTS accepts a list; averages them
            gpt_cond_len=30,                   # use more reference audio per clip
            max_ref_length=60,
        )
        gpt_cond_latent   = gpt_cond_latent.contiguous()
        speaker_embedding = speaker_embedding.contiguous()
        print(f"[STARTUP] Latents computed in {_time.time()-t1:.1f}s")
        _save_cached_latents(gpt_cond_latent, speaker_embedding)

    # I deliberately don't call torch.cuda.empty_cache() here. The
    # standalone benchmark (which runs inference in ~3s) never does, and
    # empty_cache releases cached GPU allocations back to the driver, forcing the
    # next inference to re-allocate (and on this torch/Blackwell build, recompile
    # kernels), which I think was behind the 40s-per-inference regression.
    print(f"[STARTUP] Ready in {_time.time()-t0:.1f}s total")
    try:
        _report_reference_coverage()
    except Exception as e:
        print(f"[STARTUP] reference coverage skipped: {e}")


def _report_reference_coverage():
    """If there are transcripts of the voice clips, one line per clip in
    voice_samples/transcripts.txt, analyse their prosody structure and log which
    sentence types and complexity bands the reference audio covers.

    This is purely informational and helps confirm the clips exercise the
    structures KAM actually reads. It doesn't change synthesis at all, since an
    XTTS clone is fixed once its latents have been computed."""
    tpath = os.path.join(VOICE_SAMPLES_DIR, "transcripts.txt")
    if not os.path.exists(tpath):
        return
    try:
        with open(tpath, encoding="utf-8") as f:
            lines = [ln.strip() for ln in f if ln.strip()]
    except Exception:
        return
    if not lines:
        return
    from collections import Counter
    types, bands = Counter(), Counter()
    for ln in lines:
        try:
            ctx = analyse_prosody(ln)
            types[ctx.sentence_type] += 1
            bands[_learner.chunk_profile(ln, ctx.sentence_type)["band"]] += 1
        except Exception:
            continue
    print(f"[VOICE] Reference coverage across {len(lines)} clip transcript(s):")
    print(f"[VOICE]   sentence types: {dict(types)}")
    print(f"[VOICE]   complexity bands: {dict(bands)}")
    # Gentle nudge if coverage looks narrow.
    if len(types) <= 1:
        print("[VOICE]   note: clips are structurally uniform — consider varying "
              "questions, lists and longer sentences for richer prosody transfer.")


# --- Standby lifecycle ---
_WARMUP_TEXT = ("The quick brown fox jumps over the lazy dog. "
                "She uses Python, JavaScript, and SQL daily.")

def _warm_model():
    """Compile the per-process CUDA and cuDNN kernels for the autoregressive
    path. Those caches die with the process, so this has to run on every fresh
    load and on every wake from standby, otherwise the first chunk after a wake
    pays the full kernel-compile cost of around 40 seconds."""
    print("[STARTUP] Warming up model...")
    try:
        t0 = _time.time()
        with torch.inference_mode():
            tts.synthesizer.tts_model.inference(
                text=_WARMUP_TEXT, language="en",
                gpt_cond_latent=gpt_cond_latent, speaker_embedding=speaker_embedding,
                temperature=0.33, repetition_penalty=4.1,
                top_k=45, top_p=0.90, do_sample=True, num_beams=1, speed=1.20,
            )
        print(f"[STARTUP] Model warmed in {_time.time()-t0:.1f}s")
    except Exception as e:
        print(f"[STARTUP] Warmup failed (non-fatal): {e}")


def evict_model():
    """Drop the XTTS model from VRAM and enter STANDBY. The server keeps running
    and listening; the next synthesis request wakes it. Idempotent."""
    global tts, gpt_cond_latent, speaker_embedding, _model_state
    with _standby_lock:
        if _model_state != "ready":
            return
        print("[STANDBY] Idle timeout reached — evicting model from VRAM")
        try:
            tts = None
            gpt_cond_latent = None
            speaker_embedding = None
            import gc as _gc
            _gc.collect()
            _device.empty_cache(DEV)   # correct call for whichever backend is live
        except Exception as e:
            print(f"[STANDBY] evict error: {e}")
        _model_state = "standby"
        _stage("standby")
        print("[STANDBY] Now in standby — minimal resources, ready to wake")


def wake_model():
    """Reload + re-warm the model from STANDBY. Blocks until fully ready so the
    caller never serves a request against a half-loaded model. Safe to call when
    already ready (no-op). Returns True once ready."""
    global _model_state
    with _standby_lock:
        if _model_state == "ready" and tts is not None:
            return True
        print("[STANDBY] Wake requested — reloading model")
        _model_state = "waking"
        _stage("waking")
        try:
            load_model()      # idempotent reload (latents restore from cache ~instant)
            _warm_model()     # per-process kernels must be recompiled after eviction
            _model_state = "ready"
            _stage("model-loaded")
            print("[STANDBY] Wake complete — model ready")
            return True
        except Exception as e:
            print(f"[STANDBY] Wake FAILED: {e}")
            _model_state = "standby"
            return False


def _touch_synth_activity():
    """Mark real synthesis activity, which resets the idle clock. I call it per
    chunk so health and dashboard polling never counts as activity, only work."""
    global _last_synth_ts
    _last_synth_ts = _time.time()


def _idle_monitor():
    """Background thread: evict the model after the configured idle timeout with
    no synthesis. Disabled when _IDLE_TIMEOUT_SEC == 0 (always on)."""
    while True:
        try:
            _time.sleep(30)   # coarse check; timeouts are minutes, not seconds
            if _IDLE_TIMEOUT_SEC <= 0:
                continue
            if _model_state != "ready":
                continue
            idle = _time.time() - _last_synth_ts
            if idle >= _IDLE_TIMEOUT_SEC:
                evict_model()
        except Exception as e:
            print(f"[STANDBY] idle monitor error: {e}")


def _persist_idle_timeout(secs):
    """Save the idle-timeout choice so it survives restarts."""
    try:
        _learner.set_setting("idle_timeout_sec", int(secs))
    except Exception as e:
        print(f"[STANDBY] could not persist idle timeout: {e}")


def _load_idle_timeout():
    """Restore the saved idle timeout at startup (default 0 = always on)."""
    global _IDLE_TIMEOUT_SEC
    try:
        v = _learner.get_setting("idle_timeout_sec", 0)
        _IDLE_TIMEOUT_SEC = max(0, int(v))
    except Exception:
        _IDLE_TIMEOUT_SEC = 0


# Startup, meaning the model load and warmup, is a function and only gets called
# from the __main__ block below, never at import time. Werkzeug may import this
# module a second time under the name "server"; because nothing here runs on
# import, that second import does no work and cannot duplicate the startup.
_HW_PROFILE_PATH = None   # resolved lazily; kept for reuse across helpers

def _hw_profile_path():
    global _HW_PROFILE_PATH
    if _HW_PROFILE_PATH is None:
        _HW_PROFILE_PATH = _os_boot0.path.join(_SERVER_DIR0, "hardware_profile.json")
    return _HW_PROFILE_PATH


def _detect_machine_now():
    """Cheap, no-synthesis hardware facts, used to self-profile on first boot and
    to notice that a saved profile belongs to different hardware.

    Everything here was already worked out by device.resolve() at import, on
    every backend rather than only on CUDA, so this just reshapes it."""
    return {
        "device":    DEV.torch_device,
        "backend":   DEV.backend,
        "gpu":       DEV.name if DEV.is_accelerated else None,
        "vram_gb":   DEV.vram_gb,
        "cpu_cores": DEV.cpu_cores,
        "ram_gb":    DEV.ram_gb,
    }


def _profile_matches_machine(hp, facts):
    """True if a saved profile was written on THIS hardware. Prevents silently
    adapting to an old GPU after an upgrade or a move to another machine."""
    if not hp:
        return False
    # GPU name is the strongest signal; fall back to core count when absent.
    if hp.get("gpu") != facts.get("gpu"):
        return False
    # A profile measured on a different BACKEND is not comparable even on the
    # same box, since the same laptop benchmarked on CPU and then on its GPU has
    # wildly different RTFs, and adapting from the wrong one is worse than
    # having no profile at all. Older profiles have no backend field; those are
    # accepted on the GPU-name match alone rather than being thrown away.
    if hp.get("backend") and hp["backend"] != facts.get("backend"):
        return False
    if hp.get("cpu_cores") and facts.get("cpu_cores") \
       and hp["cpu_cores"] != facts["cpu_cores"]:
        return False
    return True


def _write_detected_profile(facts):
    """Write a structural profile, with no RTF since that needs a timed run.

    This makes adaptation work out of the box for anyone who never runs
    hardware_profile.py, and running it later adds the measured RTF and turns on
    prefetch tuning."""
    payload = dict(facts)
    payload["measured_at"] = _time.time()
    payload["source"] = "auto"        # vs "profiler" when the tool wrote it
    payload["rtf"] = None             # unknown until the profiler runs
    try:
        with open(_hw_profile_path(), "w") as f:
            json.dump(payload, f, indent=2)
        print("[HW] Wrote a hardware profile from auto-detection.")
        print("[HW] Run hardware_profile.py to add measured speed "
              "(enables prefetch tuning).")
    except Exception as e:
        print(f"[HW] could not save auto-detected profile: {e}")
    return payload


def _load_hardware_profile():
    """Return the hardware profile to adapt from.

    Order of preference:
      1. A saved profile that matches this machine (from the profiler, or a
         previous auto-detection).
      2. A freshly auto-detected profile, written on first boot or whenever the
         saved one belongs to different hardware.
    This is what makes adaptation reliable rather than dependent on the user
    remembering to run a tool."""
    facts = _detect_machine_now()
    hp = {}
    try:
        with open(_hw_profile_path()) as f:
            hp = json.load(f)
    except Exception:
        hp = {}

    if hp and _profile_matches_machine(hp, facts):
        return hp

    if hp:
        print(f"[HW] Saved profile was for '{hp.get('gpu') or 'a different machine'}' "
              f"but this is '{facts.get('gpu') or 'no GPU'}' — re-detecting.")
    return _write_detected_profile(facts)


def _apply_hardware_adaptation():
    """Tune defaults to the measured machine so KAM runs well on modest hardware
    as well as fast hardware.

    There are three adaptations, each backed by a measurement, and each of them
    defers to an explicit user setting. This only fills in sensible defaults, it
    never overrides a choice someone has actually made:

      1. Idle standby     If VRAM is scarce it helps to release the model sooner,
                          though a slow cold start makes eviction more expensive.
      2. Prefetch depth   When synthesis is slower than playback (RTF >= 1) a
                          deeper buffer hides the gap, and when it's much faster
                          a shallow buffer wastes less VRAM.
      3. Whisper analysis On very low core counts, listening back competes with
                          synthesis for CPU and makes the stalls worse.
    """
    global _IDLE_TIMEOUT_SEC, _PREFETCH_TARGET, _ANALYSIS_ENABLED
    global _ANALYSIS_EVERY, _HW_PROFILE_CACHE
    hp = _load_hardware_profile()      # always returns something (auto-detects)
    _HW_PROFILE_CACHE = hp             # cached so endpoints needn't re-detect

    rtf   = hp.get("rtf")              # None unless the profiler has been run
    vram  = hp.get("vram_gb")
    cores = hp.get("cpu_cores") or 0
    wake  = (hp.get("load_s") or 0) + (hp.get("warm_s") or 0)
    notes = []

    # 1. Idle standby (only if the user has not chosen one).
    if _learner.get_setting("idle_timeout_sec", None) is None:
        if vram and vram < 6:
            _IDLE_TIMEOUT_SEC = 600            # 10 min, to reclaim scarce VRAM
            notes.append(f"standby 10 min ({vram} GB VRAM is limited)")
        elif wake and wake > 25:
            _IDLE_TIMEOUT_SEC = 7200           # 2 h, since waking is expensive here
            notes.append(f"standby 2 h (cold start ~{wake:.0f}s)")
        else:
            _IDLE_TIMEOUT_SEC = 1800           # 30 min, a balance between the two
            notes.append("standby 30 min")

    # 2. Prefetch depth.
    #
    # This previously SHRANK the buffer on fast machines ("plenty of headroom,
    # waste less"), which was wrong since it left only one chunk in flight, so any
    # hiccup, whether Whisper analysis running or one slow chunk or a GC pause,
    # drained
    # the buffer and playback gapped. A prefetched chunk costs a few hundred KB
    # of audio; there is nothing worth saving there.
    #
    # A fast machine should buffer MORE, not less: it can refill cheaply, and
    # depth is free insurance against gaps. Slow machines need depth too, to
    # hide the fact that synthesis trails playback. So the floor is generous
    # everywhere and only the reason changes.
    if rtf is not None:
        if rtf >= 1.5:
            _PREFETCH_TARGET = 6
            notes.append(f"prefetch 6 chunks (RTF {rtf:.2f}, synthesis lags playback)")
        elif rtf >= 1.0:
            _PREFETCH_TARGET = 5
            notes.append(f"prefetch 5 chunks (RTF {rtf:.2f})")
        elif rtf <= 0.4:
            _PREFETCH_TARGET = 6
            notes.append(f"prefetch 6 chunks (RTF {rtf:.2f} — fills cheaply, no gaps)")
        else:
            _PREFETCH_TARGET = 5
            notes.append(f"prefetch 5 chunks (RTF {rtf:.2f})")

    # 3. Whisper listen-back.
    #
    # On CPU-only inference, synthesis and transcription are the same scarce
    # resource. Whisper on top of an already-struggling CPU turns "slower than
    # playback" into "unusable", so analysis is sampled rather than run on every
    # chunk. Learning still happens, just from fewer examples. The user can
    # override either way from Speech Tuning.
    if cores and cores <= 2:
        _ANALYSIS_ENABLED = False
        notes.append(f"quality analysis off ({cores} cores — it would starve synthesis)")
    elif not DEV.is_accelerated and _learner.get_setting("analysis_every", None) is None:
        _ANALYSIS_EVERY = 4
        notes.append("quality analysis every 4th chunk (CPU-only: listening back "
                     "competes with synthesis for the same cores)")

    # 4. Buffer harder when there is no accelerator at all. Without a measured
    #    RTF the branch above never fires, and CPU inference is exactly the case
    #    that needs the deepest buffer.
    if rtf is None and not DEV.is_accelerated:
        _PREFETCH_TARGET = 8
        notes.append("prefetch 8 chunks (CPU-only, speed not yet measured)")

    if notes:
        print("[HW] Adapted to this machine: " + "; ".join(notes))
    band, meaning = _bench.rtf_band(rtf)
    if rtf is not None:
        print(f"[HW] Measured speed: RTF {rtf:.2f} ({band}) — {meaning}")
    else:
        print("[HW] Synthesis speed not measured yet — the first read will "
              "self-measure, or run the benchmark from the dashboard.")


# --- Benchmarking ---
# The measurement code lives in benchmark.py and is shared with
# hardware_profile.py, so the CLI profiler and the server report identical
# numbers from identical work. The difference is only where the output goes:
# the profiler writes to a terminal, the server writes through print(), which
# the dashboard console streams live via /console.
_bench_lock    = _threading.Lock()
_bench_running = False
_LAST_BENCH    = None      # most recent result, served by GET /benchmark/result


def _run_benchmark(source="server"):
    """Benchmark the model that's already loaded, streaming each result out.

    I deliberately don't use benchmark.run_standalone() here, since that loads a
    second copy of XTTS, which on a 6 GB card is enough to run the machine out of
    memory, and it measures a model that isn't the one serving requests. This way
    the numbers come from the live model on the live latents."""
    global _bench_running, _LAST_BENCH
    with _bench_lock:
        if _bench_running:
            return {"ok": False, "error": "a benchmark is already running"}
        _bench_running = True
    try:
        if _model_state != "ready" or tts is None:
            wake_model()
        if tts is None:
            return {"ok": False, "error": "model is not loaded"}

        print("=" * 58)
        print(f"[BENCH] Measuring synthesis speed on {DEV.summary()}")
        print("=" * 58)

        def _synth(text):
            """Synthesise and return seconds of audio produced."""
            with _inference_lock, torch.inference_mode():
                out = tts.synthesizer.tts_model.inference(
                    text=text, language="en",
                    gpt_cond_latent=gpt_cond_latent,
                    speaker_embedding=speaker_embedding,
                    temperature=0.33, repetition_penalty=4.1,
                    top_k=45, top_p=0.90, do_sample=True, num_beams=1, speed=1.2,
                )
            return len(out["wav"]) / 24000.0

        _stage("benchmarking")
        # The model is already warm here (startup warms it), so re-warming would
        # only add a slow, meaningless first reading to the average.
        perf = _bench.measure_inference(
            _synth, emit=lambda line: print(f"[BENCH]{line}"), warmup=False)

        # Carry the load and warm timings forward if a previous profile measured
        # them, since they describe waking from standby and I can't re-measure
        # them without restarting the process.
        prev = _HW_PROFILE_CACHE or {}
        perf.setdefault("load_s", prev.get("load_s"))
        perf.setdefault("warm_s", prev.get("warm_s"))

        payload = _bench.build_profile(DEV, perf, source=source)
        _bench.save_profile(_hw_profile_path(), payload,
                            emit=lambda line: print(f"[BENCH]{line}"))
        for line in _bench.verdict_lines(DEV, perf):
            print(f"[BENCH] {line}")
        print("=" * 58)

        # Re-adapt immediately: a fresh RTF should change buffering now, not at
        # the next restart.
        try:
            _apply_hardware_adaptation()
        except Exception as e:
            print(f"[HW] re-adaptation after benchmark skipped: {e}")

        _LAST_BENCH = {"ok": True, "device": DEV.as_dict(), "perf": perf,
                       "profile": payload,
                       "verdict": _bench.verdict_lines(DEV, perf),
                       "band": _bench.rtf_band(perf.get("rtf"))[0]}
        return _LAST_BENCH
    finally:
        with _bench_lock:
            _bench_running = False


def _maybe_selfbenchmark():
    """Measure on first boot for this hardware; stay quiet on every boot after.

    Without this, adaptation depends on the user knowing to run a separate
    script, so most installs never get a measured RTF and silently use defaults
    tuned for a fast NVIDIA card. Re-running on every start would add a minute
    to each launch, so it is keyed to the hardware: a saved profile that matches
    this machine and already has an RTF means the work is done."""
    facts = _detect_machine_now()
    hp = {}
    try:
        with open(_hw_profile_path()) as f:
            hp = json.load(f)
    except Exception:
        hp = {}
    if hp and hp.get("rtf") is not None and _profile_matches_machine(hp, facts):
        band, meaning = _bench.rtf_band(hp["rtf"])
        print(f"[BENCH] Using the saved measurement for this machine: "
              f"RTF {hp['rtf']:.2f} ({band}).")
        print("[BENCH] Re-measure any time from the dashboard, or with "
              "`python hardware_profile.py`.")
        return
    if os.environ.get("KAM_NO_BENCHMARK") == "1":
        print("[BENCH] Startup benchmark disabled (KAM_NO_BENCHMARK=1)")
        return
    print("[BENCH] First run on this hardware — measuring synthesis speed "
          "(about 15 seconds).")
    _run_benchmark(source="server")


# Prefetch depth and the analysis toggle. The defaults suit a capable machine and
# _apply_hardware_adaptation() adjusts them once there's a measured profile.
_PREFETCH_TARGET  = 5
_ANALYSIS_ENABLED = True
# How often chunks are analysed: 1 = every chunk (best learning, most GPU),
# 3 = every third, 0 = never (fastest). Persisted via learner settings.
_ANALYSIS_EVERY   = 1
_analysis_counter = 0
_HW_PROFILE_CACHE = None   # resolved profile, set by _apply_hardware_adaptation()


def _run_startup():
    _stage("device-probe")
    _log_device()
    try:
        import sys as _sys
        print(f"[STARTUP] python={_sys.executable}")
        print(f"[STARTUP] launched_by_host="
              f"{os.environ.get('KAM_LAUNCHED_BY_HOST', '0')}")
    except Exception as _e:
        print(f"[STARTUP] env probe failed: {_e}")
    print(f"[STARTUP] Loading XTTS-v2 on {DEV.label}…")
    _stage("model-loading")
    load_model()
    _stage("model-loaded")

    # Warmup compiles the CUDA and cuDNN kernels for the autoregressive path.
    # Those caches are per-process and die when the server exits, so I have to
    # warm on every startup. A stale .warmup_done flag used to make me skip it,
    # which left the first real chunk paying the full 40 second kernel-compile
    # cost. The standalone benchmark was fast precisely because it always warms
    # first.
    _warm_model()

    # Preload the POS prosody model now so the first real chunk doesn't pay the
    # spaCy load cost mid-read. Non-fatal: absence just disables the NLP layer.
    try:
        if _pos_prosody.is_available():
            _pos_prosody.analyse("Priming the parser with a short sentence.")
            print("[STARTUP] POS prosody ready")
        else:
            print("[STARTUP] POS prosody unavailable (spaCy model not installed)")
    except Exception as e:
        print(f"[STARTUP] POS preload skipped (non-fatal): {e}")

    # One-time backfill of durable feedback counters from existing verdicts.
    try:
        _learner.seed_feedback_counters()
    except Exception as e:
        print(f"[STARTUP] feedback counter seed skipped: {e}")

    # Enter READY and start the idle monitor (which honours _IDLE_TIMEOUT_SEC;
    # a value of 0 keeps the model resident indefinitely, which is "always on").
    global _model_state, _last_synth_ts
    _load_idle_timeout()
    _restore_active_voice()
    try:
        global _ANALYSIS_EVERY
        _ANALYSIS_EVERY = int(_learner.get_setting("analysis_every", 1))
    except Exception:
        pass
    # Keep the database bounded. Raw chunks/observations are pruned to a recent
    # window; learned settings, rules and reports are never touched.
    try:
        _learner.prune_old_data()
    except Exception as e:
        print(f"[LEARNER] retention skipped: {e}")
    # I measure this machine before adapting to it, so the very first boot on a
    # new device adapts to a real number instead of waiting for the user to
    # discover a separate profiler script. Skipped when a profile measured on
    # this same hardware already exists, so ordinary restarts stay fast.
    _model_state = "ready"        # benchmark needs the model callable
    try:
        _maybe_selfbenchmark()
    except Exception as e:
        print(f"[BENCH] self-benchmark skipped (non-fatal): {e}")

    # Adapt defaults to the measured machine (defers to any explicit user setting).
    _stage("adapting")
    try:
        _apply_hardware_adaptation()
    except Exception as e:
        print(f"[HW] adaptation skipped: {e}")
    _last_synth_ts = _time.time()
    try:
        _threading.Thread(target=_idle_monitor, daemon=True).start()
        print("[STANDBY] Idle monitor started")
    except Exception as e:
        print(f"[STANDBY] could not start idle monitor: {e}")


# =============================================================================
# PRONUNCIATION TABLES, THE ADAPTIVE SYSTEM
# =============================================================================
#
# Architecture:
#
#   1. PRONUNCIATION_STORE (JSON on disk, auto-created)
#      User-editable dictionary that overrides everything else.
#      Edit via POST /pronounce or directly in pronunciation_store.json.
#      Persists across restarts. Loaded once at startup, hot-reloaded on
#      each request if the file has changed.
#
#   2. BUILT-IN TABLES (below)
#      Curated entries for known-tricky abbreviations. These are the fallback
#      when no user override exists.
#
#   3. AUTO-RENDERER (render_abbreviation)
#      For any abbreviation not in either table, generates the best spoken
#      form automatically based on letter phonetics. No unknown abbreviation
#      ever reaches XTTS as raw uppercase letters.
#
#   4. Learning loop:
#      POST /pronounce {"abbr": "GPU", "spoken": "gee pee you"}
#        → saved to pronunciation_store.json
#        → immediately active without restart
#      GET /pronounce/GPU
#        → returns current spoken form + source (user/builtin/auto)
# =============================================================================

import time

# ---------------------------------------------------------------------------
# Persistent pronunciation store
# ---------------------------------------------------------------------------

STORE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'pronunciation_store.json')

# Ensure pronunciation store exists so _load_store() never hits a missing file.
if not os.path.exists(STORE_PATH):
    try:
        with open(STORE_PATH, 'w', encoding='utf-8') as _f:
            _f.write('{}')
        print(f"Pronunciation store created: {STORE_PATH}")
    except Exception as _e:
        print(f"Warning: could not create pronunciation store: {_e}")

_store_cache     = {}
_store_mtime     = 0.0

# --- Learned punctuation corrections ---
# Written by learner.learn_punctuation_correction() when the user fixes a
# chunk's phrasing. Applied (exact-match) at the start of synthesis so the
# correction takes effect on the next run. Cached with mtime invalidation.
PUNCT_PATH       = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'punctuation_corrections.json')
_punct_cache     = {}
_punct_mtime     = 0.0

# --- User-taught equation readings ---
# MATH rules come from EQUATION reports, meaning an expression the user heard
# read wrongly plus how it should be spoken. I apply them before the symbol
# expander so a taught reading always beats the generic rules. Cached with a
# short TTL
# because rules change rarely but synthesis is hot.
_math_rules_cache = []
_math_rules_ts    = 0.0
_MATH_RULES_TTL   = 20.0   # seconds

def _load_math_rules():
    """Active MATH rules as [(expression, spoken_form), …], longest first so a
    specific expression wins over a shorter one contained inside it."""
    global _math_rules_cache, _math_rules_ts
    now = _time.time()
    if _math_rules_cache and (now - _math_rules_ts) < _MATH_RULES_TTL:
        return _math_rules_cache
    try:
        rules = _learner.get_rules(active_only=True)
        pairs = [(r.get("pattern") or "", r.get("value") or "")
                 for r in rules
                 if r.get("rule_type") == "MATH" and r.get("pattern") and r.get("value")]
        pairs.sort(key=lambda p: len(p[0]), reverse=True)
        _math_rules_cache, _math_rules_ts = pairs, now
    except Exception as e:
        print(f"[MATH] could not load equation rules: {e}")
        _math_rules_cache, _math_rules_ts = [], now
    return _math_rules_cache


def apply_math_rules(text: str) -> str:
    """Substitute any user-taught equation readings. Literal (not regex) so an
    expression like X_{t+1} needs no escaping by the user."""
    if not text:
        return text
    for expr, spoken in _load_math_rules():
        if expr in text:
            text = text.replace(expr, f" {spoken} ")
            print(f"[MATH] applied taught reading: {expr[:24]} → {spoken[:34]}")
    return text


def _load_punct() -> dict:
    """Return the punctuation-corrections map, reloading only when the file
    changes on disk."""
    global _punct_cache, _punct_mtime
    try:
        m = os.path.getmtime(PUNCT_PATH)
    except OSError:
        return _punct_cache
    if m != _punct_mtime:
        try:
            with open(PUNCT_PATH, 'r', encoding='utf-8') as f:
                _punct_cache = json.load(f)
            _punct_mtime = m
        except Exception as e:
            print(f"[PUNCT] load failed: {e}")
    return _punct_cache


def apply_punct_corrections(text: str) -> str:
    """Apply any exact-match learned correction for this chunk text."""
    corrections = _load_punct()
    if not corrections:
        return text
    fixed = corrections.get(text.strip())
    if fixed and fixed != text:
        print(f"[PUNCT] applied learned correction ({len(fixed)}c)")
        return fixed
    return text

# --- Pre-compiled regex patterns ---
# Compiled once at startup so I'm not re-compiling on every chunk. Every pattern
# below has a call site, and I deleted the ones that had lost theirs rather than
# leaving them to rot. The call sites that used to re-specify a pattern inline,
# which bypassed the compiled copy entirely, now reference these instead.
_RE_HTML_CLOSE   = re.compile(r'</\w+>')
_RE_HTML_OPEN    = re.compile(r'<\w+[^>]*>')
_RE_JUPYTER_IN   = re.compile(r'\bIn\s*\[\d+\]\s*:')
_RE_JUPYTER_OUT  = re.compile(r'\bOut\s*\[\d+\]\s*:')
_RE_JUPYTER_CONT = re.compile(r'^\s*\.\.\.:?\s*', re.MULTILINE)
_RE_MARKDOWN_LNK = re.compile(r'\[([^\]]+)\]\([^)]+\)')
_RE_URL          = re.compile(r'https?://\S+')
_RE_FOOTNOTE     = re.compile(r'\[\d+\]')
_RE_HASH_FRAG    = re.compile(r'#\w+')
_RE_BLANK_LINES  = re.compile(r'\n\s*\n')
_RE_WHITESPACE   = re.compile(r'\s+')
_RE_ABBREV       = re.compile(r"\b([A-Z][A-Z0-9]{2,}|[A-Z]{2})('s|s)?\b(?![a-z])")
_RE_CAMEL        = re.compile(r'\b([a-z]+[A-Z][a-zA-Z]*)\b')
_RE_SNAKE        = re.compile(r'\b([a-z][a-z0-9]*(?:_[a-z][a-z0-9]*)+)\b')
_RE_PUNCT_DBL_CM = re.compile(r',\s*,+')
_RE_PUNCT_DOTCM  = re.compile(r'\.\s*,')
_RE_PUNCT_CMDOT  = re.compile(r',\s*\.')
_RE_PUNCT_SPCPNC = re.compile(r'\s+([.,;:!?])')
_RE_COMMA_NORM   = re.compile(r',\s*')
_RE_SENT_TERM    = re.compile(r'([.!?])\s+')
_RE_PARABR       = re.compile(r'\n{2,}')
_RE_NEWLINE      = re.compile(r'\n')
_RE_DOUBLE_DOT   = re.compile(r'\.\s*\.(?!\.)')
# Collapses a RUN of commas, not just a pair: ',\s*,' is non-overlapping, so
# ",,," left a ",," behind for the next stage to trip over.
_RE_DOUBLE_CM    = re.compile(r',(?:\s*,)+')
_RE_DASH_PAIR    = re.compile(r'\s*[—–]\s*')
_RE_ELLIPSIS     = re.compile(r'\.\.\.+\s*')
_RE_SEMICOLON    = re.compile(r'\s*;\s*')
_RE_COLON        = re.compile(r'\s*:\s*')
_RE_LONE_DASH    = re.compile(r'(?<=\s)-(?=\s)')

def _load_store() -> dict:
    """Load user pronunciation overrides from disk. Hot-reloads if file changed."""
    global _store_cache, _store_mtime
    try:
        mtime = os.path.getmtime(STORE_PATH)
        # Using > with an epsilon since float != can miss changes on Windows NTFS
        if mtime > _store_mtime + 0.001:
            with open(STORE_PATH, 'r', encoding='utf-8') as f:
                raw = json.load(f)
            # Normalise to uppercase keys and lowercase values so lookup is
            # case-insensitive
            _store_cache = {k.upper(): v.lower().strip() for k, v in raw.items()}
            _store_mtime = mtime
            print(f"  {C.PRONOUNCE}[PRONOUNCE]{C.RESET} store reloaded — {len(_store_cache)} entries")
    except FileNotFoundError:
        _store_cache = {}
    except Exception as e:
        print(f"  {C.ERR}[PRONOUNCE]{C.RESET} load error: {e}")
    return _store_cache

def _save_store(store: dict):
    """Persist the pronunciation store to disk."""
    global _store_cache, _store_mtime
    with open(STORE_PATH, 'w', encoding='utf-8') as f:
        json.dump(store, f, indent=2, ensure_ascii=False)
    _store_mtime = os.path.getmtime(STORE_PATH)
    _store_cache = store
    print(f"[PRONOUNCE] Store saved: {STORE_PATH}")


# ---------------------------------------------------------------------------
# Built-in pronunciation tables
# ---------------------------------------------------------------------------
# Only entries where auto-rendering produces a wrong or suboptimal result.
# Everything else is handled by render_abbreviation() automatically.


# Words XTTS consistently mispronounces
COMPLEX_WORD_MAP = {
    'tuple':        'tyoo pul',
    'tuples':       'tyoo pulz',
    'epoch':        'ee pok',
    'epochs':       'ee poks',
    'boolean':      'boo lee an',
    'mutex':        'mew tex',
    'semaphore':    'sem ah for',
    'daemon':       'dee mon',
    'cache':        'cash',
    'cached':       'cashed',
    'caching':      'cashing',
    'queue':        'kyoo',
    'queued':       'kyood',
    'enqueue':      'en kyoo',
    'dequeue':      'de kyoo',
    'heuristic':    'hyoo ris tick',
    'euclidean':    'yoo klid ee an',
    'gaussian':     'gow see an',
    'bayesian':     'bay zee un',
    'markov':       'mar kov',
    'poisson':      'pwa son',
    'bernoulli':    'ber noo lee',
    'dirichlet':    'deer ee shlay',
    'fourier':      'foor ee ay',
    'jacobian':     'ya ko bee an',
    'hessian':      'hess ee an',
    'lagrangian':   'la granj ee an',
    'hamiltonian':  'ham il tone ee an',
    'cosine':       'co sine',
    'arccosine':    'arc co sine',
    'arcsine':      'arc sine',
    'arctangent':   'arc tangent',
    'arctan':       'arc tan',
    'arccos':       'arc cos',
    'arcsin':       'arc sin',
    'eigenvector':  'eigen vector',
    'eigenvalue':   'eigen value',
    'eigenspace':   'eigen space',
    'eigenbasis':   'eigen basis',
    'eigendecomposition': 'eigen decomposition',
    'softmax':      'soft max',
    'dropout':      'drop out',
    'polymorphism': 'poly morphism',
    'asynchronous': 'a synchronous',
    'synchronous':  'synchronous',
}


# ---------------------------------------------------------------------------
# Auto-renderer: generates spoken form for any unknown abbreviation
#
# There are no built-in abbreviation tables. Every override lives in
# pronunciation_store.json (written by reports and POST /pronounce); anything
# not there is resolved by the word-pronounceability heuristic below, or passed
# through, because XTTS reads common abbreviations (AI, API, GPU) correctly on
# its own and forcing letter-spelling makes them sound worse.
# ---------------------------------------------------------------------------

# Common vowel patterns that suggest an acronym is word-pronounceable.
# (A per-letter phonetic table used to sit here for letter-by-letter spelling.
# Nothing read it: render_abbreviation deliberately does not letter-spell,
# because XTTS reads AI/API/GPU correctly and forcing "ay-eye" sounds worse.
# The learner keeps its own small table for the one case that does spell out,
# which is an acronym Whisper has proved was being skipped.)
_VOWELS = set('AEIOU')

def _is_word_pronounceable(token: str) -> bool:
    """
    A heuristic for whether this all-caps token can be read as a word. It's
    deliberately conservative and only returns True when I'm fairly sure.

    The conditions are:
      - 3 to 5 characters, since 2-character and 6-plus acronyms rarely read
        as words
      - at least one vowel
      - a vowel ratio of 0.30 or more, so PEFT and REST both fail at 0.25,
        which is what I want since those live in the explicit table
      - no run of 3 or more consecutive consonants
      - doesn't start and end with the same consonant cluster pattern that
        suggests an initialisation, like GRPC or VLLM
    """
    upper = token.upper()
    if len(upper) < 3 or len(upper) > 5:
        return False
    vowel_count = sum(1 for c in upper if c in _VOWELS)
    if vowel_count == 0:
        return False
    # Require at least 30% vowels for natural pronounceability
    if vowel_count / len(upper) < 0.30:
        return False
    # Reject tokens with 3+ consecutive consonants
    consonant_run = 0
    for c in upper:
        if c not in _VOWELS:
            consonant_run += 1
            if consonant_run >= 3:
                return False
        else:
            consonant_run = 0
    return True

def render_abbreviation(token: str) -> str:
    """
    Spoken form for an abbreviation.

    In priority order:
      1. The user's pronunciation store, which always wins.
      2. The word-pronounceability heuristic, for things like NASA and RADAR.
      3. Otherwise pass it through unchanged, since XTTS handles short
         abbreviations natively and doesn't need letter-by-letter phonetics.

    I don't spell things out letter by letter by default because XTTS reads
    "AI", "API", "SDK" and "GPU" perfectly naturally. Forcing "ay-eye" and the
    like sounds unnatural and confuses the model, so letter-spelling only
    happens when someone explicitly adds it to the store.
    """
    return lookup_pronunciation(token)[0]


def lookup_pronunciation(token: str) -> tuple:
    """
    Returns (spoken_form, source), where source is one of:
    'user'      an explicit override from pronunciation_store.json
    'auto_word' read as a word by the pronounceability heuristic, like NASA
    'passthru'  handed to XTTS unchanged, since AI, API and GPU all read fine
    """
    upper = token.upper()
    store = _load_store()

    if upper in store:
        return store[upper], 'user'
    if token in store:
        return store[token], 'user'
    if upper.isalpha() and _is_word_pronounceable(upper):
        return upper.lower(), 'auto_word'   # XTTS reads lowercase as a word
    return token, 'passthru'


# =============================================================================
# VARIABLE / IDENTIFIER EXPANSION
# =============================================================================

VARIABLE_PREFIXES = {
    'num':  'number',  'str':  'string',  'arr':  'array',
    'idx':  'index',   'ptr':  'pointer', 'val':  'value',
    'tmp':  'temp',    'buf':  'buffer',  'len':  'length',
    'cnt':  'count',   'msg':  'message', 'err':  'error',
    'cb':   'callback','fn':   'function','obj':  'object',
    'dict': 'dictionary','bool':'boolean','int':  'integer',
    'char': 'character','src': 'source',  'dst':  'destination',
    'res':  'result',  'req':  'request', 'resp': 'response',
    'ctx':  'context', 'cfg':  'config',  'env':  'environment',
    'pkg':  'package', 'lib':  'library', 'db':   'database',
    'col':  'column',  'pos':  'position','prev': 'previous',
    'curr': 'current', 'init': 'initialize','calc':'calculate',
    'img':  'image',   'btn':  'button',  'lbl':  'label',
    'cls':  'class',   'attr': 'attribute','arg':  'argument',
    'args': 'arguments','var': 'variable','func': 'function',
    'proc': 'procedure','iter':'iterator','gen':  'generator',
    'seq':  'sequence','cond': 'condition','expr': 'expression',
    'stmt': 'statement','decl':'declaration','ref': 'reference',
    'addr': 'address', 'sz':   'size',    'max':  'max',  'min': 'min',
}

# Whole-word code terms, which I only expand inside code blocks and fences in
# clean_code_block(). Unlike VARIABLE_PREFIXES (which matches prefixes of
# compound identifiers such as strName → string Name), these entries fire
# when the abbreviation appears as a standalone token. Keep this list
# disjoint-in-spirit from prose: a human reader of a code snippet will
# read "str" as "string", but the same rule applied to prose would mangle
# ordinary English. That is why it is scoped to code contexts only.
#
# Ordering note: longest keys first prevents "args" being clipped to "arg"
# before the whole-word match runs. We sort at runtime.
CODE_WHOLE_WORDS = {
    # Types and data
    'str':   'string',      'int':   'integer',    'bool':  'boolean',
    'num':   'number',      'char':  'character',  'dict':  'dictionary',
    'arr':   'array',       'obj':   'object',     'list':  'list',
    'tup':   'tuple',       'set':   'set',

    # Variables and values
    'val':   'value',       'var':   'variable',   'tmp':   'temp',
    'buf':   'buffer',      'ptr':   'pointer',    'ref':   'reference',
    'addr':  'address',     'idx':   'index',      'pos':   'position',

    # Counts and lengths
    'len':   'length',      'cnt':   'count',      'sz':   'size',

    # Functions and flow
    'fn':    'function',    'func':  'function',   'proc':  'procedure',
    'cb':    'callback',    'iter':  'iterator',   'gen':   'generator',
    'expr':  'expression',  'stmt':  'statement',  'decl':  'declaration',
    'cond':  'condition',   'seq':   'sequence',   'arg':   'argument',
    'args':  'arguments',   'param': 'parameter',  'params':'parameters',
    'kwarg': 'keyword argument',  'kwargs': 'keyword arguments',
    'init':  'initialize',  'calc':  'calculate',

    # Messaging and I/O
    'msg':   'message',     'err':   'error',      'errno': 'error number',
    'req':   'request',     'resp':  'response',   'res':   'result',
    'src':   'source',      'dst':   'destination',

    # Context and environment
    'ctx':   'context',     'cfg':   'config',     'conf':  'config',
    'env':   'environment', 'pkg':   'package',    'lib':   'library',
    'db':    'database',    'col':   'column',     'prev':  'previous',
    'curr':  'current',     'attr':  'attribute',
    'attrs': 'attributes',

    # UI / misc
    'img':   'image',       'btn':   'button',     'lbl':   'label',
    'nav':   'navigation',  'txt':   'text',       'desc':  'description',
    'num_':  'number',

    # Python specifics
    'self':  'self',        'repr':  'represent',
    'stdin': 'standard input',
    'stdout':'standard output',  'stderr': 'standard error',

    # Web / JS-ish
    'el':    'element',     'elem':  'element',    'evt':   'event',
    'ev':    'event',
    'id':    'I D',         'url':   'U R L',      'uri':   'U R I',
    'api':   'A P I',       'sdk':   'S D K',      'json':  'jayson',
    'xml':   'X M L',       'html':  'H T M L',    'css':   'C S S',
    'sql':   'sequel',      'yaml':  'yamel',      'csv':   'C S V',

    # File / path
    'cwd':   'current working directory',
    'pwd':   'present working directory',
    'dir':   'directory',   'filename': 'filename',  'pathname': 'pathname',

    # Regex / parsing
    'regex': 'regular expression',  're':      'regular expression',
    'lex':   'lexer',       'tok':   'token',     'tokens': 'tokens',
    'ast':   'A S T',
}


# Tokens commonly used as module names, since I don't want `re.compile(...)` to
# become "regular expression.compile(...)". When one of these appears followed
# by a dot and an identifier, it is left alone; when it appears standalone it
# is still expanded.
_CODE_MODULE_TOKENS = {'re', 'id', 'os', 'io'}


# --- Single-pass alternations for the substitution tables ---
# These three tables used to be applied by looping over their keys and running
# one re.sub per key: ~100 passes over the string for CODE_WHOLE_WORDS and ~150
# for VARIABLE_PREFIXES (three patterns x fifty prefixes), on every chunk.
# One alternation, compiled once, walks the string a single time instead.
#
# Alternation order is longest-key-first, which preserves the old longest-match
# behaviour: `args` still wins over `arg`, `kwargs` over `kwarg`.

def _alternation(keys):
    """Regex alternation over keys, longest first so the longest match wins."""
    return "|".join(re.escape(k) for k in sorted(keys, key=len, reverse=True))


# Module-name tokens (`re.compile`, `os.path`, `io.open`) must stay intact for
# the downstream dot-path splitter, so they carry a negative lookahead and are
# matched by a separate branch.
_RE_CODE_WORDS = re.compile(
    r'\b(?:' + _alternation(k for k in CODE_WHOLE_WORDS if k.lower() in _CODE_MODULE_TOKENS)
    + r')\b(?!\.[A-Za-z_])'
    r'|\b(?:' + _alternation(k for k in CODE_WHOLE_WORDS if k.lower() not in _CODE_MODULE_TOKENS)
    + r')\b',
    re.IGNORECASE)

_CODE_WORDS_LOWER = {k.lower(): v for k, v in CODE_WHOLE_WORDS.items()}

# Prefix expansion fires when the prefix is followed by a digit, a capital, or
# an underscore, meaning it's the head of a compound identifier (strName, num2,
# idx_start), never a whole ordinary word.
_RE_VAR_PREFIX = re.compile(
    r'\b(' + _alternation(VARIABLE_PREFIXES) + r')(?=(\d|[A-Z]|_))')

_RE_COMPLEX_WORD = re.compile(
    r'\b(?:' + _alternation(COMPLEX_WORD_MAP) + r')\b', re.IGNORECASE)

_COMPLEX_WORDS_LOWER = {k.lower(): v for k, v in COMPLEX_WORD_MAP.items()}


def expand_code_whole_words(text):
    """Expand standalone code-term tokens to their spoken form.

    Fires only on whole-word matches, i.e. surrounded by non-alphanumeric
    context. Runs inside `clean_code_block` so it does not touch ordinary prose.

    For tokens in `_CODE_MODULE_TOKENS` I suppress the expansion when the token
    is immediately followed by a dot and an identifier, since that shape is
    almost always a module reference and needs to stay intact.

    It's case-insensitive. The dict values are lowercase and the result feeds
    XTTS as speech, so mixed-case input like ``STR`` or ``Args`` comes out as the
    lowercase expansion, which stops the all-caps handling firing downstream.
    """
    return _RE_CODE_WORDS.sub(
        lambda m: _CODE_WORDS_LOWER.get(m.group(0).lower(), m.group(0)), text)


def expand_variable_prefixes(text):
    """strName -> string Name, num2 -> number 2, idx_start -> index_start."""
    def _repl(m):
        prefix, nxt = m.group(1), m.group(2)
        expansion = VARIABLE_PREFIXES[prefix]
        # An underscore already separates the words so I don't add a space,
        # which matches the original three-pattern behaviour exactly.
        return expansion if nxt == '_' else expansion + ' '
    return _RE_VAR_PREFIX.sub(_repl, text)


def expand_complex_words(text):
    """Phonetic respelling of words XTTS reliably mispronounces."""
    return _RE_COMPLEX_WORD.sub(
        lambda m: _COMPLEX_WORDS_LOWER.get(m.group(0).lower(), m.group(0)), text)

def expand_camel_case(word):
    """Split camelCase/PascalCase into spaced lowercase words."""
    word = re.sub(r'([a-z])([A-Z])', r'\1 \2', word)
    word = re.sub(r'([A-Z]+)([A-Z][a-z])', r'\1 \2', word)
    return word.lower()

def expand_snake_case(text):
    return re.sub(r'\b([a-z][a-z0-9]*)_([a-z][a-z0-9_]*)\b',
                  lambda m: m.group(0).replace('_', ' '), text)

def expand_math_symbols(text):
    """Speak mathematical notation that XTTS otherwise mangles or skips.

    Handles:
      • Unicode subscripts/superscripts (A₀, h², xⁿ) → "A zero", "h squared"…
      • Letter+digit variables common in maths/logic (A0, h1, Bw, Cw) →
        "A zero", "h one"; a trailing letter subscript (Aw) → "A w".
      • Greek letters by name (α → alpha) so equations read aloud.

    This isn't meant to be a full equation parser, just enough that you hear
    something sensible instead of silence or the wrong token.
    """
    SUB = {'₀':'0','₁':'1','₂':'2','₃':'3','₄':'4','₅':'5','₆':'6','₇':'7','₈':'8','₉':'9',
           'ₐ':'a','ₑ':'e','ₓ':'x','ₕ':'h','ₖ':'k','ₗ':'l','ₘ':'m','ₙ':'n','ₚ':'p','ₛ':'s','ₜ':'t'}
    SUP = {'⁰':'0','¹':'1','²':'2','³':'3','⁴':'4','⁵':'5','⁶':'6','⁷':'7','⁸':'8','⁹':'9',
           'ⁿ':'n','ⁱ':'i'}
    GREEK = {'α':'alpha','β':'beta','γ':'gamma','δ':'delta','ε':'epsilon','θ':'theta',
             'λ':'lambda','μ':'mu','π':'pi','σ':'sigma','τ':'tau','φ':'phi','ω':'omega',
             'Δ':'delta','Σ':'sigma','Ω':'omega','Π':'pi'}
    DIGIT_WORD = {'0':'zero','1':'one','2':'two','3':'three','4':'four',
                  '5':'five','6':'six','7':'seven','8':'eight','9':'nine'}

    # Whether this is maths at all, decided from the text as it arrived rather
    # than after substitutions have muddied it. The plain operators at the end of
    # this function are only safe to read in that context: "=" wants to be
    # "equals" in an equation but "is set to" in an assignment, and "+" has to
    # survive long enough for the code pass to notice "++".
    #
    # It used to work by accident. Equations carried underscores from subscripts,
    # which tripped the code-symbol heuristic, so operators got read by the code
    # path. Once subscripts were spoken properly the underscores went and the
    # operators fell silent, which is a bad thing to depend on either way.
    _MATHS_MARK = ('≠≤≥≈≡⊆⊇⊂⊃'
                   '∩∪∈∉∀∃±×÷'
                   '√∞∑∏∫∇∂→≤')
    _is_maths = ('\\' in text or '_{' in text or '^{' in text
                 or any(c in text for c in _MATHS_MARK)
                 or any(c in text for c in SUB)
                 or any(c in text for c in SUP)
                 or any(c in text for c in GREEK))

    # Unicode subscripts: a base letter immediately followed by subscript chars.
    def _sub_repl(m):
        base = m.group(1)
        digits = ''.join(str(SUB.get(c, c)) for c in m.group(2))
        spoken = ' '.join(str(DIGIT_WORD.get(d, d)) for d in digits)
        # Trailing space: without it a following letter fuses on (H₂O → "H twoO").
        return f'{base} {spoken} '
    text = re.sub(r'([A-Za-z])([₀-₉ₐₑₓₕₖₗₘₙₚₛₜ]+)', _sub_repl, text)

    # Superscripts: "squared"/"cubed" read naturally; others "to the power of".
    def _sup_repl(m):
        base = m.group(1)
        val = ''.join(str(SUP.get(c, c)) for c in m.group(2))
        if val == '2':
            return f'{base} squared '
        if val == '3':
            return f'{base} cubed '
        spoken = ' '.join(str(DIGIT_WORD.get(d, d)) for d in val)
        return f'{base} to the power of {spoken} '
    text = re.sub(r'([A-Za-z0-9])([⁰-⁹ⁿⁱ¹²³]+)', _sup_repl, text)

    # Prime notation in calculus: f'(x) → "f prime of x", f''(x) → "f double
    # prime of x". Deliberately requires a following '(' so English apostrophes
    # ("don't", "the model's") can never match.
    text = re.sub(r"\b([A-Za-z])''\s*\(", r"\1 double prime of (", text)
    text = re.sub(r"\b([A-Za-z])'\s*\(",  r"\1 prime of (", text)

    # Greek letters.
    for g, name in GREEK.items():
        text = text.replace(g, f' {name} ')

    # Logic, set and maths operators, read as words so equations like
    # "A_o ∧ R ⇒ A_w" become "A o and R implies A w" instead of being dropped.
    # One entry per symbol. This table previously listed ∪ ∩ ⊂ ⊆ ≡ twice with
    # different readings; the later literal silently won, so five symbols were
    # spoken with the wording the author had already rejected.
    OPS = {
        '⇒':' implies ', '⇔':' if and only if ', '→':' goes to ', '↔':' if and only if ',
        '∧':' and ', '∨':' or ', '¬':' not ', '⊕':' exclusive or ',
        '∀':' for all ', '∃':' there exists ', '∈':' in ', '∉':' not in ',
        '⊆':' is a subset of or equal to ', '⊂':' is a subset of ',
        '∪':' union ', '∩':' intersection ',
        '≤':' less than or equal to ', '≥':' greater than or equal to ',
        '≠':' not equal to ', '≈':' approximately ', '×':' times ', '÷':' divided by ',
        '±':' plus or minus ', '√':' square root of ', '∞':' infinity ',
        '∂':' partial ', '∇':' del ', '∝':' is proportional to ',
        '≡':' is equivalent to ', '∅':' the empty set ',
        '∴':' therefore ', '∵':' because ',
    }
    for sym, word in OPS.items():
        text = text.replace(sym, word)

    # --- LaTeX equation source → spoken maths ---
    # Selections yield LaTeX for rendered equations, since MathJax and KaTeX both
    # embed the source, so I expand it into clear teacher-style speech before the
    # simpler subscript rules below, which would otherwise mangle braced groups
    # like X_{t+1}.
    #
    # The rule that matters most is at the end: an unrecognised command is read
    # rather than deleted. Deleting it used to turn "I don't support this" into
    # "quietly wrong meaning", and that is much worse, because a reading you can
    # hear is wrong is fixable while a silent one is not. P(A \cap B) became
    # "P(A B)" and lost the intersection, and \frac{\partial L}{\partial w} became
    # "L over w", which is no longer a derivative at all.
    if "\\" in text or "_{" in text or "^{" in text:
        # Environments and layout commands carry nothing when spoken, so they go
        # before anything tries to read them as words.
        text = re.sub(r"\\(?:begin|end)\s*\{[^{}]*\}", " ", text)
        text = re.sub(r"\\(?:left|right|middle|big{1,2}|Big{1,2}|quad|qquad|"
                      r"displaystyle|textstyle|limits|nolimits)(?![A-Za-z])", " ", text)
        text = re.sub(r"\\[,;:!> ]", " ", text)          # thin spaces and \!
        # Escaped punctuation is literal, and has to survive the command sweep.
        for esc, lit in ((r"\\%", "%"), (r"\\&", " and "), (r"\\\$", "$"),
                         (r"\\#", "#"), (r"\\\{", "("), (r"\\\}", ")")):
            text = re.sub(esc, lit, text)
        # Wrappers exist to change the font, so the contents are what gets read.
        for _ in range(3):
            new = re.sub(r"\\(?:text|textrm|textbf|textit|textsf|texttt|mathrm|"
                         r"mathbf|mathbb|mathcal|mathit|mathsf|boldsymbol|"
                         r"operatorname)\s*\{([^{}]*)\}", r" \1 ", text)
            if new == text:
                break
            text = new

        _GREEK = {
            "alpha": "alpha", "beta": "beta", "gamma": "gamma", "delta": "delta",
            "epsilon": "epsilon", "varepsilon": "epsilon", "zeta": "zeta",
            "eta": "eta", "theta": "theta", "vartheta": "theta", "iota": "iota",
            "kappa": "kappa", "lambda": "lambda", "mu": "mu", "nu": "nu",
            "xi": "xi", "omicron": "omicron", "pi": "pi", "varpi": "pi",
            "rho": "rho", "varrho": "rho", "sigma": "sigma", "varsigma": "sigma",
            "tau": "tau", "upsilon": "upsilon", "phi": "phi", "varphi": "phi",
            "chi": "chi", "psi": "psi", "omega": "omega",
            # Capitals read as "capital X", since Delta especially changes the
            # meaning and "delta x" would hide that.
            "Gamma": "capital gamma", "Delta": "delta", "Theta": "capital theta",
            "Lambda": "capital lambda", "Xi": "capital xi", "Pi": "capital pi",
            "Sigma": "capital sigma", "Upsilon": "capital upsilon",
            "Phi": "capital phi", "Psi": "capital psi", "Omega": "capital omega",
        }
        # Operators and relations. Every one of these used to fall through to the
        # catch-all and disappear, which is how an intersection became a space.
        _CMD = {
            "times": " times ", "div": " divided by ", "cdot": " times ",
            "ast": " times ", "star": " star ", "circ": " composed with ",
            "pm": " plus or minus ", "mp": " minus or plus ",
            "leq": " is less than or equal to ", "le": " is less than or equal to ",
            "geq": " is greater than or equal to ", "ge": " is greater than or equal to ",
            "neq": " is not equal to ", "ne": " is not equal to ",
            "ll": " is much less than ", "gg": " is much greater than ",
            "approx": " is approximately ", "simeq": " is approximately ",
            "cong": " is congruent to ", "equiv": " is equivalent to ",
            "sim": " is distributed as ", "propto": " is proportional to ",
            "cap": " intersection ", "cup": " union ",
            "bigcap": " the intersection of ", "bigcup": " the union of ",
            "setminus": " without ", "emptyset": " the empty set ",
            "varnothing": " the empty set ",
            "in": " in ", "notin": " is not in ", "ni": " contains ",
            "subset": " is a subset of ", "subseteq": " is a subset of or equal to ",
            "supset": " is a superset of ", "supseteq": " is a superset of or equal to ",
            "forall": " for all ", "exists": " there exists ",
            "nexists": " there is no ", "neg": " not ", "lnot": " not ",
            "land": " and ", "wedge": " and ", "lor": " or ", "vee": " or ",
            "oplus": " exclusive or ", "otimes": " tensor ",
            "to": " goes to ", "rightarrow": " goes to ", "leftarrow": " comes from ",
            "mapsto": " maps to ", "implies": " implies ",
            "Rightarrow": " implies ", "Leftarrow": " is implied by ",
            "iff": " if and only if ", "Leftrightarrow": " if and only if ",
            "leftrightarrow": " if and only if ",
            "infty": " infinity ", "partial": " partial ", "nabla": " gradient ",
            "sum": " the sum of ", "prod": " the product of ", "int": " the integral of ",
            "oint": " the contour integral of ", "iint": " the double integral of ",
            "lim": " the limit of ", "max": " max ", "min": " min ",
            "sup": " the supremum of ", "inf": " the infimum of ",
            "arg": " arg ", "argmax": " arg max ", "argmin": " arg min ",
            "log": " log ", "ln": " natural log ", "exp": " e to the power of ",
            "sin": " sine ", "cos": " cosine ", "tan": " tangent ",
            "det": " the determinant of ", "dim": " the dimension of ",
            "deg": " degree ", "gcd": " the greatest common divisor of ",
            "bmod": " mod ", "mod": " mod ", "pmod": " mod ",
            "perp": " is perpendicular to ", "parallel": " is parallel to ",
            "angle": " angle ", "triangle": " triangle ",
            "ldots": " and so on ", "cdots": " and so on ", "dots": " and so on ",
            "vdots": " and so on ", "prime": " prime ",
            "hat": " hat ", "bar": " bar ", "vec": " vector ", "tilde": " tilde ",
            "dot": " dot ", "ddot": " double dot ", "overline": " bar ",
            "underline": " underlined ", "binom": " choose ", "choose": " choose ",
            "lfloor": " the floor of ", "rfloor": " ", "lceil": " the ceiling of ",
            "rceil": " ", "vert": " ", "Vert": " the norm of ",
            "mathhyphen": "-",
        }

        # Sums and integrals read far better with their limits spoken as limits
        # rather than as a subscript, and this is the commonest shape on a maths
        # or machine-learning page.
        def _limits(m):
            word = {"sum": "the sum", "prod": "the product",
                    "int": "the integral", "lim": "the limit"}[m.group(1)]
            lo, hi = m.group(2), m.group(3)
            if m.group(1) == "lim":
                return f" {word} as {_spoken_group(lo)} of "
            return f" {word} from {_spoken_group(lo)} to {_spoken_group(hi)} of "

        def _spoken_group(g):
            """Read a sub/superscript group aloud, operators included.

            A leading minus is the case that bit me: e^{-\\lambda t} left the sign
            as a bare hyphen, which a later cleanup step removed, so the exponent
            silently changed sign."""
            g = g.strip()
            g = re.sub(r"^[-\u2212]\s*", " minus ", g)
            g = g.replace("+", " plus ").replace("\u2212", " minus ")
            g = re.sub(r"(?<=[A-Za-z0-9\s])-(?=[A-Za-z0-9])", " minus ", g)
            g = g.replace("=", " equals ")
            return re.sub(r"\s{2,}", " ", g).strip()

        # Fractions, innermost first. Iterating handles one or two levels of
        # nesting, which is as deep as prose maths tends to go.
        for _ in range(4):
            new = re.sub(r"\\frac\s*\{([^{}]*)\}\s*\{([^{}]*)\}",
                         lambda m: f" {_spoken_group(m.group(1))} over "
                                   f"{_spoken_group(m.group(2))} ", text)
            if new == text:
                break
            text = new
        text = re.sub(r"\\sqrt\s*\[\s*([^\]]*)\s*\]\s*\{([^{}]*)\}",
                      lambda m: f" the {_spoken_group(m.group(1))} root of "
                                f"{_spoken_group(m.group(2))} ", text)
        text = re.sub(r"\\sqrt\s*\{([^{}]*)\}",
                      lambda m: f" the square root of {_spoken_group(m.group(1))} ", text)

        # Limits before the generic sub/superscript rules, so the two parts are
        # still adjacent and recognisable as a pair.
        text = re.sub(r"\\(sum|prod|int|lim)\s*_\s*\{([^{}]*)\}\s*\^\s*\{([^{}]*)\}", _limits, text)
        text = re.sub(r"\\(sum|prod|int|lim)\s*_\s*\{([^{}]*)\}\s*\^\s*(\\?[A-Za-z0-9]+)", _limits, text)
        text = re.sub(r"\\(sum|prod|int)\s*_\s*([A-Za-z0-9])\s*\^\s*(\\?[A-Za-z0-9]+)", _limits, text)
        text = re.sub(r"\\(lim)\s*_\s*\{([^{}]*)\}()", _limits, text)

        for name, spoken in _CMD.items():
            text = re.sub(r"\\" + name + r"(?![A-Za-z])", spoken, text)
        for name, spoken in _GREEK.items():
            text = re.sub(r"\\" + name + r"(?![A-Za-z])", " " + spoken + " ", text)

        # Braced sub/superscripts: X_{t+1} → "X sub t plus 1"; x^{2} → "x squared".
        text = re.sub(r"\^\s*\{\s*2\s*\}", " squared ", text)
        text = re.sub(r"\^\s*\{\s*3\s*\}", " cubed ", text)
        text = re.sub(r"\^\s*\{([^{}]*)\}",
                      lambda m: f" to the power of {_spoken_group(m.group(1))} ", text)
        text = re.sub(r"_\s*\{([^{}]*)\}",
                      lambda m: f" sub {_spoken_group(m.group(1))} ", text)
        # Unbraced ones, which are just as common and used to be spoken as the
        # literal characters: \int_0^\infty read as "underscore zero caret".
        text = re.sub(r"\^\s*2(?![0-9.])", " squared ", text)
        text = re.sub(r"\^\s*3(?![0-9.])", " cubed ", text)
        text = re.sub(r"\^\s*([-\u2212]?[A-Za-z0-9]+)",
                      lambda m: f" to the power of {_spoken_group(m.group(1))} ", text)
        text = re.sub(r"_\s*([A-Za-z0-9]+)",
                      lambda m: f" sub {_spoken_group(m.group(1))} ", text)

        # Anything still carrying a backslash is a command I have no reading for,
        # so it is read as its own name. That keeps an unsupported command audible
        # instead of letting it change the meaning on the way past.
        text = re.sub(r"\\([A-Za-z]+)", r" \1 ", text)
        text = re.sub(r"\\(.)", r" \1 ", text)          # stray escaped symbol
        text = text.replace("{", " ").replace("}", " ")
        text = re.sub(r"\s{2,}", " ", text).strip()

    # Underscore subscripts common in logic notation: A_o, B_w, C_o → "A o", "B w".
    # Single letter, underscore, then a short subscript (letters/digits).
    def _us_repl(m):
        base, sub = m.group(1), m.group(2)
        spoken = ' '.join(str(DIGIT_WORD.get(c, c)) for c in sub)
        return f'{base} {spoken}'
    text = re.sub(r'(?<![A-Za-z0-9])([A-Za-z])_([A-Za-z0-9]{1,3})(?![A-Za-z0-9])', _us_repl, text)

    # ASCII letter+digit maths variables: A0, h1, h2 → "A zero", "h one".
    # Only single letter + 1-2 digits, and not part of a longer alnum token
    # (so "MP3" or "COM00143M" aren't touched, since those are handled elsewhere).
    def _var_repl(m):
        letter, digits = m.group(1), m.group(2)
        spoken = ' '.join(str(DIGIT_WORD.get(d, d)) for d in digits)
        return f'{letter} {spoken}'
    text = re.sub(r'(?<![A-Za-z0-9])([A-Za-z])([0-9]{1,2})(?![A-Za-z0-9])', _var_repl, text)

    # Bare operators, in maths only. XTTS says nothing at all for a lone "=", so
    # an equation without this loses the very thing that makes it an equation.
    if _is_maths:
        text = re.sub(r'(?<![=<>!+\-*/])=(?!=)', ' equals ', text)
        text = re.sub(r'\+', ' plus ', text)
        # A spaced hyphen between operands is a minus. Unspaced is left alone so
        # "well-known" and "state-of-the-art" keep their hyphens.
        text = re.sub(r'(?<=[A-Za-z0-9)])\s+-\s+(?=[A-Za-z0-9(])', ' minus ', text)
        text = re.sub(r'\s{2,}', ' ', text)

    return text


def expand_symbols(text):
    # --- Arithmetic operators between operands (read in written order) ---
    # Only fire when the operator sits between numbers, so prose hyphens
    # ("well-known"), paths ("a/b/c"), and dates ("2024-01") are left alone.
    # This makes "20 - 10/4" speak as "twenty minus ten divided by four".
    # x^2 / x^3 → squared / cubed (common case) before the generic power rule.
    text = re.sub(r'([A-Za-z0-9])\s*\^\s*2\b', r'\1 squared', text)
    text = re.sub(r'([A-Za-z0-9])\s*\^\s*3\b', r'\1 cubed', text)
    text = re.sub(r'(\d)\s*\^\s*(\d)', r'\1 to the power of \2', text)
    # Division between numbers: "10/4" → "10 divided by 4" (but not paths/URLs).
    text = re.sub(r'(?<![A-Za-z0-9/])(\d+(?:\.\d+)?)\s*/\s*(\d+(?:\.\d+)?)(?![A-Za-z0-9/])',
                  r'\1 divided by \2', text)
    # Subtraction/minus: only when SPACED ("20 - 10"). Unspaced "5-10" / "2-3"
    # is almost always a range, date, or section ref, so it's left untouched.
    text = re.sub(r'(\d+(?:\.\d+)?)\s+-\s+(\d+(?:\.\d+)?)', r'\1 minus \2', text)
    # Multiplication/addition between numbers, explicit spacing preserved.
    text = re.sub(r'(\d+(?:\.\d+)?)\s*\*\s*(\d+(?:\.\d+)?)', r'\1 times \2', text)
    text = re.sub(r'(\d+(?:\.\d+)?)\s*\+\s*(\d+(?:\.\d+)?)', r'\1 plus \2', text)

    text = text.replace('**=', ' to the power of equals ')
    text = text.replace('//=', ' floor divide equals ')
    text = text.replace('>>=', ' right shift equals ')
    text = text.replace('<<=', ' left shift equals ')
    text = text.replace('**',  ' to the power of ')
    text = text.replace('//',  ' floor divided by ')
    text = text.replace('!=',  ' is not equal to ')
    text = text.replace('==',  ' is equal to ')
    text = text.replace('>=',  ' is greater than or equal to ')
    text = text.replace('<=',  ' is less than or equal to ')
    text = text.replace('+=',  ' plus equals ')
    text = text.replace('-=',  ' minus equals ')
    text = text.replace('*=',  ' times equals ')
    text = text.replace('/=',  ' divide equals ')
    text = text.replace('%=',  ' modulo equals ')
    text = text.replace('&=',  ' bitwise and equals ')
    text = text.replace('|=',  ' bitwise or equals ')
    text = text.replace('^=',  ' bitwise xor equals ')
    text = text.replace('>>',  ' right shift ')
    text = text.replace('<<',  ' left shift ')
    text = text.replace('->',  ' arrow ')
    text = text.replace('=>',  ' arrow ')
    text = text.replace('&&',  ' and ')
    text = text.replace('||',  ' or ')
    # Language names before the operator, since C++ is not an increment and
    # "C increment" is a genuinely confusing thing to hear in a sentence about
    # programming languages.
    text = re.sub(r'\b([Cc]|[Gg])\+\+', r'\1 plus plus', text)
    text = text.replace('++',  ' increment ')
    text = text.replace('--',  ' decrement ')
    text = re.sub(r'\$(\d[\d,\.]*)', lambda m: m.group(1) + ' dollars ', text)
    text = re.sub(r'(?<![=<>!])=(?!=)', ' is set to ', text)
    text = text.replace('*', ' times ')
    text = text.replace('+', ' plus ')
    text = text.replace('%', ' percent ')
    text = text.replace('~', ' bitwise not ')
    text = re.sub(r'(?<![a-zA-Z0-9])&(?![a-zA-Z0-9])', ' bitwise and ', text)
    text = re.sub(r'(?<![a-zA-Z0-9])\|(?![a-zA-Z0-9])', ' bitwise or ', text)
    text = re.sub(r'(?<![a-zA-Z0-9])\^(?![a-zA-Z0-9])', ' bitwise xor ', text)
    text = text.replace('@', ' at ')
    text = text.replace('\\', ' backslash ')
    text = text.replace('>', ' greater than ')
    text = text.replace('<', ' less than ')
    return text

def clean_code_block(text):
    text = _RE_JUPYTER_IN.sub('Input.', text)
    text = _RE_JUPYTER_OUT.sub('Output.', text)
    text = _RE_JUPYTER_CONT.sub('', text)
    # Expand standalone code abbreviations first, so str becomes string, len
    # becomes length, idx becomes index and so on. Running this before the prefix
    # expander avoids double processing, since after this pass any surviving
    # "strName" is still a valid
    # compound for expand_variable_prefixes to split.
    text = expand_code_whole_words(text)
    text = expand_variable_prefixes(text)
    def replace_def(m):
        name = expand_camel_case(expand_snake_case(m.group(1)))
        args = [expand_camel_case(expand_snake_case(a.strip()))
                for a in m.group(2).split(',') if a.strip()]
        return (f'define function {name}, taking {", ".join(args)}. '
                if args else f'define function {name}. ')
    text = re.sub(r'\bdef\s+([a-zA-Z_]\w*)\s*\(([^)]*)\)\s*:', replace_def, text)
    text = re.sub(r'\bclass\s+([a-zA-Z_]\w*)\s*(?:\([^)]*\))?\s*:',
                  lambda m: f'define class {expand_camel_case(m.group(1))}. ', text)
    text = re.sub(r'\b([a-zA-Z_]\w*)\s*=(?!=)\s*',
                  lambda m: f'{expand_camel_case(expand_snake_case(m.group(1)))} is set to ', text)
    def replace_call(m):
        name = expand_camel_case(expand_snake_case(m.group(1)))
        args = [a.strip() for a in m.group(2).split(',') if a.strip()]
        return f'call {name} with {", ".join(args)}' if args else f'call {name}'
    text = re.sub(r'\b([a-zA-Z_]\w*)\s*\(([^)]*)\)', replace_call, text)
    text = expand_symbols(text)
    return text


# =============================================================================
# EMPHASIS: ALL-CAPS SHOUTING VERSUS ABBREVIATIONS
# =============================================================================

# Short English words that turn up in all-caps in headings and emphasis but
# aren't abbreviations, so they need lowercasing rather than reading as acronyms.
_CAPS_STOPWORDS = {'EVEN', 'ARE', 'WITH', 'YET', 'FULL', 'LIST', 'LIKE', 'PATH', 'OLD', 'STAY', 'HAD', 'KEPT', 'BY', 'SAID', 'GET', 'WAY', 'NOTE', 'SENT', 'FIVE', 'OPEN', 'KEEP', 'ALL', 'SHE', 'TWO', 'THEY', 'MANY', 'PLAY', 'LIVE', 'BEEN', 'THEN', 'GOT', 'DONE', 'HIS', 'VERY', 'MAIN', 'AT', 'TYPE', 'A', 'KNOW', 'HOME', 'UPON', 'FOUR', 'BUT', 'SIZE', 'ME', 'MOST', 'OK', 'WE', 'USED', 'ITS', 'TILL', 'PUTS', 'SLOW', 'FROM', 'UP', 'TO', 'SOON', 'DOWN', 'PART', 'SAVE', 'FOR', 'ALSO', 'MADE', 'OWN', 'OF', 'THE', 'TEXT', 'WHO', 'STOP', 'SEEN', 'PAST', 'OR', 'WALK', 'PLAN', 'IT', 'TRUE', 'IN', 'LAST', 'AN', 'SAY', 'WAS', 'MUST', 'JUST', 'REST', 'ONES', 'IS', 'DOES', 'WISH', 'RUN', 'SOME', 'WORK', 'SUCH', 'SIDE', 'OFF', 'OVER', 'PLUS', 'HAS', 'FREE', 'SELF', 'WORD', 'FIND', 'JOIN', 'LESS', 'US', 'THAT', 'NEED', 'HIM', 'THEM', 'TIME', 'HALF', 'WERE', 'NO', 'INTO', 'GO', 'EACH', 'MORE', 'RUNS', 'ANY', 'SET', 'CALL', 'COME', 'NAME', 'TOOK', 'BOTH', 'KNEW', 'SITE', 'END', 'WHY', 'MY', 'LET', 'CAME', 'YES', 'CASE', 'FEW', 'CAN', 'VAST', 'TAKE', 'HAND', 'NOW', 'SHOW', 'GIVE', 'LOOK', 'YEAR', 'NEW', 'TRY', 'RATE', 'THUS', 'NEXT', 'YOUR', 'WAIT', 'TALK', 'BE', 'HOW', 'LOST', 'AND', 'HERE', 'EVER', 'SURE', 'LOVE', 'DO', 'ON', 'MAKE', 'GONE', 'SEE', 'HER', 'TEST', 'VIEW', 'TELL', 'USE', 'TOO', 'SIGN', 'IF', 'THIS', 'GOES', 'LONG', 'HELP', 'MAY', 'ZONE', 'MUCH', 'DAY', 'HAVE', 'ROLE', 'ONLY', 'MEAN', 'WHEN', 'OUT', 'FAR', 'OUR', 'MOVE', 'WIDE', 'SAME', 'MIND', 'ONE', 'MAN', 'BACK', 'STEP', 'THAN', 'TOLD', 'SORT', 'DID', 'AS', 'GAVE', 'WHAT', 'I', 'TASK', 'YOU', 'SO', 'HARD', 'ITEM', 'PUT', 'AWAY', 'HOLD', 'RULE', 'HIGH', 'LIFE', 'NOT', 'WILL', 'ELSE', 'ONCE', 'GOOD', 'WELL', 'TURN', 'REAL', 'TREE', 'WANT', 'SAW', 'MEN', 'READ'}

_RE_CAPS_WORD = re.compile(r'\b[A-Z]{3,}\b')
# Beyond this length an all-caps run is emphasis, not an acronym. Real
# abbreviations that are longer (and that XTTS gets wrong) are covered by an
# explicit pronunciation-store entry, which is checked first.
_CAPS_ACRONYM_MAX = 5


def normalise_shouting(text):
    """Lowercase all-caps runs that are emphasis rather than abbreviations.

    XTTS reads a long all-caps token as an unknown symbol and either spells it
    out oddly or skips it, so "this is IMPORTANT" has to become "this is
    important". I leave genuine short acronyms alone for step 12 of clean_text
    to resolve through render_abbreviation().

    I decide in this order, most authoritative first:
      1. In the pronunciation store, so it's been taught and I leave it alone.
      2. In _CAPS_STOPWORDS, so it's an ordinary English word being shouted.
      3. _CAPS_ACRONYM_MAX characters or fewer, so it could be an acronym and
         I leave it for step 12.
      4. Anything else is emphasis, so lowercase it.
    """
    store = _load_store()

    def _repl(m):
        word = m.group(0)
        if word in store:
            return word
        if word in _CAPS_STOPWORDS:
            return word.lower()
        if len(word) <= _CAPS_ACRONYM_MAX:
            return word
        return word.lower()

    return _RE_CAPS_WORD.sub(_repl, text)


# =============================================================================
# NUMBER NATURALISATION
# =============================================================================

def naturalise_numbers(text):
    # Negative numbers, which have to run before the degree and percent rules
    # consume the digit
    text = re.sub(r'(?<![\w])-(\d[\d,\.]*)',
                  lambda m: 'negative ' + m.group(1), text)
    text = re.sub(r'(\d[\d,\.]*)\s*%',    r'\1 percent', text)
    text = re.sub(r'(\d+)\s*°\s*C\b',     r'\1 degrees Celsius', text)
    text = re.sub(r'(\d+)\s*°\s*F\b',     r'\1 degrees Fahrenheit', text)
    text = re.sub(r'(\d+)\s*°',           r'\1 degrees', text)
    text = re.sub(
        r'\bv(\d+)\.(\d+)(?:\.(\d+))?\b',
        lambda m: 'version ' + '.'.join(x for x in [m.group(1), m.group(2), m.group(3)] if x),
        text)
    text = re.sub(r'\b(\d+)/(\d+)\b', r'\1 over \2', text)
    text = re.sub(
        r'(\d)(ms|kb|mb|gb|tb|hz|khz|mhz|ghz|px|pt|em|rem|fps|rpm|ns|us)\b',
        r'\1 \2', text, flags=re.IGNORECASE)
    return text


# =============================================================================
# HETEROPHONE RESOLUTION
# =============================================================================

def fix_heterophones(text):
    """
    Resolve English heterophones where context makes pronunciation unambiguous.

    "live" adjective/adverb (rhymes with five):
      live stream, live event, go live, broadcasting live, we are live.
    "live" verb (rhymes with give):
      people live, we live in, a place to live  ← unchanged
    """
    # Pattern A: "live" before a broadcast or event noun, lookahead only
    text = re.sub(
        r'\blive\b(?=\s+(?:stream|streaming|event|events|demo|demos|session|'
        r'sessions|performance|performances|broadcast|broadcasts|feed|feeds|'
        r'data|coding|interview|interviews|show|shows|concert|concerts|'
        r'recording|recordings|version|update|updates|preview|music|'
        r'chat|video|audio|coverage|footage|action|blog|test|testing)\b)',
        'lyve', text, flags=re.IGNORECASE
    )

    # Pattern B: a verb then "live", capturing the verb in group 1, no lookbehind
    text = re.sub(
        r'\b(go|goes|went|gone|streaming|broadcast|broadcasting|airing|aired|'
        r'presenting|presented|shown|showing)\s+live\b',
        lambda m: m.group(1) + ' lyve', text, flags=re.IGNORECASE
    )

    # Pattern C: a state verb then "live" at a sentence boundary, fixed width
    text = re.sub(
        r'\b(are|is|was|were|now|currently|still)\s+live\s*(?=[.,!?]|$)',
        lambda m: m.group(1) + ' lyve', text, flags=re.IGNORECASE
    )

    return text


# =============================================================================
# HALLUCINATION GUARD
# =============================================================================

_HALLUCINATION_TRIGGERS = re.compile(
    r'[\u0400-\u04FF]'       # Cyrillic
    r'|[\u0600-\u06FF]'      # Arabic
    r'|[\u4E00-\u9FFF]'      # CJK
    r'|[\u3040-\u30FF]'      # Hiragana / Katakana
    r'|[^\x00-\x7F]{3,}'     # 3+ consecutive non-ASCII
    r'|<[^>]{0,40}>',        # Residual HTML tags
    re.UNICODE
)
# A separate pattern for 5 or more repeated chars, applied once the ellipsis is
# protected
_REPEATED_CHARS = re.compile(r'(.)\1{4,}')

# Character limits. The hard cap is an XTTS constraint (longer inputs hit tensor
# reshape errors); the request cap is applied earlier, before cleaning, purely to
# stop absurd payloads walking the whole pipeline. Both live here so they cannot
# drift apart the way two bare literals in two functions did.
_XTTS_HARD_CHAR_CAP = 180
_MAX_CHUNK_CHARS    = 190

def guard_against_hallucination(text):
    """
    Strip patterns that cause XTTS to loop or emit gibberish.
    Runs as the very last step before model inference.
    Ellipsis is explicitly preserved because it is a valid prosody token.
    """
    # Preserve ellipsis before stripping repeated chars
    text = text.replace('...', '\x00ELLIPSIS\x00')

    text = _HALLUCINATION_TRIGGERS.sub(' ', text)
    text = _REPEATED_CHARS.sub(' ', text)          # 5+ repeated chars → space
    text = re.sub(r'[^\x20-\x7E\x00]', ' ', text)

    # Restore ellipsis
    text = text.replace('\x00ELLIPSIS\x00', '...')

    # Strip symbol-heavy clusters, meaning 3 or more non-word chars in a row
    # like "...---" or "///" or "***", since they confuse EOS detection
    text = re.sub(r'[^\w\s.,!?;:\'"()-]{3,}', ' ', text)

    # Strip any leading/trailing punctuation that would give XTTS
    # an ambiguous start or end token
    text = text.strip().lstrip('.,;:!?-').rstrip(',:;-')

    # Final defence: strip any semantic markers that survived all previous passes
    text = re.sub(r'\|/?(?:H[1-3]|BOLD|ITALIC|CODE|CALLOUT|CAPTION|LIST|BREAK)\|', ' ', text)
    text = re.sub(r'\|/?[A-Z][A-Z0-9/_]{1,15}\|', ' ', text)

    text = re.sub(r'\s+', ' ', text).strip()

    # Hard cap, which stops XTTS throwing tensor reshape errors
    if len(text) > _XTTS_HARD_CHAR_CAP:
        text = text[:_XTTS_HARD_CHAR_CAP].rsplit(' ', 1)[0].rstrip(',:;')

    # Ensure we still have something worth synthesising
    if len(text.strip()) < 3:
        return ''
    return text


# =============================================================================
# SEMANTIC MARKER DECODER
# =============================================================================
#
# popup.js injects semantic markers into the text stream to communicate
# formatting context that plain text cannot carry:
#
#   |H1|...|/H1|         top-level heading
#   |H2|...|/H2|         section heading
#   |H3|...|/H3|         sub-section / lower heading
#   |BOLD|...|/BOLD|     bold or strong text
#   |ITALIC|...|/ITALIC| italic or emphasis text
#   |CODE|...|/CODE|     inline code token
#   |CALLOUT|...|/CALLOUT| admonition / blockquote / note box
#   |CAPTION|...|/CAPTION| figure or table caption
#   |LIST|...|/LIST|       one item of a bulleted or numbered list
#
# Each marker is decoded here into a prosody-aware transformation:
#   Headings   → spoken with a clear pause before and after; sentence type
#                is pre-labelled so the prosody engine uses heading silence
#   Bold       → the word(s) are retained as-is; XTTS naturally stresses
#                shorter isolated runs, so no phonetic hack is needed
#   Italic     → soft-emphasis phrasing; we add a mild comma offset so the
#                model treats it as a parenthetical aside
#   Inline code→ passed through the code-block cleaner for symbol expansion
#   Callout    → prefixed with the admonition type ("Note.", "Warning.") and
#                set off with sentence breaks so it reads as a distinct aside
#   Caption    → prefixed with "Figure:" and treated as a definition
#   List item  → nothing is added to what gets said, since a bullet is not
#                spoken, it is paced. The marker only records that the chunk
#                was an item so the prosody engine can use the list gap
#
# In the normal reading path these wrappers never arrive, because background.js
# strips every marker before it posts the text and sends the structure it found
# as the "position" field instead. They are decoded here anyway, since custom
# text and anything that posts to /speak directly can still carry them, and a
# marker that reached XTTS would be read out as pipes and capitals.

# Admonition type sniffed from callout text
_ADMONITION_PATTERN = re.compile(
    r'^\s*(note|warning|tip|important|caution|example|info|danger|hint)\b',
    re.IGNORECASE
)

# Per-request structure the decoder discovers and the prosody engine needs.
# Flask serves requests on threads, so this must be thread-local: two chunks
# being cleaned concurrently must not see each other's heading level.
_structure = _threading.local()


def _reset_structure():
    """Start a fresh structure record for this request's chunk."""
    _structure.heading_level = None
    _structure.list_item     = False


def _detected_heading_level():
    """The heading level the marker decoder saw for this chunk, or None."""
    return getattr(_structure, "heading_level", None)


def _detected_list_item():
    """True when the marker decoder saw this chunk wrapped as a list item."""
    return bool(getattr(_structure, "list_item", False))


def _decode_heading(level: str, content: str) -> str:
    """
    Convert a heading marker into a spoken form with breaks that suit its level.
    H1 gets a double break for a major section boundary, H2 gets a double break
    as well since in practice it carries the same weight, and H3 gets a single
    break for a lighter sub-section pause.

    I also record the level so the prosody engine can label the chunk as
    h1_heading, h2_heading or h3_heading. The level used to survive only as
    |BREAK| markers, which a later belt-and-braces pass then stripped, so every
    heading whatever its depth was flattened to the generic 'heading' label and
    got the same pause. That made the h1, h2 and h3 entries in _SILENCE_MS
    unreachable.
    """
    _structure.heading_level = level.lower()   # 'h1' | 'h2' | 'h3'
    t = content.strip().rstrip('.')
    if level == 'H1':
        return f' |BREAK| |BREAK| {t}. |BREAK| |BREAK| '
    if level == 'H2':
        return f' |BREAK| |BREAK| {t}. |BREAK| '
    # H3 and below
    return f' |BREAK| {t}. |BREAK| '

def _decode_bold(content: str) -> str:
    return content.strip()

def _decode_italic(content: str) -> str:
    return content.strip()

def _decode_callout(content: str) -> str:
    """
    Callout blocks (notes, warnings, tips) should be read as distinct
    asides with a clear spoken label so the listener knows the register
    has changed. We prefix with the admonition type if detectable.
    """
    t = content.strip()
    m = _ADMONITION_PATTERN.match(t)
    if m:
        kind    = m.group(1).capitalize()
        body    = t[m.end():].strip().lstrip('.:- ')
        return f' |BREAK| {kind}. {body} |BREAK| '
    return f' |BREAK| Note. {t} |BREAK| '

def _decode_caption(content: str) -> str:
    """Figure and table captions, which I prefix with a spoken label."""
    t = content.strip()
    return f' Figure caption: {t} '

def _decode_list_item(content: str) -> str:
    """
    A bullet, which adds nothing to what is said and only changes the pacing.

    So I record the fact and hand the words back untouched. Saying the word
    "bullet" out loud would be worse than the run-together reading it replaces,
    and the item already got its full stop in _strip_list_markers.
    """
    _structure.list_item = True
    return f' {content.strip()} '

def decode_semantic_markers(text: str) -> str:
    """
    Decode all the semantic markers popup.js injects.

    This has to run before any other cleaning so the marker content goes through
    the right downstream pipeline, for instance inline code through the code
    cleaner. The order matters too: headings before bold and italic, so nested
    markers resolve cleanly.
    """
    # Headings
    text = re.sub(r'\|H1\|(.+?)\|/H1\|',
                  lambda m: _decode_heading('H1', m.group(1)), text, flags=re.DOTALL)
    text = re.sub(r'\|H2\|(.+?)\|/H2\|',
                  lambda m: _decode_heading('H2', m.group(1)), text, flags=re.DOTALL)
    text = re.sub(r'\|H3\|(.+?)\|/H3\|',
                  lambda m: _decode_heading('H3', m.group(1)), text, flags=re.DOTALL)

    # Callouts, before bold and italic since a callout can contain bold inside it
    text = re.sub(r'\|CALLOUT\|(.+?)\|/CALLOUT\|',
                  lambda m: _decode_callout(m.group(1)), text, flags=re.DOTALL)

    # Captions
    text = re.sub(r'\|CAPTION\|(.+?)\|/CAPTION\|',
                  lambda m: _decode_caption(m.group(1)), text, flags=re.DOTALL)

    # List items, before bold and italic since an item often has bold inside it
    text = re.sub(r'\|LIST\|(.+?)\|/LIST\|',
                  lambda m: _decode_list_item(m.group(1)), text, flags=re.DOTALL)

    # Bold
    text = re.sub(r'\|BOLD\|(.+?)\|/BOLD\|',
                  lambda m: _decode_bold(m.group(1)), text, flags=re.DOTALL)

    # Italic
    text = re.sub(r'\|ITALIC\|(.+?)\|/ITALIC\|',
                  lambda m: _decode_italic(m.group(1)), text, flags=re.DOTALL)

    # Inline code → pass through code cleaner
    text = re.sub(r'\|CODE\|(.+?)\|/CODE\|',
                  lambda m: ' ' + clean_code_block(m.group(1)) + ' ', text, flags=re.DOTALL)

    # --- Strip any markers left over, just to be safe ---
    # Handles malformed, nested, or partially-matched markers that slipped
    # through the decode passes above. None of these can reach XTTS, because it
    # will read the pipe characters and tag names out literally.
    #
    # Pass 1: any remaining complete marker pair with content
    text = re.sub(r'\|(?:H[1-3]|BOLD|ITALIC|CODE|CALLOUT|CAPTION|LIST)\|.*?\|/(?:H[1-3]|BOLD|ITALIC|CODE|CALLOUT|CAPTION|LIST)\|',
                  ' ', text, flags=re.DOTALL)
    # Pass 2: any remaining lone opening or closing tag
    text = re.sub(r'\|/?(?:H[1-3]|BOLD|ITALIC|CODE|CALLOUT|CAPTION|LIST|BREAK)\|', ' ', text)
    # Pass 3: any remaining pipe-word-pipe pattern (catches unknown future markers)
    # The /? handles both opening |WORD| and closing |/WORD| forms.
    text = re.sub(r'\|/?[A-Z][A-Z0-9/_]{1,15}\|', ' ', text)
    # Pass 4: orphaned pipe characters next to whitespace
    text = re.sub(r'\s\|\s', ' ', text)

    text = re.sub(r'\s+', ' ', text).strip()
    # Apply learned rules from quality monitor (blacklist, pronunciation
    # overrides, punctuation corrections and suppressions). This is the only
    # place they run, since clean_text used to apply them a second time, which
    # double-counted every rule hit and inserted PUNCT and SPLIT corrections
    # twice over.
    #
    # A whole-chunk SUPPRESS rule returns SUPPRESS_SENTINEL. It is passed
    # straight back to clean_text, which passes it to speak(), which emits
    # silence. The sentinel used to be returned into the middle of the cleaning
    # pipeline where nothing recognised it, so a user's "skip this" ended up
    # having XTTS read the word "SUPPRESS" aloud.
    return _learner.apply_learned_rules(text)


def _strip_list_markers(text):
    """Remove list and bullet markers, judging each one by its line.

    A marker opens an item, so it sits at the head of a line. Deciding that from
    the line is the only reliable way, because once the newlines are folded into
    spaces "b)" starting an item and "b)" inside a sentence are the same three
    characters, and a rule loose enough to catch the first silently eats the
    second along with its bracket.

    A newline is not the only boundary. popup.js has li in its block tags, so
    what actually arrives from a page is items separated by |BREAK| markers, and
    splitting on newlines alone missed every one of them: a numbered list came
    through with "1." and "2." still in it, which then got read out as numbers.
    |BREAK| means a block boundary, so it counts as the head of a line here.

    |LIST| opens an item by definition, so it counts too, and its closing tag
    ends one. Without the closing tag as a boundary the item body would run up
    to the "|" of "|/LIST|", and the full stop this adds would land after the
    marker rather than after the words.

    Each item also gets a full stop if it has no terminal punctuation of its own.
    Otherwise the markers go, |BREAK| collapses to a space, and the items run
    together into one long sentence with no pause between them, which is both
    hard to follow and wrong: bullet items are separate thoughts."""
    _BOUNDARIES = ("\n", "|BREAK|", "|LIST|", "|/LIST|")
    parts = re.split(r'(\|BREAK\||\|/?LIST\||\n)', text)
    at_boundary = True
    for k, part in enumerate(parts):
        if part in _BOUNDARIES:
            at_boundary = True
            continue
        if not at_boundary:
            continue
        at_boundary = False
        stripped = part.lstrip()
        pad = part[:len(part) - len(stripped)]
        # A digit or letter marker, so "1." / "2)" / "a)" / "B."
        m = re.match(r'(?:\d{1,3}|[a-zA-Z])[.)]\s+(?=\S)', stripped)
        if not m:
            # Bullet glyphs, including a hyphen or asterisk used as one. A dash
            # needs the trailing space, so a negative number is left alone.
            m = re.match(r'[•‣⁃▪▫◦∙·]\s*(?=\S)|[–—\-\*]\s+(?=\S)', stripped)
        if not m:
            continue
        stripped = stripped[m.end():]
        # Give the item an ending so it stays a separate thought once the
        # boundary marker itself collapses to whitespace.
        body = stripped.rstrip()
        if body and body[-1] not in '.!?;:,':
            trail = stripped[len(body):]
            stripped = body + '.' + trail
        parts[k] = pad + stripped
    return "".join(parts)


def clean_text(text):
    # Per-chunk structure record (heading level, …) starts empty; the marker
    # decoder fills it in as it recognises wrappers.
    _reset_structure()

    # 0-pre. List markers, while the line breaks that identify them still exist.
    #        The marker decoder folds newlines into spaces, so this cannot wait
    #        until the old step 6: by then "1." at the head of a line and "b)" in
    #        the middle of a sentence look identical, and matching both is how
    #        P(A ∩ B) lost its closing bracket and a spaced minus disappeared out
    #        of an exponent. Anchoring to the line is the whole point.
    text = _strip_list_markers(text)

    # 0. Decode the semantic markers, which has to run before everything else so
    #    that heading, bold, italic and code content gets routed through the
    #    appropriate downstream transformations.
    text = decode_semantic_markers(text)
    if text == _learner.SUPPRESS_SENTINEL:
        return text     # caller emits silence; nothing to clean

    # 1. All-caps emphasis down to lowercase, real acronyms survive to step 12
    text = normalise_shouting(text)

    # 1a-math. User-taught equation readings (from EQUATION reports) run FIRST,
    # so an explicitly corrected expression always beats the generic rules.
    text = apply_math_rules(text)

    # 1b. Mathematical notation, meaning subscripts, superscripts and
    # letter-plus-digit variables like A₀, h², A0 and h1, so that equations get
    # spoken rather than skipped.
    text = expand_math_symbols(text)

    # 2. Raw HTML tags (anything the extractor missed)
    text = _RE_HTML_CLOSE.sub(' ', text)
    text = _RE_HTML_OPEN.sub(' ', text)

    # 3. Fenced + inline code blocks
    text = re.sub(r'```(?:\w+)?\n?(.*?)```',
                  lambda m: clean_code_block(m.group(1)) + ' ', text, flags=re.DOTALL)
    text = re.sub(r'`([^`]+)`', lambda m: clean_code_block(m.group(1)), text)

    # 3b. Dot-separated namespace/package paths → spoken words
    # e.g. Azure.AI.Vision.Face → Azure AI Vision Face
    #      azure.ai.vision.imageanalysis → azure ai vision imageanalysis
    # Only expand when segments look like identifiers (not decimal numbers)
    def _expand_dot_path(m):
        parts = m.group(0).split('.')
        # If any part is purely numeric, leave it (decimal number)
        if any(p.isdigit() for p in parts):
            return m.group(0)
        # Expand each PascalCase/camelCase part
        spoken = []
        for p in parts:
            # PascalCase split: ImageAnalysis → Image Analysis
            p2 = re.sub(r'([a-z])([A-Z])', r'\1 \2', p)
            p2 = re.sub(r'([A-Z]+)([A-Z][a-z])', r'\1 \2', p2)
            spoken.append(p2)
        return ' '.join(spoken)

    # Match identifier.identifier chains (2+ segments, no spaces)
    text = re.sub(
        r'\b([A-Za-z][A-Za-z0-9]*)(?:\.[A-Za-z][A-Za-z0-9]*){1,}\b',
        _expand_dot_path, text
    )

    # 4. Jupyter prompts
    text = _RE_JUPYTER_IN.sub('Input.', text)
    text = _RE_JUPYTER_OUT.sub('Output.', text)
    text = _RE_JUPYTER_CONT.sub('', text)

    # 5. Hyperlink noise, keeping the visible word and stripping the annotation
    #    Markdown: [NumPy Documentation](url) → "NumPy Documentation"
    text = _RE_MARKDOWN_LNK.sub(r'\1', text)
    text = _RE_URL.sub('', text)
    #    "opens in new tab/window", which is the noise rather than the link text
    text = re.sub(r'\(?\s*opens?\s+in\s+(a\s+)?new\s+(tab|window)\s*\)?', '', text, flags=re.IGNORECASE)
    text = re.sub(r'\(?\s*external\s+link\s*\)?', '', text, flags=re.IGNORECASE)
    #    "Links to an external site", the sentence fragment that turns up next
    #    to linked text on LMS platforms like Canvas, Moodle and Blackboard
    text = re.sub(r'[\.,]?\s*[Ll]inks?\s+to\s+(an?\s+)?(external\s+site|pdf|document|page)[\.,]?', '', text, flags=re.IGNORECASE)
    text = re.sub(r'\(?\s*[Ll]inks?\s+to\s+(an?\s+)?(external\s+site|pdf|document|page)\s*\)?', '', text, flags=re.IGNORECASE)
    #    Residual UI labels
    text = re.sub(r'\b(download\s+pdf|view\s+pdf|open\s+pdf|read\s+more|learn\s+more|see\s+more|back\s+to\s+top|skip\s+to\s+content|click\s+here)\b', '', text, flags=re.IGNORECASE)
    text = re.sub(r'\[\s*(pdf|doc|docx|xlsx|download)\s*\]', '', text, flags=re.IGNORECASE)

    # 6. Bullet glyphs that survived, which happens when a list was written as a
    #    run on one line. Only the unambiguous glyphs are safe here, since the
    #    newlines are gone by now and a bare letter or digit would take real
    #    words with it. The line-anchored stripping happens in step 0-pre.
    text = re.sub(r'\s*[\u2022\u2023\u2043]\s*', '. ', text)
    text = re.sub(r':\s*\n', '. ', text)

    # 7. Reference / nav noise
    text = _RE_FOOTNOTE.sub('', text)
    text = _RE_HASH_FRAG.sub('', text)
    text = re.sub(r'\bPrevious\b', '', text)
    text = re.sub(r'\bNext\b', '', text)
    text = re.sub(r'Please click.*?to continue\.', '', text, flags=re.IGNORECASE)
    text = re.sub(r'The following video demonstrates.*?\.', '', text, flags=re.IGNORECASE)
    text = re.sub(r'Please watch.*?\.', '', text, flags=re.IGNORECASE)
    text = re.sub(r'Click here.*?\.', '', text, flags=re.IGNORECASE)

    # 7b. Latin abbreviations and common shorthand
    # These appear as plain text and XTTS reads them letter-by-letter or stumbles.
    text = re.sub(r'\be\.g\.(?:\s*,)?', 'for example,', text, flags=re.IGNORECASE)
    text = re.sub(r'\bi\.e\.(?:\s*,)?', 'that is,', text, flags=re.IGNORECASE)
    text = re.sub(r'\bvs\.', 'versus', text, flags=re.IGNORECASE)
    text = re.sub(r'\betc\.', 'and so on', text, flags=re.IGNORECASE)
    text = re.sub(r'\bapprox\.', 'approximately', text, flags=re.IGNORECASE)
    text = re.sub(r'\bca\.', 'approximately', text, flags=re.IGNORECASE)
    text = re.sub(r'\bcf\.', 'compare', text, flags=re.IGNORECASE)
    text = re.sub(r'\bno\.', 'number', text, flags=re.IGNORECASE)
    text = re.sub(r'\bviz\.', 'namely', text, flags=re.IGNORECASE)
    text = re.sub(r'\bfig\.', 'figure', text, flags=re.IGNORECASE)
    text = re.sub(r'\bsec\.', 'section', text, flags=re.IGNORECASE)
    text = re.sub(r'\bch\.', 'chapter', text, flags=re.IGNORECASE)
    text = re.sub(r'\bpp\.', 'pages', text, flags=re.IGNORECASE)
    text = re.sub(r'\bp\.(?=\s*\d)', 'page ', text, flags=re.IGNORECASE)


    # 8-11: Conditionally run heavy technical passes.
    # For plain prose these do nothing but still cost time, so I skip them when
    # the text has no code, identifiers, symbols or abbreviations left in it.
    _needs_code_passes = (
        any(c in text for c in '`_*')
        or bool(re.search(r'[A-Z]{2,}', text))
        or bool(re.search(r'[a-z][A-Z]', text))
        or any(op in text for op in ('==','!=','>=','<=','->','=>','++','--'))
    )
    # 8. Complex word phonetics, which always run since these are natural
    #    English mispronunciations rather than code artefacts.
    text = expand_complex_words(text)
    if _needs_code_passes:
        # 9. Variable prefix expansion
        text = expand_variable_prefixes(text)
        # 10. Symbol expansion
        text = expand_symbols(text)
        # 11. camelCase / snake_case
        text = _RE_CAMEL.sub(lambda m: expand_camel_case(m.group(1)), text)
        text = _RE_SNAKE.sub(lambda m: m.group(0).replace('_', ' '), text)

    # 12. Abbreviation expansion through the adaptive pronunciation system
    # render_abbreviation() checks: user store → word table → spell table → auto
    # Handles plurals: "GPUs" → spoken form + "s", "API's" → spoken + "'s"
    def _abbrev_sub(m):
        token  = m.group(1)
        suffix = m.group(2) or ''
        spoken = render_abbreviation(token)
        if suffix.lower() == 's':
            return spoken + 's'
        if suffix == "'s":
            return spoken + "'s"
        return spoken

    # Match: all-caps token (2+ chars, optional digit suffix), optional plural/possessive
    # Longest-first implicit in regex since it's greedy
    text = _RE_ABBREV.sub(_abbrev_sub, text)

    # NOTE: learned rules are applied exactly ONCE per chunk, in
    # decode_semantic_markers (step 0). A second pass used to run here, which
    # double-counted every rule's hit tally, and worse than that applied PUNCT
    # and SPLIT rules twice, so the corrected fragment went into the text two
    # times over.

    # 13. Numbers and units
    text = naturalise_numbers(text)

    # 13b. Context-sensitive heterophone fixes
    text = fix_heterophones(text)

    # 14. Final whitespace and punctuation cleanup
    text = _RE_BLANK_LINES.sub(' ', text)
    text = _RE_WHITESPACE.sub(' ', text)
    # Clean punctuation artefacts that cause XTTS hesitation
    text = _RE_PUNCT_DBL_CM.sub(',', text)
    text = _RE_PUNCT_DOTCM.sub('.', text)
    text = _RE_PUNCT_CMDOT.sub('.', text)
    text = _RE_PUNCT_SPCPNC.sub(r'\1', text)
    text = _RE_WHITESPACE.sub(' ', text)
    return text.strip()


# =============================================================================
# PROSODY / PAUSE SHAPING
# =============================================================================

def add_natural_pauses(text):
    # Em/en dash → comma (brief breath between clauses)
    text = _RE_DASH_PAIR.sub(', ', text)

    # Keep the ellipsis exactly as it is, since XTTS reads it as a pause token.
    text = _RE_ELLIPSIS.sub('... ', text)

    # Semicolons and colons: normalise spacing only
    text = _RE_SEMICOLON.sub('; ', text)
    text = _RE_COLON.sub(': ', text)

    # Parentheticals, of which there are two cases:
    # 1. Imperative/reference parentheticals → drop entirely (don't speak them)
    #    (see X), (cf. X), (refer to X), (note: X), (figure N), (table N)
    text = re.sub(
        r'\(\s*(?:see|cf\.?|refer\s+to|compare|note:|figure|fig\.?|table|'
        r'tab\.?|eq\.?|equation|appendix|section|sec\.?|chapter|ch\.?|'
        r'above|below|left|right|ibid|op\.?\s*cit)[^)]{0,60}\)',
        '', text, flags=re.IGNORECASE
    )
    # 2. Content parentheticals → spoken as a comma aside
    text = re.sub(r'\(([^)]{3,80})\)', r', \1', text)

    # Standalone dash (not a hyphen in a word like well-known)
    text = _RE_LONE_DASH.sub(', ', text)

    # Comma normalisation, so a single space after each comma
    text = _RE_COMMA_NORM.sub(', ', text)

    # Sentence-terminal spacing
    text = _RE_SENT_TERM.sub(r'\1 ', text)

    # Paragraph / line breaks
    text = _RE_PARABR.sub('. ', text)
    text = _RE_NEWLINE.sub(' ', text)

    # Clean doubled punctuation artefacts
    text = _RE_DOUBLE_CM.sub(',', text)
    text = _RE_DOUBLE_DOT.sub('.', text)
    text = _RE_WHITESPACE.sub(' ', text)
    return text.strip()


# =============================================================================
# ADAPTIVE PROSODY ENGINE
# =============================================================================
#
# Two passes run in sequence on every chunk before it reaches XTTS, over a
# SINGLE spaCy parse of the chunk.
#
#   Pass 1, sentence type detection.
#     Identifies the communicative function of the chunk and ensures the
#     correct terminal punctuation is present so XTTS interprets the tone
#     correctly. Punctuation heuristics are decided first, then the dependency
#     parse overrides them where grammar is the better evidence (fragment vs
#     sentence, imperative vs declarative, interrogative without a "?"). It also
#     recognises pre-labelled callout, caption and heading-level chunks handed
#     over by the semantic marker decoder.
#
#   Pass 2, context-aware silence classification.
#     Returns a ProsodicContext with the inter-chunk silence duration
#     calibrated to content type, not just the terminal punctuation mark.
# =============================================================================

import dataclasses

@dataclasses.dataclass
class ProsodicContext:
    """
    Carries all prosodic metadata for a single chunk through the pipeline.

    Attributes
    ----------
    sentence_type : str
        Communicative function of the chunk. One of SENTENCE_TYPES, i.e.
        'h1_heading', 'h2_heading', 'h3_heading', 'heading', 'callout',
        'caption', 'list_item', 'sentence', 'definition', 'question',
        'exclamation', 'imperative', 'parenthetical', 'mid_clause'.

    terminal_punct : str
        The punctuation character that ends the (possibly modified) chunk.

    silence_ms : int
        Inter-chunk gap in milliseconds. Used directly by process_audio.

    modified_text : str
        The text after the prosody passes. Passed to add_natural_pauses and
        then to the model.

    pos_emphasis : list
        Content words (nouns/verbs/adjectives/adverbs) the POS layer marked for
        emphasis. Empty when spaCy is unavailable.

    pos_available : bool
        Whether POS analysis actually ran for this chunk.
    """
    sentence_type:  str
    terminal_punct: str
    silence_ms:     int
    modified_text:  str
    pos_emphasis:   Optional[list] = None
    pos_available:  bool = False
    # Prosody structure signals, recorded per chunk for analysis. They aren't
    # part of the learning fingerprint, since that would fragment the evidence.
    clause_count:   int = 0
    ends_mid:       bool = False
    emphasis_density: float = 0.0
    sentence_count: int = 0     # >1 means the splitter missed a boundary
    is_fragment:    bool = False  # no root verb, so labelled mid_clause not sentence
    synth_ms:       int = 0     # wall-clock synthesis cost for this chunk
    audio_ms:       Optional[int] = None   # length of audio produced
    rtf:            Optional[float] = None   # synth_ms / audio_ms (<1 = faster than real time)
    # Populated by generate_speech. Declared here rather than attached
    # dynamically so every consumer can rely on the attribute existing.
    used_params:    Optional[dict] = None   # the exact synth params used
    profile:        Optional[dict] = None   # chunk_profile() for THIS spoken text
    profile_str:    Optional[str] = None    # band|length|punct|lexis key
    retried:        int = 0                 # synthesis attempts beyond the first
    failure:        Optional[dict] = None   # stage/type/message when a try failed
    rejected:       int = 0                 # waveforms thrown away as hallucinated
    output_check:   Optional[dict] = None   # verdict on the audio finally sent


# ---------------------------------------------------------------------------
# Silence duration table (milliseconds)
# ---------------------------------------------------------------------------
# Design: XTTS already produces its own natural trailing breath. These gaps
# only fill the space between that breath and the start of the next clip.
# Content-type calibration matters more than terminal punctuation alone.

_SILENCE_MS = {
    # --- Structural breaks (paragraph / heading level) ---
    'h1_heading':          90,   # a new major section, so the longest pause
    'h2_heading':          72,   # section heading
    'h3_heading':          58,   # sub-section heading
    'heading':             62,   # generic heading heuristic
    'callout':             48,   # note/warning aside
    'caption':             28,   # figure caption, kept brief
    # --- Bullet points ---
    # Longer than a full stop, since each item is its own complete thought and
    # the gap is what signals "new item" to the listener
    'list_item':           32,   # between bullet items, which are whole thoughts
    # --- Full stops (sentence end) ---
    'sentence':            35,   # declarative sentence
    'definition':          26,   # a definition, which flows into its example
    'question':            45,   # question needs a beat to land
    'exclamation':         24,   # energetic, so kept tight
    'imperative':          33,   # an instruction or step, crisp and a bit tighter
    # --- Comma / mid-clause (near zero) ---
    'parenthetical':        8,   # an aside, so rejoin immediately
    'mid_clause':           4,   # continuous flow
    # --- Default ---
    'unknown':             28,
}

# Every label the classifier can emit. Kept next to the silence table so the two
# cannot drift apart: a type with no silence entry silently falls back to
# 'unknown', which is how h1/h2/h3_heading and parenthetical sat in this table
# for a long time without any code path ever producing them.
SENTENCE_TYPES = tuple(k for k in _SILENCE_MS if k != 'unknown')

# Document-structure hints the reader (or the marker decoder) can supply, mapped
# to the label they force. Anything the extension knows for certain beats the
# text heuristics, because it saw the actual DOM element.
_POSITION_TYPE = {
    'h1':        'h1_heading',
    'h2':        'h2_heading',
    'h3':        'h3_heading',
    'heading':   'heading',
    'list_item': 'list_item',
}

# Labels that come from an explicit signal in the text itself, so a "?" or a
# "Note." prefix. A bullet does not outrank these, since being an item says the
# chunk is one of several and says nothing about how it is spoken, while a
# question mark says exactly how it is spoken. Headings are the other way round
# and override outright, because the reader saw the real h1 and the title-case
# heuristic is only ever guessing at it.
_INTONATION_TYPES = frozenset({'question', 'exclamation', 'callout', 'caption'})

# Question-word starters, which I use to spot implicit questions
_QUESTION_STARTERS = re.compile(
    r'^(what|when|where|which|who|whom|whose|why|how|' 
    r'is|are|was|were|do|does|did|can|could|should|would|' 
    r'will|shall|may|might|have|has|had|am)\b',
    re.IGNORECASE
)

# Tag question endings
_QUESTION_ENDERS = re.compile(
    r"\b(right|correct|yes|no|ok|okay|really|though|eh|huh|isn't it"
    r"|aren't they|don't you|didn't it|wouldn't it|shouldn't it)\s*$",
    re.IGNORECASE
)

# A chunk that is itself an aside, so fully bracketed or fenced by dashes.
_RE_WHOLLY_PARENTHETICAL = re.compile(
    r'^\s*(?:\((?:[^()]|\([^()]*\))*\)|[—–][^—–]+[—–])'
    r'\s*[.,;:]?\s*$'
)


# ---------------------------------------------------------------------------
# Pass 1: Sentence-type detection and terminal punctuation correction
# ---------------------------------------------------------------------------

def _detect_sentence_type(text: str, pos=None) -> tuple:
    """
    Analyse the chunk and return (sentence_type, corrected_text).

    The rules fire in priority order and the first match wins. I only ever modify
    the text to add missing terminal punctuation, never to remove marks that are
    already there.

    `pos` is the single POSProsody parse for this chunk, handed in by
    analyse_prosody, and it's what lets the grammar override the punctuation. A
    chunk ending in a full stop is only a 'sentence' if it actually contains a
    clause, and a base-form verb with no subject is an imperative however it
    happens to be punctuated. When spaCy isn't available `pos` is unavailable and
    the regex rules stand on their own, exactly as they did before.

    The priority order is:
      1.  An explicit "?" makes it a question
      2.  An explicit "!" makes it an exclamation
      3a. A pre-labelled callout ("Note.", "Warning.") makes it a callout
      3b. A pre-labelled caption ("Figure caption:") makes it a caption
      3c. A wholly parenthetical aside makes it parenthetical
      3d. The title-cased heading heuristic makes it a heading
      4.  A question-word start, or a parsed interrogative without a "?"
      5.  A tag question ending, which adds the "?"
      6.  A parsed imperative
      7.  A definition pattern
      8.  Comma enumeration makes it a list_item
      9.  No terminal punctuation makes it mid_clause
      10. Otherwise a sentence, dropped to mid_clause if the parse says fragment
    """
    stripped  = text.rstrip()
    if not stripped:
        return 'sentence', text

    _parsed = pos is not None and getattr(pos, "available", False)
    last_char = stripped[-1]

    # --- Rule 1: Explicit question mark ---
    if last_char == '?':
        return 'question', text

    # --- Rule 2: Explicit exclamation mark ---
    if last_char == '!':
        return 'exclamation', text

    # --- Rule 3a: Pre-labelled callout ---
    # _decode_callout() prefixes with "Note.", "Warning." etc.
    _callout_m = re.match(
        r'^(Note|Warning|Tip|Important|Caution|Example|Info|Danger|Hint)\.',
        stripped, re.IGNORECASE
    )
    if _callout_m:
        return 'callout', stripped if last_char in '.!?' else stripped + '.'

    # --- Rule 3b: Pre-labelled caption ---
    if re.match(r'^Figure caption:', stripped, re.IGNORECASE):
        return 'caption', stripped

    # --- Rule 3c: Wholly parenthetical aside ---
    # A chunk that is itself the aside, like "(and this is the part most people
    # miss)". It rejoins the surrounding sentence almost immediately, so it needs
    # the shortest structural gap in the table. This
    # label existed in _SILENCE_MS from the beginning but nothing ever produced
    # it, so every aside was silently paced as an ordinary sentence.
    if _RE_WHOLLY_PARENTHETICAL.match(stripped):
        return 'parenthetical', stripped

    # --- Rule 3d: Heading heuristic ---
    # Short (≤8 words), title-cased, no terminal punct, no question/aux verb.
    words = stripped.split()
    _heading_excl = re.search(
        r'\b(is|are|was|were|has|have|will|can|could|should|do|does|did'
        r'|what|when|where|which|who|why|how)\b',
        stripped[:25], re.IGNORECASE
    )
    if (last_char not in '.!?;:,'
            and len(words) <= 8
            and sum(1 for w in words if w and w[0].isupper()) >= len(words) * 0.6
            and not _heading_excl):
        return 'heading', stripped + '.'

    # --- Rule 4: Question-word start without "?" ---
    # The parse is the stronger signal (it distinguishes "How the model works"
    # from "How does the model work"), but the word-list keeps working when
    # spaCy is absent.
    _looks_question = (pos.is_interrogative if _parsed
                       else bool(_QUESTION_STARTERS.match(stripped)))
    if _looks_question and last_char not in '.!?':
        return 'question', stripped + '?'

    # --- Rule 5: Tag question ending ---
    if _QUESTION_ENDERS.search(stripped) and last_char not in '!?':
        return 'question', stripped + '?'

    # --- Rule 6: Imperative ---
    # "Load the data." "Clean the missing values." Instructions and procedural
    # steps have a different prosodic shape from declaratives, with a flatter
    # contour and a tighter close, and they're very common in the technical pages
    # this reads. Only the dependency parse can tell them apart from a
    # declarative,
    # so this rule is skipped entirely without spaCy.
    if _parsed and pos.is_imperative:
        return 'imperative', stripped if last_char in '.!?' else stripped + '.'

    # --- Rule 7: Definition pattern ---
    if re.search(
        r'\b(is defined as|means|refers to|is known as|is called|is a type of'
        r'|is an? |stands for|is short for|measures|describes|represents'
        r'|denotes|indicates)\b',
        stripped, re.IGNORECASE
    ):
        return 'definition', text if last_char in '.!?' else stripped + '.'

    # --- Rule 8: Comma enumeration → list_item ---
    _no_main_verb = not re.search(
        r'\b(is|are|was|were|has|have|will|can|should|does|did'
        r'|converge|converges|converged|learn|learns|trained|produces'
        r'|generates|computes|outputs|returns|raises|throws|runs|executes)\b',
        stripped, re.IGNORECASE
    )
    _comma_count = stripped.count(',')
    if _no_main_verb and len(words) <= 14:
        if last_char == ',':
            return 'list_item', stripped[:-1] + '.'
        if _comma_count >= 2 and len(words) <= 10:
            return 'list_item', stripped

    # --- Rule 9: No terminal punctuation → mid_clause ---
    if last_char not in '.!?;:,':
        return 'mid_clause', text

    # --- Rule 10: Default sentence ---
    # Refine with the parse: a 'sentence' with no root verb is really a fragment,
    # so I label it mid_clause for continuation-style pacing. It's guarded, so
    # without spaCy the regex result stands unchanged.
    if _parsed and pos.is_fragment:
        return 'mid_clause', text
    return 'sentence', text


# ---------------------------------------------------------------------------
# Pass 2: Silence classification
# ---------------------------------------------------------------------------
#
# The old "stress-position" pass that lived here inserted a comma before a
# dense terminal cluster. It was disabled long ago because XTTS stutters on the
# word preceding an artificial comma in roughly one chunk in ten, and it has now
# been deleted rather than left as unreachable code. The silence table below is
# what actually shapes pacing.

def _classify_silence(sentence_type: str, terminal_punct: str) -> int:
    """
    Return the inter-chunk silence in milliseconds.
    sentence_type is the primary signal; terminal_punct breaks ties.
    """
    if sentence_type not in _SILENCE_MS:
        # A label with no pause of its own is a bug, not a style: the chunk gets
        # generic pacing and nothing says so. Adding a type to the classifier
        # without adding it here is exactly how h1/h2/h3_heading and
        # parenthetical ended up defined-but-unreachable, so say it out loud.
        print(f"  {C.WARN}[PROSODY]{C.RESET} no silence defined for label "
              f"'{sentence_type}' — using default pacing")
        return _SILENCE_MS['unknown']
    base_ms = _SILENCE_MS[sentence_type]
    if sentence_type == 'sentence':
        if terminal_punct == '?':
            return _SILENCE_MS['question']
        if terminal_punct == '!':
            return _SILENCE_MS['exclamation']
    return base_ms


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

# Fallback mid-thought detector, used only when spaCy is unavailable.
_RE_ENDS_MID = re.compile(
    r'\b(because|which|that|such as|although|while|whereas|since|unless'
    r'|and|or|but|so|if|when|where|to|of|with|for|by|as|than|like)\s*[,;:]?\s*$',
    re.IGNORECASE)


def analyse_prosody(text: str, position: Optional[str] = None) -> ProsodicContext:
    """
    Run the prosody passes and return a ProsodicContext.

    position is an optional document-structure hint. It comes either from the
    reader (which saw the real DOM element) or from the semantic-marker decoder
    (which saw an |H1|/|H2|/|H3| wrapper), and it always beats the text
    heuristics:
      'h1' | 'h2' | 'h3'  chunk came from a heading of that level
      'heading'           chunk came from a heading of unknown level
      'list_item'         chunk closes a bullet, so it earns the item gap
      'paragraph_end'     chunk closes a paragraph, so it earns a longer breath

    The chunk is parsed ONCE here and the result is shared with
    _detect_sentence_type; it used to be parsed a second time inside that
    function, doubling the spaCy cost of every chunk.
    """
    stripped_in = text.rstrip()
    pos = _pos_prosody.analyse(stripped_in)

    sentence_type, text = _detect_sentence_type(text, pos)
    stripped            = text.rstrip()
    terminal_punct      = stripped[-1] if stripped and stripped[-1] in '.!?;:,' else ','

    # Document structure beats the heuristic: the reader saw the actual <h1>,
    # so an H1 is labelled h1_heading and gets the full section-boundary pause
    # instead of collapsing to the generic 'heading' bucket.
    forced = _POSITION_TYPE.get(position or '')
    if forced == 'list_item' and sentence_type in _INTONATION_TYPES:
        forced = None       # a bullet that is a question stays a question
    if forced:
        sentence_type = forced

    silence_ms = _classify_silence(sentence_type, terminal_punct)

    # A mid-clause chunk ending mid-thought is mid-sentence: shorten the pause so
    # the next chunk follows naturally. The POS parse (trailing conjunction/
    # preposition/marker) is more reliable than the word-list regex; fall back to
    # the regex when spaCy is absent.
    ends_mid = (pos.ends_mid_thought if pos.available
                else bool(_RE_ENDS_MID.search(stripped)))
    if sentence_type == 'mid_clause' and ends_mid:
        silence_ms = int(silence_ms * 0.6)

    # Clause-dense chunks (several embedded clauses) read more clearly with a
    # touch more breathing room after them. Capped so it never drags.
    if pos.available and len(pos.clause_breaks) >= 3:
        silence_ms = min(silence_ms + 40, 600)

    # A paragraph-closing chunk earns a longer breath before the next idea.
    if position == 'paragraph_end':
        silence_ms += 150

    _n_words = max(1, len(re.findall(r"[A-Za-z0-9']+", stripped)))
    return ProsodicContext(
        sentence_type  = sentence_type,
        terminal_punct = terminal_punct,
        silence_ms     = silence_ms,
        modified_text  = text,
        pos_emphasis   = pos.emphasis_words if pos.available else [],
        pos_available  = pos.available,
        clause_count   = len(pos.clause_breaks) if pos.available else 0,
        ends_mid       = bool(ends_mid),
        emphasis_density = round(len(pos.emphasis_words) / _n_words, 3) if pos.available else 0.0,
        sentence_count   = pos.sentence_count if pos.available else 0,
        is_fragment      = bool(pos.available and pos.is_fragment),
    )

# Loudness / gain staging.
#
# The old chain peak-normalised to 0.97, applied a further 1.15x makeup gain,
# then HARD-CLIPPED whatever went past full scale. Every chunk was driven to
# 1.1155, so the loudest syllable of each one was squared off. You could hear it
# as a crackle, and it's exactly the distortion Whisper then scores as poor.
#
# The makeup gain itself was fine and deliberate; only the clipping was wrong.
# So the gain staging is unchanged and just the ceiling behaviour is replaced:
# instead of slicing peaks flat, anything above the knee is bent smoothly into
# the remaining headroom. Level matches the old chain to within a fraction of a
# decibel, and nothing ever reaches full scale.
_PEAK_TARGET   = 0.97      # peak-normalisation target (as before)
_MAKEUP_GAIN   = 1.15      # post-normalisation makeup (as before)
_LIMIT_KNEE    = 0.92      # untouched below this, so ordinary speech is unaffected
_LIMIT_CEILING = 0.99      # the absolute maximum, leaving int16 rounding headroom


def _soft_limit(audio):
    """Smoothly compress everything above the knee into the remaining headroom.

    Below the knee the signal is untouched, so ordinary speech is completely
    unaffected. Above it, a tanh curve maps the overshoot into what is left
    below the ceiling, so the waveform bends instead of being sliced flat and
    the output can never exceed _LIMIT_CEILING."""
    over = np.abs(audio) > _LIMIT_KNEE
    if not over.any():
        return audio
    headroom = _LIMIT_CEILING - _LIMIT_KNEE
    excess   = (np.abs(audio[over]) - _LIMIT_KNEE) / headroom
    audio[over] = np.sign(audio[over]) * (_LIMIT_KNEE + headroom * np.tanh(excess))
    return audio


def process_audio(wav_data, silence_ms: int = 80, text_len: int = 0):
    """Runaway guard, dead-air trim, normalise, then gentle fades.

    The trim is deliberately conservative, using a relative threshold, generous
    padding, and only the lead and any excess trailing silence. An earlier and
    more aggressive energy trim cut actual words so I removed it. This one can't
    do that, since it only removes silence sitting well clear of any speech."""
    gap   = silence_ms / 1000.0
    SR    = 24000
    audio = np.array(wav_data, dtype=np.float32)

    # Runaway guard at twice the expected duration, generous enough that it can
    # never cut real speech
    if text_len > 0:
        max_samples = int(text_len * 0.070 * SR * 2.0)
        if len(audio) > max_samples:
            audio = audio[:max_samples]

    # Dead-air trim. XTTS often emits 100-300ms of silence before speech starts
    # and a long tail after it ends; both pad every chunk boundary and make
    # playback feel sluggish. Threshold is 1% of the chunk's own peak.
    if len(audio) > SR // 4:
        athresh = float(np.max(np.abs(audio))) * 0.01
        if athresh > 0:
            loud = np.flatnonzero(np.abs(audio) > athresh)
            if loud.size:
                lead_pad  = int(SR * 0.060)   # keep 60ms before first sound
                trail_pad = int(SR * 0.150)   # keep 150ms after last sound
                start = max(0, int(loud[0]) - lead_pad)
                # Only trim the lead when it saves a meaningful amount (>80ms).
                if start < int(SR * 0.080):
                    start = 0
                # Cap only EXCESS tail: act when more than 350ms of silence
                # follows the last sound, and keep 150ms of it.
                end = len(audio)
                tail_silence = len(audio) - int(loud[-1])
                if tail_silence > int(SR * 0.350):
                    end = min(len(audio), int(loud[-1]) + trail_pad)
                audio = audio[start:end]

    # savgol_filter raises when the window exceeds the signal, which a very
    # short chunk can hit, like a one-word heading or a stray fragment. That used
    # to come back as an opaque 500 from /speak instead of as audio.
    if len(audio) > 7:
        audio = np.asarray(signal.savgol_filter(audio, window_length=7, polyorder=2),
                           dtype=np.float32)

    # Peak-normalise, apply the makeup gain, then SOFT-limit rather than clip.
    peak = float(np.max(np.abs(audio))) if audio.size else 0.0
    if peak > 0:
        audio = audio / peak * _PEAK_TARGET * _MAKEUP_GAIN
        audio = _soft_limit(audio)

    # Gentle fades, 8ms in and 25ms out
    fade_in  = min(int(SR * 0.008), len(audio) // 4)
    fade_out = min(int(SR * 0.025), len(audio) // 4)
    if fade_in  > 0: audio[:fade_in]   *= np.sin(np.linspace(0, np.pi/2, fade_in))
    if fade_out > 0: audio[-fade_out:] *= np.sin(np.linspace(np.pi/2, np.pi, fade_out))

    silence = np.zeros(int(SR * gap), dtype=np.float32)
    audio   = np.concatenate([audio, silence])
    return (audio * 32767).astype(np.int16)


# =============================================================================
# SPEECH GENERATION
# =============================================================================

# --- Output-quality retries ---
# How hard to try when a synthesised chunk fails its waveform check. Two retries
# is the sweet spot: sampling failures are stochastic, so a colder re-roll
# usually succeeds first time, and a chunk that fails three times in a row is
# nearly always a text problem (an unspeakable token) that further retries will
# not fix, and would only stall playback.
_MAX_QUALITY_RETRIES = 2
_RETRY_TEMP_FACTOR   = 0.65   # multiply temperature by this on each retry
_MIN_RETRY_TEMP      = 0.15   # never go colder than this: it starts to sound flat
_RETRY_REP_BUMP      = 0.6    # add to repetition_penalty (targets the loop case)

# Running tally of what was caught, per problem type. Surfaced by
# /quality/rejections so the hallucination rate is a number, not an impression.
_rejections = _collections.Counter()
_rejections_lock = _threading.Lock()


def _record_rejection(problem):
    with _rejections_lock:
        _rejections[problem or "unknown"] += 1
        _rejections["total"] += 1


def generate_speech(text, position: Optional[str] = None):
    """Synthesise speech for a single cleaned text chunk. position is an
    optional document-structure hint ('heading' or 'paragraph_end') forwarded
    from the reader."""

    # If the model was evicted while idle I wake it, meaning reload and rewarm,
    # before doing anything else, since serving against a half-loaded model would
    # 500. This blocks for roughly the load time on
    # the first chunk after a long pause, then normal speed resumes.
    if _model_state != "ready" or tts is None:
        wake_model()
    _touch_synth_activity()   # real work → reset the idle clock

    # Apply any user-taught punctuation/phrasing correction before prosody.
    text = apply_punct_corrections(text)

    # --- Adaptive prosody analysis ---
    ctx  = analyse_prosody(text, position)
    text = ctx.modified_text

    text = text.rstrip(',;')
    text = add_natural_pauses(text)

    # NOTE: an SSML layer used to run here. XTTS exposes no per-word prosody
    # input, so its <emphasis> tags were explicitly a no-op by design and its
    # <break> cues were deliberately not injected (the comment in that module
    # said so), which left a renderer whose only remaining effect was whitespace
    # tidying that add_natural_pauses had already done one line earlier. It has
    # been removed. The emphasis words it consumed are still computed and are
    # still recorded per chunk as ctx.pos_emphasis / emphasis_density, so the
    # signal is preserved for a future phoneme-capable backend.

    text = guard_against_hallucination(text)
    if not text:
        raise ValueError("Text was empty after cleaning")

    # Strip doubled terminal punctuation then ensure exactly one mark
    text = re.sub(r'\.\s*\.(?!\.)', '.', text)
    text = re.sub(r'\?\s*\.', '?', text)
    text = re.sub(r'!\s*\.', '!', text)
    text = re.sub(r',\s*\.', '.', text)
    text = re.sub(r'[,;:]+\s*$', '', text)
    if text and text[-1] not in '.!?':
        text = text + '.'  

    # Tell the learner to pause Whisper analysis while we synthesize so the
    # CPU-heavy transcribe never starves this thread (the per-chunk stall fix).
    # inference_mode() drops autograd bookkeeping for a small speedup. Flag is
    # always cleared in finally so a crash can't wedge analysis permanently.
    try:
        _learner.mark_synthesis_busy(True)
    except Exception:
        pass

    # Close the feedback loop. Temperature: bias toward the confirmed-good value
    # for this sentence type. Speed: the user's live speed is the anchor, shaded
    # by the learned COMPLEXITY-BAND modifier, so a too-fast report on a dense
    # technical chunk slows future dense chunks without touching simple ones
    # (the old per-type rate store made one report shift nearly everything).
    _synth_temp = _LIVE_SETTINGS["temperature"]
    _synth_speed = _LIVE_SETTINGS["speed"]
    _synth_rep = _LIVE_SETTINGS["repetition_penalty"]
    _synth_topk = _LIVE_SETTINGS["top_k"]
    _synth_topp = _LIVE_SETTINGS["top_p"]
    try:
        # This is the fingerprint for the chunk. I work it out exactly once, here,
        # from the spoken text, which is the text the parameters below are chosen
        # for, and then carry it on the context so log_chunk stores that same key
        # instead of recomputing it from the display text.
        #
        # This used to be computed twice from two different strings: here from
        # the spoken text, and again in log_chunk from the human-readable
        # display text. Cleaning changes the features the fingerprint is built
        # from ("SQL 3.5" is lexis=symbolic, "sequel three point five" is
        # lexis=plain), so the two keys routinely disagreed. The observation
        # rows, and so everything the autotuner learned, were filed under a key
        # that synthesis never reads back, which left the whole
        # closed loop writing to one address and reading from another.
        _profile = _learner.chunk_profile(text, ctx.sentence_type)
        _pstr = f"{_profile['band']}|{_profile['length']}|{_profile['punct']}|{_profile['lexis']}"
        ctx.profile = _profile
        ctx.profile_str = _pstr

        # Temperature: resolve the most specific learned profile (sentence type +
        # complexity + length + punctuation + lexis) that has enough evidence,
        # else fall back to coarser buckets / legacy per-type.
        _synth_temp = _learner.resolve_profile_temperature(
            _profile["keys"], _LIVE_SETTINGS["temperature"])
        # rep_penalty and top_k: prefer an autotuned per-profile value, else the
        # per-sentence-type learned value, else the live default.
        _synth_rep = _learner.resolve_profile_param(_pstr, "repetition_penalty",
            _learner.get_preferred_param(ctx.sentence_type, "repetition_penalty",
                _LIVE_SETTINGS["repetition_penalty"], 1.0, 10.0))
        _synth_topk = int(_learner.resolve_profile_param(_pstr, "top_k",
            _learner.get_preferred_param(ctx.sentence_type, "top_k",
                _LIVE_SETTINGS["top_k"], 1, 100, integer=True)))
        # top_p: autotuned per-profile if learned, else the live default.
        _synth_topp = _learner.resolve_profile_param(_pstr, "top_p", _LIVE_SETTINGS["top_p"])
        # speed: band-based pacing modifier, then any autotuned per-profile trim.
        _base_speed = _LIVE_SETTINGS["speed"] * _learner.get_rate_modifier(_profile["band"])
        _speed_mod = _learner.resolve_profile_param(_pstr, "speed_mod", 1.0)
        _synth_speed = round(max(0.5, min(2.0, _base_speed * _speed_mod)), 3)
    except Exception as e:
        # Falling back to live settings is correct, but doing it SILENTLY hid
        # the fact that learned parameters had stopped being applied at all.
        # A chunk that sounds wrong because its learned profile failed to
        # resolve is exactly the case this needs to be diagnosable.
        ctx.failure = {"stage": "resolve_params", "type": type(e).__name__,
                       "message": str(e)[:200]}
        print(f"  {C.WARN}[PARAMS]{C.RESET} learned parameters unavailable for this "
              f"chunk, using live settings ({type(e).__name__}: {str(e)[:80]})")

    _synth_t0 = _time.time()
    try:
        # --- Synthesise, then look at the shape of what came back ---
        # XTTS is autoregressive and sometimes fails to emit an end-of-sequence
        # token. It doesn't error when that happens, it returns audio, and the
        # audio is babble or a looped fragment or a sentence cut off mid-word.
        # Nothing downstream noticed: the runaway guard in process_audio simply
        # truncated the babble to a plausible length and shipped it, and Whisper
        # scored it badly minutes later, by which point the user had heard it.
        #
        # These failures are visible in the waveform (see audio_quality.py), and
        # sampling temperature is what drives them. So a rejected chunk is
        # re-synthesised with a colder, steadier setting instead of being sent.
        # Each attempt costs one more inference on a chunk that was going to be
        # wrong anyway, and the retry is heavily biased toward succeeding.
        outputs = None
        attempt_temp, attempt_rep = _synth_temp, _synth_rep
        for attempt in range(_MAX_QUALITY_RETRIES + 1):
            with _inference_lock, torch.inference_mode():
                outputs = tts.synthesizer.tts_model.inference(
                    text=text,
                    language="en",
                    gpt_cond_latent=gpt_cond_latent,
                    speaker_embedding=speaker_embedding,
                    temperature=attempt_temp,
                    repetition_penalty=attempt_rep,
                    top_k=_synth_topk,
                    top_p=_synth_topp,
                    do_sample=True,
                    num_beams=1,
                    speed=_synth_speed,
                )
            check = _aq.check_output(outputs["wav"], len(text), _synth_speed)
            ctx.output_check = check.as_dict()
            if check.ok:
                if attempt:
                    print(f"  {C.OK}[QUALITY]{C.RESET} clean on attempt "
                          f"{attempt + 1} (temperature {attempt_temp:.2f})")
                break
            ctx.rejected += 1
            _record_rejection(check.problem)
            # Write the failure down as evidence. This attempt is a labelled
            # negative at a known parameter set on this exact fingerprint, and
            # the retry below deliberately changes one variable, so the pair
            # forms a controlled comparison the autotuner can learn from.
            # Discarding it, as this loop originally did, threw away the only
            # causal data the system ever generates.
            try:
                _learner.record_rejected_attempt(
                    ctx.profile_str, ctx.sentence_type,
                    {"temperature": attempt_temp, "top_p": _synth_topp,
                     "top_k": _synth_topk, "repetition_penalty": attempt_rep,
                     "speed": _synth_speed},
                    check.problem)
            except Exception as e:
                print(f"  {C.WARN}[QUALITY]{C.RESET} could not record the "
                      f"rejected attempt: {e}")
            if attempt >= _MAX_QUALITY_RETRIES:
                # Out of retries. I send the last attempt rather than silence,
                # since a flawed chunk still carries the sentence, but I record
                # why so /diagnose can explain it and the rate stays visible.
                print(f"  {C.ERR}[QUALITY]{C.RESET} still {check.problem} after "
                      f"{attempt + 1} attempts — sending it: {check.detail}")
                ctx.failure = {"stage": "output_check", "type": check.problem,
                               "message": check.detail}
                break
            # Cool the sampler: lower temperature makes the model far less
            # likely to wander, and a higher repetition penalty directly
            # discourages the looping case.
            attempt_temp = round(max(_MIN_RETRY_TEMP,
                                     attempt_temp * _RETRY_TEMP_FACTOR), 3)
            attempt_rep = round(min(10.0, attempt_rep + _RETRY_REP_BUMP), 2)
            print(f"  {C.RETRY}[QUALITY]{C.RESET} {check.problem}: {check.detail}")
            print(f"  {C.RETRY}[QUALITY]{C.RESET} re-synthesising at "
                  f"temperature {attempt_temp:.2f}, repetition penalty {attempt_rep:.1f}")
        _synth_temp, _synth_rep = attempt_temp, attempt_rep
    finally:
        try:
            _learner.mark_synthesis_busy(False)
        except Exception:
            pass
    # Per-chunk cost. Recorded so the learner can answer "which chunk SHAPES are
    # expensive on this machine", which is what lets me adapt buffering to the
    # content rather than to one global average.
    _synth_ms = int((_time.time() - _synth_t0) * 1000)

    ctx.used_params = {
        "temperature": _synth_temp,
        "top_p": _synth_topp,
        "top_k": _synth_topk,
        "repetition_penalty": _synth_rep,
        "speed": _synth_speed,
    }
    # Attach per-chunk performance to the context so log_chunk can persist it.
    # audio_ms / synth_ms give a REAL-TIME FACTOR per chunk shape, rather than
    # the single machine-wide figure hardware_profile.py measures.
    ctx.synth_ms = _synth_ms
    _wav = outputs["wav"]
    _audio_ms = int(len(_wav) / 24000.0 * 1000) if _wav is not None else 0
    ctx.audio_ms = _audio_ms or None
    ctx.rtf = round(_synth_ms / _audio_ms, 3) if _audio_ms else None
    return _wav, ctx


# =============================================================================
# ROUTES
# =============================================================================

@app.route("/health", methods=["GET"])
def health():
    # Deliberately minimal: localhost ports are fingerprintable from web pages,
    # so this leaks nothing beyond liveness. Device details live in /settings
    # behind the token.
    return jsonify({"status": "ok"})


def clean_display_text(raw: str) -> str:
    """Human-readable chunk text for display in the dashboard and reports.

    This strips the semantic markers like |H1|, |BREAK| and |BOLD| and collapses
    the whitespace, but keeps the grammar exactly as it appeared on the page,
    with no pronunciation substitution and no number expansion. So 'SQL 3.5'
    shows as 'SQL 3.5' rather than 'sequel three point five'."""
    t = re.sub(r'\|\/?(?:H1|H2|H3|BOLD|ITALIC|CODE|CALLOUT|CAPTION|LIST|BREAK)\|', ' ', raw or '')
    t = re.sub(r'\s+', ' ', t).strip()
    return t


# =============================================================================
# CODE-CHUNK SPEECH
# =============================================================================
# Code identifiers with mixed digits/colons crash XTTS with CUDA index errors,
# because their characters fall outside the model vocabulary, so a chunk that
# looks like code gets rewritten into speakable English before XTTS ever sees it.
#
# These helpers and their patterns used to be defined INSIDE the /speak handler,
# so every request re-created three closures and re-compiled nine detection
# patterns plus a dozen substitution patterns before a single word was spoken.
# They are pure functions of their input; they belong at module scope, compiled
# once.

# Any one of these matching means "treat this chunk as code".
_CODE_PATTERNS = tuple(re.compile(p) for p in (
    r'\bimport\s+\w',                                  # import statement
    r'\bfrom\s+[\w.]+\s+import\b',                     # from X import Y
    r'\w+\.\w+\(',                                     # method call: obj.method(
    r'[A-Z][a-z]+(?:[A-Z][a-z]+){2,}',                 # CamelCase 3+: ImageAnalysisClient
    r'\w+=\s*[A-Z][a-z]+[A-Z]\w+\(',                   # assignment to class: x = MyClass(
    r'<[A-Z][A-Z_]{2,}>',                              # placeholder: <YOUR_KEY>
    r'\w+_\w+_\w+',                                    # snake_case, 2+ underscores
    r'\b(def|class|return|elif|isinstance|lambda)\b',  # unambiguous keywords
    r'[A-Z][A-Za-z]+\.[A-Z][A-Z_]+\b',                 # enum access: VisualFeatures.READ
))

_RE_CODE_FROM_IMPORT = re.compile(r'from\s+([\w.]+)\s+import\s+([\w.,\s]+)')
_RE_CODE_IMPORT      = re.compile(r'import\s+([\w.]+)')
_RE_CODE_PLACEHOLDER = re.compile(r"""["']<([^>]+)>["']""")
_RE_CODE_URL_LIT     = re.compile(r"""["']https?://[^"']+["']""")
_RE_CODE_DQ_STR      = re.compile(r'"[^"]{0,60}"')
_RE_CODE_SQ_STR      = re.compile(r"'[^']{0,60}'")
_RE_CODE_NEW_CLASS   = re.compile(r'=\s*([A-Z][A-Za-z0-9]+)\s*\(')
_RE_CODE_METHOD      = re.compile(r'(\w+)\.(\w+)\s*\(')
_RE_CODE_ASSIGN      = re.compile(r'(\w+)\s*=\s*(?!=)')
_RE_CODE_CAMEL       = re.compile(r'[A-Z][a-z]+(?:[A-Z][a-z]+)+')
_RE_CODE_SNAKE       = re.compile(r'\b[a-z]+(?:_[a-z]+)+\b')
_RE_CODE_SYMBOLS     = re.compile(r'[\[\]{}();,#@|^&~`]')
_RE_CODE_NON_SPEECH  = re.compile(r'[^a-zA-Z0-9 ,]')
_RE_CAMEL_ACRONYM    = re.compile(r'([A-Z]+)([A-Z][a-z])')
_RE_CAMEL_BOUNDARY   = re.compile(r'([a-z])([A-Z])')

_CODE_OPERATORS = (
    ('!=', ' is not equal to '), ('==', ' equals '),
    ('>=', ' greater than or equal to '), ('<=', ' less than or equal to '),
    ('=>', ' returns '), ('->', ' returns '),
    ('**', ' to the power of '), ('//', ' floor divided by '),
    ('+=', ' plus equals '), ('-=', ' minus equals '),
)


def _camel_to_words(name):
    """ImageAnalysisClient → image analysis client"""
    s = _RE_CAMEL_ACRONYM.sub(r'\1 \2', name)
    s = _RE_CAMEL_BOUNDARY.sub(r'\1 \2', s)
    return s.lower()


def _snake_to_words(name):
    return name.replace('_', ' ')


def looks_like_code(text: str) -> bool:
    """True when the chunk should go through _speak_code_chunk."""
    return any(p.search(text) for p in _CODE_PATTERNS)


def _speak_code_chunk(t):
    """
    Convert code/import blocks to natural spoken English.
    Handles: imports, method calls, assignments, identifiers,
    string literals, operators, and format strings.
    Never passes CUDA-crashing tokens to XTTS.
    """
    # --- 1. Import statements ---
    # "from azure.ai.vision import X" → "importing X from azure ai vision"
    def _speak_import(m):
        module = m.group(1).replace('.', ' ').replace('_', ' ')
        name   = m.group(2).replace('.', ' ').replace('_', ' ')
        return f"importing {name} from {module}"
    t = _RE_CODE_FROM_IMPORT.sub(_speak_import, t)
    t = _RE_CODE_IMPORT.sub(
        lambda m: 'importing ' + m.group(1).replace('.', ' ').replace('_', ' '), t)

    # --- 2. String literals → describe their content ---
    # placeholder strings like "<YOUR_KEY>" → "your key placeholder"
    t = _RE_CODE_PLACEHOLDER.sub(
        lambda m: m.group(1).lower().replace('_', ' ') + ' placeholder', t)
    t = _RE_CODE_URL_LIT.sub('endpoint URL', t)
    t = _RE_CODE_DQ_STR.sub('quoted text', t)
    t = _RE_CODE_SQ_STR.sub('quoted text', t)

    # --- 3. Common operators → spoken form ---
    for op, spoken in _CODE_OPERATORS:
        t = t.replace(op, spoken)

    # --- 4. Class instantiation: ClassName(...) → "new ClassName" ---
    t = _RE_CODE_NEW_CLASS.sub(
        lambda m: '= new ' + _camel_to_words(m.group(1)) + ' with ', t)

    # --- 5. Method calls: obj.method(args) → "obj dot method" ---
    t = _RE_CODE_METHOD.sub(
        lambda m: _snake_to_words(m.group(1)) + ' dot ' + _snake_to_words(m.group(2)) + ' ', t)

    # --- 6. Assignments: var = value → "var set to" ---
    t = _RE_CODE_ASSIGN.sub(lambda m: _snake_to_words(m.group(1)) + ' set to ', t)

    # --- 7. CamelCase identifiers → spaced words ---
    t = _RE_CODE_CAMEL.sub(lambda m: _camel_to_words(m.group(0)), t)

    # --- 8. snake_case identifiers → spaced words ---
    t = _RE_CODE_SNAKE.sub(lambda m: m.group(0).replace('_', ' '), t)

    # --- 9. Dot notation remaining → spaces ---
    t = t.replace('.', ' ')

    # --- 10. Remove remaining code symbols (numbers are kept) ---
    t = _RE_CODE_SYMBOLS.sub(' ', t)
    t = _RE_CODE_NON_SPEECH.sub(' ', t)

    # --- 11. Clean whitespace ---
    return _RE_WHITESPACE.sub(' ', t).strip()


# =============================================================================
# /speak
# =============================================================================

# 200ms, which is short enough to be imperceptible and long enough for WebAudio
# to decode reliably. Every "nothing to say" path returns exactly this.
_SILENT_MS = 200
_RE_ORPHAN_CONJ = re.compile(r'^(and|but|or|so|yet|nor|for)\s+', re.IGNORECASE)


def _silence_response():
    """A short valid WAV, for chunks that resolve to nothing worth speaking."""
    silence = np.zeros(int(24000 * _SILENT_MS / 1000.0), dtype=np.int16)
    buf = io.BytesIO()
    wav.write(buf, 24000, silence)
    buf.seek(0)
    return send_file(buf, mimetype="audio/wav", as_attachment=False)


@app.route("/speak", methods=["POST"])
def speak():
    data = request.get_json()
    if not data or "text" not in data:
        return jsonify({"error": "No text provided"}), 400

    raw = data["text"].strip()
    if not raw:
        return jsonify({"error": "Text is empty"}), 400
    _chunk_no = data.get("index")
    _position = data.get("position")  # 'heading' | 'paragraph_end' | None

    text = clean_text(raw)

    # A whole-chunk SUPPRESS rule: the user asked for this content to be
    # skipped, so honour it with silence rather than synthesising the sentinel.
    if text == _learner.SUPPRESS_SENTINEL:
        print(f"  {C.SKIP}[SUPPRESS]{C.RESET} user rule silences: {clean_display_text(raw)[:60]}")
        return _silence_response()

    if not text:
        return jsonify({"error": "Text cleaned to empty"}), 400

    # A heading marker in the source is authoritative about the chunk's level;
    # prefer it over the generic 'heading' hint the reader may have sent.
    _level = _detected_heading_level()
    if _level:
        _position = _level
    elif _detected_list_item() and not _position:
        # A |LIST| wrapper that survived into the request, which happens when
        # something posts raw marked-up text rather than going through the
        # reader. The reader normally works this out itself and sends
        # 'list_item' in the position field, so I only fall back to the wrapper
        # when it has said nothing at all.
        _position = 'list_item'

    if len(text) > _MAX_CHUNK_CHARS:
        print(f"  {C.WARN}[WARN]{C.RESET} over-length ({len(text)}c) — truncating")
        text = text[:_MAX_CHUNK_CHARS].rsplit(' ', 1)[0].rstrip(',:;')

    # Strip leading comma artefacts from bold/marker decoder
    text = text.lstrip(', ')
    if text and text[0].islower():
        text = text[0].upper() + text[1:]

    # Orphan conjunction fix
    _om = _RE_ORPHAN_CONJ.match(text)
    if _om and len(text) <= 50:
        text = text[_om.end():].strip()
        if text and text[0].islower():
            text = text[0].upper() + text[1:]
        print(f"  {C.WARN}[ORPHAN]{C.RESET} → {repr(text)}")
        if not text or len(text) < 3:
            return _silence_response()

    # Convert code tokens to speakable English before XTTS sees them.
    if looks_like_code(text):
        _converted = _speak_code_chunk(text)
        if _converted and _converted != text:
            print(f"  {C.SKIP}[CODE] {repr(text[:40])} → {repr(_converted[:40])}{C.RESET}")
            text = _converted

    # Short-chunk gate, so I skip inference for trivially short text since it
    # gives XTTS no prosodic context and wastes a GPU call.
    if len(text) < 3:
        print(f"  {C.SKIP}[SKIP] too short ({len(text)}c): {repr(text)}{C.RESET}")
        return _silence_response()

    # Thread-safe dedup, which stops prefetch races synthesising the same chunk
    # twice.
    #
    # I take the reservation after all the short-circuit gates above and it is
    # released on every exit path via try/finally. It used to be taken before
    # them and only ever cleared on the success path, so a chunk that was
    # suppressed, skipped as too short, or failed synthesis left a permanent
    # "in flight, no audio yet" marker: the next request for the same text sat
    # in the wait loop below for a full 12 seconds before giving up.
    _h = hashlib.md5(text.encode()).hexdigest()
    _reserved = False
    _w = 0.0
    while True:
        with _dedup_lock:
            _now = time.time()
            for k in list(_dedup_cache):
                if _now - _dedup_cache[k][0] > _DEDUP_WINDOW:
                    del _dedup_cache[k]
            _e = _dedup_cache.get(_h)
            if _e is None:
                _dedup_cache[_h] = (_now, None)
                _reserved = True
                break
            if _e[1] is not None:
                print(f"  {C.DEDUP}[DEDUP] cached: {text[:50]}{C.RESET}")
                return send_file(io.BytesIO(_e[1]), mimetype="audio/wav")
        if _w >= 12.0:
            break
        time.sleep(0.1); _w += 0.1

    try:
        return _synthesise_and_log(text, raw, _position, _chunk_no, _h)
    finally:
        # Release an unfulfilled reservation so a retry is not made to wait.
        if _reserved:
            with _dedup_lock:
                if _dedup_cache.get(_h, (0, None))[1] is None:
                    _dedup_cache.pop(_h, None)


def _synthesise_and_log(text, raw, position, chunk_no, dedup_key):
    """Synthesise one prepared chunk, persist everything known about it, and
    return the audio response. Split out of speak() so the dedup reservation
    can be released in a finally block around the whole of it."""
    global _analysis_counter

    _tag = f"#{chunk_no} " if chunk_no is not None else ""
    print(f"  {C.OK}{C.BOLD}[TTS]{C.RESET} {C.DIM}{_tag}({len(text)}c){C.RESET} "
          f"{text[:90]}{'…' if len(text) > 90 else ''}")

    # --- Synthesis, with two escalating recovery attempts ---
    # Every attempt is recorded: which one it was, what failed, and why. A chunk
    # that only succeeded on the second try is a real instability signal, and
    # the retried column existed for it but was never written.
    attempts = []
    wav_data = ctx = None
    recoveries = (
        ("retry_after_cache_clear", "CUDA cache cleared — retrying", None),
        ("retry_after_model_reload", "Retry failed — reloading model", load_model),
    )
    try:
        wav_data, ctx = generate_speech(text, position)
    except ValueError as ve:
        # The text cleaned to nothing, which isn't a failure, there's just
        # nothing to say.
        print(f"  {C.SKIP}[SKIP] {ve}{C.RESET}")
        _learner.log_error(text, error_type="EMPTY", stage="clean",
                           message=str(ve), fatal=False)
        return _silence_response()
    except Exception as first_err:
        attempts.append({"attempt": 1, "stage": "inference",
                         "type": type(first_err).__name__,
                         "message": str(first_err)[:300]})
        print(f"\n  {C.ERR}{C.BOLD}[ERROR]{C.RESET} {str(first_err)[:120]}")
        for n, (label, note, before) in enumerate(recoveries, start=2):
            try:
                _device.empty_cache(DEV)
                if before is not None:
                    before()
                print(f"  {C.RETRY}[RETRY]{C.RESET} {note}")
                wav_data, ctx = generate_speech(text, position)
                break
            except Exception as retry_err:
                attempts.append({"attempt": n, "stage": label,
                                 "type": type(retry_err).__name__,
                                 "message": str(retry_err)[:300]})
        if ctx is None:
            last = attempts[-1]
            print(f"  {C.ERR}{C.BOLD}[FATAL]{C.RESET} {last['type']}: {last['message'][:120]}")
            chunk_id = _learner.log_error(
                text, error_type=last["type"], stage=last["stage"],
                message=last["message"], attempts=attempts,
                display_text=clean_display_text(raw), fatal=True)
            # Return the id alongside the error so the failure is addressable:
            # GET /diagnose/<id> explains exactly what happened to this chunk.
            return jsonify({"error": last["message"], "error_type": last["type"],
                            "stage": last["stage"], "attempts": len(attempts),
                            "chunk_id": chunk_id}), 500
        ctx.retried = len(attempts)
        ctx.failure = attempts[-1]
        print(f"  {C.OK}[RECOVERED]{C.RESET} after {len(attempts)} failed attempt(s)")

    audio_int16 = process_audio(wav_data, silence_ms=ctx.silence_ms, text_len=len(text))
    buf = io.BytesIO()
    wav.write(buf, 24000, audio_int16)
    _wb = buf.getvalue()
    with _dedup_lock:
        _dedup_cache[dedup_key] = (time.time(), _wb)

    # Log chunk to quality monitor. Store the ORIGINAL grammatical text (raw,
    # markers stripped) for the dashboard and reports, rather than the
    # pronunciation-processed spoken form. Whisper analysis still uses the
    # processed audio; only the displayed/stored text is the human-readable one.
    _display = clean_display_text(raw)

    # Content facets come from the SAME chunk_profile() call that chose this
    # chunk's synthesis parameters, so the console tag, the stored record and
    # the learning fingerprint cannot disagree. analyse_facets used to be run a
    # second time here, on the raw marker-laden text, giving a third answer.
    _facets = (ctx.profile or {}).get("facets") or {"math": False, "primary": "prose", "count": 0}
    _has_math = bool(_facets.get("math"))
    if _has_math:
        print(f"[MATH] equation/symbol chunk: {(_display or raw)[:70]}")
    _other = [k for k in ("list", "definition", "code", "quote", "citation") if _facets.get(k)]
    if _other:
        print(f"[CHUNK] facets: {', '.join(_other)}")

    try:
        # On very low-core machines _ANALYSIS_ENABLED is False: skip passing the
        # audio so Whisper never competes with synthesis for CPU. The chunk is
        # still logged; only the listen-back analysis is dropped.
        # Sampling: analysing every chunk doubles GPU work (Whisper runs on the
        # same device as XTTS). Sampling keeps learning going at a fraction of
        # the cost; 0 disables it entirely for maximum speed.
        _analysis_wav = None
        if _ANALYSIS_ENABLED and _ANALYSIS_EVERY > 0:
            _analysis_counter += 1
            if _analysis_counter % _ANALYSIS_EVERY == 0:
                _analysis_wav = _wb
        chunk_id = _learner.log_chunk(
            text, ctx.sentence_type, ctx.silence_ms,
            wav_bytes=_analysis_wav, display_text=_display,
            synth_params=ctx.used_params,
            has_math=_has_math,
            # The fingerprint resolved at synthesis time, passed in rather than
            # recomputed, so what the autotuner reads back matches what chose
            # these parameters.
            profile_str=ctx.profile_str,
            prosody={
                "clause_count":     ctx.clause_count,
                "ends_mid":         ctx.ends_mid,
                "emphasis_density": ctx.emphasis_density,
                "sentence_count":   ctx.sentence_count,
                "is_fragment":      ctx.is_fragment,
                "pos_available":    ctx.pos_available,
                "primary_facet":    _facets.get("primary", "prose"),
                "facet_count":      _facets.get("count", 0),
                "synth_ms":         ctx.synth_ms,
                "audio_ms":         ctx.audio_ms,
                "rtf":              ctx.rtf,
                # Waveforms discarded as hallucinated count as retries too: both
                # mean "this chunk took more than one attempt to get right", and
                # that is the instability signal the AI page reports.
                "retried":          ctx.retried + ctx.rejected,
                "rejected":         ctx.rejected,
                "output_check":     ctx.output_check,
                "failure":          ctx.failure,
            })
    except Exception as _e:
        # The audio already exists, so I never fail the request over logging.
        chunk_id = hashlib.sha1((text or "").strip().encode("utf-8")).hexdigest()[:16]
        print(f"[SERVER] log_chunk failed, returning audio anyway: {_e}")

    buf.seek(0)
    resp = send_file(buf, mimetype="audio/wav", as_attachment=False)
    resp.headers['X-Chunk-Id'] = chunk_id
    return resp


@app.route("/pronounce/<token>", methods=["GET"])
def get_pronunciation(token):
    """Return the current spoken form for an abbreviation and its source."""
    spoken, source = lookup_pronunciation(token)
    return jsonify({
        "token":  token,
        "spoken": spoken,
        "source": source,   # user | word | spell | auto_word | auto_letters
    })

@app.route("/pronounce", methods=["POST"])
def set_pronunciation():
    """
    Teach the model a new pronunciation, saved to disk straight away.
    Body: {"abbr": "GPU", "spoken": "gee pee you"}
    Optionally {"abbr": "GPU", "delete": true} to remove an override.
    """
    data = request.get_json()
    if not data or "abbr" not in data:
        return jsonify({"error": "Missing 'abbr' field"}), 400

    token  = data["abbr"].strip().upper()
    store  = _load_store()

    if data.get("delete"):
        removed = store.pop(token, None)
        _save_store(store)
        return jsonify({"status": "deleted" if removed else "not_found", "token": token})

    if "spoken" not in data:
        return jsonify({"error": "Missing 'spoken' field"}), 400

    spoken = data["spoken"].strip()
    store[token] = spoken
    _save_store(store)
    print(f"  {C.PRONOUNCE}[PRONOUNCE]{C.RESET} learned: {token} → {spoken}")
    return jsonify({"status": "saved", "token": token, "spoken": spoken})

@app.route("/pronounce", methods=["GET"])
def list_pronunciations():
    """List all user-defined pronunciation overrides."""
    store = _load_store()
    return jsonify({"overrides": store, "count": len(store)})


# --- Learner / Report endpoints ---

@app.route("/report", methods=["POST"])
def submit_report_route():
    d = request.json or {}
    result = _learner.submit_report(
        chunk_text = d.get("chunk_text", ""),
        chunk_id   = d.get("chunk_id"),
        issue      = d.get("issue", "IGNORE"),
        token      = d.get("token"),
        expected   = d.get("expected"),
        heard      = d.get("heard"),
        action     = d.get("action", "IGNORE"),
        confidence = d.get("confidence", "MEDIUM"),
        notes      = d.get("notes"),
    )
    # Validation failure → HTTP 400 with user-facing message
    if isinstance(result, dict) and result.get("ok") is False:
        return jsonify(result), 400
    return jsonify(result)


@app.route("/learned", methods=["GET"])
def get_learned_route():
    """What the model has learned: per-band rate modifiers and per-type
    temperatures. Shown in the Model Tuning panel."""
    try:
        summary = _learner.get_learned_summary()
        # The server owns the factory baseline; inject it so the AI tab can show
        # each learned per-type temperature against its original value (#3).
        summary["factory"] = {
            "temperature":        _FACTORY_SETTINGS.get("temperature"),
            "repetition_penalty": _FACTORY_SETTINGS.get("repetition_penalty"),
            "top_k":              _FACTORY_SETTINGS.get("top_k"),
            "top_p":              _FACTORY_SETTINGS.get("top_p"),
        }
        return jsonify(summary)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/learned/reset_rates", methods=["POST"])
def reset_learned_rates_route():
    """Clear all learned speaking rates (band modifiers and legacy per-type
    values). Temperatures and pronunciation learning are untouched."""
    try:
        return jsonify(_learner.reset_learned_rates())
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/shutdown", methods=["POST"])
def shutdown_route():
    """Graceful stop, used by the power button via kam_host. Lets any in-flight
    synthesis finish (bounded wait) so the process never dies mid-chunk or
    mid-database-write, then exits."""
    def _graceful_exit():
        got = _inference_lock.acquire(timeout=15)
        try:
            print(f"{C.WARN}[HOST] graceful shutdown requested — exiting{C.RESET}")
        finally:
            if got:
                _inference_lock.release()
        os._exit(0)
    _threading.Timer(0.3, _graceful_exit).start()
    return jsonify({"ok": True, "stopping": True})


@app.route("/session/complete", methods=["POST"])
def session_complete_route():
    """Called by the extension when a read finishes naturally. Every chunk from
    the session with no verdict becomes 'solid' (heard, fine, nothing to report)
    and the settings that produced it are gently reinforced."""
    d = request.json or {}
    played = d.get("played")
    since = d.get("since_ts")
    if played is None and since is None:
        return jsonify({"error": "played or since_ts required"}), 400
    try:
        return jsonify(_learner.mark_session_solid(since_ts=since, played=played))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/rules/purge_flag_blacklists", methods=["POST"])
def purge_flag_blacklists_route():
    """One-time cleanup: deactivate blacklist rules created by the old buggy
    auto path that were stripping real words from speech."""
    try:
        return jsonify(_learner.purge_flag_blacklists())
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/maintenance/compact", methods=["POST"])
def maintenance_compact_route():
    """Prune old reports and learning history beyond a retention window and
    VACUUM the database. Chunks are content-addressed (re-reads dedupe) and are
    kept; rules and confirmed learning are never touched."""
    d = request.json or {}
    days = int(d.get("days", 90))
    try:
        return jsonify(_learner.maintenance_compact(days))
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/report/history", methods=["GET"])
def report_history():
    limit     = int(request.args.get("limit", 50))
    data_type = request.args.get("type", "chunks")
    if data_type == "reports":
        return jsonify(_learner.get_reports(limit))
    return jsonify(_learner.get_chunk_feed(limit))

@app.route("/diagnose/<chunk_id>", methods=["GET"])
def diagnose_chunk_route(chunk_id):
    """Everything known about one chunk: how it was labelled, which parameters
    were used and where they came from, what it cost, how it scored, what (if
    anything) failed, and which learned rules rewrote it.

    This is the answer to "why did THIS chunk come out wrong", which previously
    required reading four unrelated views and the console scrollback."""
    try:
        return jsonify(_learner.diagnose_chunk(chunk_id=chunk_id))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/diagnose", methods=["POST"])
def diagnose_by_text_route():
    """Same as GET /diagnose/<id> but keyed by the chunk TEXT, so a chunk can be
    looked up straight from the page without knowing its id."""
    d = request.get_json(silent=True) or {}
    text = (d.get("chunk_text") or "").strip()
    if not text and not d.get("chunk_id"):
        return jsonify({"error": "chunk_id or chunk_text required"}), 400
    try:
        return jsonify(_learner.diagnose_chunk(chunk_id=d.get("chunk_id"),
                                               chunk_text=text))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/quality/rejections", methods=["GET"])
def quality_rejections_route():
    """How often synthesis produced audio bad enough to throw away and redo.

    This is the hallucination rate measured rather than guessed. `caught` counts
    the waveforms I rejected before anyone heard them, broken down by how they
    failed: runaway means it never stopped, looping means it repeated a fragment,
    truncated means it was cut off mid-word, and dead means silence."""
    with _rejections_lock:
        counts = dict(_rejections)
    total = counts.pop("total", 0)
    return jsonify({
        "caught": total,
        "by_problem": counts,
        "max_retries": _MAX_QUALITY_RETRIES,
        "note": ("Each of these was re-synthesised at a lower temperature "
                 "instead of being played."),
    })


@app.route("/analysis/backlog", methods=["GET"])
def analysis_backlog_route():
    """Listen-back queue health. A rising `dropped` count is why some chunks
    have no quality score: synthesis is outrunning Whisper, not that those
    chunks were fine."""
    try:
        info = _learner.analysis_backlog()
        info["enabled"] = _ANALYSIS_ENABLED
        info["analysis_every"] = _ANALYSIS_EVERY
        return jsonify(info)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/report/stats", methods=["GET"])
def report_stats():
    stats = _learner.get_stats()
    # The server owns the POS module (learner has no spaCy dependency), so the
    # availability flag is injected here for the Analysis Engines panel.
    stats["pos_available"] = _pos_prosody.is_available()
    # The full label vocabulary, so the type-distribution panel can show which
    # labels never get produced. A label the classifier can emit but never does
    # is the signature of an unreachable rule, which is how three heading levels
    # and the parenthetical label went unnoticed for so long.
    seen = {t["type"] for t in stats.get("type_distribution", [])}
    stats["sentence_types"] = list(SENTENCE_TYPES)
    stats["types_unused"] = [t for t in SENTENCE_TYPES if t not in seen]
    return jsonify(stats)

@app.route("/report/stats/reset", methods=["POST"])
def reset_stats():
    """Clear all the chunk history, which resets the stats page to zero."""
    _learner.reset_stats()
    return jsonify({"ok": True})


@app.route("/report/learning", methods=["GET"])
def report_learning():
    """Permanent learning history (newest first) + numeric summary. This is the
    long-term record that survives 'clear'. Distinct from /report/history, which
    serves the per-session chunk/report feed."""
    limit = int(request.args.get("limit", 200))
    return jsonify({
        "summary": _learner.get_history_summary(),
        "events":  _learner.get_history(limit=limit),
    })

@app.route("/report/learning/clear", methods=["POST"])
def report_learning_clear():
    """Clear the permanent learning history and the durable solid total.

    This is the dedicated reset for the long-term log, kept separate from the
    per-session 'clear console' so it never gets wiped by accident."""
    try:
        _learner.clear_history()
        _learner.clear_counter("solid_total")
        _learner.clear_counter("perfect_total")
        _learner.clear_counter("negative_total")
        # Keep the seed marker SET so the startup backfill does not re-import the
        # chunk-row verdicts that still exist and undo this clear.
        _learner.set_counter("feedback_seeded", 1)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/report/rules", methods=["GET"])
def report_rules():
    return jsonify(_learner.get_rules())

@app.route("/rules/boundary", methods=["GET"])
def boundary_rules():
    """Active chunk-boundary corrections: fragments the user reported as wrongly
    split off. The extension chunker fetches these at read start and keeps each
    fragment attached to its preceding sentence."""
    try:
        rules = _learner.get_rules(active_only=True)
        frags = [r["pattern"] for r in rules
                 if r.get("rule_type") == "BOUNDARY"
                 and r.get("action") == "keep_with_previous"
                 and r.get("pattern")]
        return jsonify({"fragments": frags})
    except Exception as e:
        return jsonify({"error": str(e), "fragments": []}), 500

@app.route("/report/rules", methods=["POST"])
def add_rule_route():
    d = request.json or {}
    result = _learner.add_rule_manual(
        d.get("rule_type", "FLAG"),
        d.get("pattern", ""),
        d.get("action", ""),
        d.get("value", ""),
    )
    return jsonify(result)

@app.route("/report/chunks", methods=["GET"])
def chunks_filtered():
    limit       = int(request.args.get("limit", 100))
    offset      = int(request.args.get("offset", 0))
    filter_type = request.args.get("filter", request.args.get("quality", "all"))
    return jsonify(_learner.get_chunks_filtered(limit, offset, filter_type))

@app.route("/report/rules/cleanup", methods=["POST"])
def cleanup_auto_flags():
    """Delete every AUTO-source FLAG rule, clearing the noisy auto-flags that
    build up on common words."""
    return jsonify(_learner.cleanup_auto_flag_rules())

@app.route("/report/rules/<int:rule_id>", methods=["DELETE"])
def delete_rule_route(rule_id):
    # If this is a PRONUNCIATION rule, also remove from pronunciation_store.json
    import json as _json
    with _learner._db_lock:
        conn = _learner._get_db()
        row  = conn.execute("SELECT rule_type, pattern FROM rules WHERE id=?", (rule_id,)).fetchone()
        conn.close()
    if row and row["rule_type"] == "PRONUNCIATION":
        token = row["pattern"].upper()
        try:
            if _learner.STORE_PATH.exists():
                store = _json.load(open(_learner.STORE_PATH))
                if token in store:
                    del store[token]
                    _json.dump(store, open(_learner.STORE_PATH,"w"), indent=2)
                    print(f"[LEARNER] Removed '{token}' from pronunciation store")
        except Exception as e:
            print(f"[LEARNER] Store remove error: {e}")
    _learner.delete_rule(rule_id)
    return jsonify({"ok": True})

@app.route("/settings", methods=["GET"])
def get_settings():
    """Return current live inference settings."""
    return jsonify(_LIVE_SETTINGS)

@app.route("/ai/insights", methods=["GET"])
def ai_insights_route():
    """Deep quality insight for the AI page (fingerprint trends, maths split,
    splitter health, report outcomes, voice comparison)."""
    try:
        return jsonify(_learner.ai_insights())
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/voices", methods=["GET"])
def list_voices():
    """Voice profiles with clip counts and which is active."""
    return jsonify({"voices": _list_voices(), "active": _ACTIVE_VOICE,
                    "screening": _CLIP_SCREENING.get(_ACTIVE_VOICE)})


@app.route("/voices/check", methods=["POST"])
def check_voice_route():
    """Measure a voice's reference clips and report what would be excluded.

    This lets you see the quality of a profile's source material before switching
    to it and waiting on latents. The recording problems that ruin a clone, so
    clipping, room noise, dead air and clips that are too short, are all
    objective, and none of them are obvious by ear even on good headphones."""
    d = request.get_json(silent=True) or {}
    vid = (d.get("voice_id") or _ACTIVE_VOICE).strip()
    clips = _discover_voice_samples(vid)
    if not clips:
        return jsonify({"ok": False, "voice_id": vid,
                        "error": f"No .wav clips found for '{vid}'."}), 400
    reports = [_aq.analyse_clip(p) for p in clips]
    usable = [r for r in reports if r.ok]
    return jsonify({
        "ok": True, "voice_id": vid,
        "n_total": len(reports), "n_usable": len(usable),
        "clips": [{
            "name": r.name, "verdict": r.verdict, "usable": r.ok,
            "duration": round(r.duration, 1), "sample_rate": r.sample_rate,
            "peak": round(r.peak, 3), "silence_frac": round(r.silence_frac, 3),
            "clip_frac": round(r.clip_frac, 5),
            "snr_db": (round(r.snr_db, 1) if r.snr_db is not None else None),
            "reasons": r.reasons,
        } for r in reports],
    })


# --- Recording clips from the dashboard ---
# The clips have to come from somewhere, and asking people to find recording
# software, pick mono, pick a sample rate and save into the right folder was the
# one step that assumed real comfort with computers. The dashboard records them
# now, so these three routes are what it needs: save a take, list what has been
# recorded, and delete a bad one. Playback reuses /voices/clip below.
#
# The browser sends a finished 24 kHz mono WAV, so there is no decoding here on
# purpose. Keeping audio formats out of the server means no ffmpeg dependency
# and one less thing to go wrong on someone else's machine.

_MAX_CLIP_BYTES = 12 * 1024 * 1024      # about 4 minutes at 24 kHz mono 16-bit


def _clip_path(vid, slot):
    """Where a passage's take lives. Numbering by slot means re-recording
    overwrites the previous attempt instead of piling up near-duplicates, which
    would quietly skew the averaged speaker embedding."""
    return os.path.join(_voice_dir(vid), f"passage_{int(slot):02d}.wav")


def _transcript_path(wav_path):
    """A clip's text lives beside it as a plain .txt with the same stem.

    Keeping what was actually said is worth more than it looks. A reference clip
    with known text can be checked against a transcription to catch a misread or
    a stumble, and the punctuation says where the speaker paused, which is real
    prosody evidence rather than something inferred from the audio alone. A
    sidecar rather than a manifest because clip discovery globs *.wav, so a .txt
    cannot be mistaken for audio, and deleting a clip by hand does not leave an
    index pointing at a file that has gone."""
    return os.path.splitext(wav_path)[0] + ".txt"


def _read_transcript(wav_path):
    try:
        with open(_transcript_path(wav_path), encoding="utf-8") as f:
            return f.read().strip()
    except (FileNotFoundError, OSError, UnicodeDecodeError):
        return ""


@app.route("/voices/record", methods=["POST"])
def record_clip():
    """Save one recorded passage and say straight away whether it is usable.

    The verdict matters more than the saving. Clipping, room noise, dead air and
    clips that are too short are exactly what ruins a clone, and none of them are
    obvious by ear, so finding out now beats finding out after sixteen takes.

    Multipart rather than a raw body, since the take arrives with the text that
    was read and I want both written together or not at all."""
    vid  = (request.form.get("voice") or _ACTIVE_VOICE).strip()
    text = (request.form.get("text") or "").strip()
    try:
        slot = int(request.form.get("slot") or 0)
    except ValueError:
        return jsonify({"ok": False, "error": "slot must be a number"}), 400

    fs = request.files.get("audio")
    if fs is None:
        return jsonify({"ok": False, "error": "no audio received"}), 400
    data = fs.read() or b""
    if not data:
        return jsonify({"ok": False, "error": "no audio received"}), 400
    if len(data) > _MAX_CLIP_BYTES:
        return jsonify({"ok": False, "error": "that recording is too long"}), 413
    if data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        return jsonify({"ok": False, "error": "that is not a WAV file"}), 400

    os.makedirs(_voice_dir(vid), exist_ok=True)
    # Slot 0 means a passage of the user's own, which has no fixed numbering, so
    # it takes the next free slot above the standard ones.
    if slot <= 0:
        slot = _next_free_slot(vid)
    elif slot > _MAX_SLOTS:
        return jsonify({"ok": False, "error": f"slot must be 1-{_MAX_SLOTS}"}), 400

    path = _clip_path(vid, slot)
    # Write beside the target and rename, so a failed write can never leave a
    # half-written clip that later reads as a corrupt reference.
    tmp = path + ".part"
    try:
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
        if text:
            with open(_transcript_path(path), "w", encoding="utf-8") as f:
                f.write(text)
    except Exception as e:
        try: os.remove(tmp)
        except OSError: pass
        return jsonify({"ok": False, "error": f"could not save: {e}"}), 500

    rep = _aq.analyse_clip(path)
    print(f"[VOICE] Recorded passage {slot} for '{vid}': {rep.verdict}")
    return jsonify({"ok": True, "voice_id": vid, "slot": slot,
                    "clip": _clip_json(rep),
                    "n_clips": len(_discover_voice_samples(vid))})


# Room for the standard passages plus a good number of the user's own.
_MAX_SLOTS = 99


def _next_free_slot(vid):
    """Lowest unused slot above the standard passages, so a custom recording
    never lands on top of one of the numbered ones."""
    used = set()
    for p in _discover_voice_samples(vid):
        m = re.match(r"passage_(\d+)\.wav$", os.path.basename(p), re.I)
        if m:
            used.add(int(m.group(1)))
    n = len(VOICE_PASSAGES) + 1
    while n in used and n < _MAX_SLOTS:
        n += 1
    return n


def _clip_json(rep, path=None):
    """One clip's measurements, shaped the same wherever they are returned."""
    return {
        "name": rep.name, "verdict": rep.verdict, "usable": rep.ok,
        "duration": round(rep.duration, 1), "sample_rate": rep.sample_rate,
        "peak": round(rep.peak, 3), "silence_frac": round(rep.silence_frac, 3),
        "clip_frac": round(rep.clip_frac, 5),
        "snr_db": (round(rep.snr_db, 1) if rep.snr_db is not None else None),
        "reasons": rep.reasons,
        "text": _read_transcript(path or rep.path),
    }


@app.route("/voices/clips", methods=["GET"])
def list_clips():
    """Every clip in a profile with its measurements, so the dashboard can show
    what has been recorded without anyone opening a file manager."""
    vid   = (request.args.get("voice") or _ACTIVE_VOICE).strip()
    clips = _discover_voice_samples(vid)
    out   = []
    for p in clips:
        rep  = _aq.analyse_clip(p)
        info = _clip_json(rep)
        m = re.match(r"passage_(\d+)\.wav$", os.path.basename(p), re.I)
        info["slot"] = int(m.group(1)) if m else None
        out.append(info)
    out.sort(key=lambda c: (c["slot"] is None, c["slot"] or 0, c["name"]))
    return jsonify({"ok": True, "voice_id": vid, "clips": out,
                    "n_usable": sum(1 for c in out if c["usable"])})


@app.route("/voices/clip", methods=["GET"])
def get_clip():
    """Serve one clip back so a take can be played in the dashboard.

    The name is matched against the clips actually discovered rather than joined
    onto the folder, since anything built from a query parameter is a path
    traversal waiting to happen."""
    vid  = (request.args.get("voice") or _ACTIVE_VOICE).strip()
    name = (request.args.get("name") or "").strip()
    for p in _discover_voice_samples(vid):
        if os.path.basename(p) == name:
            return send_file(p, mimetype="audio/wav")
    return jsonify({"ok": False, "error": "no such clip"}), 404


@app.route("/voices/clip/delete", methods=["POST"])
def delete_clip():
    """Throw away a take. Same name matching as playback, for the same reason."""
    d    = request.get_json(silent=True) or {}
    vid  = (d.get("voice_id") or _ACTIVE_VOICE).strip()
    name = (d.get("name") or "").strip()
    for p in _discover_voice_samples(vid):
        if os.path.basename(p) == name:
            try:
                os.remove(p)
            except OSError as e:
                return jsonify({"ok": False, "error": str(e)}), 500
            # The transcript is only meaningful next to its audio, so it goes
            # too rather than being left behind to confuse a later listing.
            try:
                os.remove(_transcript_path(p))
            except OSError:
                pass
            print(f"[VOICE] Deleted {name} from '{vid}'")
            return jsonify({"ok": True, "n_clips": len(_discover_voice_samples(vid))})
    return jsonify({"ok": False, "error": "no such clip"}), 404


@app.route("/voices/select", methods=["POST"])
def select_voice():
    d = request.get_json(force=True) or {}
    vid = (d.get("voice_id") or "").strip()
    if not vid:
        return jsonify({"ok": False, "error": "voice_id required"}), 400
    res = switch_voice(vid)
    return jsonify(res), (200 if res.get("ok") else 400)


@app.route("/voices/create", methods=["POST"])
def create_voice():
    """Create an empty voice-profile folder. The user records passages into it
    from the dashboard (or drops in cleaned clips), then selects it."""
    d = request.get_json(force=True) or {}
    raw = (d.get("name") or "").strip()
    vid = re.sub(r"[^A-Za-z0-9_-]+", "_", raw).strip("_").lower()
    if not vid or vid == "default":
        return jsonify({"ok": False, "error": "Give the voice a simple name (letters/numbers)."}), 400
    path = _voice_dir(vid)
    if os.path.isdir(path):
        return jsonify({"ok": False, "error": f"Voice '{vid}' already exists."}), 400
    os.makedirs(path, exist_ok=True)
    print(f"[VOICE] Created profile '{vid}' → {path}")
    return jsonify({"ok": True, "voice_id": vid, "path": path})


@app.route("/voices/open", methods=["POST"])
def open_voice_folder():
    """Open the voice's clip folder in the OS file manager so the user can add,
    replace or edit the source recordings directly."""
    d = request.get_json(force=True) or {}
    vid = (d.get("voice_id") or _ACTIVE_VOICE).strip()
    path = _voice_dir(vid)
    if not os.path.isdir(path):
        os.makedirs(path, exist_ok=True)
    try:
        if os.name == "nt":
            os.startfile(path)               # Windows Explorer
        else:
            import subprocess as _sp
            _sp.Popen(["xdg-open" if os.uname().sysname == "Linux" else "open", path])
        return jsonify({"ok": True, "path": path})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e), "path": path}), 500


@app.route("/voices/passages", methods=["GET"])
def voice_passages():
    """The 12 standard recording passages for onboarding a new voice."""
    return jsonify({"passages": [
        {"n": i + 1, "title": t, "text": x} for i, (t, x) in enumerate(VOICE_PASSAGES)
    ]})


@app.route("/standby", methods=["GET"])
def standby_status():
    """Report model power state + idle-timeout config for the UI."""
    idle = _time.time() - _last_synth_ts if _last_synth_ts else 0
    return jsonify({
        "state": _model_state,                     # cold|ready|standby|waking
        "idle_timeout_sec": _IDLE_TIMEOUT_SEC,      # 0 = always on
        "idle_seconds": round(idle),
        "choices": _IDLE_CHOICES,
        # Hardware-adapted runtime hints for the extension.
        "prefetch_target": _PREFETCH_TARGET,
        "analysis_enabled": _ANALYSIS_ENABLED,
        "analysis_every": _ANALYSIS_EVERY,
        "hw_source": (_HW_PROFILE_CACHE or {}).get("source"),
        "hw_rtf": (_HW_PROFILE_CACHE or {}).get("rtf"),
    })

@app.route("/standby", methods=["POST"])
def standby_config():
    """Set the idle timeout (seconds; 0 = always on), or force wake/evict.
      {"idle_timeout_sec": 1800}  → idle after 30 min
      {"wake": true}              → wake now
      {"evict": true}             → evict now (manual standby)"""
    global _IDLE_TIMEOUT_SEC
    data = request.get_json(silent=True) or {}
    if "idle_timeout_sec" in data:
        try:
            _IDLE_TIMEOUT_SEC = max(0, int(data["idle_timeout_sec"]))
            _persist_idle_timeout(_IDLE_TIMEOUT_SEC)
            _touch_synth_activity()   # reset clock so a new setting starts fresh
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 400
    if data.get("wake"):
        wake_model()
    if data.get("evict"):
        evict_model()
    return jsonify({"ok": True, "state": _model_state, "idle_timeout_sec": _IDLE_TIMEOUT_SEC})

@app.route("/storage", methods=["GET"])
def storage_status():
    """Database size and retention limits, so disk use is visible not hidden."""
    try:
        return jsonify(_learner.storage_info())
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/autotune", methods=["GET"])
def autotune_status():
    """Report whether autonomous self-tuning is on, plus observation coverage."""
    try:
        return jsonify(_learner.autotune_info())
    except Exception as e:
        return jsonify({"enabled": False, "error": str(e)})

@app.route("/autotune", methods=["POST"])
def autotune_set():
    """Turn autonomous self-tuning on or off from the AI page. The flag used to
    be changeable only by hand-editing good_settings.json, since the toggle in
    the UI had no endpoint behind it."""
    d = request.get_json(force=True) or {}
    on = bool(d.get("enabled"))
    try:
        _learner.set_autotune(on)
        print(f"[LEARNER] Autonomous self-tuning {'ENABLED' if on else 'disabled'} by user")
        return jsonify(_learner.autotune_info())
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/autotune/report", methods=["GET"])
def autotune_report_route():
    """Rich self-tuning analysis for the AI tab: coverage, per-profile quality,
    recent autonomous changes, quality trend, plus live hardware/power state."""
    try:
        rep = _learner.autotune_report()
    except Exception as e:
        rep = {"error": str(e)}
    # Hardware and efficiency context, so the device and current power state.
    rep["device"] = device
    rep["model_state"] = _model_state
    rep["idle_timeout_sec"] = _IDLE_TIMEOUT_SEC
    rep["backend"] = DEV.backend
    rep["device_name"] = DEV.name
    used_mb, total_mb = _device.memory_used_mb(DEV)
    if used_mb is not None:
        rep["vram_used_mb"] = used_mb
    if total_mb is not None:
        rep["vram_total_mb"] = total_mb
    return jsonify(rep)

# NOTE: a second POST /autotune handler used to live here. Flask registers both,
# but URL matching takes the FIRST rule registered for a method, so it could
# never be reached, since autotune_set() above handles every POST /autotune.

@app.route("/settings", methods=["POST"])
def update_settings():
    """Update the live inference settings, which take effect on the next chunk.
    Accepts any of temperature, speed, repetition_penalty, top_k and top_p.
      {"reset": true}         restores the current default, saved or factory
      {"factory_reset": true} restores the original KAM defaults
      {"save_default": true}  saves the current live settings as the default"""
    d = request.json or {}
    if d.get("factory_reset"):
        global _DEFAULT_SETTINGS
        _DEFAULT_SETTINGS = dict(_FACTORY_SETTINGS)
        _LIVE_SETTINGS.update(_FACTORY_SETTINGS)
        try:
            if os.path.exists(_USER_DEFAULTS_PATH):
                os.remove(_USER_DEFAULTS_PATH)
        except Exception:
            pass
        return jsonify({"ok": True, "factory_reset": True, "settings": _LIVE_SETTINGS})
    if "analysis_every" in d:
        global _ANALYSIS_EVERY
        try:
            _ANALYSIS_EVERY = max(0, min(10, int(d["analysis_every"])))
            _learner.set_setting("analysis_every", _ANALYSIS_EVERY)
            print(f"[LEARNER] Quality analysis: "
                  + ("off" if _ANALYSIS_EVERY == 0 else f"every {_ANALYSIS_EVERY} chunk(s)"))
        except Exception:
            pass
        return jsonify({"ok": True, "analysis_every": _ANALYSIS_EVERY})
    if d.get("save_default"):
        _DEFAULT_SETTINGS = dict(_LIVE_SETTINGS)
        try:
            with open(_USER_DEFAULTS_PATH, "w") as f:
                json.dump(_LIVE_SETTINGS, f, indent=2)
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 500
        return jsonify({"ok": True, "saved_default": True, "settings": _LIVE_SETTINGS})
    if d.get("reset"):
        _LIVE_SETTINGS.update(_DEFAULT_SETTINGS)
        return jsonify({"ok": True, "reset": True, "settings": _LIVE_SETTINGS})
    updated = {}
    if "temperature" in d:
        _LIVE_SETTINGS["temperature"] = round(max(0.05, min(0.95, float(d["temperature"]))), 3)
        updated["temperature"] = _LIVE_SETTINGS["temperature"]
    if "speed" in d:
        _LIVE_SETTINGS["speed"] = round(max(0.5, min(2.0, float(d["speed"]))), 2)
        updated["speed"] = _LIVE_SETTINGS["speed"]
    if "repetition_penalty" in d:
        _LIVE_SETTINGS["repetition_penalty"] = round(max(1.0, min(10.0, float(d["repetition_penalty"]))), 2)
        updated["repetition_penalty"] = _LIVE_SETTINGS["repetition_penalty"]
    if "top_k" in d:
        _LIVE_SETTINGS["top_k"] = int(max(1, min(100, int(d["top_k"]))))
        updated["top_k"] = _LIVE_SETTINGS["top_k"]
    if "top_p" in d:
        _LIVE_SETTINGS["top_p"] = round(max(0.1, min(1.0, float(d["top_p"]))), 2)
        updated["top_p"] = _LIVE_SETTINGS["top_p"]
    return jsonify({"ok": True, "updated": updated, "settings": _LIVE_SETTINGS,
                    "defaults": _DEFAULT_SETTINGS})


# Two fixed benchmark sentences mixing numbers + tricky words, so the user can
# A/B their parameter changes on consistent material.
_BENCHMARK_SENTENCES = [
    "In 2024, the A-star search expanded 15,000 nodes per second.",
    "The heuristic h2 dominated h1 by 3.7 times, reaching 98.6 percent accuracy.",
]

@app.route("/benchmark", methods=["GET"])
def benchmark_sentences():
    """Return the fixed benchmark sentences so the popup can speak them with the
    current live settings for an A/B comparison."""
    return jsonify({"sentences": _BENCHMARK_SENTENCES})


@app.route("/benchmark/run", methods=["POST"])
def benchmark_run_route():
    """Measure synthesis speed on this machine, now.

    Runs on a background thread and returns immediately: the measurement takes
    ~15s and each sentence is printed as it completes, so the dashboard console
    shows it happening live rather than the request appearing to hang. Poll
    GET /benchmark/result for the finished numbers."""
    with _bench_lock:
        if _bench_running:
            return jsonify({"ok": False, "running": True,
                            "error": "a benchmark is already running"}), 409
    _threading.Thread(target=_run_benchmark, kwargs={"source": "server"},
                      daemon=True).start()
    return jsonify({"ok": True, "started": True,
                    "message": "Benchmarking — watch the console for results."})


@app.route("/benchmark/result", methods=["GET"])
def benchmark_result_route():
    """The most recent measurement, plus what it means in plain language."""
    if _LAST_BENCH:
        return jsonify({**_LAST_BENCH, "running": _bench_running})
    hp = _HW_PROFILE_CACHE or {}
    rtf = hp.get("rtf")
    return jsonify({
        "ok": rtf is not None,
        "running": _bench_running,
        "device": DEV.as_dict(),
        "profile": hp,
        "band": _bench.rtf_band(rtf)[0],
        "verdict": _bench.verdict_lines(DEV, hp if rtf is not None else None),
    })


@app.route("/device", methods=["GET"])
def device_route():
    """What KAM is running on, and what else it could run on.

    Everything the dashboard needs to explain the machine to the user: the
    chosen backend, why it was chosen, alternatives available, and current
    accelerator memory use."""
    info = DEV.as_dict()
    used_mb, total_mb = _device.memory_used_mb(DEV)
    info["memory_used_mb"] = used_mb
    info["memory_total_mb"] = total_mb
    info["model_state"] = _model_state
    info["benchmarking"] = _bench_running
    hp = _HW_PROFILE_CACHE or {}
    info["rtf"] = hp.get("rtf")
    info["rtf_band"] = _bench.rtf_band(hp.get("rtf"))[0]
    return jsonify(info)

# NOTE: POST /chunk/feedback used to live here. It was superseded by
# /chunk/verdict (which records the same thumbs up/down plus the reason and the
# learning action taken), no client ever called it, and it ran an ALTER TABLE on
# every single request to create a column the migrations already add.


def _store_verdict(chunk_id, verdict, note, applied_text):
    """Persist a user verdict and keep the durable lifetime totals honest."""
    with _learner._db_lock:
        conn = _learner._get_db()
        # Read the prior verdict so re-rating the same chunk doesn't double-count
        # the durable lifetime totals.
        prior_row = conn.execute("SELECT user_feedback FROM chunks WHERE id=?", (chunk_id,)).fetchone()
        prior = prior_row[0] if prior_row else None
        legacy = "positive" if verdict == "sounded_perfect" else "negative"
        conn.execute("UPDATE chunks SET user_feedback=?, user_verdict=?, user_note=?, applied_action=? WHERE id=?",
                     (legacy, verdict, note or "", applied_text, chunk_id))
        conn.commit(); conn.close()
    # Durable lifetime totals (survive clears), only when the verdict newly
    # changes to this class, which keeps Reports and Stats consistent.
    if legacy != prior:
        _learner.bump_counter("perfect_total" if legacy == "positive" else "negative_total", 1)


@app.route("/chunk/verdict", methods=["POST"])
def chunk_verdict():
    """Rich verdict feedback (AI feed verdicts + live-feed thumbs)."""
    d = request.json or {}
    chunk_id   = d.get("chunk_id")
    chunk_text = d.get("chunk_text") or ""
    verdict    = d.get("verdict")
    token      = (d.get("token")     or "").strip()
    expected   = (d.get("expected")  or "").strip()
    note       = (d.get("note")      or "").strip()
    corrected  = (d.get("corrected") or "").strip()
    if not chunk_id or not verdict:
        return jsonify({"error": "chunk_id and verdict required"}), 400
    applied_text = ""
    if verdict == "sounded_perfect":
        res = None
        if hasattr(_learner, "confirm_chunk_quality"):
            try: res = _learner.confirm_chunk_quality(chunk_id)
            except Exception as e: print(f"[LEARNER] reinforce failed: {e}")
        if res and res.get("reinforced"):
            b = res["new_baseline"]
            applied_text = (f"Baseline reinforced - pitch std {b.get('pitch_std','?')}, "
                            f"sample {b.get('sample_count','?')}. Positive exemplar.")
        else:
            applied_text = "Marked good. Baseline updates once analysis completes."
        print(f"[LEARNER] ✨ sounded_perfect {chunk_id[:8]}")
    elif verdict == "mostly_right":
        _learner._add_rule("MONITOR", chunk_id, note[:80] if note else "minor_issue", "", source="REPORT")
        applied_text = (('Logged minor issue: "'+note[:60]+'".') if note else "Logged minor issue. Watching for recurrence.")
        print(f"[LEARNER] ⚠ mostly_right {chunk_id[:8]}")
    elif verdict == "wrong_word":
        if not token or not expected:
            return jsonify({"error": "token and expected required"}), 400
        try:
            _learner._add_to_pronunciation_store(token, expected)
            _learner._add_rule("PRONUNCIATION", token, "user_correction", expected, source="REPORT")
        except Exception as e: print(f"[LEARNER] pronunciation add failed: {e}")
        applied_text = 'Pronunciation rule added - "'+token+'" -> "'+expected+'".'
        print(f"[LEARNER] ✎ wrong_word {token}->{expected}")
    elif verdict == "fix_punctuation":
        if not corrected:
            return jsonify({"error": "corrected text required"}), 400
        edits = 0
        try:
            res = _learner.learn_punctuation_correction(chunk_text, corrected)
            edits = res.get("edits", 0) if res else 0
        except Exception as e: print(f"[LEARNER] punctuation learn failed: {e}")
        applied_text = f"Corrected phrasing saved ({edits} edit(s))."
        print(f"[LEARNER] ✏ fix_punctuation {chunk_id[:8]}")
    elif verdict == "voice_off":
        # Direction comes from the measured pitch variance: a flat, monotone
        # chunk wants more variation, an erratic one wants less.
        delta = 0.02
        try:
            with _learner._db_lock:
                conn = _learner._get_db()
                row = conn.execute("SELECT pitch_variance FROM chunks WHERE id=?", (chunk_id,)).fetchone()
                conn.close()
            pv = row[0] if row else None
        except Exception: pv = None
        if pv is not None and pv > 12.0: delta = -0.02
        # Adjust this chunk's PROFILE, not the user's live temperature slider.
        # This was the last place a per-chunk verdict reached out and moved a
        # global control the user owns, so one report on one sentence re-tuned
        # every kind of text, and silently overwrote a value they had set by
        # hand. The profile hierarchy already handles the "the whole voice is
        # off" case: repeated reports accumulate into the coarser keys, which is
        # near-global anyway, but only on the evidence.
        try:
            prof = _learner.lookup_chunk_profile(chunk_id, chunk_text)
            _key, new_t = _learner._adjust_profile_temperature(prof["keys"], delta)
            _learner.log_history("TEMP",
                f"🎚 voice_off profile temp {'+' if delta > 0 else ''}{delta} → {new_t} "
                f"({prof['band']},{prof['length']},{prof['punct']},{prof['lexis']})", "user")
            applied_text = (
                f"Temperature {'raised' if delta > 0 else 'lowered'} to {new_t} for "
                f"profile [{prof['band']}/{prof['length']}/{prof['punct']}/{prof['lexis']}]"
                + (" (pitch was erratic)" if delta < 0 else " (pitch was flat)") + ".")
            print(f"[LEARNER] 🎚 voice_off profile temp → {new_t} (pitch variance {pv})")
        except Exception as e:
            print(f"[LEARNER] voice_off adjust failed: {e}")
            applied_text = "Voice issue logged."
    elif verdict == "sounded_wrong":
        # A one-click hallucination report, which learns the same way as a
        # HALLUCINATION report with no named token: lower the temperature for the
        # chunk's full
        # composite PROFILE (fingerprint), not just its sentence type, so the
        # fix targets the actual failure pattern and converges instead of
        # oscillating a broad bucket. Legacy per-type kept in sync as fallback.
        stype = "sentence"
        try:
            # The fingerprint the chunk was SYNTHESISED under, read back from
            # its stored row, rather than recomputed from the display text.
            prof  = _learner.lookup_chunk_profile(chunk_id, chunk_text)
            stype = prof["sentence_type"]
            _key, new_t = _learner._adjust_profile_temperature(prof["keys"], -0.03)
            _learner._adjust_pref_temperature(stype, -0.03)   # legacy sync
            _learner.log_history("TEMP",
                f"👎 hallucination profile temp -0.03 → {new_t} "
                f"({prof['band']},{prof['length']},{prof['punct']},{prof['lexis']})", "user")
            applied_text = (f"Hallucination logged. Temperature lowered to {new_t} "
                            f"for profile [{prof['band']}/{prof['length']}/{prof['punct']}/{prof['lexis']}].")
        except Exception as e:
            print(f"[LEARNER] sounded_wrong adjust failed: {e}")
            applied_text = "Hallucination logged."
        print(f"[LEARNER] 👎 sounded_wrong {chunk_id[:8]} ({stype})")
    elif verdict == "revert":
        # Undo a previous thumbs up/down on this chunk: clear its feedback,
        # decrement the durable counter, and reverse the learning nudge so a
        # mislabel doesn't permanently skew the model.
        prior = None
        try:
            with _learner._db_lock:
                conn = _learner._get_db()
                row = conn.execute("SELECT user_feedback, sentence_type FROM chunks WHERE id=?", (chunk_id,)).fetchone()
                conn.close()
            prior = row[0] if row else None
            stype = (row[1] if row and len(row) > 1 else None) or _learner._lookup_chunk_type(chunk_id, chunk_text)
        except Exception as e:
            print(f"[LEARNER] revert lookup failed: {e}")
            stype = "sentence"
        if prior == "negative":
            # Reverse the -0.03 hallucination nudge on BOTH the composite profile
            # and the legacy per-type value (matches the sounded_wrong path).
            # Same stored fingerprint, so the revert lands on the key the
            # original nudge moved.
            try:
                prof = _learner.lookup_chunk_profile(chunk_id, chunk_text)
                _key, new_t = _learner._adjust_profile_temperature(prof["keys"], +0.03)
                _learner._adjust_pref_temperature(stype, +0.03)   # legacy sync
                _learner.bump_counter("negative_total", -1)
                _learner.log_history("TEMP", f"↩ reverted hallucination profile temp +0.03 → {new_t}", "user")
                applied_text = f"Reverted. Temperature restored to {new_t}."
            except Exception as e:
                print(f"[LEARNER] revert(neg) failed: {e}")
                applied_text = "Reverted hallucination flag."
        elif prior == "positive":
            # Remove the reinforcement sample and decrement the counter.
            try:
                _learner.bump_counter("perfect_total", -1)
                if hasattr(_learner, "unconfirm_chunk_quality"):
                    _learner.unconfirm_chunk_quality(chunk_id)
                _learner.log_history("CONFIRM", "↩ reverted a perfect mark", "user")
                applied_text = "Reverted. Reinforcement from this chunk removed."
            except Exception as e:
                print(f"[LEARNER] revert(pos) failed: {e}")
                applied_text = "Reverted perfect mark."
        else:
            applied_text = "Nothing to revert on this chunk."
        # Clear the stored verdict columns.
        try:
            with _learner._db_lock:
                conn = _learner._get_db()
                conn.execute("UPDATE chunks SET user_feedback=NULL, user_verdict=NULL, applied_action=NULL WHERE id=?", (chunk_id,))
                conn.commit(); conn.close()
        except Exception as e:
            print(f"[LEARNER] revert clear failed: {e}")
        print(f"[LEARNER] ↩ revert {chunk_id[:8]} (was {prior})")
        return jsonify({"ok": True, "verdict": "revert", "applied_text": applied_text, "was": prior})
    elif verdict == "skip":
        _learner._add_rule("SUPPRESS", chunk_id, "user_skipped", "", source="REPORT")
        applied_text = "Suppressed. Similar chunks will not be flagged again."
        print(f"[LEARNER] ⤷ skip {chunk_id[:8]}")
    else:
        return jsonify({"error": f"unknown verdict: {verdict}"}), 400
    _store_verdict(chunk_id, verdict, note, applied_text)
    return jsonify({"ok": True, "verdict": verdict, "applied_text": applied_text})


@app.route("/console", methods=["GET"])
def console_stream():
    """Stream console log lines to the player.html dashboard."""
    since = int(request.args.get("since", 0))
    with _console_lock:
        total  = _console_cursor
        lines  = list(_console_log)
    # Return lines since cursor position
    start   = max(0, len(lines) - (total - since))
    new_lines = lines[start:]
    return jsonify({
        "lines":  new_lines,
        "cursor": total,
        "status": "online"
    })

if __name__ == "__main__":
    # Single-instance guard: if something is already serving on 5050, exit
    # immediately instead of starting a second server. Two servers competing for
    # the port is what caused double model-loads and frozen playback.
    import socket as _socket
    _probe = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
    try:
        _probe.settimeout(0.5)
        if _probe.connect_ex(("127.0.0.1", 5050)) == 0:
            print("[STARTUP] Port 5050 already in use — another server is running. Exiting.", flush=True)
            _stage("duplicate-aborted")
            import sys as _sx
            _sx.exit(0)
    except Exception:
        pass
    finally:
        _probe.close()

    # Load the model and warm up, only here in __main__. A re-import under the
    # name "server" never reaches this, so startup can't run twice.
    #
    # Missing reference clips is the one failure every fresh install hits, since
    # voice audio is personal and cannot ship with the code. Letting it surface
    # as a traceback made a normal setup step look like a crash, so it gets
    # instructions instead. Anything else still raises properly, because a
    # traceback is the right answer for an actual bug.
    try:
        _run_startup()
    except NoVoiceClipsError:
        BAR = "=" * 62
        print(f"\n{C.WARN}{C.BOLD}{BAR}{C.RESET}")
        print(f"  {C.WARN}{C.BOLD}No voice to clone yet{C.RESET}")
        print(f"{C.WARN}{C.BOLD}{BAR}{C.RESET}")
        print("  KAM speaks in a cloned voice, so it needs reference audio")
        print("  before it can start. Nothing is broken, this is the one setup")
        print("  step that cannot ship with the code.")
        print()
        print(f"  Put 6-10 WAV clips, 8-15 seconds each, in:")
        print(f"    {C.BOLD}{VOICE_SAMPLES_DIR}{C.RESET}")
        print()
        print("  Clean speech, no music or processing. Separate clips clone")
        print("  better than one long file, since XTTS averages across them.")
        print("  The dashboard lists 16 passages to read under the microphone")
        print("  tab, and they work well because they cover a good range.")
        print()
        print(f"  Then check them with:  {C.BOLD}python check_voice_clips.py{C.RESET}")
        print(f"  and start the server again.")
        print(f"{C.WARN}{C.BOLD}{BAR}{C.RESET}\n")
        _stage("no-voice-clips")
        import sys as _sx
        _sx.exit(1)

    BAR = "=" * 50
    print(f"\n{C.OK}{C.BOLD}{BAR}{C.RESET}")
    print(f"  {C.OK}{C.BOLD}TTS Server  →  http://localhost:5050{C.RESET}")
    print(f"  {C.DIM}Pronunciation store: {STORE_PATH}{C.RESET}")
    print(f"{C.OK}{C.BOLD}{BAR}{C.RESET}")
    print(f"  {C.OK}{C.BOLD}Model is ready.{C.RESET}\n")
    # Register live settings with learner so Whisper can auto-tune temperature
    _learner.register_live_settings(_LIVE_SETTINGS)

    # --- Silence all PowerShell output from this point forward ---
    import logging as _logging, warnings as _warnings
    _logging.getLogger('werkzeug').setLevel(_logging.ERROR)
    # Suppress Whisper CPU warning
    _warnings.filterwarnings('ignore', message='Performing inference on CPU')
    # Redirect stdout so print() reaches the dashboard buffer, but ALSO keep
    # writing to the real stdout. The native-messaging host (kam_host.py) reads
    # the process's real stdout to stream startup logs and detect the MODEL
    # READY marker, so I can't swallow it. I tee to both.
    import sys as _sys
    _real_stdout = _sys.stdout

    # The host now detects readiness via the .kam_ready sentinel FILE (written
    # below) rather than by parsing stdout, so the dashboard should get each log
    # line through exactly one channel, which is the /console HTTP poll. This
    # sink used to tee to both _clog, feeding the /console poll, and the real
    # stdout pipe, feeding the host and then the dashboard, so every line showed
    # up twice in the console. That was the "duplication" I was seeing: one
    # server, displayed twice. So I write to _clog only.
    class _DashboardOnly:
        """Route print() to the in-app dashboard buffer only (single channel)."""
        def write(self, msg):
            line = msg.rstrip('\n')
            if line.strip():
                try:
                    _clog(line)
                except Exception:
                    pass
        def flush(self):
            pass
        def fileno(self): return 1   # satisfy Flask's check
        def isatty(self): return False

    _sys.stdout = _DashboardOnly()

    # Explicit marker the native-messaging host watches for to flip the
    # dashboard power button to "Model Ready". Keep the literal text in sync
    # with kam_host.py READY_MARKERS.
    print("[STARTUP] MODEL READY — serving on http://127.0.0.1:5050", flush=True)

    # Buffering-proof readiness signal: write a sentinel file the host can poll.
    # stdout can be block-buffered when launched via a pipe, so the host watches
    # for this file rather than relying solely on seeing the printed marker.
    try:
        with open(_READY_SENTINEL, "w") as _rf:
            _rf.write("ready")
    except Exception:
        pass

    # use_reloader=False is explicit: the Werkzeug auto-reloader re-executes the
    # module in a child process, which re-ran the whole startup (second model
    # load + duplicate banner). debug=False usually disables it, but we force it
    # off so a second server can never be spawned this way.
    app.run(host="127.0.0.1", port=5050, debug=False, threaded=True, use_reloader=False)
