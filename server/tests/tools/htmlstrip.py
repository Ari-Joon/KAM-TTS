"""Strip comments from an HTML file, leaving everything else intact.

Three comment syntaxes live in these files and each is only valid in its own
region, so the document is split into regions first:

    inside <style>   /* ... */          CSS comments
    inside <script>  // ...  and /*..*/ JS comments (via jsstrip)
    everywhere else  <!-- ... -->       HTML comments

Getting this wrong in either direction matters: "/*" outside a <style> is just
text, and "<!--" inside one is CSS. Used to prove a comment-only restyle changed
no markup, no CSS and no script.
"""
import re, sys, pathlib
import jsstrip

_REGION = re.compile(r"(<style[^>]*>)(.*?)(</style>)|(<script[^>]*>)(.*?)(</script>)",
                     re.S | re.I)


def strip(src: str) -> str:
    out, pos = [], 0
    for m in _REGION.finditer(src):
        # everything before this region is ordinary markup
        out.append(re.sub(r"<!--.*?-->", "", src[pos:m.start()], flags=re.S))
        if m.group(1):                                   # <style>
            out.append(m.group(1))
            out.append(re.sub(r"/\*.*?\*/", "", m.group(2), flags=re.S))
            out.append(m.group(3))
        else:                                            # <script>
            out.append(m.group(4))
            body = m.group(5)
            # a src= script has an empty body; jsstrip handles either
            out.append(jsstrip.strip(body))
            out.append(m.group(6))
        pos = m.end()
    out.append(re.sub(r"<!--.*?-->", "", src[pos:], flags=re.S))
    return "".join(out)


def normalised(path) -> str:
    s = strip(pathlib.Path(path).read_text(encoding="utf-8"))
    return "\n".join(l.rstrip() for l in s.splitlines() if l.strip())


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[2] == "--out":
        pathlib.Path(sys.argv[3]).write_text(
            strip(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8")),
            encoding="utf-8")
        print(f"stripped -> {sys.argv[3]}")
    else:
        a, b = sys.argv[1], sys.argv[2]
        same = normalised(a) == normalised(b)
        print(("  MARKUP+CSS IDENTICAL   " if same else "  CONTENT DIFFERS !!  ")
              + pathlib.Path(b).name)
        sys.exit(0 if same else 1)
