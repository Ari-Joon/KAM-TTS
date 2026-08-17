"""How maths, LaTeX and marker-shaped punctuation get read aloud.

Two habits are worth keeping here. First, an unsupported LaTeX command must be
audible rather than deleted, because a reading you can hear is wrong is fixable
and a silent one is not. Second, prose that merely looks like a list marker has
to survive: "item b)" and "P(A ∩ B)" both used to lose a bracket."""
import pathlib as _pl
import sys

HERE = _pl.Path(__file__).resolve().parent
head = (HERE / "test_pipeline.py").read_text(encoding="utf-8").split(
    "# ---------------------------------------------------------------------------")[0]
_g = {"__file__": str(HERE / "test_pipeline.py"), "__name__": "maths_probe"}
exec(compile(head, str(HERE / "test_pipeline.py"), "exec"), _g)
S = _g["S"]

PASS = FAIL = 0


def says(label, text, *must_contain):
    """Assert on content rather than an exact string, since the wording around
    these expressions is allowed to improve without breaking the test."""
    global PASS, FAIL
    got = S.clean_text(text)
    missing = [w for w in must_contain if w not in got]
    if not missing:
        PASS += 1
        print(f"  ok   {label}")
    else:
        FAIL += 1
        print(f"  FAIL {label}\n         got     {got!r}\n         missing {missing}")


def never(label, text, *must_not_contain):
    global PASS, FAIL
    got = S.clean_text(text)
    present = [w for w in must_not_contain if w in got]
    if not present:
        PASS += 1
        print(f"  ok   {label}")
    else:
        FAIL += 1
        print(f"  FAIL {label}\n         got   {got!r}\n         found {present}")


def exact(label, text, want):
    global PASS, FAIL
    got = S.clean_text(text)
    if got == want:
        PASS += 1
        print(f"  ok   {label}")
    else:
        FAIL += 1
        print(f"  FAIL {label}\n         got  {got!r}\n         want {want!r}")


print("\n=== LaTeX operators are spoken, not dropped ===")
# Every one of these fell through a catch-all that deleted unknown commands, so
# an intersection became a space and the meaning changed with no sign of it.
says("intersection survives",   r"P(A \cap B)",        "intersection")
says("union survives",          r"A \cup B",           "union")
says("element-of survives",     r"x \in S",            " in ")
says("subset survives",         r"A \subseteq B",      "subset")
says("for-all survives",        r"\forall x",          "for all")
says("there-exists survives",   r"\exists y",          "there exists")
says("partial survives",        r"\partial L",         "partial")
says("gradient survives",       r"\nabla f",           "gradient")
says("implies survives",        r"A \implies B",       "implies")
says("proportional survives",   r"y \propto x",        "proportional")
says("distributed-as survives", r"X \sim N",           "distributed")

print("\n=== the bracket comes back with it ===")
# The closing bracket used to be eaten by the list-marker rule, which matched
# " B)" anywhere. Losing a bracket mid-equation is silent and total.
says("closing bracket kept", r"P(A \cap B) = P(A)P(B)", "B)")
says("and the relation too",  r"P(A \cap B) = P(A)P(B)", "equals")

print("\n=== signs are not lost ===")
# A vanished minus in an exponent is the worst case in this file: the maths is
# still fluent and completely wrong.
says("negative exponent",       r"e^{-\lambda t}",  "minus")
says("minus inside a fraction", r"\frac{a}{b-c}",   "minus")
never("no bare hyphen left",    r"e^{-\lambda t}",  " - ")

print("\n=== fractions, roots, powers ===")
says("simple fraction",   r"\frac{1}{2}",       "1 over 2")
says("fraction operands", r"\frac{a+b}{c-d}",   "a plus b", "c minus d")
says("square root",       r"\sqrt{x}",          "square root")
says("nth root",          r"\sqrt[3]{x}",       "root")
says("squared",           r"x^{2}",             "squared")
says("cubed",             r"x^{3}",             "cubed")
says("general power",     r"x^{n}",             "to the power of")
says("derivative reads as one", r"\frac{\partial L}{\partial w}",
     "partial", "over")

print("\n=== sums and integrals read as limits, not subscripts ===")
says("sum with limits",      r"\sum_{i=1}^{n} x_i", "the sum from", " to n")
says("integral with limits", r"\int_0^\infty f",    "the integral from", "infinity")
never("no literal caret",    r"\int_0^\infty f",    "^", "_")

print("\n=== subscripts on multi-letter bases ===")
# \beta_0 used to leave a literal underscore, because the fallback rule only
# matched a single-letter base and "beta" is four.
says("greek with a subscript", r"\beta_0", "beta", "sub")
never("no stray underscore",   r"\beta_0", "_")
says("braced subscript",       r"X_{t+1}", "sub t plus 1")

print("\n=== an unknown command is read, never deleted ===")
# This is the safety property. A command with no reading should still be heard,
# so an unsupported one is obvious instead of quietly changing the sentence.
says("unknown command is audible", r"A \wibble B", "wibble")
never("and leaves no backslash",   r"A \wibble B", "\\")
says("environments are dropped",   r"\begin{align} x = 1 \end{align}", "x")

print("\n=== unicode maths, which arrives when the page is not LaTeX ===")
says("subset glyph",     "A ⊆ B",  "subset")
says("intersect glyph",  "A ∩ B",  "intersection")
says("for-all glyph",    "∀x",     "for all")
says("superscript two",  "x²",     "squared")
says("plus-minus",       "±0.5",   "plus or minus")
says("times glyph",      "3 × 4",  "times")
says("equals is spoken", "x² = 5", "equals")

print("\n=== list markers are stripped, and the item gets an ending ===")
# The full stop is deliberate. The boundary between items collapses to a space
# further down the pipeline, so without it the items run together into one long
# sentence and the pause between separate thoughts disappears.
exact("lettered marker", "a) first item",    "first item.")
exact("capital marker",  "B) second item",   "second item.")
exact("numbered marker", "1. numbered item", "numbered item.")
exact("paren number",    "3) another",       "another.")
exact("bullet glyph",    "• bullet item",  "bullet item.")
exact("dash bullet",     "- dash item",      "dash item.")
exact("star bullet",     "* star item",      "star item.")
exact("existing punctuation is not doubled", "- already done.", "already done.")

print("\n=== every marker in a multi-line list, not just the first ===")
says("all three numbers go", "Steps:\n1. Boil water\n2. Add tea\n3. Wait",
     "Boil water", "Add tea", "Wait")
never("no numbers left over", "Steps:\n1. Boil water\n2. Add tea\n3. Wait",
      "1.", "2.", "3.")
never("no letters left over", "Options:\na) keep it\nb) drop it", "a)", "b)")

print("\n=== the shape a page actually sends, which is |BREAK| not newlines ===")
# popup.js has li in its block tags, so items arrive separated by |BREAK|.
# Splitting on newlines alone missed every one of them, and a numbered list came
# through with its numbers intact and then read them out loud.
never("numbered markers go",
      "Steps:|BREAK|1. Boil water|BREAK|2. Add tea|BREAK|3. Wait", "1.", "2.", "3.")
says("and the items survive",
     "Steps:|BREAK|1. Boil water|BREAK|2. Add tea|BREAK|3. Wait",
     "Boil water", "Add tea", "Wait")
never("dash markers go", "|BREAK|- keep it|BREAK|- drop it", " - ")
exact("bullets become separate thoughts",
      "|BREAK|• one|BREAK|• two|BREAK|• three", "one. two. three.")
exact("dashes too", "|BREAK|- keep it|BREAK|- drop it", "keep it. drop it.")
says("items do not run together",
     "Steps:|BREAK|1. Boil water|BREAK|2. Add tea", "water. Add")

print("\n=== prose that only looks like a marker is left alone ===")
# The rule used to fire on anything after a space, so ordinary sentences lost
# characters at random.
says("mid-sentence letter and bracket", "See item b) for the exception.", "b)")
says("year range keeps its dash",  "The range was 2020-2021.",   "2020-2021")
says("score keeps its dash",       "He scored 7-3 today.",       "7-3")
says("hyphenated words intact",    "a well-known state-of-the-art result",
     "well-known", "state-of-the-art")
says("life dates intact",          "Ada Lovelace (1815-1852) wrote it.", "1815-1852")
says("sentence-ending number",     "Read section 4. Then continue.", "section 4")

print("\n=== C++ is a language, not an increment ===")
says("C++ reads as plus plus",  "The C++ language", "C plus plus")
says("g++ too",                 "Compile with g++", "g plus plus")
says("but code still increments", "count++", "increment")

print()
print(f"{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
