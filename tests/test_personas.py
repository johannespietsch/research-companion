"""Anon persona selection (#72): the lens an anonymous visitor picks flows
into the analyzer's profile, partitions the cache, and never overrides a
signed-in user's saved lens."""
from __future__ import annotations

from types import SimpleNamespace

import pytest


class TestResolve:
    def test_known_keys_return_their_profile(self):
        from bot.personas import ANON_PERSONA_KEYS, resolve_anon_profile
        assert ANON_PERSONA_KEYS == {"leader", "explorer", "builder"}
        leader = resolve_anon_profile("leader").lower()
        explorer = resolve_anon_profile("explorer").lower()
        builder = resolve_anon_profile("builder").lower()
        # Each captures a posture (decide / ease-in / do), not a topic.
        assert "decision-maker" in leader
        assert "low-pressure" in explorer
        assert "hands-on" in builder

    def test_personas_are_topic_agnostic(self):
        """The lenses must work whatever the reader filters (markets, learning,
        a hobby), not just AI — so the prompts describe posture, not subject."""
        from bot.personas import ANON_PERSONAS
        leader = ANON_PERSONAS["leader"].lower()
        # leader tailors to decisions "whatever the topic", not AI adoption.
        assert "whatever the topic" in leader
        assert "ai adoption" not in leader

    def test_builder_is_not_code_only(self):
        """Regression for the "Get hands-on" relabel: the builder lens must
        still land for non-coders (a trader's backtest, a learner's drill) and
        not assume the reader writes code."""
        from bot.personas import resolve_anon_profile
        builder = resolve_anon_profile("builder").lower()
        # Offers a non-technical hands-on path, not only code.
        assert "when it isn't" in builder
        assert "assume they can read code" not in builder

    def test_unknown_or_empty_is_none(self):
        from bot.personas import resolve_anon_profile
        assert resolve_anon_profile("") is None
        assert resolve_anon_profile(None) is None
        assert resolve_anon_profile("ceo") is None

    def test_key_is_case_insensitive(self):
        from bot.personas import resolve_anon_profile
        assert resolve_anon_profile("BUILDER") == resolve_anon_profile("builder")


class TestLoadProfile:
    def test_anon_persona_selects_that_lens(self):
        from bot.analyzer import _load_profile
        from bot.personas import ANON_PERSONAS
        assert _load_profile(None, "leader") == ANON_PERSONAS["leader"]

    def test_anon_without_persona_falls_back_to_default(self):
        from bot.analyzer import DEFAULT_PROFILE, _load_profile
        assert _load_profile(None, "") == DEFAULT_PROFILE
        assert _load_profile(None, "bogus") == DEFAULT_PROFILE

    def test_signed_in_lens_ignores_persona(self, db):
        from bot.analyzer import _load_profile
        uid = db.upsert_user_by_email("u@example.com")
        db.set_user_profile(uid, "My own saved lens.")
        # Even with a persona passed, the saved lens wins for signed-in users.
        assert _load_profile(uid, "leader") == "My own saved lens."


class TestAnalyzerIntegration:
    """The persona text actually reaches the prompt, and changes the cache key."""

    def _fake(self, monkeypatch):
        from bot import analyzer
        prompts = []

        def create(**kw):
            prompts.append(kw["messages"][0]["content"])
            return SimpleNamespace(
                content=[SimpleNamespace(type="tool_use", name="record_analysis",
                                         input={"main_idea": "x", "why_it_matters": "y",
                                                "grounded_in": "g", "category": "c",
                                                "time_required": "5m", "verdict": "watch",
                                                "suggestions": []})],
                usage=SimpleNamespace(input_tokens=1, output_tokens=1),
            )
        monkeypatch.setattr(analyzer, "_PROVIDER", "anthropic")
        monkeypatch.setattr(analyzer, "_MODEL", "claude-haiku-4-5-20251001")
        monkeypatch.setattr(analyzer, "_PREMIUM_MODEL", "claude-haiku-4-5-20251001")
        monkeypatch.setattr(analyzer, "_get_client",
                            lambda: SimpleNamespace(messages=SimpleNamespace(create=create)))
        return analyzer, prompts

    def test_persona_text_reaches_the_prompt(self, db, monkeypatch):
        analyzer, prompts = self._fake(monkeypatch)
        analyzer.analyze("content", ctx=analyzer.UsageContext(persona="leader"))
        assert "decision-maker" in prompts[0].lower()
        assert "skip step-by-step implementation" in prompts[0].lower()

    def test_cache_key_differs_by_persona(self, db):
        from bot import analyzer
        from bot.personas import ANON_PERSONAS
        k_leader = analyzer._cache_key_analyze("t", ANON_PERSONAS["leader"], "m")
        k_builder = analyzer._cache_key_analyze("t", ANON_PERSONAS["builder"], "m")
        assert k_leader != k_builder


class TestJobEndpoint:
    def test_start_job_accepts_persona(self, client, auth_headers):
        r = client.post("/api/job",
                        json={"url": "https://example.com/x", "persona": "leader"},
                        headers=auth_headers)
        assert r.status_code == 202

    def test_run_job_threads_persona_into_analysis(self, client, db, monkeypatch):
        import asyncio
        import bot.api
        from bot.personas import ANON_PERSONAS
        seen = {}

        def fake_analyze(text, user_id=None, *, ctx=None, **kw):
            from bot.analyzer import _load_profile
            seen["profile"] = _load_profile(ctx.user_id, ctx.persona)
            return {"main_idea": "x", "why_it_matters": "", "grounded_in": "",
                    "category": "", "time_required": "", "verdict": "watch",
                    "suggestions": []}

        monkeypatch.setattr(bot.pipeline, "analyze", fake_analyze)
        db.create_job("job-persona")
        asyncio.run(bot.api._run_job("job-persona", "https://example.com/x", None, "",
                                     anon_id="a1", persona="explorer"))
        assert seen["profile"] == ANON_PERSONAS["explorer"]
