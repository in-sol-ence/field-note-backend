"""Real main.py + real pipeline.py, with only the three paid APIs faked.

Everything a client touches is genuine: FastAPI routing, preflight, SSE framing
and the pydantic models. Only preprocess_stream is swapped for a canned Cursor
dossier, so this exercises the whole wiring without spending a credit.

Two uses: verifying the plumbing when you have no keys, and rehearsing the demo
without betting it on three external APIs staying up.

    uv run uvicorn demo_server:app --port 8090

It returns the same Cursor dossier whatever you ask for — never point a real
run at it.
"""

import asyncio
from datetime import datetime, timezone

import pipeline
from schema import (
    Disambiguation, Identity, NameCollision, PackageRef, ProductDossier,
    Provenance, ResultEvent, Source, StageEvent, Vocabulary, What,
)

NOW = datetime.now(timezone.utc)
WIKI = "https://en.wikipedia.org/wiki/Cursor_(databases)"
W3S = "https://www.w3schools.com/sql/sql_cursor.asp"


def _dossier() -> ProductDossier:
    return ProductDossier(
        identity=Identity(
            canonical_name="Cursor", slug="cursor",
            aliases=["Cursor IDE", "Cursor editor", "cursor.sh"],
            tagline="The AI code editor", homepage="https://cursor.com",
            repo="getcursor/cursor", docs_url="https://docs.cursor.com",
            package_names=[PackageRef(registry="npm", name="cursor")],
            cli_commands=["cursor"], org_or_owner="Anysphere",
            official_accounts={"x": "cursor_ai"},
        ),
        what=What(
            category="AI code editor", subcategory="developer tooling",
            description="A code editor built for pair-programming with AI, forked from VS Code.",
            primary_use_cases=["AI pair programming", "codebase-wide Q&A", "multi-file refactors"],
            target_users=["software engineers", "indie developers"],
            key_features=["Tab autocomplete", "Composer multi-file edits", "Cmd-K inline edit"],
            tech_stack=["Electron", "TypeScript", "VS Code fork"],
            pricing_model="freemium", license="proprietary", maturity="GA",
        ),
        vocabulary=Vocabulary(
            user_terms=["cursor", "cursor ai", "cursor ide"],
            common_misspellings=["curser", "cusor"],
            feature_jargon=["composer", "cmd-k", "tab model", "cursorrules", ".cursorrules"],
            adjacent_products=["GitHub Copilot", "Windsurf", "Zed", "Claude Code"],
        ),
        disambiguation=Disambiguation(
            ambiguity_score=0.75,
            name_collisions=[
                NameCollision(name="database cursor", what_it_is="control structure for traversing query result sets",
                              domain="en.wikipedia.org",
                              why_confusable="identical word, extremely common in developer forums where this product is also discussed",
                              evidence_url=WIKI),
                NameCollision(name="SQL CURSOR statement", what_it_is="SQL keyword taught in beginner tutorials",
                              domain="w3schools.com",
                              why_confusable="'cursor' queries on Stack Overflow overwhelmingly return SQL help",
                              evidence_url=W3S),
            ],
            positive_signals=["cursor.com", "composer", ".cursorrules", "cmd-k", "Anysphere", "tab model"],
            negative_signals=["DECLARE CURSOR", "FETCH NEXT", "pl/sql", "mouse pointer", "text caret"],
            notes="Crowded name. Require at least one positive signal before treating a post as on-topic.",
        ),
        provenance=Provenance(
            sources=[Source(url=u, fetched_at=NOW, via=v) for u, v in [
                ("https://cursor.com", "firecrawl"), ("https://cursor.com/pricing", "firecrawl"),
                ("https://docs.cursor.com", "firecrawl"), ("https://github.com/getcursor/cursor", "firecrawl"),
                (WIKI, "exa"), (W3S, "exa"),
            ]],
            field_confidence={"what.category": 0.95, "what.license": 0.35, "identity.package_names": 0.4},
            degraded_sources=["dropped unsourced collision 'Cursor Bank' (https://cursorbank.example/)"],
            generated_at=NOW, runtime_ms=38420,
        ),
    )


async def fake_stream(website, repo, form, name):
    for stage, detail in [
        ("map", "63 pages found"), ("scrape_site", "https://cursor.com"),
        ("scrape_repo", "getcursor/cursor"), ("search_collisions", "12 hits"),
        ("find_similar", "8 hits"), ("search_context", "8 hits"),
    ]:
        yield StageEvent(stage=stage, status="running")
        await asyncio.sleep(0.25)
        yield StageEvent(stage=stage, status="done", detail=detail)
    yield StageEvent(stage="synthesize", status="running", detail="grok-4.6")
    await asyncio.sleep(0.6)
    yield StageEvent(stage="synthesize", status="done")
    yield ResultEvent(dossier=_dossier())


pipeline.preprocess_stream = fake_stream

from main import app  # noqa: E402  - imported after the patch lands
