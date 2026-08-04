"""
device.py — works out which compute backend to run KAM on.

I used to decide this with one line, device = "cuda" if torch.cuda.is_available()
else "cpu", which is fine on my machine and wrong on most others. AMD ROCm builds
report themselves as "cuda" so they went down the NVIDIA path and got TF32
settings that don't exist for them, Apple Silicon was never used at all, Intel
GPUs were ignored, and a CPU-only machine ran with PyTorch's default thread count
while the Whisper worker fought it for cores.

So this module answers four questions once at startup and hands the answers to
whoever needs them (the server, the profiler, the benchmark):

    1. Which backends exist here?          detect()
    2. Which one should I use?             select()
    3. How should it be set up?            configure()
    4. Does it actually work for XTTS?     verify()

Number 4 matters more than it sounds. A backend can report itself as available
and still fail the moment real work hits it, and MPS in particular is missing
operations that XTTS needs depending on which torch and coqui-tts versions you
have. I don't want to find that out during the first sentence of a page, so
verify() runs a tiny tensor round-trip up front and drops back to CPU before the
model is ever loaded.

Nothing here imports the TTS stack, so it stays cheap and the profiler can call
it before anything heavy loads.
"""
from __future__ import annotations

import os
import platform
from dataclasses import dataclass, field
from typing import List, Optional


# Order I prefer them in when more than one works. CUDA first since it's the only
# one XTTS is really fast on, CPU last since it always works.
_PREFERENCE = ("cuda", "xpu", "mps", "cpu")

# Readable names for the logs and the dashboard.
BACKEND_LABEL = {
    "cuda": "NVIDIA CUDA",
    "rocm": "AMD ROCm",
    "xpu":  "Intel XPU",
    "mps":  "Apple Metal (MPS)",
    "cpu":  "CPU",
}


@dataclass
class DeviceInfo:
    """Everything I've worked out about compute on this machine."""
    torch_device: str = "cpu"      # what to pass to .to(): cuda | xpu | mps | cpu
    backend:      str = "cpu"      # what it really is: cuda | rocm | xpu | mps | cpu
    name:         str = "CPU"      # the accelerator's product name
    vram_gb:      Optional[float] = None
    cpu_cores:    Optional[int] = None
    ram_gb:       Optional[float] = None
    torch_version: str = ""
    build:        str = ""         # CUDA or ROCm toolkit version, or ""
    capability:   Optional[str] = None   # CUDA compute capability, e.g. "8.9"
    supports_fp16: bool = False
    supports_tf32: bool = False
    available:    List[str] = field(default_factory=list)   # everything usable here
    notes:        List[str] = field(default_factory=list)   # why I picked what I did
    fell_back:    bool = False     # verify() rejected the backend I wanted

    @property
    def is_accelerated(self) -> bool:
        return self.backend != "cpu"

    @property
    def label(self) -> str:
        return BACKEND_LABEL.get(self.backend, self.backend)

    def summary(self) -> str:
        """One line to print in the console and show on the dashboard."""
        bits = [f"{self.label}"]
        if self.name and self.name != "CPU":
            bits.append(self.name)
        if self.vram_gb:
            bits.append(f"{self.vram_gb:g} GB VRAM")
        if self.cpu_cores:
            bits.append(f"{self.cpu_cores} cores")
        if self.ram_gb:
            bits.append(f"{self.ram_gb:g} GB RAM")
        return " · ".join(bits)

    def as_dict(self) -> dict:
        return {
            "torch_device": self.torch_device, "backend": self.backend,
            "name": self.name, "vram_gb": self.vram_gb,
            "cpu_cores": self.cpu_cores, "ram_gb": self.ram_gb,
            "torch_version": self.torch_version, "build": self.build,
            "capability": self.capability, "supports_fp16": self.supports_fp16,
            "supports_tf32": self.supports_tf32, "available": self.available,
            "notes": self.notes, "fell_back": self.fell_back,
            "label": self.label, "summary": self.summary(),
        }


def _host_facts(info: DeviceInfo) -> None:
    """CPU and RAM, which matter even when there's a GPU doing the work."""
    info.cpu_cores = os.cpu_count()
    try:
        import psutil  # optional
        info.ram_gb = round(psutil.virtual_memory().total / (1024 ** 3), 1)
    except Exception:
        # Fallback for POSIX. On Windows without psutil I just report unknown.
        try:
            if hasattr(os, "sysconf") and "SC_PHYS_PAGES" in os.sysconf_names:
                info.ram_gb = round(
                    os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
                    / (1024 ** 3), 1)
        except Exception:
            pass


def detect() -> DeviceInfo:
    """Look at the machine and list every backend that could be used.

    This only detects, it doesn't select or configure anything."""
    info = DeviceInfo()
    _host_facts(info)
    info.available = ["cpu"]

    try:
        import torch  # type: ignore
    except ImportError:
        info.notes.append("PyTorch is not installed — install it from pytorch.org")
        return info

    info.torch_version = getattr(torch, "__version__", "")

    # NVIDIA CUDA and AMD ROCm both show up as torch.cuda, and torch.version.hip
    # is the only reliable way to tell them apart. Worth doing since the TF32
    # switches further down only exist on NVIDIA.
    try:
        if torch.cuda.is_available() and torch.cuda.device_count() > 0:
            is_rocm = bool(getattr(torch.version, "hip", None))
            info.available.append("cuda")
            info.name = torch.cuda.get_device_name(0)
            info.build = (torch.version.hip if is_rocm
                          else (torch.version.cuda or "")) or ""
            try:
                _free, total = torch.cuda.mem_get_info()
                info.vram_gb = round(total / (1024 ** 3), 1)
            except Exception:
                try:
                    info.vram_gb = round(
                        torch.cuda.get_device_properties(0).total_memory
                        / (1024 ** 3), 1)
                except Exception:
                    pass
            if not is_rocm:
                try:
                    major, minor = torch.cuda.get_device_capability(0)
                    info.capability = f"{major}.{minor}"
                    # TF32 only exists on Ampere (8.0) and newer.
                    info.supports_tf32 = major >= 8
                    # fp16 has been fine since Pascal (6.0) in practice.
                    info.supports_fp16 = major >= 6
                except Exception:
                    pass
            else:
                info.supports_fp16 = True     # ROCm cards do fp16, TF32 isn't a thing
                info.notes.append("ROCm build detected, PyTorch reports it as "
                                  "'cuda' but the TF32 tuning doesn't apply")
    except Exception as e:
        info.notes.append(f"CUDA/ROCm probe failed: {e}")

    # Intel XPU (Arc and the Data Center GPUs), needs torch 2.4 or newer.
    try:
        xpu = getattr(torch, "xpu", None)
        if xpu is not None and xpu.is_available() and xpu.device_count() > 0:
            info.available.append("xpu")
            if "cuda" not in info.available:
                try:
                    info.name = xpu.get_device_name(0)
                except Exception:
                    info.name = "Intel XPU"
                info.supports_fp16 = True
    except Exception:
        pass

    # Apple Silicon, through Metal Performance Shaders.
    try:
        mps = getattr(torch.backends, "mps", None)
        if mps is not None and mps.is_available() and mps.is_built():
            info.available.append("mps")
            if "cuda" not in info.available and "xpu" not in info.available:
                info.name = f"Apple Silicon ({platform.machine()})"
                # fp16 works on MPS but I run XTTS in fp32 there, see configure().
                info.supports_fp16 = False
    except Exception:
        pass

    if info.name == "CPU":
        info.name = platform.processor() or platform.machine() or "CPU"
    return info


def _rocm(info: DeviceInfo) -> bool:
    return info.backend == "rocm"


def select(info: DeviceInfo, prefer: Optional[str] = None) -> DeviceInfo:
    """Pick one of the available backends, unless the user has forced a choice.

    Setting KAM_DEVICE to cpu, cuda, mps or xpu overrides me. I wanted a way out
    that doesn't involve editing code, since someone might want to keep their GPU
    free for something else or work around a driver that's misbehaving."""
    forced = (prefer or os.environ.get("KAM_DEVICE") or "").strip().lower()
    if forced:
        if forced in info.available:
            info.torch_device = forced
            info.notes.append(f"device forced to '{forced}' by KAM_DEVICE")
        else:
            info.notes.append(
                f"KAM_DEVICE='{forced}' isn't available here "
                f"(I can see: {', '.join(info.available)}), so I'm ignoring it")
            forced = ""
    if not forced:
        info.torch_device = next(
            (d for d in _PREFERENCE if d in info.available), "cpu")

    # Now that a device is picked I can tell ROCm and CUDA apart.
    info.backend = info.torch_device
    if info.torch_device == "cuda":
        try:
            import torch  # type: ignore
            if getattr(torch.version, "hip", None):
                info.backend = "rocm"
        except Exception:
            pass
    return info


def verify(info: DeviceInfo) -> DeviceInfo:
    """Check the chosen backend can really run something, and drop it if it can't.

    "Available" only tells me the driver is there. MPS and fresh ROCm installs
    both report available fairly often and then throw on the first real kernel,
    so I'd rather spend a few milliseconds finding out now than have a read break
    halfway down a page."""
    if info.torch_device == "cpu":
        return info
    try:
        import torch  # type: ignore
        d = torch.device(info.torch_device)
        # A matmul plus a tanh, which is enough to touch the kernels a real
        # forward pass needs and cheap enough that nobody notices it.
        a = torch.randn(64, 64, device=d)
        b = torch.tanh(a @ a.t()).sum().item()
        if b != b:  # NaN
            raise RuntimeError("backend produced NaN on a trivial matmul")
    except Exception as e:
        info.notes.append(
            f"{BACKEND_LABEL.get(info.backend, info.backend)} reported available "
            f"but failed a test operation ({type(e).__name__}: {str(e)[:90]}), "
            f"so I'm falling back to CPU")
        info.available = [d for d in info.available if d != info.torch_device]
        info.torch_device = "cpu"
        info.backend = "cpu"
        info.fell_back = True
        info.supports_tf32 = False
        info.supports_fp16 = False
    return info


def configure(info: DeviceInfo) -> DeviceInfo:
    """Set up the backend with the settings that actually make a difference.

    Each of these is here because it measurably helps on that backend, and just
    as importantly I don't apply them where they're meaningless or harmful."""
    try:
        import torch  # type: ignore
    except ImportError:
        return info

    if info.backend == "cuda":
        try:
            # TF32 speeds up the GPT attention matmuls with no audible quality
            # loss, so I enable it on Ampere and newer. Older cards accept the
            # flags but ignore them, and ROCm uses a different setting entirely.
            if info.supports_tf32:
                torch.backends.cuda.matmul.allow_tf32 = True
                torch.backends.cudnn.allow_tf32 = True
                torch.set_float32_matmul_precision("high")
                info.notes.append("TF32 matmuls enabled (Ampere or newer)")
            else:
                info.notes.append(
                    f"TF32 isn't available on compute capability "
                    f"{info.capability or '<8.0'}, so running full fp32")
            # cudnn.benchmark has to stay off. XTTS is autoregressive so every
            # token is a new shape, and with benchmark on cuDNN re-runs its
            # autotuning search for each one, which took synthesis from about
            # 3 seconds to 40.
            torch.backends.cudnn.benchmark = False
        except Exception as e:
            info.notes.append(f"CUDA tuning skipped: {e}")

    elif info.backend == "rocm":
        try:
            torch.backends.cudnn.benchmark = False
            info.notes.append("ROCm: autotuning off (variable-length inference)")
        except Exception:
            pass

    elif info.backend == "mps":
        # XTTS is correct on MPS in fp32 but has gaps in fp16, and this fallback
        # is what stops a missing operation killing a read outright. It costs a
        # CPU round-trip for those operations, which I'll take over a crash.
        os.environ.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")
        info.notes.append("MPS: fp32 with CPU fallback enabled for missing ops")

    if info.backend == "cpu":
        # PyTorch gives itself one thread per logical core, which is too many
        # here since Flask and the Whisper listen-back worker both need CPU too.
        # Oversubscribing makes every chunk slower rather than faster, so I cap
        # it at roughly the physical core count instead.
        try:
            cores = info.cpu_cores or 4
            threads = max(1, min(cores, max(2, cores // 2)))
            env = os.environ.get("KAM_CPU_THREADS")
            if env and env.isdigit():
                threads = max(1, int(env))
            torch.set_num_threads(threads)
            info.notes.append(
                f"CPU inference using {threads} of {cores} threads "
                f"(leaves room for listen-back, override with KAM_CPU_THREADS)")
        except Exception as e:
            info.notes.append(f"CPU thread tuning skipped: {e}")
    return info


def resolve(prefer: Optional[str] = None) -> DeviceInfo:
    """The whole thing in one call: detect, select, verify, configure."""
    return configure(verify(select(detect(), prefer)))


def empty_cache(info: DeviceInfo) -> None:
    """Free cached accelerator memory, whichever backend is in use.

    I call this when evicting the model and when retrying after running out of
    memory. The server used to call torch.cuda.empty_cache() regardless, which
    would throw an AttributeError on a machine with no CUDA at all."""
    try:
        import torch  # type: ignore
        if info.backend in ("cuda", "rocm"):
            torch.cuda.empty_cache()
        elif info.backend == "xpu" and hasattr(torch, "xpu"):
            torch.xpu.empty_cache()
        elif info.backend == "mps" and hasattr(torch.backends, "mps"):
            getattr(torch, "mps", None) and torch.mps.empty_cache()
    except Exception:
        pass


def memory_used_mb(info: DeviceInfo):
    """Accelerator memory as (used, total) in MB, or (None, None) if I can't tell."""
    try:
        import torch  # type: ignore
        if info.backend in ("cuda", "rocm"):
            free_b, total_b = torch.cuda.mem_get_info()
            return round((total_b - free_b) / 1048576), round(total_b / 1048576)
        if info.backend == "mps" and hasattr(torch, "mps"):
            used = torch.mps.current_allocated_memory()
            return round(used / 1048576), (round(info.vram_gb * 1024)
                                           if info.vram_gb else None)
    except Exception:
        pass
    return None, None
