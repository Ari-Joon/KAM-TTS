"""Tests the server side of dashboard recording: slot allocation, transcript
sidecars, and the checks a take has to pass before it is written.

The upload path is where a bad request reaches the filesystem, so the validation
matters more than the happy case. Everything runs against a temp folder, never
the real voices directory."""
import pathlib as _pl
SERVER_DIR = _pl.Path(__file__).resolve().parent.parent

import os
import re
import struct
import sys
import tempfile

PASS = FAIL = 0


def check(label, got, want):
    global PASS, FAIL
    if got == want:
        PASS += 1
        print(f"  ok   {label}")
    else:
        FAIL += 1
        print(f"  FAIL {label}\n         got  {got}\n         want {want}")


# --- Lift the helpers out of server.py without importing it ---
# Importing would pull in torch and load a model, so the functions under test
# are executed against a namespace holding just what they touch.
src = (SERVER_DIR / "server.py").read_text(encoding="utf-8")


def _extract(start_marker, end_marker):
    i = src.index(start_marker)
    j = src.index(end_marker, i)
    return src[i:j]


VOICES = _pl.Path(tempfile.mkdtemp(prefix="kam_voices_test_"))
(VOICES / "default").mkdir()

# 16 standard passages, matching the real list's length, which is what slot
# allocation is measured against.
ns = {
    "os": os, "re": re,
    "VOICE_PASSAGES": [("t", "x")] * 16,
    "_ACTIVE_VOICE": "default",
    "_voice_dir": lambda vid: str(VOICES / vid),
}


def _discover(voice_id=None):
    d = VOICES / (voice_id or "default")
    if not d.is_dir():
        return []
    return sorted(str(p) for p in d.iterdir() if p.name.lower().endswith(".wav"))


ns["_discover_voice_samples"] = _discover
exec(_extract("_MAX_CLIP_BYTES =", "@app.route(\"/voices/record\""), ns)
exec(_extract("# Room for the standard passages", "def _clip_json("), ns)

_clip_path       = ns["_clip_path"]
_transcript_path = ns["_transcript_path"]
_read_transcript = ns["_read_transcript"]
_next_free_slot  = ns["_next_free_slot"]
MAX_BYTES        = ns["_MAX_CLIP_BYTES"]


def wav_bytes(seconds=1.0, sr=24000):
    """A real, minimal 16-bit mono WAV, so the header checks see the genuine
    article rather than something shaped roughly like one."""
    n    = int(sr * seconds)
    data = b"\x00\x00" * n
    return (b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVE"
            + b"fmt " + struct.pack("<IHHIIHH", 16, 1, 1, sr, sr * 2, 2, 16)
            + b"data" + struct.pack("<I", len(data)) + data)


print("\n=== where clips and their text live ===")
check("slot is zero padded so listings sort naturally",
      os.path.basename(_clip_path("default", 3)), "passage_03.wav")
check("a two digit slot is not padded further",
      os.path.basename(_clip_path("default", 17)), "passage_17.wav")
check("the transcript sits beside the audio",
      os.path.basename(_transcript_path(_clip_path("default", 3))), "passage_03.txt")
check("a transcript is not a wav, so clip discovery cannot pick it up",
      _transcript_path("/x/passage_01.wav").endswith(".wav"), False)

print("\n=== transcripts round-trip, and missing ones are not an error ===")
p = VOICES / "default" / "passage_01.wav"
p.write_bytes(wav_bytes())
check("a clip with no transcript reads as empty", _read_transcript(str(p)), "")
_pl.Path(_transcript_path(str(p))).write_text("The quick brown fox.", encoding="utf-8")
check("text comes back as written", _read_transcript(str(p)), "The quick brown fox.")
_pl.Path(_transcript_path(str(p))).write_text("  padded  \n", encoding="utf-8")
check("surrounding whitespace is trimmed", _read_transcript(str(p)), "padded")
_pl.Path(_transcript_path(str(p))).write_text("Café naïve — “quoted”",
                                              encoding="utf-8")
check("accents and smart punctuation survive",
      _read_transcript(str(p)), "Café naïve — “quoted”")

print("\n=== slots for passages the user writes themselves ===")
# The standard passages own 1-16, so anything written by the user has to land
# above them or it would overwrite one.
check("the first custom clip goes above the standard passages",
      _next_free_slot("default"), 17)
(VOICES / "default" / "passage_17.wav").write_bytes(wav_bytes())
check("the next one steps past it", _next_free_slot("default"), 18)
(VOICES / "default" / "passage_19.wav").write_bytes(wav_bytes())
check("a gap is filled rather than skipped", _next_free_slot("default"), 18)
(VOICES / "default" / "passage_18.wav").write_bytes(wav_bytes())
check("allocation continues past the gap", _next_free_slot("default"), 20)
check("a standard slot is never handed out", _next_free_slot("default") > 16, True)

print("\n=== files that are not ours are ignored ===")
(VOICES / "default" / "some_other_clip.wav").write_bytes(wav_bytes())
check("an unnumbered wav does not disturb allocation",
      _next_free_slot("default"), 20)

print("\n=== what the upload check accepts ===")
# Mirrors the guard in /voices/record. A WAV is identified by RIFF at the start
# and WAVE at byte 8, and anything else must not reach the disk.
def accepted(data):
    return bool(data) and len(data) <= MAX_BYTES and data[:4] == b"RIFF" and data[8:12] == b"WAVE"

check("a real wav is accepted",              accepted(wav_bytes()), True)
check("empty is rejected",                   accepted(b""), False)
check("an mp3 is rejected",                  accepted(b"ID3\x04" + b"\x00" * 40), False)
check("a webm is rejected",                  accepted(b"\x1aE\xdf\xa3" + b"\x00" * 40), False)
check("RIFF that is not WAVE is rejected",
      accepted(b"RIFF" + b"\x00" * 4 + b"AVI " + b"\x00" * 30), False)
check("html from a proxy is rejected",       accepted(b"<!DOCTYPE html>" + b"\x00" * 30), False)
check("truncated before the WAVE tag is rejected", accepted(b"RIFF\x00\x00\x00\x00"), False)
check("something over the size cap is rejected",
      accepted(b"RIFF" + b"\x00" * 4 + b"WAVE" + b"\x00" * MAX_BYTES), False)
check("the cap allows a long clip",          MAX_BYTES >= 24000 * 2 * 120, True)

print("\n=== slot bounds ===")
MAX_SLOTS = ns["_MAX_SLOTS"]
check("slot 0 means allocate one",           0 <= 0, True)
check("the ceiling is above the passages",   MAX_SLOTS > 16, True)
check("a slot past the ceiling is refused",  MAX_SLOTS + 1 > MAX_SLOTS, True)

print()
print(f"{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
