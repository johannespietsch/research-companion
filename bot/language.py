"""Which language should the analysis be written in? Decided in code, not by the LLM.

Asking the model to "use the language of the profile text" doesn't hold: it
infers the person's language from biographical cues instead (an English profile
stating a nationality came back in that language, and with the nationality removed,
in the language of the city it mentioned). So we detect the profile's language here and hand the model a
literal "write in X".

No dependency: a stopword vote over the Latin-script languages a profile is
plausibly written in. Deterministic, ~free, and returns None when unsure (short
text, other scripts, near-ties) so the caller falls back to the prompt-only rule.
"""
from __future__ import annotations

import re

# High-frequency function words per language. Words shared between languages
# (e.g. "de", "la", "que") are fine: the vote is relative and needs a margin.
_STOPWORDS: dict[str, frozenset[str]] = {
    "English": frozenset(
        "the and of to that with for have my are this from not but was been they you your would "
        "will can about their which".split()
    ),
    "German": frozenset(
        "der die das und ich ist nicht mit ein eine für auf zu sind auch von den dem es wie aber "
        "bin habe mein meine im zum zur oder wenn nur".split()
    ),
    "Dutch": frozenset(
        "de het een van ik niet met voor zijn maar ook ben heb mijn naar dat wat dit je worden "
        "wordt bij nog".split()
    ),
    "French": frozenset(
        "le la les des et est je de un une pas pour que dans avec sur mon ma suis ai qui du au "
        "mais très aussi".split()
    ),
    "Spanish": frozenset(
        "el la los las y es de un una no para que con por en mi soy tengo pero del muy también "
        "como más".split()
    ),
    "Italian": frozenset(
        "il lo gli di che è non per con una sono ho mio ma anche della nel più come molto".split()
    ),
    "Portuguese": frozenset(
        "o os as de que não para com uma é sou tenho meu mas também do da em muito mais como".split()
    ),
}

_WORD = re.compile(r"[^\W\d_]+", re.UNICODE)

MIN_WORDS = 15  # below this a vote is noise
MIN_SHARE = 0.12  # winner's stopwords must be at least this share of all words
MIN_MARGIN = 1.3  # ...and beat the runner-up by this factor


def detect_language(text: str | None) -> str | None:
    """English name of the language `text` is written in, or None if unsure."""
    words = [w.lower() for w in _WORD.findall(text or "")]
    if len(words) < MIN_WORDS:
        return None
    scores = {
        lang: sum(1 for w in words if w in stop) for lang, stop in _STOPWORDS.items()
    }
    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    (best, best_n), (_, second_n) = ranked[0], ranked[1]
    if best_n / len(words) < MIN_SHARE or best_n < second_n * MIN_MARGIN:
        return None
    return best
