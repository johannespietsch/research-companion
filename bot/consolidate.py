"""Cross-source suggestion consolidation (#70): N reads → 1 action.

Different sources frequently converge on the same next step ("set up an eval
harness", "try the new model on your pipeline"). Without consolidation each
read appends its own copy, and the Shortlist becomes the inbox-overflow it
was meant to cure. Two consumers use this module:

- the Shortlist save path: a new save that closely matches an existing entry
  *merges into it* (the existing entry gains a source) instead of appending;
- the weekly digest: convergent suggestions from the week's items collapse
  into one action backed by all of its sources.

Similarity is deliberately cheap and deterministic — token overlap (Jaccard)
over the normalized title+detail. No embeddings, no LLM calls, fully
testable. It only needs to catch *obvious* convergence; a missed merge is a
minor annoyance, a wrong merge would be confusing, so the threshold errs
conservative.
"""
from __future__ import annotations

import re

# Jaccard similarity at/above this counts as "the same next step". Tuned
# conservative: distinct-but-related suggestions should NOT merge.
SIMILARITY_THRESHOLD = 0.5

# Words too common in suggestion copy to carry signal.
_STOPWORDS = frozenset(
    "a an the and or of to in on for with your you it its this that try use "
    "set up out new build add make start get".split()
)

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def tokens(text: str) -> frozenset[str]:
    """Normalized signal tokens of a suggestion text."""
    return frozenset(
        t for t in _TOKEN_RE.findall((text or "").lower())
        if len(t) > 2 and t not in _STOPWORDS
    )


def similarity(a: str, b: str) -> float:
    """Jaccard overlap of the two texts' token sets. 0.0 when either is empty."""
    ta, tb = tokens(a), tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def suggestion_text(title: str, detail: str) -> str:
    return f"{title or ''} {detail or ''}".strip()


def find_similar(title: str, detail: str, candidates: list[dict]) -> dict | None:
    """Best candidate at/above the threshold, else None.

    ``candidates`` are dicts (or Rows) with at least title/detail. Ties go to
    the highest score; equal scores keep the first (oldest-listed) candidate
    so merging is stable.
    """
    text = suggestion_text(title, detail)
    best, best_score = None, 0.0
    for c in candidates:
        score = similarity(text, suggestion_text(c["title"], c["detail"]))
        if score >= SIMILARITY_THRESHOLD and score > best_score:
            best, best_score = c, score
    return best


def cluster(items: list[dict]) -> list[list[dict]]:
    """Greedy single-link clustering for the digest: each item joins the first
    cluster whose representative it matches, else starts its own. Order is
    preserved (first occurrence leads the cluster), so an input ranked
    best-first yields clusters ranked best-first."""
    clusters: list[list[dict]] = []
    for item in items:
        text = suggestion_text(item.get("title", ""), item.get("detail", ""))
        for c in clusters:
            rep = c[0]
            if similarity(text, suggestion_text(rep.get("title", ""), rep.get("detail", ""))) >= SIMILARITY_THRESHOLD:
                c.append(item)
                break
        else:
            clusters.append([item])
    return clusters
