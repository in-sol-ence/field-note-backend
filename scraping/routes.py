"""HTTP routes for live / fixture social scrapes.

``POST /scrape/x`` and ``POST /scrape/social`` return ``assets.Signal`` JSON
and the frozen Post objects from ``scraping.mapper.signal_to_post``.
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict
from typing import Any, Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from harvest import (
    ScrapeUnavailable,
    load_fixtures,
    product_targets,
    scrape_social_platforms,
)
from scraping.mapper import signals_to_posts
from scraping.x_providers import scrape_x, website_to_product_hint

router = APIRouter(tags=["scrape"])


class XScrapeRequest(BaseModel):
    """Scrape X into assets.Signal objects.

    ``provider`` selects the backend:
    - ``x-scraper`` — local Playwright checkout (default)
    - ``social-signals`` — remote watch API
    - ``fixture`` — load Signal/tweet JSON (offline demo)
    """

    provider: Literal["x-scraper", "social-signals", "fixture"] = "x-scraper"
    search_queries: list[str] = Field(default_factory=list)
    product: str | None = Field(
        default=None,
        description="If set and search_queries empty, builds a complaint query.",
    )
    website: str | None = Field(
        default=None,
        description="Optional; used to derive product when product is omitted.",
    )
    count: int = Field(default=20, ge=1, le=200)
    result_type: Literal["Latest", "Top", "Media"] = "Latest"
    fixture_path: str | None = None
    accounts: list[str] = Field(default_factory=list)
    x_scraper_root: str | None = None
    social_signals_url: str | None = None


class SocialScrapeRequest(BaseModel):
    """Scrape Reddit / Hacker News into assets.Signal + Post objects.

    ``provider``:
    - ``auto`` — live social-signals, then recorded fixtures if live fails
    - ``social-signals`` — live only (502 when :8899 is down)
    - ``fixture`` — recorded Perplexity signals under ``scraping/data/``
    """

    platforms: list[Literal["hackernews", "reddit"]] = Field(
        default_factory=lambda: ["hackernews"]
    )
    product: str = Field(..., min_length=1)
    per_target_limit: int = Field(default=15, ge=1, le=100)
    provider: Literal["auto", "social-signals", "fixture"] = "auto"
    allow_fixtures: bool = True
    subreddits: list[str] = Field(default_factory=list)
    search_queries: list[str] = Field(default_factory=list)
    # Console defaults off for speed; harvest /preprocess still uses detail pass.
    fetch_bodies: bool = False


class ScrapeResponse(BaseModel):
    provider: str
    count: int
    queries: list[str]
    signals: list[dict[str, Any]]
    posts: list[dict[str, Any]]
    mapping_errors: list[str] = Field(default_factory=list)


def _filter_product(signals: list[dict[str, Any]], product: str) -> list[dict[str, Any]]:
    key = product.strip().casefold()
    if not key:
        return signals
    matched = [
        s
        for s in signals
        if key in (s.get("title") or "").casefold()
        or key in (s.get("body") or "").casefold()
        or key in str((s.get("raw") or {}).get("search_query") or "").casefold()
    ]
    return matched or signals


def _fixture_batch(
    platforms: list[str], product: str, per_target_limit: int
) -> list[dict[str, Any]]:
    signals: list[dict[str, Any]] = []
    for platform in platforms:
        batch = load_fixtures(platform)
        if not batch:
            continue
        batch = _filter_product(batch, product)[:per_target_limit]
        signals.extend(batch)
    return signals


@router.post("/scrape/x", response_model=ScrapeResponse)
async def scrape_x_endpoint(req: XScrapeRequest) -> ScrapeResponse:
    product = req.product
    if not product and req.website:
        product = website_to_product_hint(req.website)

    queries = list(req.search_queries)
    try:
        # Playwright search is blocking; keep the event loop alive so health
        # checks and the Next proxy don't see a dead connection mid-scrape.
        signals = await asyncio.to_thread(
            scrape_x,
            provider=req.provider,
            search_queries=queries or None,
            product=product,
            count=req.count,
            result_type=req.result_type,
            fixture_path=req.fixture_path,
            accounts=req.accounts,
            x_scraper_root=req.x_scraper_root,
            social_signals_url=req.social_signals_url,
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"{type(exc).__name__}: {exc}") from exc

    used_queries = queries
    if not used_queries and product:
        used_queries = [f"(auto complaint query for {product})"]

    signal_dicts = [asdict(s) for s in signals]
    posts, mapping_errors = signals_to_posts(signal_dicts)

    return ScrapeResponse(
        provider=req.provider,
        count=len(signals),
        queries=used_queries,
        signals=signal_dicts,
        posts=posts,
        mapping_errors=mapping_errors,
    )


@router.post("/scrape/social", response_model=ScrapeResponse)
async def scrape_social_endpoint(req: SocialScrapeRequest) -> ScrapeResponse:
    if not req.platforms:
        raise HTTPException(status_code=422, detail="platforms must be non-empty")

    targets = product_targets(
        req.product,
        subreddits=req.subreddits or None,
        search_queries=req.search_queries or None,
    )
    queries = [
        *targets.reddit.search_queries,
        *targets.hackernews.search_queries,
    ]

    signals: list[dict[str, Any]] = []
    provider = req.provider
    notes: list[str] = []

    if req.provider == "fixture":
        signals = _fixture_batch(req.platforms, req.product, req.per_target_limit)
        provider = "fixture"
    else:
        try:
            signals, notes = await scrape_social_platforms(
                list(req.platforms),
                targets,
                per_target_limit=req.per_target_limit,
                fetch_bodies=req.fetch_bodies,
            )
            provider = "social-signals"
        except ScrapeUnavailable as exc:
            if req.provider == "social-signals" or not req.allow_fixtures:
                raise HTTPException(
                    status_code=502,
                    detail=f"live social scrape failed: {exc}",
                ) from exc
            signals = _fixture_batch(req.platforms, req.product, req.per_target_limit)
            provider = "fixture"
            notes.append(f"fell back to fixtures after live failure: {exc}")

    if not signals:
        raise HTTPException(
            status_code=404,
            detail=(
                f"no signals for {req.platforms} (product={req.product!r}, "
                f"provider={req.provider}). Start social-signals-lite on :8899 "
                "or check scraping/data/ fixtures."
            ),
        )

    posts, mapping_errors = signals_to_posts(signals)
    if notes:
        mapping_errors = [*notes, *mapping_errors]

    return ScrapeResponse(
        provider=provider,
        count=len(signals),
        queries=queries or [req.product],
        signals=signals,
        posts=posts,
        mapping_errors=mapping_errors,
    )
