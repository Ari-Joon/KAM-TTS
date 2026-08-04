"""
run_tests.py — run every KAM test suite and report a single verdict.

    python tests/run_tests.py          run everything
    python tests/run_tests.py pipeline run the suites whose name contains that

None of these need torch, TTS, whisper or spaCy. The heavy imports are stubbed
so the suites run on whatever Python you have to hand, which matters because the
server itself runs on a specific 3.10 install and I do not want testing to
depend on that. The JavaScript suite needs node and is skipped if it is missing.

Nothing here touches the live database or the learned settings. The suites that
need them copy to a temp directory first.
"""
import pathlib
import shutil
import subprocess
import sys

# The suites print stars, arrows and box characters, and we relay their output
# verbatim on failure. Without this the runner itself dies with a
# UnicodeEncodeError while reporting a failure, which hides the actual result.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HERE = pathlib.Path(__file__).resolve().parent

SUITES = [
    ("pipeline",      "test_pipeline.py",     "text cleaning, prosody labelling, audio"),
    ("learner",       "test_learner.py",      "migrations, diagnosis, rule cache, reports"),
    ("modules",       "test_modules.py",      "device selection, clip gate, hallucination checks"),
    ("startup",       "test_startup.py",      "startup across CUDA/ROCm/MPS/XPU/CPU"),
    ("reinforcement", "test_reinforcement.py","what the learning loop reinforces"),
    ("extension_id",  "test_extension_id.py", "which extension origin the server trusts"),
    ("facets",        "test_facets.py",       "content-facet labelling accuracy"),
    ("audio",         "test_audio.py",        "gain staging and clipping"),
    ("feed",          "test_feed.mjs",        "dashboard live-feed pause"),
]


def main():
    only = sys.argv[1] if len(sys.argv) > 1 else None
    node = shutil.which("node")
    results, skipped = [], []

    for name, filename, blurb in SUITES:
        if only and only not in name:
            continue
        path = HERE / filename
        if not path.exists():
            skipped.append((name, "file missing"))
            continue
        if filename.endswith(".mjs") and not node:
            skipped.append((name, "node not installed"))
            continue

        cmd = [node, str(path)] if filename.endswith(".mjs") else [sys.executable, str(path)]
        proc = subprocess.run(cmd, cwd=HERE, capture_output=True, text=True,
                              encoding="utf-8", errors="replace")
        # Each suite prints its own "N passed, M failed" as the last real line.
        tail = [l for l in (proc.stdout or "").splitlines() if l.strip()]
        summary = next((l.strip() for l in reversed(tail) if "passed" in l), "no summary")
        ok = proc.returncode == 0
        results.append((name, ok, summary, blurb, proc))
        print(f"  {'PASS' if ok else 'FAIL'}  {name:14} {summary:24} {blurb}")

    for name, why in skipped:
        print(f"  SKIP  {name:14} {why}")

    failed = [r for r in results if not r[1]]
    if failed:
        print("\n" + "=" * 70)
        for name, _ok, _s, _b, proc in failed:
            print(f"--- {name} ---")
            print((proc.stdout or "")[-1500:])
            if proc.stderr.strip():
                print((proc.stderr or "")[-800:])
    total = len(results)
    print("\n" + "=" * 70)
    print(f"  {total - len(failed)}/{total} suites passed"
          + (f", {len(skipped)} skipped" if skipped else ""))
    print("=" * 70)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
