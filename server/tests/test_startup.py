"""Simulate server startup on several machine types and check that the console
output a user would actually see is correct and informative on each."""
import pathlib as _pl
SERVER_DIR    = _pl.Path(__file__).resolve().parent.parent
EXTENSION_DIR = SERVER_DIR.parent / "extension"
import sys, types, pathlib, io, os, contextlib
import numpy as np

SERVER = SERVER_DIR
sys.path.insert(0, str(SERVER))
sys.path.insert(0, str(pathlib.Path(__file__).parent))

# Reuse the torch stub from test_modules without running its test body.
_src = (pathlib.Path(__file__).resolve().parent / "test_modules.py").read_text(encoding="utf-8")
_ns = {"types": types, "np": np, "sys": sys, "os": os}
exec(_src[_src.index("def make_torch("):_src.index("def resolve_with(")], _ns)
make_torch = _ns["make_torch"]

PASS = FAIL = 0
def check(label, got, want):
    global PASS, FAIL
    if got == want: PASS += 1; print(f"  ok   {label}")
    else:
        FAIL += 1; print(f"  FAIL {label}\n         got:  {got!r}\n         want: {want!r}")

def boot(torch_mod, env=None):
    """Import device.py fresh under a given fake machine, return (info, log)."""
    for mod in ("device",):
        sys.modules.pop(mod, None)
    sys.modules["torch"] = torch_mod
    old = {}
    for k, v in (env or {}).items():
        old[k] = os.environ.get(k); os.environ[k] = v
    buf = io.StringIO()
    try:
        import device as D
        info = D.resolve()
        # Reproduce server._log_device() exactly, so this tests the lines the
        # user actually sees on the dashboard rather than a paraphrase.
        with contextlib.redirect_stdout(buf):
            print(f"[DEVICE] {info.summary()}")
            print(f"[DEVICE] torch {info.torch_version}"
                  + (f" · {info.label} build {info.build}" if info.build else "")
                  + (f" · compute {info.capability}" if info.capability else ""))
            if len(info.available) > 1:
                print(f"[DEVICE] backends available: {', '.join(info.available)} "
                      f"(override with the KAM_DEVICE environment variable)")
            for note in info.notes:
                print(f"[DEVICE] {note}")
            if not info.is_accelerated:
                print("[DEVICE] No GPU in use — synthesis will be slower than "
                      "playback. KAM will still read, with pauses to buffer.")
        return info, buf.getvalue()
    finally:
        for k, v in old.items():
            if v is None: os.environ.pop(k, None)
            else: os.environ[k] = v

MACHINES = [
    ("NVIDIA RTX 5090",   make_torch(cuda=True, cap=(12, 0), vram_gb=32)),
    ("NVIDIA GTX 1080",   make_torch(cuda=True, cap=(6, 1), vram_gb=8)),
    ("AMD RX 7900 (ROCm)",make_torch(cuda=True, hip="6.2", vram_gb=24)),
    ("Apple M3",          make_torch(mps=True)),
    ("Intel Arc A770",    make_torch(xpu=True)),
    ("CPU-only laptop",   make_torch()),
    ("Broken MPS",        make_torch(mps=True, fail_on="mps")),
]

for name, t in MACHINES:
    print(f"\n=== {name} ===")
    info, log = boot(t)
    for line in log.rstrip().splitlines():
        print("   " + line)
    check(f"{name}: a backend was chosen", bool(info.torch_device), True)
    check(f"{name}: summary is non-empty", len(info.summary()) > 3, True)
    # Whatever happens, the user must be able to tell what is running.
    check(f"{name}: log names the backend", info.label in log, True)

print("\n=== hardware_profile viability on each machine ===")
import importlib
for name, t in MACHINES:
    info, _ = boot(t)
    sys.modules.pop("hardware_profile", None)
    import hardware_profile as HP
    importlib.reload(HP)
    verdict, notes = HP.viability(info)
    print(f"  {name:22} {verdict:9} {notes[0][:58] if notes else ''}")
    check(f"{name}: verdict is a known value", verdict in
          ("GOOD", "OK", "MARGINAL", "CPU-ONLY", "BLOCKED"), True)

print("\n=== no PyTorch at all ===")
notorch = types.ModuleType("torch")
del notorch
sys.modules.pop("device", None)
sys.modules["torch"] = None       # force ImportError path
class _Blocker:
    def find_module(self, *a): return None
saved = sys.modules.pop("torch")
sys.modules["torch"] = saved
# Simulate absence by removing it entirely
sys.modules.pop("torch", None)
sys.modules.pop("device", None)
import builtins
real_import = builtins.__import__
def no_torch(name, *a, **k):
    if name == "torch":
        raise ImportError("No module named 'torch'")
    return real_import(name, *a, **k)
builtins.__import__ = no_torch
try:
    import device as D
    importlib.reload(D)
    info = D.resolve()
    check("no torch -> cpu, no crash", info.backend, "cpu")
    check("no torch -> explained", any("PyTorch is not installed" in n
                                       for n in info.notes), True)
    sys.modules.pop("hardware_profile", None)
    import hardware_profile as HP
    importlib.reload(HP)
    v, notes = HP.viability(info)
    check("no torch -> BLOCKED verdict", v, "BLOCKED")
    print(f"       {notes[0]}")
finally:
    builtins.__import__ = real_import

print(f"\n{'='*64}\n  {PASS} passed, {FAIL} failed\n{'='*64}")
sys.exit(1 if FAIL else 0)
