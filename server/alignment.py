"""
alignment.py — word-level alignment analysis (Part 3)

Whisper's transcribe(word_timestamps=True) returns, per word, a start time, end
time and an average token probability. That is effectively a forced alignment of
the synthesized audio to its transcript, for free — no extra model, no GPU
contention (the exact risk that retired GPU-Whisper earlier in this project).

This module turns those raw timings into actionable signal:

  - per-word confidence      → which specific words Whisper was unsure it heard
                               (low confidence = the synthesis was unclear there)
  - local speaking rate      → words/sec across the chunk and any rushed span
                               (a burst far above the chunk mean = clipped/rushed)
  - problem localisation     → instead of "this chunk scored 0.84", the learner
                               learns "the word 'algorithm' at 2.3s was low
                               confidence and rushed", a far richer training cue

It upgrades the feedback loop without changing where synthesis happens. Pure
analysis: it consumes Whisper's result dict and returns a small summary.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List


# A word is "low confidence" below this average token probability.
_LOW_CONF = 0.45
# A word is "rushed" if its local rate exceeds the chunk mean by this factor.
_RUSH_FACTOR = 1.8
# Ignore ultra-short function words for rate (they're naturally fast).
_RATE_MIN_CHARS = 4


@dataclass
class WordTiming:
    word:  str
    start: float
    end:   float
    prob:  float

    @property
    def duration(self) -> float:
        return max(1e-3, self.end - self.start)


@dataclass
class AlignmentResult:
    available:       bool = False
    word_count:      int = 0
    mean_confidence: float = 1.0
    low_conf_words:  List[str] = field(default_factory=list)   # unclear words
    rushed_words:    List[str] = field(default_factory=list)   # clipped/rushed words
    chunk_rate:      float = 0.0                               # words per second
    duration:        float = 0.0


def _extract_words(whisper_result: dict) -> List[WordTiming]:
    """Pull a flat list of WordTiming from Whisper's segment/word structure.
    Whisper nests words under segments when word_timestamps=True."""
    out: List[WordTiming] = []
    for seg in (whisper_result or {}).get("segments", []) or []:
        for w in seg.get("words", []) or []:
            try:
                token = str(w.get("word", "")).strip()
                if not token:
                    continue
                out.append(WordTiming(
                    word  = token,
                    start = float(w.get("start", 0.0)),
                    end   = float(w.get("end", 0.0)),
                    prob  = float(w.get("probability", w.get("prob", 1.0))),
                ))
            except (TypeError, ValueError):
                continue
    return out


def analyse(whisper_result: dict) -> AlignmentResult:
    """Analyse Whisper's word-timestamp output into alignment signals.

    Safe on any input: returns available=False if the result has no word-level
    timing (e.g. an older Whisper call without word_timestamps)."""
    words = _extract_words(whisper_result)
    if not words:
        return AlignmentResult()

    n = len(words)
    mean_conf = sum(w.prob for w in words) / n
    total_dur = max(1e-3, words[-1].end - words[0].start)
    chunk_rate = n / total_dur

    low_conf = [w.word for w in words if w.prob < _LOW_CONF]

    # Rushed words: local rate (1/duration) far above the chunk's mean word rate.
    mean_word_dur = sum(w.duration for w in words) / n
    rushed = [
        w.word for w in words
        if len(w.word) >= _RATE_MIN_CHARS
        and w.duration > 0
        and (mean_word_dur / w.duration) >= _RUSH_FACTOR
    ]

    return AlignmentResult(
        available       = True,
        word_count      = n,
        mean_confidence = round(mean_conf, 3),
        low_conf_words  = low_conf[:5],
        rushed_words    = rushed[:5],
        chunk_rate      = round(chunk_rate, 2),
        duration        = round(total_dur, 2),
    )


def summary_flags(result: AlignmentResult) -> str:
    """Compact quality-flag fragment for the chunk record, mirroring the
    existing WHISPER_SKIP/HALLUCINATION style. Empty when nothing notable."""
    parts: List[str] = []
    if result.low_conf_words:
        parts.append("LOWCONF:" + ",".join(result.low_conf_words[:3]))
    if result.rushed_words:
        parts.append("RUSHED:" + ",".join(result.rushed_words[:3]))
    return "|".join(parts)
