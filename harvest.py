"""T1 — scraping, with a fixture fallback.

Drives the social-signals service over `POST /v1/jobs/watch`, polls it to
completion, and normalizes what comes back through `scraping/mapper.py` into
the frozen `Post` contract.

Reddit takes 3-5 minutes for a single target and needs a browser session with
live cookies, so it is the least reliable thing in the pipeline and the most
likely to fail during a demo. When it does, the recorded signals in
`scraping/data/` are substituted rather than ending the run — but the
substitution is always announced, never silent: `Harvest.live` goes false and
the reason lands in `degraded_sources`.

    from harvest import harvest_stream   # -> AsyncIterator[Event], last is HarvestEvent
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from schema import (
    ErrorEvent,
    Event,
    Harvest,
    HarvestEvent,
    HeartbeatEvent,
    Post,
    ScrapeTargets,
    StageEvent,
)

# mapper.py lives beside the scraped data it was written against, not on the
# package path.
sys.path.insert(0, str(Path(__file__).parent / "scraping"))
from mapper import signals_to_posts  # noqa: E402

__all__ = ["FIXTURES", "harvest_stream", "load_fixtures", "to_posts"]

DATA = Path(__file__).parent / "scraping" / "data"

FIXTURES = {
    "reddit": DATA / "signals_reddit.json",
    "hackernews": DATA / "signals_hackernews.json",
}

# Reddit's own pace, not ours: ~3-5 min for 15 posts plus detail fetches.
POLL_INTERVAL = 3.0
POLL_TIMEOUT = 420.0
HEARTBEAT_EVERY = 10.0


# --------------------------------------------------------------------------
# Pure helpers
# --------------------------------------------------------------------------


def to_posts(signals: list[dict[str, Any]]) -> tuple[list[Post], list[str]]:
    """Map raw signals to Posts. One unmappable signal never kills a batch."""
    mapped, errors = signals_to_posts(signals)
    posts: list[Post] = []
    for raw in mapped:
        try:
            posts.append(Post.model_validate(raw))
        except Exception as exc:  # noqa: BLE001 - a bad post is not a bad run
            errors.append(f"{raw.get('url', '?')}: {exc}")
    return posts, errors


def load_fixtures(platform: str) -> list[dict[str, Any]]:
    """Recorded signals for a platform. Empty list when there are none."""
    path = FIXTURES.get(platform)
    if path is None or not path.exists():
        return []
    return json.loads(path.read_text())


def watch_payload(platform: str, targets: ScrapeTargets, per_target_limit: int) -> dict:
    """Build the /v1/jobs/watch body. Pure, so the mapping is testable."""
    if platform == "reddit":
        inner: dict[str, Any] = {
            "subreddits": targets.reddit.subreddits,
            "search_queries": targets.reddit.search_queries,
            "sort": "top",
            "time_filter": "month",
            "fetch_bodies": True,
            "fetch_bodies_limit": 10,
            "comment_limit": 25,
        }
    else:
        inner = {"search_queries": targets.hackernews.search_queries}

    return {
        "platform": platform,
        "per_target_limit": per_target_limit,
        "targets": {platform: inner},
        # hackernews ships disabled in the base vertical config.
        "enable_platforms": [platform],
        "include_signals": True,
    }


def has_targets(platform: str, targets: ScrapeTargets) -> bool:
    if platform == "reddit":
        return bool(targets.reddit.subreddits or targets.reddit.search_queries)
    return bool(targets.hackernews.search_queries)


# --------------------------------------------------------------------------
# The scraper service
# --------------------------------------------------------------------------


class ScrapeUnavailable(RuntimeError):
    """The live scraper could not produce signals. Callers fall back."""


async def _run_watch(
    platform: str, targets: ScrapeTargets, per_target_limit: int
) -> AsyncIterator[Any]:
    """Submit a watch job and poll it. Yields floats (seconds waited) as
    progress, then finally the list of signals."""
    import httpx

    from preprocess import get_settings

    s = get_settings()
    base = s.social_signals_url.rstrip("/")
    headers = {"Authorization": f"Bearer {s.social_signals_api_key}"}
    payload = watch_payload(platform, targets, per_target_limit)

    async with httpx.AsyncClient(timeout=30.0) as http:
        try:
            resp = await http.post(f"{base}/v1/jobs/watch", headers=headers, json=payload)
            resp.raise_for_status()
            job = resp.json()
        except Exception as exc:  # noqa: BLE001
            raise ScrapeUnavailable(f"cannot reach the scraper at {base}: {exc}") from exc

        poll_url = job.get("poll_url")
        if not poll_url:
            raise ScrapeUnavailable("scraper accepted the job but returned no poll_url")

        started = time.monotonic()
        while True:
            await asyncio.sleep(POLL_INTERVAL)
            waited = time.monotonic() - started
            if waited > POLL_TIMEOUT:
                raise ScrapeUnavailable(f"{platform} scrape exceeded {POLL_TIMEOUT:.0f}s")
            try:
                res = (await http.get(f"{base}{poll_url}", headers=headers)).json()
            except Exception as exc:  # noqa: BLE001
                raise ScrapeUnavailable(f"lost the scraper mid-job: {exc}") from exc

            if res.get("status") == "running":
                yield waited
                continue

            result = res.get("result") or {}
            signals = result.get("signals")
            if signals is None:
                detail = res.get("error") or res.get("status") or "no signals returned"
                raise ScrapeUnavailable(f"{platform} scrape failed: {detail}")
            yield list(signals)
            return


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------


async def _collect(
    platform: str, targets: ScrapeTargets, per_target_limit: int, started: float
) -> AsyncIterator[Any]:
    """One platform: live scrape, heartbeating while it waits."""
    last_beat = 0.0
    async for item in _run_watch(platform, targets, per_target_limit):
        if isinstance(item, list):
            yield item
            return
        if item - last_beat >= HEARTBEAT_EVERY:
            last_beat = item
            yield HeartbeatEvent(elapsed_ms=int((time.monotonic() - started) * 1000))
            yield StageEvent(
                stage=f"scrape_{platform}",  # type: ignore[arg-type]
                status="running",
                detail=f"{int(item)}s elapsed — Reddit is slow by design",
            )


async def harvest_stream(
    targets: ScrapeTargets,
    *,
    per_target_limit: int = 15,
    allow_fixtures: bool = True,
) -> AsyncIterator[Event]:
    """Scrape every platform with targets, then emit one HarvestEvent."""
    started = time.monotonic()
    signals: list[dict[str, Any]] = []
    degraded: list[str] = []
    live = True

    for platform in ("reddit", "hackernews"):
        if not has_targets(platform, targets):
            continue
        stage = f"scrape_{platform}"
        yield StageEvent(stage=stage, status="running")  # type: ignore[arg-type]

        got: list[dict[str, Any]] | None = None
        try:
            async for item in _collect(platform, targets, per_target_limit, started):
                if isinstance(item, list):
                    got = item
                else:
                    yield item
        except ScrapeUnavailable as exc:
            degraded.append(f"{stage}: {exc}")
            yield ErrorEvent(stage=stage, detail=str(exc)[:300], fatal=False)  # type: ignore[arg-type]

        if got:
            signals += got
            yield StageEvent(
                stage=stage,  # type: ignore[arg-type]
                status="done",
                detail=f"{len(got)} signals",
            )
            continue

        # Nothing live. Fall back, loudly.
        if not allow_fixtures:
            yield StageEvent(stage=stage, status="failed", detail="no signals")  # type: ignore[arg-type]
            continue
        fixture = load_fixtures(platform)
        if not fixture:
            yield StageEvent(stage=stage, status="failed", detail="no signals")  # type: ignore[arg-type]
            continue
        live = False
        signals += fixture
        note = (
            f"{stage}: substituted {len(fixture)} recorded signals from "
            f"{FIXTURES[platform].name} — these are about Perplexity, not this product"
        )
        degraded.append(note)
        yield ErrorEvent(stage=stage, detail=note, fatal=False)  # type: ignore[arg-type]
        yield StageEvent(
            stage=stage,  # type: ignore[arg-type]
            status="done",
            detail=f"{len(fixture)} recorded signals (fallback)",
        )

    yield StageEvent(stage="map_posts", status="running")
    posts, errors = to_posts(signals)
    yield StageEvent(stage="map_posts", status="done", detail=f"{len(posts)} posts")

    # Settle `live` before describing it: a run that scraped nothing is not a
    # live run, whatever happened on the way there.
    live = live and bool(posts)
    if live:
        note = "live scrape"
    elif posts:
        note = "recorded signals — the live scraper was unavailable"
    else:
        note = "no signals: the scraper was unavailable and no fixtures were used"

    yield HarvestEvent(
        harvest=Harvest(
            targets=targets,
            posts=posts,
            live=live,
            source_note=note,
            mapping_errors=errors + degraded,
        )
    )
