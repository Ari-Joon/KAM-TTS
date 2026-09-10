"""The native-host launcher: what gets built, that it relays, and what remove()
takes with it.

This exists because of a bug that hid for three weeks. After the folder moved I
re-registered the host, every file and registry key checked out, and Chrome
still said "Specified native messaging host not found". The cause was Chrome
itself: from 113 it invokes native hosts as executables directly rather than via
cmd.exe, so the .bat that had always worked was being refused at the manifest
level. The fix is a compiled launcher, and the point of these tests is that the
launcher is proven to relay the protocol rather than assumed to.

Nothing here touches the real registration. The registry write is never called,
the registry delete is stubbed to the "no key" path, and every generated file
goes to a temp directory.
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
# Point every generated artefact at the temp directory, so a test run can never
# overwrite the launcher a real registration depends on.
R.LAUNCHER_EXE  = str(TMP / "kam_host.exe")
R.LAUNCHER_SRC  = str(TMP / "kam_host_launcher.cs")
R.LAUNCHER_BAT  = str(TMP / "kam_host.bat")
R.MANIFEST_PATH = str(TMP / "com.kam.tts.json")

print("\n=== quoting a path into C# ===")
check("a plain path is a verbatim string", R._cs_verbatim(r"C:\x\y.exe"), r'@"C:\x\y.exe"')
check("backslashes are left alone, which is what verbatim means",
      "\\\\" in R._cs_verbatim(r"C:\a\b"), False)
check("a quote is doubled, the only escape verbatim strings have",
      R._cs_verbatim('C:\\odd"name'), '@"C:\\odd""name"')

print("\n=== the generated launcher source ===")
src = R._launcher_source(r"C:\py\python.exe")
check("bakes in the interpreter",  r'@"C:\py\python.exe"' in src, True)
check("bakes in the host script",  R._cs_verbatim(R.HOST_SCRIPT) in src, True)
check("relays rather than inherits", "RedirectStandardInput = true" in src
      and "RedirectStandardOutput = true" in src, True)
check("passes the interpreter on to the host",
      'EnvironmentVariables["KAM_PYTHON"]' in src, True)
check("never shows a window", "CreateNoWindow = true" in src, True)

print("\n=== the .bat fallback ===")
# The old writer opened the file in text mode and wrote \r\n by hand, so every
# line ended \r\r\n. cmd tolerated it, which is why nobody noticed.
bat = R._write_launcher_bat(r"C:\py\python.exe")
raw = open(bat, "rb").read()
check("ends lines with exactly CRLF", b"\r\r\n" in raw, False)
check("and has CRLF at all",         b"\r\n" in raw, True)
check("runs the host with the chosen interpreter",
      b'"C:\\py\\python.exe" "' in raw and b"kam_host.py" in raw, True)

print("\n=== finding the compiler ===")
csc = R._find_csc()
check("returns an existing file or None",
      csc is None or os.path.exists(csc), True)
if os.name == "nt":
    check("on Windows the framework compiler is there", csc is not None, True)

print("\n=== a launcher that fails its self-test is never registered ===")
_real = R._write_launcher_windows
R._write_launcher_windows = lambda python_exe=None: None
try:
    if os.name == "nt":
        ok = R.register(R.PINNED_EXTENSION_ID)
        check("register() reports failure", ok, False)
        check("and writes no manifest", os.path.exists(R.MANIFEST_PATH), False)
    else:
        print("  (skipped: the launcher path is Windows-only)")
finally:
    R._write_launcher_windows = _real

print("\n=== the real thing: build it and make it answer ===")
# The one test that matters. It bakes in the Python running this suite, since
# kam_host.py needs only the standard library to answer a ping, then sends the
# framed ping Chrome would send and expects the framed pong back. On a machine
# with no compiler it is skipped rather than faked.
if os.name == "nt" and csc:
    exe = R._write_launcher_windows(sys.executable)
    check("the launcher is an .exe, not a .bat", bool(exe) and exe.endswith(".exe"), True)
    check("built where it was told to",          exe, R.LAUNCHER_EXE)
    check("the source is kept beside it",        os.path.exists(R.LAUNCHER_SRC), True)
    check("it relays a framed ping to the host and back",
          R._self_test(exe) if exe else False, True)
    import struct
    d = open(exe, "rb").read()
    pe = struct.unpack_from("<I", d, 0x3C)[0]
    check("it is a console-subsystem exe, so it has standard handles to relay",
          struct.unpack_from("<H", d, pe + 24 + 68)[0], 3)
else:
    print("  (skipped: needs Windows and the .NET Framework compiler)")

print("\n=== remove() takes every generated piece with it ===")
for p in (R.LAUNCHER_EXE, R.LAUNCHER_SRC, R.LAUNCHER_BAT, R.MANIFEST_PATH):
    pathlib.Path(p).write_bytes(b"x")
if os.name == "nt":
    import winreg
    _del = winreg.DeleteKey
    # The real key must survive this suite, so the delete is routed to the
    # "nothing there" branch rather than allowed to run.
    winreg.DeleteKey = lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError())
    try:
        R.remove(R.PINNED_EXTENSION_ID)
    finally:
        winreg.DeleteKey = _del
else:
    R.remove(R.PINNED_EXTENSION_ID)
for p in (R.LAUNCHER_EXE, R.LAUNCHER_SRC, R.LAUNCHER_BAT, R.MANIFEST_PATH):
    check(f"{os.path.basename(p)} is gone", os.path.exists(p), False)

shutil.rmtree(TMP, ignore_errors=True)
print(f"\n{'='*62}\n  {PASS} passed, {FAIL} failed\n{'='*62}")
sys.exit(1 if FAIL else 0)
