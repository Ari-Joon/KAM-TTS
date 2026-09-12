"""Find names that are read but never defined or imported, and module-level names
that are never read. Pure AST, so nothing is imported or executed.

    python tests/tools/namecheck.py        report on every server module

The analysis is per file rather than per scope, so a name bound anywhere in the
file counts as defined. That makes it blind to some mistakes, but what it does
flag is almost always real: it is how `sys.executable` was caught in a file that
only ever imports sys under an alias, a NameError that a broad except had been
swallowing on every boot. test_namecheck.py runs find_names() as a gate.
"""
import ast
import builtins
import pathlib
from collections import defaultdict

BUILTINS = set(dir(builtins))

SERVER_DIR = pathlib.Path(__file__).resolve().parent.parent.parent

# Every Python module the server ships. Tests and tools are left out, since they
# stub and exec things in ways a file-level check cannot follow.
MODULES = (
    "server.py", "learner.py", "pos_prosody.py", "alignment.py",
    "register_host.py", "scan_host.py", "kam_host.py", "device.py",
    "audio_quality.py", "benchmark.py", "setup_kam.py",
    "clean_voice_clips.py", "hardware_profile.py",
)


def find_names(path):
    """Return (unresolved, unread) for one file.

    unresolved maps each name read but never bound to the lines it is read on.
    unread lists module-level functions, classes and assignments that nothing in
    the file reads, which for a Flask app is mostly routes and so is advisory."""
    src = pathlib.Path(path).read_text(encoding="utf-8")
    tree = ast.parse(src)

    defined = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            defined.add(node.name)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            if not isinstance(node, ast.Lambda):
                defined.add(node.name)
            args = node.args
            defined.update(a.arg for a in args.posonlyargs)
            defined.update(a.arg for a in args.args)
            defined.update(a.arg for a in args.kwonlyargs)
            if args.vararg: defined.add(args.vararg.arg)
            if args.kwarg:  defined.add(args.kwarg.arg)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for a in node.names:
                defined.add((a.asname or a.name).split(".")[0])
        elif isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            defined.add(node.id)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            defined.add(node.name)
        elif isinstance(node, ast.comprehension):
            for t in ast.walk(node.target):
                if isinstance(t, ast.Name): defined.add(t.id)
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            defined.update(node.names)
        elif isinstance(node, (ast.With, ast.AsyncWith)):
            for item in node.items:
                if item.optional_vars:
                    for t in ast.walk(item.optional_vars):
                        if isinstance(t, ast.Name): defined.add(t.id)
        elif isinstance(node, ast.NamedExpr) and isinstance(node.target, ast.Name):
            defined.add(node.target.id)

    used = defaultdict(list)
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            used[node.id].append(node.lineno)

    unresolved = {n: ls for n, ls in used.items()
                  if n not in defined and n not in BUILTINS}

    top = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
            top.add(node.name)
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name): top.add(t.id)
    unread = sorted(n for n in top if n not in used and not n.startswith("__"))
    return unresolved, unread


def report(path):
    unresolved, unread = find_names(path)
    print(f"\n=== {pathlib.Path(path).name} ===")
    if unresolved:
        print("  UNDEFINED NAMES:")
        for n, ls in sorted(unresolved.items()):
            print(f"    {n}  (lines {ls[:6]})")
    else:
        print("  no undefined names")
    if unread:
        print("  module-level names never read in this file:")
        for n in unread:
            print(f"    {n}")


if __name__ == "__main__":
    for name in MODULES:
        p = SERVER_DIR / name
        if p.exists():
            report(p)
