# Tests

```bash
python tests/run_tests.py            # everything
python tests/run_tests.py pipeline   # just the suites matching that name
```

Exit code is 0 only if every suite passes, so it works as a gate.

**These do not need torch, TTS, whisper or spaCy.** The heavy imports are stubbed
and the module source is executed with the `__main__` block sliced off, which
matters because the server runs on one specific Python install and I did not want
testing to depend on that. Any Python 3.10+ with numpy will do. The one
JavaScript suite needs node and is skipped if it is missing.

**Nothing here touches the live data.** The two suites that need a database copy
`tts_quality.db` and `good_settings.json` into a temp directory first, because
they write chunks, rules, reports and observations. If no database exists yet,
learner builds an empty one and the tests still pass.

| Suite | Covers |
|-------|--------|
| `test_pipeline.py` | Text cleaning, sentence-type labelling, heading levels, the substitution tables, audio processing, the SUPPRESS sentinel |
| `test_learner.py` | Schema migrations, report validation, failure records, `/diagnose`, the rule cache, voice isolation, the fingerprint round-trip |
| `test_modules.py` | Device selection across CUDA/ROCm/MPS/XPU/CPU, the reference-clip gate, hallucination detection |
| `test_startup.py` | What the console prints on seven machine types, plus the no-PyTorch case |
| `test_reinforcement.py` | That a thumbs-up reinforces the chunk's own parameters, that the learner never writes the live settings, and that rejected attempts become usable evidence |
| `test_extension_id.py` | Which extension origin the server trusts, including missing and corrupt manifests |
| `test_facets.py` | Content-facet labelling, especially that ordinary prose is not filed as a definition or code |
| `test_audio.py` | Gain staging: level preserved against the old chain, nothing clipped |
| `test_feed.mjs` | The dashboard live-feed pause and its pending count |
| `test_maths.py` | LaTeX and unicode maths, and list markers that prose only looks like |
| `test_markers.mjs` | Structure from the page walkers through the chunker to the position hint: that a bullet is wrapped and a heading keeps its level, that every hint is one `_POSITION_TYPE` acts on, and that the marker grammar matches across the four files that strip it |
| `test_recorder.mjs` | WAV encoding and where a take gets trimmed |
| `test_voiceclips.py` | Clip slots, transcript sidecars, and what the recorder uploads |

## tools/

Verification helpers rather than tests. They exist to prove a change touched only
what it claimed to:

- `logiccheck.py A B` — compares two Python files by AST with every string
  constant blanked, so control flow, calls, names and numbers must match. This is
  what proved the comment restyle changed no code.
- `jsstrip.py A B` — the same idea for JavaScript. It handles template literals
  and regex literals, which share `/` with comments. Validated by checking that
  `node --check` still passes on the stripped output.
- `htmlstrip.py A B` — region-aware, since `<!-- -->`, `/* */` and `//` are each
  only valid in their own part of an HTML file.
- `namecheck.py` — undefined names and module-level names nothing reads.

## Adding to these

Each suite is a plain script with a `check(label, got, want)` helper, a running
count, and `sys.exit(1)` when anything failed. No framework. Keep it that way:
the runner only needs an exit code and a final `N passed, M failed` line.

Worth confirming a new test actually fails when the code is wrong. It is easy to
write one that passes for the wrong reason, and a test that cannot fail is worse
than none because it reads as coverage.
