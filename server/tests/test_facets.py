# Import the real facet detectors from learner.py without importing learner
# (which would pull in whisper/librosa/sqlite side effects). We exec only the
# regex block.
import pathlib as _pl
SERVER_DIR    = _pl.Path(__file__).resolve().parent.parent
EXTENSION_DIR = SERVER_DIR.parent / "extension"
import re, io, sys, pathlib

src = (SERVER_DIR / "learner.py").read_text(encoding="utf-8")
start = src.index("_FACET_MATH = re.compile(")
end   = src.index("def chunk_profile(")
ns = {"re": re}
exec(src[start:end], ns)
analyse_facets = ns["analyse_facets"]

PROSE = [
    "The sky is blue today.",
    "Cats are wonderful animals.",
    "He walked home slowly.",
    "The class met on Tuesday afternoon.",
    "Import duties rose sharply last year.",
    "The function of the heart is to pump blood.",
    "It doesn't matter what you're doing.",
    "We were told the results are ready.",
    "There are seven steps to follow here.",
    "The morning light came through the window slowly.",
    "Although the plan seemed straightforward, the details mattered.",
    "In the end, what matters most is not how quickly you finish.",
    "She looked at the results and considered them carefully.",
    "This approach is faster than the previous one.",
    "The weather was perfect and everyone was in good spirits.",
]
TRUE_DEFN = [
    "A tuple is defined as an ordered pair.",
    "BFS stands for breadth-first search.",
    "Entropy means the average surprise of a distribution.",
    "A heuristic is a kind of estimate of remaining cost.",
    "Recursion - a function that calls itself.",
]
TRUE_CODE = [
    "def parse(x):",
    "import numpy as np",
    "const total = items.length;",
    "result = client.analyze(image)",
    "value = compute()",
]
TRUE_QUOTE = [
    'She said, "This changes everything."',
    "The author writes at length about the problem.",
    "\u201cWe solved the wrong problem,\u201d he replied.",
]

PASS = FAIL = 0

def rate(name, samples, facet, want):
    global PASS, FAIL
    hits = [s for s in samples if analyse_facets(s)[facet]]
    ok = len(hits) if want else len(samples) - len(hits)
    PASS += ok
    FAIL += len(samples) - ok
    print(f"  {name:14} {ok}/{len(samples)} correct")
    for s in samples:
        got = analyse_facets(s)[facet]
        if got != want:
            print(f"      MISLABELLED ({facet}={got}): {s}")

print("Ordinary prose must NOT get technical facets:")
rate("definition", PROSE, "definition", False)
rate("code",       PROSE, "code",       False)
rate("quote",      PROSE, "quote",      False)

print("\nReal instances must still be detected:")
rate("definition", TRUE_DEFN,  "definition", True)
rate("code",       TRUE_CODE,  "code",       True)
rate("quote",      TRUE_QUOTE, "quote",      True)

print("\nprimary facet assigned to ordinary prose:")
from collections import Counter
c = Counter(analyse_facets(s)["primary"] for s in PROSE)
print("  ", dict(c))

# Ordinary prose must end up as plain prose, not filed under a technical facet.
_stray = [s for s in PROSE if analyse_facets(s)["primary"] != "prose"]
for s in _stray:
    print(f"  WRONG PRIMARY ({analyse_facets(s)['primary']}): {s}")
PASS += len(PROSE) - len(_stray)
FAIL += len(_stray)

print()
print(f"{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
