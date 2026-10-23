import pytest

from bot.language import detect_language

AI_ENGINEER = (
    "Day to day I build and evaluate LLM-powered features, maintain our retrieval "
    "pipelines, review pull requests and write evals for new models. I want "
    "practical, concrete next steps that I can try within a week."
)
DE = (
    "Ich bin KI-Ingenieur. Täglich baue und bewerte ich Funktionen auf Basis von LLMs, "
    "pflege unsere Retrieval-Pipelines und prüfe Pull Requests. Ich möchte konkrete "
    "Vorschläge, die ich innerhalb einer Woche ausprobieren kann."
)
# A nationality or city in an English profile must not change the language.
EN_WITH_CUES = "I'm a Polish AI engineer based in Lisbon. " + AI_ENGINEER


@pytest.mark.parametrize(
    "text,expected",
    [
        (AI_ENGINEER, "English"),
        (EN_WITH_CUES, "English"),
        (DE, "German"),
        ("Ik ben AI-engineer. Elke dag bouw en evalueer ik functies met LLM's, onderhoud ik "
         "onze retrieval-pipelines en review ik pull requests. Ik wil concrete suggesties "
         "die ik binnen een week kan uitproberen.", "Dutch"),
        ("Je suis ingénieur IA. Chaque jour, je développe et j'évalue des fonctionnalités "
         "basées sur des LLM, je maintiens nos pipelines de recherche et je relis des pull "
         "requests. Je veux des suggestions concrètes que je peux essayer en une semaine.", "French"),
    ],
)
def test_detects_profile_language(text, expected):
    assert detect_language(text) == expected


@pytest.mark.parametrize(
    "text",
    [None, "", "I like AI", "私はソフトウェアエンジニアで、AIについて学びたいです。時間があまりありません。"],
)
def test_returns_none_when_unsure(text):
    assert detect_language(text) is None


def test_anon_default_and_personas_are_english():
    from bot.analyzer import DEFAULT_PROFILE
    from bot.personas import ANON_PERSONAS

    for text in (DEFAULT_PROFILE, *ANON_PERSONAS.values()):
        assert detect_language(text) == "English"
