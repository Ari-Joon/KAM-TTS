"""No server module reads a name it never defines.

This became a gate after a NameError shipped. server.py only imports sys under
aliases, the boot-time registration repair called sys.executable, and the except
around it turned the NameError into a log line nobody reads: "Registration check
skipped (name 'sys' is not defined)", on every boot, while the feature it
disabled was described as working. tests/tools/namecheck.py found it in seconds,
but only because it happened to be run by hand.

The check is per file rather than per scope, so it cannot catch everything, but
anything it does report is worth failing on. __file__ is the one standing
exception: it is set by the interpreter for every module and the AST cannot
know that.
"""
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "tools"))
import namecheck as N

PASS = FAIL = 0


def check(label, got, want):
    global PASS, FAIL
    if got == want:
        PASS += 1
        print(f"  ok   {label}")
    else:
        FAIL += 1
        print(f"  FAIL {label}\n         got:  {got!r}\n         want: {want!r}")


ALLOWED = {"__file__"}

print("\n=== every server module defines what it reads ===")
for name in N.MODULES:
    path = N.SERVER_DIR / name
    if not path.exists():
        print(f"  (skipped: {name} not present)")
        continue
    unresolved, _ = N.find_names(path)
    bad = {n: ls[:6] for n, ls in unresolved.items() if n not in ALLOWED}
    check(f"{name} has no undefined names", bad, {})

print("\n=== the checker would have caught the bug that made it a gate ===")
# A test that cannot fail is worse than none, so prove it fails on the exact
# shape of the original mistake.
import tempfile
probe = pathlib.Path(tempfile.mkdtemp(prefix="kam_namecheck_")) / "probe.py"
probe.write_text("import sys as _sys\n\ndef boot():\n    return sys.executable\n",
                 encoding="utf-8")
unresolved, _ = N.find_names(probe)
check("sys read under an alias-only import is reported", "sys" in unresolved, True)
probe.write_text("import sys as _sys\n\ndef boot():\n    return _sys.executable\n",
                 encoding="utf-8")
unresolved, _ = N.find_names(probe)
check("and the fixed form is not", "sys" in unresolved, False)

print(f"\n{'='*62}\n  {PASS} passed, {FAIL} failed\n{'='*62}")
sys.exit(1 if FAIL else 0)
