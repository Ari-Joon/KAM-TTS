"""Prove a restyle changed only prose: compare the AST with every string
constant blanked, so control flow, calls, names and numbers must match exactly."""
import ast, sys

class Blank(ast.NodeTransformer):
    def visit_Constant(self, node):
        if isinstance(node.value, str):
            return ast.copy_location(ast.Constant(value=""), node)
        return node
    def visit_JoinedStr(self, node):          # f-strings
        self.generic_visit(node)
        return ast.copy_location(ast.Constant(value=""), node)

def logic(path):
    t = ast.parse(open(path, encoding="utf-8").read())
    for n in ast.walk(t):
        if isinstance(n, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                          ast.ClassDef)) and n.body:
            f = n.body[0]
            if (isinstance(f, ast.Expr) and isinstance(f.value, ast.Constant)
                    and isinstance(f.value.value, str)):
                n.body = n.body[1:] or [ast.Pass()]
    return ast.dump(ast.fix_missing_locations(Blank().visit(t)))

a, b = sys.argv[1], sys.argv[2]
ok = logic(a) == logic(b)
print(("  LOGIC IDENTICAL   " if ok else "  LOGIC CHANGED !!  ") + b.split("\\")[-1])
sys.exit(0 if ok else 1)
