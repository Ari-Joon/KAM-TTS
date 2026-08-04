"""
hardware_profile.py — detect this machine's capability and recommend settings.

Run once per install, from the server folder:
    python hardware_profile.py            measure and save a profile
    python hardware_profile.py --no-test  report the machine, skip the timing run
    python hardware_profile.py --device cpu   force a backend (also KAM_DEVICE)

What it does
------------
1. Reports what KAM will actually run on: which compute backends exist here,
   which one it picked and why, plus GPU/VRAM/CPU/RAM.
2. Measures the real cost of synthesis on THIS machine — model load, latent
   compute, warmup, and three timed inferences — and derives the real-time
   factor (RTF): seconds of compute per second of audio produced.
3. Turns those measurements into concrete recommendations and writes
   hardware_profile.json, which the server reads on boot to tune its defaults.

Why RTF is the number that matters
----------------------------------
KAM reads continuously, so synthesis must keep ahead of playback. RTF < 1.0
means a chunk is produced faster than it is spoken — playback never stalls.
RTF > 1.0 means the reader will pause waiting for audio.

The measurements are honest: nothing here is estimated from model size or GPU
name. It runs the same inference path the server uses, through the same shared
benchmark module (benchmark.py), so the numbers here and the numbers the server
prints to the dashboard are produced by identical code.

The server also self-benchmarks on first boot for a given machine, so running
this is optional — it exists for people who want the detail in a terminal, or
who want to measure before installing the extension.
"""
import os
import sys
import shutil

# Windows consoles still default to a legacy codepage (cp1252), where printing
# any non-ASCII character raises UnicodeEncodeError and kills the script. This
# has to run before anything prints, and cannot live in a shared import,
# because an import that itself printed would crash first.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import device as _device
import benchmark as _bench

HERE = os.path.dirname(os.path.abspath(__file__))
PROFILE_PATH = os.path.join(HERE, "hardware_profile.json")
VOICE_DIR = os.path.join(HERE, "voice_samples")


# ── Machine inventory ────────────────────────────────────────────────────────

def print_machine(dev):
    print("─" * 62)
    print("MACHINE")
    print("─" * 62)
    if not dev.torch_version:
        print("  PyTorch      NOT INSTALLED")
    else:
        print(f"  PyTorch      {dev.torch_version}")
    print(f"  Backend      {dev.label}"
          + (f"  (build {dev.build})" if dev.build else ""))
    if dev.is_accelerated:
        print(f"  Accelerator  {dev.name}")
        if dev.vram_gb:
            print(f"  VRAM         {dev.vram_gb} GB")
        if dev.capability:
            print(f"  Compute      {dev.capability}")
    else:
        print("  Accelerator  none detected — CPU-only inference")
    print(f"  CPU cores    {dev.cpu_cores}")
    print(f"  RAM          {dev.ram_gb if dev.ram_gb else 'unknown'} GB")
    print(f"  ffmpeg       {'found' if shutil.which('ffmpeg') else 'not found (optional)'}")
    if len(dev.available) > 1:
        print(f"  Also usable  {', '.join(b for b in dev.available if b != dev.torch_device)}"
              f"   (force with --device or KAM_DEVICE)")
    for note in dev.notes:
        print(f"  note         {note}")


# ── Capability gate ──────────────────────────────────────────────────────────

def viability(dev):
    """Return (verdict, notes) judging whether KAM can run usefully here."""
    notes = []
    if not dev.torch_version:
        return "BLOCKED", ["PyTorch is not installed — install it from pytorch.org"]

    if dev.is_accelerated:
        v = dev.vram_gb or 0
        if dev.backend == "mps":
            verdict = "OK"
            notes.append("Apple Silicon: XTTS runs in fp32 with a CPU fallback "
                         "for unimplemented ops. Slower than CUDA but usable.")
        elif v and v < 4:
            notes.append(f"{v} GB VRAM is tight for XTTS-v2; expect out-of-memory "
                         "errors on long chunks. 6 GB+ recommended.")
            verdict = "MARGINAL"
        elif v and v < 6:
            notes.append(f"{v} GB VRAM will work but leaves little headroom. "
                         "Use a shorter idle-standby timeout to free VRAM.")
            verdict = "OK"
        else:
            verdict = "GOOD"
        if dev.backend == "rocm":
            notes.append("ROCm support in PyTorch is less mature than CUDA; if "
                         "synthesis errors, force CPU with KAM_DEVICE=cpu.")
    else:
        verdict = "CPU-ONLY"
        notes.append("No GPU backend available. Synthesis will be several times "
                     "slower than playback; KAM will pause to buffer. Usable for "
                     "short passages, not comfortable for continuous reading.")

    cores = dev.cpu_cores or 0
    if cores and cores < 4:
        notes.append(f"{cores} CPU cores is below the recommended 4 — Whisper "
                     "analysis competes with synthesis.")
    if dev.ram_gb and dev.ram_gb < 8:
        notes.append(f"{dev.ram_gb} GB RAM is below the 8 GB minimum.")
    if dev.fell_back:
        notes.append("A GPU backend was detected but failed a test operation, so "
                     "CPU was selected instead. See the note above.")
    return verdict, notes


# ── Recommendations ──────────────────────────────────────────────────────────

def recommend(dev, perf):
    print("\n" + "─" * 62)
    print("RECOMMENDATION")
    print("─" * 62)

    verdict, notes = viability(dev)
    label = {
        "GOOD":     "Good — comfortable for continuous reading.",
        "OK":       "Usable — should keep up with playback.",
        "MARGINAL": "Marginal — expect occasional stalls or memory errors.",
        "CPU-ONLY": "CPU-only — workable for short passages, not continuous reading.",
        "BLOCKED":  "Cannot run yet.",
    }[verdict]
    print(f"  Verdict: {label}")
    for n in notes:
        print(f"    - {n}")

    if not perf:
        print("\n  Add voice clips and re-run to get speed-based advice.")
        return

    print()
    for line in _bench.verdict_lines(dev, perf):
        print(f"  {line}")


def main():
    print("\nKAM TTS — hardware profile\n")

    forced = None
    if "--device" in sys.argv:
        i = sys.argv.index("--device")
        if i + 1 < len(sys.argv):
            forced = sys.argv[i + 1]

    dev = _device.resolve(forced)
    print_machine(dev)

    verdict, _ = viability(dev)
    if verdict == "BLOCKED":
        recommend(dev, None)
        sys.exit(1)

    perf = None
    if "--no-test" not in sys.argv:
        print("\n" + "─" * 62)
        print(f"MEASURING (backend: {dev.label})")
        print("─" * 62)
        try:
            perf = _bench.run_standalone(dev, VOICE_DIR, emit=print)
        except Exception as e:
            print(f"\n  Speed test could not run: {e}")

    recommend(dev, perf)
    print()
    _bench.save_profile(PROFILE_PATH, _bench.build_profile(dev, perf, "profiler"))
    print("  The server reads this on boot to tune its defaults for this machine.")
    print()


if __name__ == "__main__":
    main()
