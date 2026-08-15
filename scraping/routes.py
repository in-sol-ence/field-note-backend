"""HTTP routes for live / fixture social scrapes.

``POST /scrape/x`` and ``POST /scrape/social`` return ``assets.Signal`` JSON
and the frozen Post objects from ``scraping.mapper.signal_to_post``.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any, Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from harvest import load_fixtures
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

    Uses recorded fixtures under ``scraping/data/`` so the console demo works
    without a live social-signals browser session. Product text filters hits
    when possible; otherwise the first ``per_target_limit`` rows are returned.
    """

    platforms: list[Literal["hackernews", "reddit"]] = Field(
        default_factory=lambda: ["hackernews"]
    )
    product: str = Field(..., min_length=1)
    per_target_limit: int = Field(default=15, ge=1, le=100)


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


@router.post("/scrape/x", response_model=ScrapeResponse)
def scrape_x_endpoint(req: XScrapeRequest) -> ScrapeResponse:
    product = req.product
    if not product and req.website:
        product = website_to_product_hint(req.website)

    queries = list(req.search_queries)
    try:
        signals = scrape_x(
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
def scrape_social_endpoint(req: SocialScrapeRequest) -> ScrapeResponse:
    if not req.platforms:
        raise HTTPException(status_code=422, detail="platforms must be non-empty")

    signals: list[dict[str, Any]] = []
    for platform in req.platforms:
        batch = load_fixtures(platform)
        if not batch:
            continue
        batch = _filter_product(batch, req.product)[: req.per_target_limit]
        signals.extend(batch)

    if not signals:
        raise HTTPException(
            status_code=404,
            detail=(
                f"no fixture signals for {req.platforms} "
                f"(product={req.product!r}). Check scraping/data/."
            ),
        )

    posts, mapping_errors = signals_to_posts(signals)
    return ScrapeResponse(
        provider="fixture",
        count=len(signals),
        queries=[req.product],
        signals=signals,
        posts=posts,
        mapping_errors=mapping_errors,
    )
