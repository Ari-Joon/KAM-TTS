"""
pos_prosody.py — POS-informed prosody (NLP layer)

Uses spaCy's small English model to analyse a chunk's part-of-speech and clause
structure, then derives prosody signals the synthesis pipeline can act on:

  - clause boundaries  → where to place natural pauses (commas the writer
                          omitted, conjunction/subordination boundaries)
  - content vs function → which words carry emphasis (nouns, verbs, adjectives,
                          adverbs) versus unstressed glue (determiners, prepos-
                          itions, conjunctions, auxiliaries)
  - tag-aware ending    → whether the chunk ends mid-thought (trailing
                          conjunction/preposition) for pause shortening
  - clause form         → fragment / imperative / interrogative, so the chunk is
                          LABELLED by what it grammatically is rather than by
                          whichever punctuation mark happens to end it

This is the genuine NLP tier of KAM TTS: a statistical model performs
tokenisation, POS tagging and dependency parsing, and the result drives prosody
rather than punctuation heuristics alone.

Design notes:
  - The spaCy model is loaded lazily and ONCE (first call), on CPU, so it never
    contends with XTTS/Whisper on the GPU and adds no import-time cost when a
    request never needs it.
  - ONE parse per chunk. Every signal below comes out of a single nlp() call.
    The module previously exposed analyse() and sentence_info() as separate
    entry points and the server called both, parsing each chunk twice.
  - If spaCy or the model is unavailable, every function degrades to a safe
    no-op result, so the server runs unchanged without the dependency.
  - Pure analysis: nothing here mutates global state or touches the model.
"""
from __future__ import annotations

import importlib.util as _ilu
from dataclasses import dataclass, field
from typing import List

# Availability is probed without importing the heavy package at module load.
_SPACY_OK = _ilu.find_spec("spacy") is not None

_NLP = None          # the loaded spaCy pipeline (set on first use)
_NLP_TRIED = False    # so a failed load is attempted only once

# Universal POS tags that carry semantic content (candidates for emphasis).
_CONTENT_POS = {"NOUN", "PROPN", "VERB", "ADJ", "ADV", "NUM"}
# Dependency labels that mark the start of a new clause/phrase boundary.
_CLAUSE_DEPS = {"cc", "mark", "advcl", "relcl", "conj", "prep"}
# Subject dependencies — their ABSENCE on an initial base-form verb is what
# distinguishes an imperative ("Load the data") from a declarative.
_SUBJECT_DEPS = {"nsubj", "nsubjpass", "expl", "csubj"}


@dataclass
class POSProsody:
    """Prosody signals derived from the POS/dependency analysis of one chunk."""
    available:        bool = False           # was spaCy analysis actually run
    emphasis_words:   List[str] = field(default_factory=list)  # content lemmas to stress
    clause_breaks:    List[int] = field(default_factory=list)  # token indices that begin a new clause
    ends_mid_thought: bool = False           # trailing conjunction/preposition
    token_count:      int = 0
    sentence_count:   int = 0                # >1 = the splitter missed a boundary
    # Clause form — used to label the chunk correctly rather than guess from
    # punctuation. A fragment must not be called a 'sentence'; an imperative
    # ("Load the data.") is a distinct prosodic shape from a declarative.
    has_root_verb:    bool = False
    is_fragment:      bool = False
    is_imperative:    bool = False
    is_interrogative: bool = False


def _get_nlp():
    """Load spaCy's small English model once, on CPU. Returns None if spaCy or
    the model is not installed (the caller then degrades to a no-op)."""
    global _NLP, _NLP_TRIED
    if _NLP is not None or _NLP_TRIED:
        return _NLP
    _NLP_TRIED = True
    if not _SPACY_OK:
        print("[POS] spaCy not installed — POS prosody disabled "
              "(pip install spacy && python -m spacy download en_core_web_sm)")
        return None
    try:
        import spacy  # type: ignore
        try:
            # Disable NER: we only need the tagger and parser, so this is faster.
            _NLP = spacy.load("en_core_web_sm", disable=["ner", "lemmatizer"])
        except OSError:
            print("[POS] en_core_web_sm not found — run: "
                  "python -m spacy download en_core_web_sm")
            _NLP = None
        else:
            print("[POS] spaCy en_core_web_sm loaded (POS prosody active)")
    except Exception as e:
        print(f"[POS] spaCy load failed, POS prosody disabled: {e}")
        _NLP = None
    return _NLP


def analyse(text: str) -> POSProsody:
    """Analyse a chunk and return ALL its POS-derived prosody and structure
    signals from a single parse.

    Safe to call unconditionally: returns an empty (available=False) result if
    spaCy is unavailable or the text is trivial."""
    nlp = _get_nlp()
    if nlp is None or not text or not text.strip():
        return POSProsody()

    try:
        doc = nlp(text)
    except Exception as e:
        print(f"[POS] analyse error (skipping): {e}")
        return POSProsody()

    emphasis: List[str] = []
    clause_breaks: List[int] = []
    last_alpha = None
    root = None

    for tok in doc:
        if tok.pos_ in _CONTENT_POS and tok.is_alpha and len(tok.text) > 2:
            emphasis.append(tok.text)
        if tok.dep_ in _CLAUSE_DEPS and tok.i > 0:
            clause_breaks.append(tok.i)
        if tok.is_alpha:
            last_alpha = tok
        if tok.dep_ == "ROOT" and root is None:
            root = tok

    has_root_verb = root is not None and root.pos_ in ("VERB", "AUX")

    # Imperative: the root is a base-form verb with no subject of its own, and
    # the chunk opens on it (or on an adverb modifying it) — "Load the data",
    # "Now compare against the benchmark".
    is_imperative = False
    if has_root_verb and root is not None:
        has_subject = any(c.dep_ in _SUBJECT_DEPS for c in root.children)
        leading = [t for t in doc[:root.i] if t.is_alpha]
        is_imperative = (
            not has_subject
            and root.tag_ == "VB"                      # base form
            and all(t.pos_ in ("ADV", "INTJ") for t in leading)
        )

    # Interrogative: a wh-word or an auxiliary/verb leading the clause, judged
    # from the parse rather than from a trailing "?" that may be missing.
    is_interrogative = False
    if len(doc) > 0:
        first = next((t for t in doc if t.is_alpha), None)
        if first is not None:
            is_interrogative = (
                first.tag_ in ("WDT", "WP", "WP$", "WRB")
                or (first.pos_ == "AUX" and has_root_verb and not is_imperative)
            )

    # Mid-thought if the last real word is a conjunction, preposition, or
    # subordinating marker — the sentence is reaching forward to a continuation.
    ends_mid = bool(last_alpha is not None and last_alpha.pos_ in {"CCONJ", "SCONJ", "ADP"})

    return POSProsody(
        available        = True,
        emphasis_words   = emphasis,
        clause_breaks    = clause_breaks,
        ends_mid_thought = ends_mid,
        token_count      = len(doc),
        sentence_count   = len(list(doc.sents)),
        has_root_verb    = has_root_verb,
        is_fragment      = not has_root_verb,
        is_imperative    = is_imperative,
        is_interrogative = is_interrogative,
    )


def is_available() -> bool:
    """True if spaCy + model loaded successfully (for startup logging/health)."""
    return _get_nlp() is not None
