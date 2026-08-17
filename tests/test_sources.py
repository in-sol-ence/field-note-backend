"""T1 source selection: the pure parts, and which model it routes to."""

import asyncio
import os
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from schema import (
    Disambiguation,
    HackerNewsTargets,
    Identity,
    NameCollision,
    ProductDossier,
    Provenance,
    RedditTargets,
    ScrapeTargets,
    SubstackTargets,
    Vocabulary,
    What,
)
from sources import MAX_REDDIT_QUERIES, MAX_SUBREDDITS, MAX_SUBSTACK_TOPICS, build_prompt, clamp, fallback_targets


def _dossier(**kw) -> ProductDossier:
    base = dict(
        identity=Identity(canonical_name="Cursor", slug="cursor", aliases=["Cursor IDE"]),
        what=What(description="An AI code editor", key_features=["Tab completion"]),
        vocabulary=Vocabulary(feature_jargon=["composer mode"], user_terms=["cursor"]),
        disambiguation=Disambiguation(
            ambiguity_score=0.6,
            name_collisions=[
                NameCollision(
                    name="database cursor",
                    what_it_is="a SQL result-set iterator",
                    evidence_url="https://example.com/cursor",
                )
            ],
            negative_signals=["SQL", "iterator"],
        ),
        provenance=Provenance(generated_at=datetime.now(timezone.utc), runtime_ms=1),
    )
    return ProductDossier(**{**base, **kw})


def test_clamp_normalizes_every_subreddit_spelling() -> None:
    got = clamp(
        ScrapeTargets(
            reddit=RedditTargets(
                subreddits=[
                    "r/cursor",
                    "/r/Cursor",
                    "https://reddit.com/r/cursor/",
                    "programming",
                ]
            )
        )
    )
    # The first three are the same target written three ways.
    assert got.reddit.subreddits == ["cursor", "programming"]


def test_clamp_enforces_the_scrape_budget() -> None:
    """Every target costs minutes, so caps are applied to the answer, not asked
    for in the prompt — a model told 'at most four' still returns seven."""
    got = clamp(
        ScrapeTargets(
            reddit=RedditTargets(
                subreddits=[f"sub{i}" for i in range(10)],
                search_queries=[f"query {i}" for i in range(10)],
            ),
            hackernews=HackerNewsTargets(search_queries=[f"hn {i}" for i in range(10)]),
            substack=SubstackTargets(topics=[f"topic {i}" for i in range(10)]),
        )
    )
    assert len(got.reddit.subreddits) == MAX_SUBREDDITS
    assert len(got.reddit.search_queries) == MAX_REDDIT_QUERIES
    assert len(got.hackernews.search_queries) == 3
    assert len(got.substack.topics) == MAX_SUBSTACK_TOPICS


def test_clamp_dedupes_queries_case_insensitively() -> None:
    got = clamp(
        ScrapeTargets(
            reddit=RedditTargets(search_queries=["Cursor bug", "cursor  bug", "cursor slow"])
        )
    )
    assert got.reddit.search_queries == ["Cursor bug", "cursor slow"]


def test_fallback_needs_no_llm_and_stays_inside_budget() -> None:
    got = fallback_targets(_dossier())
    assert got.reddit.subreddits == ["cursor"]
    assert any("composer mode" in q for q in got.reddit.search_queries)
    assert got.substack.topics == ["Cursor"]
    assert got.topic == "Cursor"
    assert len(got.reddit.search_queries) <= MAX_REDDIT_QUERIES
    assert got.rationale


def test_fallback_survives_an_empty_dossier() -> None:
    bare = _dossier(
        identity=Identity(canonical_name="", slug=""),
        vocabulary=Vocabulary(),
    )
    got = fallback_targets(bare)
    assert got.reddit.subreddits == []
    assert got.reddit.search_queries  # bug/not-working queries still apply


def test_prompt_carries_the_collisions_forward() -> None:
    """The whole point of T0's collision work is that T1 avoids those queries."""
    prompt = build_prompt(_dossier())
    assert "database cursor" in prompt
    assert "composer mode" in prompt
    assert "RULES A HIT OUT" in prompt
    assert "0.60" in prompt


def test_prompt_omits_sections_it_has_no_data_for() -> None:
    prompt = build_prompt(
        _dossier(
            vocabulary=Vocabulary(),
            disambiguation=Disambiguation(ambiguity_score=0.0),
        )
    )
    assert "DISTINCTIVE JARGON" not in prompt
    assert "NAME COLLISIONS" not in prompt


# --------------------------------------------------------------------------
# Which model actually runs
# --------------------------------------------------------------------------


def test_selection_goes_through_grok_not_the_overridable_client(monkeypatch) -> None:
    """The hackathon requires Grok to be load-bearing, and this is the stage
    that decides where the whole scrape budget goes.

    preprocess._llm honours LLM_BASE_URL/LLM_MODEL, so routing through it means
    a gateway override silently moves source selection off Grok — which is
    exactly the bug this guards.
    """
    import models
    import preprocess
    import sources

    seen = {}

    async def fake_call_grok(prompt, output_type, **kw):
        seen["prompt"], seen["type"], seen["model"] = prompt, output_type, kw.get("model", "grok-4.6")
        return ScrapeTargets(reddit=RedditTargets(subreddits=["r/cursor"]))

    def forbidden():
        raise AssertionError("source selection must not use the overridable client")

    monkeypatch.setattr(models, "call_grok", fake_call_grok)
    monkeypatch.setattr(preprocess, "_llm", forbidden)
    monkeypatch.setenv("XAI_API_KEY", "test-key")

    got = asyncio.run(sources.select_targets(_dossier()))

    assert seen["type"] is ScrapeTargets
    assert seen["model"] == "grok-4.6"
    assert "database cursor" in seen["prompt"], "the collisions must reach the model"
    # and the answer is still budget-capped and normalized
    assert got.reddit.subreddits == ["cursor"]


def test_the_env_key_reaches_the_xai_sdk(monkeypatch) -> None:
    """models/grok.py reads os.environ directly, but pydantic-settings loads
    .env into a Settings object without touching the environment."""
    import preprocess
    import sources

    monkeypatch.delenv("XAI_API_KEY", raising=False)
    monkeypatch.setattr(
        preprocess, "get_settings", lambda: SimpleNamespace(xai_api_key="from-dot-env")
    )

    sources._export_xai_key()
    assert os.environ["XAI_API_KEY"] == "from-dot-env"


def test_an_already_exported_key_wins(monkeypatch) -> None:
    import preprocess
    import sources

    monkeypatch.setenv("XAI_API_KEY", "from-the-shell")
    monkeypatch.setattr(
        preprocess, "get_settings", lambda: SimpleNamespace(xai_api_key="from-dot-env")
    )

    sources._export_xai_key()
    assert os.environ["XAI_API_KEY"] == "from-the-shell"
