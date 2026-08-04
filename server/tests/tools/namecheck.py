"""Find names that are read but never defined/imported at module level, and
module-level names that are never read. Pure AST — no imports executed."""
import ast, sys, builtins, pathlib
from collections import defaultdict

BUILTINS = set(dir(builtins))

def analyse(path):
    src = pathlib.Path(path).read_text(encoding="utf-8")
    tree = ast.parse(src)

    defined = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            defined.add(node.name)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            defined.add(node.name)
            defined.update(a.arg for a in node.args.posonlyargs)
            defined.update(a.arg for a in node.args.args)
            defined.update(a.arg for a in node.args.kwonlyargs)
            if node.args.vararg: defined.add(node.args.vararg.arg)
            if node.args.kwarg:  defined.add(node.args.kwarg.arg)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for a in node.names:
                defined.add((a.asname or a.name).split(".")[0])
        elif isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            defined.add(node.id)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            defined.add(node.name)
        elif isinstance(node, (ast.comprehension,)):
            for t in ast.walk(node.target):
                if isinstance(t, ast.Name): defined.add(t.id)
        elif isinstance(node, ast.Global):
            defined.update(node.names)
        elif isinstance(node, (ast.With, ast.AsyncWith)):
            for item in node.items:
                if item.optional_vars:
                    for t in ast.walk(item.optional_vars):
                        if isinstance(t, ast.Name): defined.add(t.id)

    used = defaultdict(list)
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            used[node.id].append(node.lineno)

    unresolved = {n: ls for n, ls in used.items()
                  if n not in defined and n not in BUILTINS}

    # Module-level assignments/functions that are never read anywhere in file.
    top = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
            top.add(node.name)
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name): top.add(t.id)
    unread = sorted(n for n in top if n not in used and not n.startswith("__"))

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

base = str(pathlib.Path(__file__).resolve().parent.parent.parent)
for f in ("server.py", "learner.py", "pos_prosody.py", "alignment.py"):
    analyse(f"{base}\\{f}")
