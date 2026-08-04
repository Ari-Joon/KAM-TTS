"""
KAM TTS — one-command setup for a new machine.

Run from the server folder with the Python you intend to use:

    py -3.10 setup_kam.py

Steps (idempotent, safe to re-run):
  1. Install dependencies from requirements.txt into this Python.
  2. Verify torch sees a CUDA GPU (warns and continues on CPU-only).
  3. Register the Chrome native-messaging host so the extension's power
     button can launch the server (runs register_host.py).
  4. Optionally run hardware_profile.py so you know synthesis speed is healthy
     before first use (RTF well under 1.0 expected on a modern GPU).

The XTTS model weights and the voice latents are downloaded / computed on the
server's first boot, not here, because they depend on the runtime environment.
"""
import os
import subprocess
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

HERE = os.path.dirname(os.path.abspath(__file__))


def _run(args, desc):
    print(f"\n=== {desc} ===")
    r = subprocess.run(args, cwd=HERE)
    if r.returncode != 0:
        print(f"FAILED: {desc} (exit {r.returncode})")
        sys.exit(r.returncode)


def _preflight():
    """Check the machine BEFORE installing several GB of dependencies, so an
    unsuitable machine is discovered in seconds rather than after a long
    download. Never hard-blocks: CPU-only is slow but valid, so we warn and let
    the user decide."""
    print("\n=== Pre-flight hardware check ===")
    cores = os.cpu_count() or 0
    ram_gb = None
    try:
        import psutil  # optional
        ram_gb = round(psutil.virtual_memory().total / (1024 ** 3), 1)
    except Exception:
        try:
            if hasattr(os, "sysconf") and "SC_PAGE_SIZE" in os.sysconf_names:
                ram_gb = round(os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
                               / (1024 ** 3), 1)
        except Exception:
            pass

    print(f"  CPU cores: {cores}")
    print(f"  RAM:       {ram_gb if ram_gb else 'unknown'} GB")

    warnings = []
    if cores and cores < 4:
        warnings.append(f"{cores} CPU cores is below the recommended 4.")
    if ram_gb and ram_gb < 8:
        warnings.append(f"{ram_gb} GB RAM is below the 8 GB minimum.")

    # torch may not be installed yet — that's expected on a fresh machine, and
    # device.detect() reports that case rather than raising. Detection only: no
    # backend is configured or verified here, because nothing is going to run
    # yet and the check must stay instant.
    try:
        import device as _device
        dev = _device.detect()
        if not dev.torch_version:
            print("  Compute:   (PyTorch not installed yet — checked again after install)")
        elif len(dev.available) > 1:
            dev = _device.select(dev)
            print(f"  Compute:   {dev.label}"
                  + (f" — {dev.name}" if dev.is_accelerated else ""))
            if dev.vram_gb:
                print(f"  VRAM:      {dev.vram_gb} GB")
            others = [b for b in dev.available if b != dev.torch_device]
            if others:
                print(f"  Also:      {', '.join(others)} available "
                      f"(force with KAM_DEVICE)")
            v = dev.vram_gb or 0
            if v and v < 4:
                warnings.append(f"{v} GB VRAM is tight for XTTS-v2; "
                                "out-of-memory errors are likely.")
            elif v and v < 6:
                warnings.append(f"{v} GB VRAM works but leaves little headroom.")
            if dev.backend == "mps":
                warnings.append("Apple Silicon: XTTS runs in fp32 with a CPU "
                                "fallback for unimplemented ops — slower than "
                                "CUDA but usable.")
            elif dev.backend == "rocm":
                warnings.append("ROCm support in PyTorch is less mature than "
                                "CUDA. If synthesis errors, set KAM_DEVICE=cpu.")
        else:
            print("  Compute:   CPU only (no GPU backend visible to PyTorch)")
            warnings.append("No GPU detected. Synthesis will be several times "
                            "slower than playback — usable for short passages, "
                            "not continuous reading.")
    except Exception as e:
        print(f"  Compute:   (could not probe: {e})")

    if warnings:
        print("\n  Notes:")
        for w in warnings:
            print(f"    - {w}")
        ans = input("\n  Continue with setup anyway? [Y/n] ").strip().lower()
        if ans == "n":
            print("  Setup cancelled.")
            sys.exit(0)
    else:
        print("  Looks good.")


def _check_voice_clips():
    """Warn if no reference audio exists yet — the server cannot clone a voice
    without it, and finding out at first boot is a confusing failure."""
    samples_dir = os.path.join(HERE, "voice_samples")
    clips = []
    if os.path.isdir(samples_dir):
        clips = [f for f in os.listdir(samples_dir) if f.lower().endswith(".wav")]
    legacy = os.path.exists(os.path.join(HERE, "my_voice.wav"))
    if clips:
        print(f"\n=== Voice reference: {len(clips)} clip(s) found in voice_samples/ ===")
        print("  Tip: run  python check_voice_clips.py  to validate them.")
    elif legacy:
        print("\n=== Voice reference: my_voice.wav found ===")
        print("  Tip: several short clips in voice_samples/ clone better than one file.")
    else:
        print("\n=== Voice reference: NONE FOUND ===")
        print("  KAM cannot clone a voice until you add reference audio.")
        print("  Add 6-10 clean WAV clips (8-15s each) to:")
        print(f"    {samples_dir}")
        print("  Then run:  python check_voice_clips.py")


def main():
    print(f"KAM TTS setup using: {sys.executable}")

    # 0. Check the hardware before downloading anything large.
    _preflight()

    # 1. Dependencies
    _run([sys.executable, "-m", "pip", "install", "-r", "requirements.txt"],
         "Installing dependencies")

    # 1b. spaCy English model for POS-informed prosody (not a pip package).
    print("\n=== Fetching spaCy English model (en_core_web_sm) ===")
    subprocess.run([sys.executable, "-m", "spacy", "download", "en_core_web_sm"], cwd=HERE)

    # 2. Compute backend check (informational; CPU still works, just slowly).
    #    Run in a SUBPROCESS so this reflects the torch that was just installed,
    #    not whatever state this process imported earlier.
    print("\n=== Checking the compute backend ===")
    probe = subprocess.run(
        [sys.executable, "-c",
         "import device; d = device.resolve(); print(d.summary());"
         " [print('  ' + n) for n in d.notes]"],
        cwd=HERE, capture_output=True, text=True)
    out = probe.stdout.strip()
    print(out or probe.stderr.strip())
    if "CPU" in out.split("·")[0]:
        print("NOTE: no GPU backend is in use. Synthesis will be slower than "
              "playback.")
        print("Install the PyTorch build matching your hardware from "
              "https://pytorch.org")
        print("  (CUDA for NVIDIA, ROCm for AMD on Linux, the default wheel "
              "for Apple Silicon, XPU for Intel Arc)")

    # 3. Native messaging host (power button)
    _run([sys.executable, "register_host.py"],
         "Registering Chrome native-messaging host")

    # 4. Voice reference audio — required before the server can clone a voice.
    _check_voice_clips()

    # 5. Hardware profile: measures real synthesis speed on THIS machine and
    #    gives a verdict + tailored settings advice. Needs reference clips, so
    #    only offer it when some exist.
    samples_dir = os.path.join(HERE, "voice_samples")
    has_clips = (os.path.isdir(samples_dir) and
                 any(f.lower().endswith(".wav") for f in os.listdir(samples_dir))) \
                or os.path.exists(os.path.join(HERE, "my_voice.wav"))
    if has_clips:
        print("\nThe server measures synthesis speed itself on its first run "
              "for this machine, so this step is optional.")
        ans = input("Run the hardware profile now anyway? Takes about a minute. "
                    "[y/N] ").strip().lower()
        if ans == "y":
            _run([sys.executable, "hardware_profile.py"], "Profiling hardware")
    else:
        print("\nSkipping the speed test — add voice clips first. The server "
              "will measure itself on first run, or run:")
        print("  python hardware_profile.py")

    print("\nSetup complete. Start the server from the extension's power button")
    print("or with:  python server.py")


if __name__ == "__main__":
    main()