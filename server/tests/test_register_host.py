"""The native-host registration: what it installs, that it repairs and upgrades
itself, and that it never leaves a working button broken.

This suite exists because of a bug with several layers. The registration held
absolute paths, so moving the project silently killed the power button with
everything still looking correct from outside. The first compiled launcher could
not hand Chrome's pipes to Python, because .NET's Process class cannot, and the
self-test that approved it only sent a single ping. And a log that recorded only
failures made a healthy launcher look like one Chrome never started.

The important tests are the ones about not breaking what works: a launcher that
builds but cannot relay a message must never replace one that can, and an
upgrade has to succeed while the old launcher is running, since the server that
triggers upgrades on boot was usually started through it.

Nothing here touches the real registration, and that is enforced rather than
intended. An earlier version of this suite redirected the install directory but
left the registry write going to the real key, so running the tests pointed
Chrome at a temp folder that the suite then deleted. All three registry functions
are replaced with an in-memory dictionary, and the real key is read before and
after and asserted unchanged.
"""
import json
import os
import pathlib
import shutil
import struct
import subprocess
import sys
import tempfile
import time

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

# What the real registration says before this suite runs a single line.
_REAL_BEFORE = R._registry_value() if os.name == "nt" else None

# Every registry touch in register_host goes through these three, so replacing
# them makes it impossible for the suite to reach the real key however the code
# under test decides to repair itself.
_FAKE_REGISTRY = {}
R._write_registry  = lambda: _FAKE_REGISTRY.__setitem__("value", R.manifest_path())
R._registry_value  = lambda: _FAKE_REGISTRY.get("value")
R._delete_registry = lambda: _FAKE_REGISTRY.pop("value", None) is not None

HOST = str(SERVER_DIR / "kam_host.py")
WINDOWS_BUILD = os.name == "nt" and bool(R._find_csc())

print("\n=== the install location is outside the project, on purpose ===")
real_dir = pathlib.Path(os.environ.get("LOCALAPPDATA", "~")) / "KAMTTS"
check("it lives under LOCALAPPDATA", "KAMTTS" in str(real_dir), True)
check("and not inside the project",
      str(SERVER_DIR).lower() in str(real_dir).lower(), False)
check("with no spaces in its own name", " " in real_dir.name, False)

print("\n=== the config the launcher reads at run time ===")
R.write_config(r"C:\py\python.exe", r"C:\proj\kam_host.py", launcher_sha="abc123")
cfg = R.read_config()
check("interpreter round-trips", cfg["python"], r"C:\py\python.exe")
check("host script round-trips",  cfg["host"],   r"C:\proj\kam_host.py")
check("build fingerprint round-trips", cfg["launcher_sha"], "abc123")
# Rewriting paths after a move must not disturb which build is installed.
R.write_config(r"C:\Program Files\Py\python.exe", r"C:\My Project\kam_host.py")
cfg = R.read_config()
check("a path with spaces survives", cfg["host"], r"C:\My Project\kam_host.py")
check("and the fingerprint is kept when not given", cfg["launcher_sha"], "abc123")

print("\n=== paths that have to pass through cmd.exe ===")
# Chrome starts the host with cmd.exe /d /s /c, so the path in the manifest must
# not contain anything cmd treats as an operator or variable marker.
check("an ordinary path is left alone", R.cmd_safe_path(r"C:\Users\ann\KAMTTS\kam_host.exe"),
      r"C:\Users\ann\KAMTTS\kam_host.exe")
check("a path that does not exist is left alone rather than guessed at",
      R.cmd_safe_path(r"C:\nope (x) & y\kam_host.exe"), r"C:\nope (x) & y\kam_host.exe")
awkward = TMP / "Smith & Jones (2)"
awkward.mkdir()
(awkward / "kam_host.exe").write_bytes(b"x")
long_p = str(awkward / "kam_host.exe")
safe_p = R.cmd_safe_path(long_p)
check("an awkward path still names the same file", os.path.samefile(safe_p, long_p), True)
if safe_p != long_p:
    check("and its safe form has nothing cmd would interpret",
          any(ch in R._CMD_UNSAFE for ch in safe_p), False)
else:
    print("  (short names are off on this drive, so the long path is kept and "
          "register() warns)")

print("\n=== the generated launcher hardcodes nothing about the project ===")
src = R._launcher_source()
check("no interpreter baked in",  "python.exe" in src, False)
check("no project path baked in", "kam_host.py\"" in src, False)
check("it reads its own config",  "kam_host.cfg" in src, True)
check("it calls CreateProcess itself", "CreateProcess" in src, True)
check("and marks the pipes inheritable", "SetHandleInformation" in src, True)
check("passing our own std handles",    "STARTF_USESTDHANDLES" in src, True)
# A log of failures only reads exactly like "never launched" when all is well.
check("it records every launch, not only failures", 'Note("launched ' in src, True)

print("\n=== the build fingerprint ===")
check("it is stable", R.launcher_sha(), R.launcher_sha())
_real_source = R._launcher_source
R._launcher_source = lambda: _real_source() + "\n// changed\n"
changed = R.launcher_sha()
R._launcher_source = _real_source
check("and changes when the launcher source does", changed != R.launcher_sha(), True)

if WINDOWS_BUILD:
    print("\n=== build to a staging name, test it, then swap it in ===")
    R.write_config(sys.executable, HOST)
    staged = R.build_launcher()
    check("the build goes to a staging name, not over the real launcher",
          staged == R.launcher_staged(), True)
    check("the staged launcher answers two messages while the pipe is open",
          R.self_test(staged), True)
    check("swapping it in succeeds", R.install_built(staged), True)
    check("the real launcher now exists", os.path.exists(R.launcher_path()), True)
    check("and the staged copy has gone", os.path.exists(staged), False)
    d = open(R.launcher_path(), "rb").read()
    pe = struct.unpack_from("<I", d, 0x3C)[0]
    check("it is a console exe, so it has std handles to pass on",
          struct.unpack_from("<H", d, pe + 24 + 68)[0], 3)
    log = pathlib.Path(R.launcher_log())
    check("the launch was written to host.log",
          log.exists() and "launched" in log.read_text(encoding="utf-8", errors="replace"), True)

    print("\n=== a full registration, end to end ===")
    check("register() succeeds", R.register(R.PINNED_EXTENSION_ID, sys.executable, quiet=True), True)
    with open(R.manifest_path(), encoding="utf-8") as f:
        man = json.load(f)
    check("the manifest points at the launcher's cmd-safe path",
          man["path"], R.cmd_safe_path(R.launcher_path()))
    check("the registry points at the manifest", R._registry_value(), R.manifest_path())
    check("the installed build is recorded", R.read_config()["launcher_sha"], R.launcher_sha())
    check("so there is nothing to repair", R.ensure_registered(sys.executable, verbose=False), True)

    print("\n=== a launcher that fails its test never replaces one that works ===")
    good = pathlib.Path(R.launcher_path()).read_bytes()
    _real_test = R.self_test
    R.self_test = lambda *a, **k: False
    try:
        check("register() reports the failure",
              R.register(R.PINNED_EXTENSION_ID, sys.executable, quiet=True), False)
    finally:
        R.self_test = _real_test
    check("the working launcher is byte for byte untouched",
          pathlib.Path(R.launcher_path()).read_bytes() == good, True)
    check("and the failed build was not left lying about",
          os.path.exists(R.launcher_staged()), False)

    print("\n=== upgrading while the old launcher is running ===")
    # The server calls ensure_registered on boot, and was usually started
    # through the launcher it would be replacing. Windows will not overwrite a
    # running exe, so this has to work by moving it aside.
    running = subprocess.Popen([R.launcher_path(), f"chrome-extension://{R.PINNED_EXTENSION_ID}/"],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL)
    time.sleep(1.0)
    check("the old launcher is running", running.poll() is None, True)
    # The "newer" source stays in force until the clean-up check below. Putting
    # the real source back first makes the installed build look outdated again,
    # so the clean-up call would do a second upgrade and create a fresh .old,
    # and the test would be checking its own side effect.
    R._launcher_source = lambda: _real_source() + "\n// a newer build\n"
    try:
        check("an outdated launcher is noticed and rebuilt",
              R.ensure_registered(sys.executable, verbose=False), True)
        check("the new build is recorded", R.read_config()["launcher_sha"], R.launcher_sha())
        check("the new launcher is in place", os.path.exists(R.launcher_path()), True)
        check("the old one was moved aside rather than overwritten",
              os.path.exists(R.launcher_old()), True)
        check("and the running copy carried on regardless", running.poll() is None, True)
        try:
            running.stdin.close()
            running.wait(timeout=10)
        except Exception:
            subprocess.run(["taskkill", "/PID", str(running.pid), "/T", "/F"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        # Windows releases an exe's image a moment after the process exits.
        for _ in range(20):
            R.ensure_registered(sys.executable, verbose=False)
            if not os.path.exists(R.launcher_old()):
                break
            time.sleep(0.25)
        check("once it has stopped, the moved-aside copy is cleared up",
              os.path.exists(R.launcher_old()), False)
    finally:
        R._launcher_source = _real_source

    print("\n=== a launcher whose config points nowhere fails, and says so ===")
    R.write_config(r"C:\nope\python.exe", HOST)
    check("a missing interpreter is caught by the self-test",
          R.self_test(R.launcher_path(), timeout=6), False)
    check("and the launcher wrote down why",
          "missing" in pathlib.Path(R.launcher_log()).read_text(encoding="utf-8", errors="replace").lower(),
          True)

    print("\n=== moving the project only rewrites the config ===")
    before = pathlib.Path(R.launcher_path()).read_bytes()
    R.write_config(sys.executable, r"D:\somewhere\else\kam_host.py")
    check("the launcher binary is untouched by a move",
          pathlib.Path(R.launcher_path()).read_bytes() == before, True)
    check("but the config now points at the new place",
          R.read_config()["host"], r"D:\somewhere\else\kam_host.py")
else:
    print("\n  (build, swap and upgrade tests skipped: need Windows and the .NET Framework compiler)")

print("\n=== ensure_registered() never raises, whatever it finds ===")
for label, prep in (("with a good config", lambda: R.write_config(sys.executable, HOST)),
                    ("with no config at all",
                     lambda: os.path.exists(R.config_path()) and os.remove(R.config_path()))):
    prep()
    try:
        R.ensure_registered(python_exe=sys.executable, verbose=False)
        check(f"survives {label}", True, True)
    except Exception as e:
        check(f"survives {label}", f"raised {e!r}", True)

print("\n=== remove() takes every generated piece ===")
R.write_config(sys.executable, HOST)
R.write_manifest(R.PINNED_EXTENSION_ID, R.launcher_path())
R._write_registry()
for extra in (R.launcher_staged(), R.launcher_old()):
    pathlib.Path(extra).write_bytes(b"x")
check("the key was set before removing", R._registry_value() is not None, True)
R.remove(R.PINNED_EXTENSION_ID)
for p in (R.manifest_path(), R.launcher_path(), R.launcher_staged(), R.launcher_old(),
          R.launcher_src(), R.config_path()):
    check(f"{os.path.basename(p)} is gone", os.path.exists(p), False)
check("and the key went with them", R._registry_value(), None)

print("\n=== and none of that reached the real registration ===")
if os.name == "nt":
    import importlib
    _clean = importlib.reload(importlib.import_module("register_host"))
    after = _clean._registry_value()
    check("the real registry key is exactly as we found it", after, _REAL_BEFORE)
    check("and does not point into a temp directory",
          bool(after) and "kam_register_test" in str(after), False)

shutil.rmtree(TMP, ignore_errors=True)
print(f"\n{'='*64}\n  {PASS} passed, {FAIL} failed\n{'='*64}")
sys.exit(1 if FAIL else 0)
