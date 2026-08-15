"""The pure helpers: no network, no globals, no mocks needed."""

import pytest

from preprocess import (
    _draft_note,
    _section_reached,
    inputs_look_related,
    ambiguity_score,
    drop_unsourced_collisions,
    host_of,
    normalize_repo,
    normalize_website,
    partition_results,
    rank_sitemap_urls,
    registrable,
    score_url,
    slugify,
)
from schema import (
    DisambiguationDraft,
    Identity,
    NameCollision,
    SynthesisDraft,
    Vocabulary,
    What,
)

SITEMAP = [
    "https://acme.dev/blog/2019/hello-world",
    "https://acme.dev/privacy",
    "https://acme.dev/pricing",
    "https://acme.dev/",
    "https://acme.dev/docs/getting-started",
    "https://acme.dev/careers",
    "https://acme.dev/about",
]


def test_high_value_pages_outrank_blog_and_legal() -> None:
    ranked = rank_sitemap_urls(SITEMAP, limit=5)

    assert ranked[0] == "https://acme.dev/pricing"
    assert "https://acme.dev/privacy" not in ranked
    assert "https://acme.dev/careers" not in ranked
    assert "https://acme.dev/blog/2019/hello-world" not in ranked


def test_ranking_excludes_homepage_and_respects_limit() -> None:
    ranked = rank_sitemap_urls(SITEMAP, limit=2)

    assert len(ranked) == 2
    assert "https://acme.dev/" not in ranked


def test_ranking_is_deterministic_and_dedupes() -> None:
    duped = SITEMAP + ["https://acme.dev/pricing"]

    assert rank_sitemap_urls(duped) == rank_sitemap_urls(duped)
    assert rank_sitemap_urls(duped).count("https://acme.dev/pricing") == 1


def test_ranking_returns_nothing_rather_than_junk() -> None:
    assert rank_sitemap_urls(["https://acme.dev/privacy", "https://acme.dev/terms"]) == []


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("https://github.com/getcursor/cursor.git", "getcursor/cursor"),
        ("github.com/getcursor/cursor/", "getcursor/cursor"),
        ("https://github.com/getcursor/cursor/tree/main/src", "getcursor/cursor"),
        ("getcursor/cursor", "getcursor/cursor"),
        ("  ", None),
        (None, None),
        ("nope", None),
    ],
)
def test_normalize_repo(raw, expected) -> None:
    assert normalize_repo(raw) == expected


def test_normalize_website_adds_scheme_and_trims() -> None:
    assert normalize_website("acme.dev/") == "https://acme.dev"
    assert normalize_website("http://acme.dev") == "http://acme.dev"


def test_host_and_registrable_collapse_subdomains() -> None:
    assert host_of("https://www.acme.dev/x") == "acme.dev"
    assert registrable(host_of("https://docs.acme.dev/x")) == "acme.dev"


def test_registrable_does_not_merge_unrelated_multi_part_tlds() -> None:
    # Collapsing both to "co.uk" would read two unrelated companies as one
    # owner, and a real collision would be filtered out as product-owned.
    assert registrable("shop.co.uk") != registrable("other.co.uk")
    assert registrable("exa.com.sa") == "exa.com.sa"
    assert registrable("exa.net.uk") == "exa.net.uk"
    assert registrable("docs.acme.co.uk") == "acme.co.uk"
    assert registrable("localhost") == "localhost"


def test_collision_on_a_multi_part_tld_is_not_suppressed() -> None:
    urls = ["https://acme.co.uk/a", "https://rival.co.uk/b"]

    mine, other = partition_results(urls, ["acme.co.uk"])

    assert mine == ["https://acme.co.uk/a"]
    assert other == ["https://rival.co.uk/b"]


def test_slugify() -> None:
    assert slugify("  Field Note!! ") == "field-note"
    assert slugify("!!!") == "product"


def test_partition_splits_owned_from_foreign() -> None:
    urls = [
        "https://acme.dev/a",
        "https://docs.acme.dev/b",
        "https://en.wikipedia.org/wiki/Acme",
        "https://reddit.com/r/x",
    ]

    mine, other = partition_results(urls, ["acme.dev"])

    assert mine == ["https://acme.dev/a", "https://docs.acme.dev/b"]
    assert len(other) == 2


def _collision(url: str, domain: str | None = None) -> NameCollision:
    return NameCollision(name="x", what_it_is="y", evidence_url=url, domain=domain)


def test_ambiguity_score_counts_hits_on_identified_rivals() -> None:
    urls = ["https://acme.dev/a", "https://other.org/b", "https://third.io/c"]

    both = [_collision("https://other.org/b"), _collision("https://third.io/c")]
    assert ambiguity_score(urls, both) == 0.667
    assert ambiguity_score(urls, [_collision("https://other.org/b")]) == 0.333


def test_ambiguity_is_zero_when_nothing_shares_the_name() -> None:
    # A product too new to rank for its own name owns none of these results.
    # Scoring "not mine" would call that maximally contested; it is not.
    urls = ["https://unrelated.org/a", "https://noise.io/b"]

    assert ambiguity_score(urls, []) == 0.0
    assert ambiguity_score([], [_collision("https://x.org/")]) == 0.0


def _draft(*collisions: NameCollision) -> SynthesisDraft:
    return SynthesisDraft(
        identity=Identity(canonical_name="Acme", slug="acme"),
        what=What(),
        vocabulary=Vocabulary(),
        disambiguation=DisambiguationDraft(name_collisions=list(collisions)),
    )


def test_guard_drops_collisions_we_never_fetched() -> None:
    real = NameCollision(
        name="Acme Corp", what_it_is="cartoon company", evidence_url="https://seen.example/a"
    )
    fabricated = NameCollision(
        name="Acme Bank", what_it_is="invented", evidence_url="https://never-fetched.example/x"
    )

    kept, dropped = drop_unsourced_collisions(_draft(real, fabricated), ["https://seen.example/a"])

    assert [c.name for c in kept.disambiguation.name_collisions] == ["Acme Corp"]
    assert len(dropped) == 1
    assert "Acme Bank" in dropped[0]


def test_guard_ignores_trailing_slash_mismatch() -> None:
    c = NameCollision(name="X", what_it_is="y", evidence_url="https://seen.example/a/")

    kept, dropped = drop_unsourced_collisions(_draft(c), ["https://seen.example/a"])

    assert len(kept.disambiguation.name_collisions) == 1
    assert dropped == []


def test_related_inputs_are_accepted() -> None:
    assert inputs_look_related(
        "https://codexisland.com", "ericjypark/codex-island",
        "CodexIsland shows usage in your notch", "codex-island by ericjypark",
    )
    assert inputs_look_related(
        "https://cursor.com", "getcursor/cursor",
        "The AI code editor", "Homepage cursor.com — issues for Cursor",
    )


def test_a_repo_about_a_different_product_is_flagged() -> None:
    # The exact failure this guards: a German product-design studio paired with
    # an unrelated Swift app produces a confident, blended, useless dossier.
    assert not inputs_look_related(
        "https://your-product.com", "ericjypark/codex-island",
        "your product ist das Werkstattatelier für Produktentwicklung",
        "CodexIsland - AI usage limits in your MacBook notch",
    )


def test_no_repo_is_never_flagged() -> None:
    assert inputs_look_related("https://acme.dev", None, "anything", "")


# ---- synthesis sub-steps -------------------------------------------------


def _sections(chunks: list[str]) -> list[str]:
    """What the note would say, fed the draft one chunk at a time."""
    seen: set[str] = set()
    tail, out = "", []
    for piece in chunks:
        tail = (tail + piece)[-64:]
        if label := _section_reached(tail, seen):
            out.append(label)
    return out


def test_sections_are_announced_in_the_order_the_model_writes_them() -> None:
    draft = '{"identity": {"canonical_name": "Acme"}, "what": {}, "vocabulary": {}}'
    assert _sections(list(draft)) == [
        "naming the product",
        "describing what it does",
        "collecting its vocabulary",
    ]


def test_a_key_split_across_chunks_is_still_seen() -> None:
    # Token boundaries fall wherever the model puts them, so the key the note
    # keys off arrives in pieces more often than not.
    assert _sections(['{"ident', 'ity": {}, "wh', 'at": {}}']) == [
        "naming the product",
        "describing what it does",
    ]


def test_a_section_is_announced_once_however_often_it_recurs() -> None:
    # "what" appears again inside the prose the model writes, and a note that
    # walked backwards would read as the run losing its place.
    assert _sections(['{"what": {"category": "', 'a tool for "what" it is"}}']) == [
        "describing what it does"
    ]


def test_the_note_reports_size_only_once_there_is_a_draft_to_measure() -> None:
    assert _draft_note("grok-4.6", "naming the product", 0) == (
        "grok-4.6 · naming the product"
    )
    assert _draft_note("grok-4.6", "naming the product", 2431) == (
        "grok-4.6 · naming the product · 2.4k chars"
    )
