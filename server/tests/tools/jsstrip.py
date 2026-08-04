"""Strip comments from JavaScript, leaving everything else byte-identical.

Used to prove a comment-only restyle changed no code. Handles the cases that
actually bite: template literals with nested ${}, and regex literals, which
share the '/' character with comments and division.

Regex-vs-division is decided by the previous significant token, which is the
standard heuristic and is exact for all the code in this project.
"""
import sys, pathlib

# After these, a '/' starts a REGEX. After anything else (identifier, number,
# ')', ']', '}') it is division.
_REGEX_OK_PUNCT = set("({[,;:=!&|?+-*%^~<>")
_REGEX_OK_WORDS = {"return", "typeof", "instanceof", "in", "of", "new", "delete",
                   "void", "throw", "case", "do", "else", "yield", "await"}


def strip(src: str) -> str:
    out = []
    i, n = 0, len(src)
    prev = ""            # last significant char emitted
    prev_word = ""       # last identifier emitted
    tmpl_depth = []      # stack for `${ }` inside template literals

    while i < n:
        c = src[i]
        nxt = src[i + 1] if i + 1 < n else ""

        # line comment
        if c == "/" and nxt == "/":
            while i < n and src[i] != "\n":
                i += 1
            continue
        # block comment
        if c == "/" and nxt == "*":
            i += 2
            while i < n and not (src[i] == "*" and i + 1 < n and src[i + 1] == "/"):
                i += 1
            i += 2
            out.append(" ")
            continue
        # strings
        if c in "'\"":
            q = c
            out.append(c); i += 1
            while i < n:
                if src[i] == "\\":
                    out.append(src[i:i + 2]); i += 2; continue
                out.append(src[i])
                if src[i] == q:
                    i += 1; break
                i += 1
            prev = q
            continue
        # template literal
        if c == "`":
            out.append(c); i += 1
            while i < n:
                if src[i] == "\\":
                    out.append(src[i:i + 2]); i += 2; continue
                if src[i] == "$" and i + 1 < n and src[i + 1] == "{":
                    out.append("${"); i += 2
                    depth = 1
                    # recurse through the interpolation, which may contain
                    # comments, strings and nested templates
                    start = i
                    while i < n and depth:
                        if src[i] == "{": depth += 1
                        elif src[i] == "}": depth -= 1
                        elif src[i] in "'\"`":
                            q = src[i]; i += 1
                            while i < n and src[i] != q:
                                i += 2 if src[i] == "\\" else 1
                        elif src[i] == "/" and i + 1 < n and src[i + 1] == "/":
                            while i < n and src[i] != "\n": i += 1
                            continue
                        i += 1
                    out.append(strip(src[start:i - 1]))
                    out.append("}")
                    continue
                out.append(src[i])
                if src[i] == "`":
                    i += 1; break
                i += 1
            prev = "`"
            continue
        # regex literal
        if c == "/":
            is_regex = (prev == "" or prev in _REGEX_OK_PUNCT
                        or prev_word in _REGEX_OK_WORDS)
            if is_regex:
                out.append(c); i += 1
                in_class = False
                while i < n:
                    if src[i] == "\\":
                        out.append(src[i:i + 2]); i += 2; continue
                    if src[i] == "[": in_class = True
                    elif src[i] == "]": in_class = False
                    out.append(src[i])
                    if src[i] == "/" and not in_class:
                        i += 1; break
                    i += 1
                while i < n and src[i].isalpha():   # flags
                    out.append(src[i]); i += 1
                prev = "/"
                continue

        out.append(c)
        if not c.isspace():
            prev = c
            if c.isalnum() or c in "_$":
                prev_word = (prev_word + c) if (prev_word or c.isalpha() or c in "_$") else c
            else:
                prev_word = ""
        i += 1
    return "".join(out)


def normalised(path) -> str:
    """Comment-free source with whitespace collapsed, for comparison."""
    s = strip(pathlib.Path(path).read_text(encoding="utf-8"))
    return "\n".join(l.rstrip() for l in s.splitlines() if l.strip())


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--out":
        print("usage: jsstrip.py FILE --out OUT | jsstrip.py A B")
    elif len(sys.argv) == 4 and sys.argv[2] == "--out":
        pathlib.Path(sys.argv[3]).write_text(
            strip(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8")),
            encoding="utf-8")
        print(f"stripped -> {sys.argv[3]}")
    else:
        a, b = sys.argv[1], sys.argv[2]
        same = normalised(a) == normalised(b)
        print(("  CODE IDENTICAL   " if same else "  CODE DIFFERS !!  ")
              + pathlib.Path(b).name)
        sys.exit(0 if same else 1)
