"""T1 source selection. Pure parts only — no network."""

from datetime import datetime, timezone

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
    Vocabulary,
    What,
)
from sources import MAX_REDDIT_QUERIES, MAX_SUBREDDITS, build_prompt, clamp, fallback_targets


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
        )
    )
    assert len(got.reddit.subreddits) == MAX_SUBREDDITS
    assert len(got.reddit.search_queries) == MAX_REDDIT_QUERIES
    assert len(got.hackernews.search_queries) == 3


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
