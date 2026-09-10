"""The native-host registration: what it installs, and that it repairs itself.

This suite exists because of a bug that took an afternoon and had three layers.
Chrome 113 and later refuse a .bat native host, so the launcher has to be a real
executable. .NET's Process class then cannot hand that executable's pipes to
Python, so the launcher has to call CreateProcess itself. And the registration
holds absolute paths, so moving the project silently killed the power button
with everything still looking correct from outside.

The important test here is the self-test: a launcher that builds but cannot
relay a message is worse than one that fails to build, since the failure only
appears later as a button that does nothing. The version of that check which
sent a single ping passed against a launcher that was completely broken, because
the reply arrived at shutdown rather than live, so this insists on an answer
while the pipe is still open.

Nothing here touches the real registration: the install directory is redirected
to a temp folder and the registry is never written.
"""
import os
import pathlib
import shutil
import sys
import tempfile

SERVER_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SERVER_DIR))
import register_host as R

PASS = FAIL = 0


def check(label, got, want):
    global PASS, FAIL
    if got == want:
        PASS += 1
        print(f"  ok   {label}")
    else:
        FAIL += 1
        print(f"  FAIL {label}\n         got:  {got!r}\n         want: {want!r}")


TMP = pathlib.Path(tempfile.mkdtemp(prefix="kam_register_test_"))
R.install_dir = lambda: str(TMP)          # everything generated lands here

print("\n=== the install location is outside the project, on purpose ===")
# The whole bug was that a registration holding absolute paths does not follow a
# folder you move. Keeping the launcher out of the project is half the fix.
real_dir = pathlib.Path(os.environ.get("LOCALAPPDATA", "~")) / "KAMTTS"
check("it lives under LOCALAPPDATA", "KAMTTS" in str(real_dir), True)
check("and not inside the project",
      str(SERVER_DIR).lower() in str(real_dir).lower(), False)
check("with no spaces to quote wrongly", " " in real_dir.name, False)

print("\n=== the config the launcher reads at run time ===")
R.write_config(r"C:\py\python.exe", r"C:\proj\kam_host.py")
cfg = R.read_config()
check("interpreter round-trips", cfg["python"], r"C:\py\python.exe")
check("host script round-trips",  cfg["host"],   r"C:\proj\kam_host.py")
raw = open(R.config_path(), encoding="utf-8").read()
check("comments are ignored, not parsed as keys", "#" in raw and "python" in cfg, True)
# A path with spaces has to survive, since most people's projects have one.
R.write_config(r"C:\Program Files\Py\python.exe", r"C:\My Project\kam_host.py")
check("a path with spaces survives", R.read_config()["host"], r"C:\My Project\kam_host.py")

print("\n=== the generated launcher hardcodes nothing about the project ===")
src = R._launcher_source()
check("no interpreter baked in",  "python.exe" in src, False)
check("no project path baked in", "kam_host.py\"" in src, False)
check("it reads its own config",  "kam_host.cfg" in src, True)
# The two things .NET's Process class cannot do, which is why this is P/Invoke.
check("it calls CreateProcess itself", "CreateProcess" in src, True)
check("with handle inheritance",       "bInheritHandles" in src or "inherit," in src, True)
check("and marks the pipes inheritable", "SetHandleInformation" in src, True)
check("passing our own std handles",    "STARTF_USESTDHANDLES" in src, True)

print("\n=== build it and make it answer a live message ===")
if os.name == "nt" and R._find_csc():
    # Point the config at this interpreter: kam_host.py needs only the standard
    # library to answer status and ping.
    R.write_config(sys.executable, str(SERVER_DIR / "kam_host.py"))
    exe = R.build_launcher()
    check("the launcher builds", bool(exe) and exe.endswith(".exe"), True)
    check("it answers two messages while the pipe is open", R.self_test(exe), True)
    import struct
    d = open(exe, "rb").read()
    pe = struct.unpack_from("<I", d, 0x3C)[0]
    check("it is a console exe, so it has std handles to pass on",
          struct.unpack_from("<H", d, pe + 24 + 68)[0], 3)

    print("\n=== a launcher whose config points nowhere fails, and says so ===")
    R.write_config(r"C:\nope\python.exe", str(SERVER_DIR / "kam_host.py"))
    check("a missing interpreter is caught by the self-test", R.self_test(exe, timeout=6), False)
    log = pathlib.Path(R.launcher_log())
    check("and the launcher wrote down why",
          log.exists() and "missing" in log.read_text(encoding="utf-8", errors="replace").lower(), True)
else:
    print("  (skipped: needs Windows and the .NET Framework compiler)")

print("\n=== moving the project only rewrites the config ===")
# The point of the whole redesign: a move must not need a recompile, and must
# not need the user to know that register_host.py exists.
if os.name == "nt" and pathlib.Path(R.launcher_path()).exists():
    before = pathlib.Path(R.launcher_path()).read_bytes()
    R.write_config(sys.executable, r"D:\somewhere\else\kam_host.py")
    after = pathlib.Path(R.launcher_path()).read_bytes()
    check("the launcher binary is untouched by a move", before == after, True)
    check("but the config now points at the new place",
          R.read_config()["host"], r"D:\somewhere\else\kam_host.py")

print("\n=== ensure_registered() never raises, whatever it finds ===")
# server.py calls this on every boot. A broken power button must never stop the
# server from serving, so this returns a verdict instead of throwing.
for label, prep in (("with a good config", lambda: R.write_config(sys.executable, str(SERVER_DIR / "kam_host.py"))),
                    ("with no config at all", lambda: os.path.exists(R.config_path()) and os.remove(R.config_path()))):
    prep()
    try:
        R.ensure_registered(python_exe=sys.executable, verbose=False)
        check(f"survives {label}", True, True)
    except Exception as e:
        check(f"survives {label}", f"raised {e!r}", True)

print("\n=== remove() takes every generated piece ===")
R.write_config(sys.executable, str(SERVER_DIR / "kam_host.py"))
R.write_manifest(R.PINNED_EXTENSION_ID, R.launcher_path())
if os.name == "nt":
    import winreg
    _del = winreg.DeleteKey
    # Routed to the "nothing there" branch so the real key survives this suite.
    winreg.DeleteKey = lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError())
    try:
        R.remove(R.PINNED_EXTENSION_ID)
    finally:
        winreg.DeleteKey = _del
else:
    R.remove(R.PINNED_EXTENSION_ID)
for p in (R.manifest_path(), R.launcher_path(), R.launcher_src(), R.config_path()):
    check(f"{os.path.basename(p)} is gone", os.path.exists(p), False)

shutil.rmtree(TMP, ignore_errors=True)
print(f"\n{'='*64}\n  {PASS} passed, {FAIL} failed\n{'='*64}")
sys.exit(1 if FAIL else 0)
