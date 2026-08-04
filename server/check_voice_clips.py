"""
check_voice_clips.py — objective quality gate for voice reference clips.

Run this against a folder of .wav clips BEFORE loading them into voice_samples/.
It cannot hear them (only you can judge that), but it catches the technical
problems that quietly ruin a cloned voice: wrong sample rate, clipping,
excessive silence, audible room noise, or clips that are too short or too long.

Usage:
    python check_voice_clips.py                 # checks ./voice_samples
    python check_voice_clips.py path/to/folder  # checks a specific folder
    python check_voice_clips.py voices/borat    # checks a named voice profile

Exit code is 0 only if every clip passes (no FAILs), so it can gate a script.

The measurement lives in audio_quality.py, which the SERVER also uses: clips
that fail here are the same clips the server excludes from the speaker embedding
when it computes latents. So this tool tells you in advance exactly what the
server will do, rather than being a separate opinion about the same files.

Guidance encoded there (tuned for XTTS-v2 zero-shot cloning):
  - Target sample rate 22050 Hz mono (XTTS native). Higher is fine; it resamples.
  - Each clip 4-20s of speech; 8-15s is the sweet spot.
  - No hard clipping (peaks at digital max = distortion baked into the clone).
  - Silence ratio under ~65%. This counts natural pauses between sentences as
    well as dead air, so normal read-aloud speech measures 40-50% — only
    clearly excessive gaps are flagged.
  - Speech well above the noise floor: a noisy room is cloned along with you.
"""
import os
import sys

# Windows consoles still default to a legacy codepage (cp1252), where printing
# any non-ASCII character raises UnicodeEncodeError and kills the script. This
# has to run before anything prints, and cannot live in a shared import,
# because an import that itself printed would crash first.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import audio_quality as _aq


def main():
    folder = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "voice_samples")
    if not os.path.isdir(folder):
        print(f"Folder not found: {folder}")
        print("Create it and drop your .wav clips in, then run this again.")
        sys.exit(2)

    clips = sorted(f for f in os.listdir(folder) if f.lower().endswith(".wav"))
    if not clips:
        print(f"No .wav files in {folder}")
        sys.exit(2)

    print(f"Checking {len(clips)} clip(s) in {folder}\n" + "─" * 60)
    reports = [_aq.analyse_clip(os.path.join(folder, name), name) for name in clips]

    for r in reports:
        print(r.line())
        for note in r.reasons:
            print(f"         - {note}")

    passes = sum(1 for r in reports if r.verdict == "PASS")
    fails = [r for r in reports if r.verdict == "FAIL"]

    print("─" * 60)
    print(f"{passes}/{len(reports)} clean pass.")
    if fails:
        print(f"{len(fails)} clip(s) FAILED and the server will EXCLUDE them from "
              f"the voice: {', '.join(r.name for r in fails)}")
        print("Fix or remove them — XTTS averages every clip into one speaker "
              "embedding, so a bad clip degrades the whole voice.")
    else:
        print("All usable. Aim for 8-12 clips covering different sentence types "
              "(statements, a question, a list, numbers) for the best clone.")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
