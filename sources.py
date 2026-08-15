"""T1 — source selection.

Turns a T0 dossier into the concrete subreddits and search queries T2 will
scrape. This is where T0's disambiguation work finally pays off: the whole
point of knowing that "Cursor" collides with database cursors is to not spend
a scrape budget on them.

The stage is deliberately small and mostly pure — one LLM call, everything
around it deterministic — so a bad expansion is cheap to reproduce in a test:

    from sources import select_targets   # -> ScrapeTargets (async, one call)
    from sources import fallback_targets # -> ScrapeTargets (pure, no network)
"""

from __future__ import annotations

import json
import re

from pydantic import ValidationError

from schema import HackerNewsTargets, ProductDossier, RedditTargets, ScrapeTargets

__all__ = ["clamp", "fallback_targets", "select_targets"]

# Reddit takes 3-5 minutes for ~15 posts against one target, and the run has a
# demo-length budget. Caps are applied after the model answers rather than
# asked for in the prompt, because a model told "at most 4" still returns 7.
MAX_SUBREDDITS = 4
MAX_REDDIT_QUERIES = 4
MAX_HN_QUERIES = 3

_SUB_CLEAN = re.compile(r"^(?:https?://)?(?:www\.)?(?:reddit\.com)?/?r/", re.IGNORECASE)


def _normalize_subreddit(name: str) -> str:
    """`r/foo`, `/r/foo`, a full URL and a bare `foo` all mean the same target."""
    return _SUB_CLEAN.sub("", name.strip()).strip("/").split("/")[0]


def _dedupe(values: list[str], limit: int) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for v in values:
        v = " ".join(v.split())
        key = v.casefold()
        if not v or key in seen:
            continue
        seen.add(key)
        out.append(v)
        if len(out) == limit:
            break
    return out


def clamp(targets: ScrapeTargets) -> ScrapeTargets:
    """Normalize and budget-cap whatever the model returned."""
    subs = _dedupe(
        [_normalize_subreddit(s) for s in targets.reddit.subreddits], MAX_SUBREDDITS
    )
    return ScrapeTargets(
        reddit=RedditTargets(
            subreddits=subs,
            search_queries=_dedupe(targets.reddit.search_queries, MAX_REDDIT_QUERIES),
        ),
        hackernews=HackerNewsTargets(
            search_queries=_dedupe(targets.hackernews.search_queries, MAX_HN_QUERIES),
        ),
        rationale=targets.rationale,
    )


def fallback_targets(dossier: ProductDossier) -> ScrapeTargets:
    """Targets without an LLM, for when synthesis fails.

    Deliberately conservative: the product's own subreddit if the name suggests
    one, and queries built from jargon that T0 already established has no
    namesakes. A weak scrape beats a wrong one.
    """
    ident = dossier.identity
    name = ident.canonical_name or ident.slug
    jargon = dossier.vocabulary.feature_jargon[:2]

    queries = [f"{name} {term}" for term in jargon]
    # Complaint-shaped queries find complaints; the bare name finds press.
    queries += [f"{name} bug", f"{name} not working"]

    return clamp(
        ScrapeTargets(
            reddit=RedditTargets(
                subreddits=[ident.slug.replace("-", "")] if ident.slug else [],
                search_queries=queries,
            ),
            hackernews=HackerNewsTargets(search_queries=[name]),
            rationale="LLM source selection unavailable — derived from the "
            "dossier's own name and jargon.",
        )
    )


_SYSTEM = """You choose where to look for real user complaints about a software \
product. Your output drives a scraper with a strict budget, so every target you \
name costs minutes. Choose few, choose well.

Rules:

- Subreddits must be ones you are confident exist and where this product's users \
actually post. A product subreddit, and the two or three general subreddits its \
users live in. Never invent a subreddit.
- Search queries are the more valuable half. Most complaints are posted in \
subreddits nobody would think to list, and only site-wide search finds them. \
Write queries the way an annoyed user writes a title, not the way a marketer \
writes a headline.
- Use the product's distinctive jargon. If a term is listed as having no \
namesakes, it is a better search key than the product name itself.
- Respect the collisions. If the name is ambiguous, never emit a bare-name query \
that would return the namesake instead of the product.
- Bias every query toward dissatisfaction: bugs, regressions, billing, \
cancellation, "switched from", "anyone else". Praise is not what we are here for.
- Return only JSON matching the schema."""


def build_prompt(dossier: ProductDossier) -> str:
    """The prompt, separated from the call so a test can assert on it."""
    d, v, ident = dossier.disambiguation, dossier.vocabulary, dossier.identity
    parts = [
        f"PRODUCT: {ident.canonical_name}",
        f"WHAT IT IS: {dossier.what.description or dossier.what.category or 'unknown'}",
    ]
    if ident.aliases:
        parts.append("ALIASES: " + ", ".join(ident.aliases))
    if v.user_terms:
        parts.append("WHAT USERS CALL IT: " + ", ".join(v.user_terms))
    if v.feature_jargon:
        parts.append(
            "DISTINCTIVE JARGON (no namesakes — strong search keys): "
            + ", ".join(v.feature_jargon)
        )
    if dossier.what.target_users:
        parts.append("USERS: " + ", ".join(dossier.what.target_users))
    if dossier.what.key_features:
        parts.append("FEATURES (complaints cluster here): " + ", ".join(dossier.what.key_features))

    parts.append(f"AMBIGUITY SCORE: {d.ambiguity_score:.2f} (1.0 = the product owns none of its name)")
    if d.name_collisions:
        parts.append(
            "NAME COLLISIONS — a query that returns these is a wasted target:\n"
            + "\n".join(f"  - {c.name}: {c.what_it_is}" for c in d.name_collisions)
        )
    if d.positive_signals:
        parts.append("CONFIRMS A REAL HIT: " + ", ".join(d.positive_signals))
    if d.negative_signals:
        parts.append("RULES A HIT OUT: " + ", ".join(d.negative_signals))
    if v.adjacent_products:
        parts.append(
            "COMPETITORS (users compare and defect — good query material): "
            + ", ".join(v.adjacent_products)
        )

    parts.append(
        "\n--- OUTPUT SCHEMA ---\n"
        + json.dumps(ScrapeTargets.model_json_schema(), indent=1)
    )
    return "\n\n".join(parts)


async def select_targets(dossier: ProductDossier) -> ScrapeTargets:
    """One Grok call. Raises on failure — the caller decides whether to degrade."""
    from preprocess import _llm, get_settings

    client, s = _llm(), get_settings()
    resp = await client.chat.completions.create(
        model=s.llm_model,
        messages=[
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content": build_prompt(dossier)},
        ],
        response_format={"type": "json_object"},
    )
    raw = resp.choices[0].message.content or "{}"
    try:
        return clamp(ScrapeTargets.model_validate_json(raw))
    except ValidationError as exc:
        raise ValueError(f"source selection returned invalid JSON: {exc}") from exc
