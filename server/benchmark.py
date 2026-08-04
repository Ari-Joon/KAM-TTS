"""
benchmark.py — measures what synthesis actually costs on this machine.

The number I care about is the real-time factor, which is seconds of compute per
second of audio produced. KAM reads continuously so synthesis has to keep ahead
of playback, and an RTF below 1.0 means a chunk is produced faster than it's
spoken so playback never stalls. Above 1.0 and the reader waits.

None of this is estimated from a GPU name or a model size. It runs the same
inference path the server runs, on your own voice clips, and reports what it saw.

I put the measuring code here rather than in the profiler script so the server
can run the identical benchmark on demand and stream each result to the dashboard
console as it happens. Otherwise someone on an unfamiliar machine has no way of
finding out whether KAM will keep up without opening a terminal.

Every function takes an emit callback, which defaults to print, so the same code
can write to a terminal or to the dashboard log or to both.
"""
from __future__ import annotations

import os
import json
import time
from typing import Callable, List, Optional

# A plain sentence, a clause-heavy one, and one with numbers and an acronym in
# it. Mixed on purpose since cost varies by content, and a benchmark made only of
# easy sentences flatters the machine.
SENTENCES: List[str] = [
    "Breadth-first search can be a useful algorithm.",
    "Particularly when the step costs are all equal or you care about path length.",
    "The API returned a 404 error at 3:15 in the afternoon on version 2.7.",
]

WARMUP_TEXT = "The quick brown fox jumps over the lazy dog."

# RTF bands, shared between the profiler, the server log and the README table.
RTF_BANDS = (
    (0.5, "comfortable", "Synthesis comfortably outpaces playback."),
    (1.0, "good",        "Synthesis keeps ahead of playback."),
    (2.0, "marginal",    "Synthesis is slower than playback — expect brief buffering."),
    (1e9, "poor",        "Synthesis is much slower than playback; reading will stall."),
)


def rtf_band(rtf: Optional[float]):
    """Returns (name, sentence) saying what an RTF means for the listener."""
    if rtf is None:
        return "unknown", "Speed has not been measured on this machine yet."
    for limit, name, text in RTF_BANDS:
        if rtf < limit:
            return name, text
    return "poor", RTF_BANDS[-1][2]


def discover_clips(folder: str) -> List[str]:
    """Clips to benchmark with, from a voice folder or else my_voice.wav."""
    if os.path.isdir(folder):
        clips = sorted(os.path.join(folder, f) for f in os.listdir(folder)
                       if f.lower().endswith(".wav"))
        if clips:
            return clips
    single = os.path.join(os.path.dirname(folder), "my_voice.wav")
    return [single] if os.path.exists(single) else []


def measure_inference(synth: Callable[[str], float],
                      emit: Callable[[str], None] = print,
                      sentences: Optional[List[str]] = None,
                      warmup: bool = True) -> dict:
    """Times the real synthesis path.

    synth(text) has to synthesise and return how many seconds of audio it
    produced, and this function does the timing. Taking the callable in means I
    don't care how the caller holds the model, since the server already has one
    loaded and warm while the profiler loads its own.

    Each sentence gets reported through emit as it finishes so a slow machine
    shows progress instead of looking like it's hung for a minute.
    """
    sentences = sentences or SENTENCES
    out = {"warm_s": None, "runs": [], "rtf": None, "rtf_min": None,
           "rtf_max": None, "audio_s": 0.0, "compute_s": 0.0}

    if warmup:
        # The first inference in a process compiles the kernels, so timing it as
        # if it were representative would make the machine look several times
        # slower than it is. I measure it separately instead since it's the real
        # cost of waking from standby, which is worth knowing on its own.
        emit("  warming up (compiles kernels — one-off cost)…")
        t0 = time.time()
        try:
            synth(WARMUP_TEXT)
        except Exception as e:
            emit(f"  warmup failed: {e}")
            raise
        out["warm_s"] = time.time() - t0
        emit(f"  warmup            {out['warm_s']:6.1f}s   (one-off, per process)")

    rtfs = []
    for i, text in enumerate(sentences, 1):
        t0 = time.time()
        audio_s = synth(text)
        dur = time.time() - t0
        rtf = (dur / audio_s) if audio_s else 0.0
        rtfs.append(rtf)
        out["runs"].append({"text": text, "compute_s": round(dur, 3),
                            "audio_s": round(audio_s, 3), "rtf": round(rtf, 3)})
        out["audio_s"] += audio_s
        out["compute_s"] += dur
        emit(f"  sentence {i}        {dur:6.2f}s   for {audio_s:4.1f}s audio  "
             f"(RTF {rtf:.2f})")

    if rtfs:
        out["rtf"]     = round(sum(rtfs) / len(rtfs), 3)
        out["rtf_min"] = round(min(rtfs), 3)
        out["rtf_max"] = round(max(rtfs), 3)
    return out


def verdict_lines(dev, perf: Optional[dict]) -> List[str]:
    """What the measurement means in plain English, for console and dashboard."""
    lines = []
    if not perf or perf.get("rtf") is None:
        lines.append("Speed has not been measured yet — run the benchmark to "
                     "find out whether this machine keeps up with playback.")
        return lines

    rtf = perf["rtf"]
    name, meaning = rtf_band(rtf)
    lines.append(f"Real-time factor {rtf:.2f} "
                 f"({'faster' if rtf < 1 else 'slower'} than real time) — {meaning}")
    if perf.get("rtf_min") is not None and perf.get("rtf_max") is not None:
        lines.append(f"  per-sentence spread {perf['rtf_min']:.2f}–{perf['rtf_max']:.2f}"
                     f" (content affects cost; dense technical text is the slow end)")

    if name == "comfortable":
        lines.append("  No tuning needed.")
    elif name == "good":
        lines.append("  Fine for continuous reading.")
    elif name == "marginal":
        lines.append("  Shorter chunks, or a lower top_k, reduce per-chunk cost.")
    else:
        if not getattr(dev, "is_accelerated", False):
            lines.append("  This is CPU-only inference. A CUDA GPU is strongly "
                         "recommended for continuous reading; short passages "
                         "still work.")
        else:
            lines.append("  Consider a smaller idle-standby timeout and shorter "
                         "chunks; the GPU is struggling with this model.")

    wake = (perf.get("load_s") or 0) + (perf.get("warm_s") or 0)
    if wake:
        lines.append(f"Waking from standby costs about {wake:.0f}s "
                     f"(model load + kernel warmup).")
        if getattr(dev, "vram_gb", None) and dev.vram_gb < 6:
            lines.append("  VRAM is limited here, so a short standby timeout "
                         "(5-15 min) is worth the wake cost.")
        elif wake > 25:
            lines.append("  That is a long wake — prefer a 1-2 hour timeout, or "
                         "'Always on'.")
        else:
            lines.append("  A 30-minute standby timeout is a reasonable balance.")
    return lines


def build_profile(dev, perf: Optional[dict], source: str) -> dict:
    """The hardware_profile.json the server adapts its defaults from."""
    perf = perf or {}
    return {
        "measured_at": time.time(),
        "source":      source,            # "profiler" | "server" | "auto"
        "device":      dev.torch_device,
        "backend":     dev.backend,
        "gpu":         dev.name if dev.is_accelerated else None,
        "device_name": dev.name,
        "vram_gb":     dev.vram_gb,
        "cpu_cores":   dev.cpu_cores,
        "ram_gb":      dev.ram_gb,
        "torch":       dev.torch_version,
        "build":       dev.build,
        "capability":  dev.capability,
        "rtf":         perf.get("rtf"),
        "rtf_min":     perf.get("rtf_min"),
        "rtf_max":     perf.get("rtf_max"),
        "load_s":      perf.get("load_s"),
        "latent_s":    perf.get("latent_s"),
        "warm_s":      perf.get("warm_s"),
        "runs":        perf.get("runs", []),
    }


def save_profile(path: str, payload: dict,
                 emit: Callable[[str], None] = print) -> bool:
    try:
        with open(path, "w") as f:
            json.dump(payload, f, indent=2)
        emit(f"  profile saved → {os.path.basename(path)}")
        return True
    except Exception as e:
        emit(f"  could not save profile: {e}")
        return False


def run_standalone(dev, voice_dir: str,
                   emit: Callable[[str], None] = print) -> Optional[dict]:
    """Loads the model from scratch and benchmarks it, which is what the
    profiler does.

    Returns None, after explaining why, when there's nothing to measure with.
    The server doesn't use this since it benchmarks the model it already has
    loaded, which is faster and a truer measure of the running system."""
    clips = discover_clips(voice_dir)
    if not clips:
        emit("  No voice reference clips found — skipping the speed test.")
        emit(f"  Add .wav files to {os.path.basename(voice_dir)}/ (or my_voice.wav)")
        emit("  and re-run to measure synthesis speed on this machine.")
        return None

    import torch  # type: ignore
    from TTS.api import TTS  # type: ignore

    emit(f"  loading XTTS-v2 on {dev.label}…")
    t = time.time()
    tts = TTS("tts_models/multilingual/multi-dataset/xtts_v2").to(dev.torch_device)
    load_s = time.time() - t
    emit(f"  model load        {load_s:6.1f}s")

    model = tts.synthesizer.tts_model
    t = time.time()
    gpt, spk = model.get_conditioning_latents(audio_path=clips)
    gpt, spk = gpt.contiguous(), spk.contiguous()
    latent_s = time.time() - t
    emit(f"  latent compute    {latent_s:6.1f}s   ({len(clips)} clip(s))")

    def synth(text: str) -> float:
        with torch.inference_mode():
            out = model.inference(
                text=text, language="en",
                gpt_cond_latent=gpt, speaker_embedding=spk,
                temperature=0.33, repetition_penalty=4.1,
                top_k=45, top_p=0.90, do_sample=True, num_beams=1, speed=1.2,
            )
        return len(out["wav"]) / 24000.0

    perf = measure_inference(synth, emit=emit)
    perf["load_s"] = load_s
    perf["latent_s"] = latent_s
    return perf
