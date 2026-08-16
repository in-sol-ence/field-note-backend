"""T1 — scraping, with a fixture fallback.

Drives the social-signals service over `POST /v1/jobs/watch`, polls it to
completion, and normalizes what comes back through `scraping.mapper` into
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
from scraping.mapper import signals_to_posts

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
    if platform == "hackernews":
        return bool(targets.hackernews.search_queries)
    if platform == "x":
        return bool(targets.x.search_queries)
    return False


# --------------------------------------------------------------------------
# The scraper service
# --------------------------------------------------------------------------


class ScrapeUnavailable(RuntimeError):
    """The live scraper could not produce signals. Callers fall back."""


async def _run_x_scraper(
    targets: ScrapeTargets, per_target_limit: int
) -> list[dict[str, Any]]:
    """Live X via the local x-scraper checkout (subprocess Playwright)."""
    from dataclasses import asdict

    from scraping.x_providers import scrape_x

    queries = list(targets.x.search_queries)
    if not queries:
        raise ScrapeUnavailable("no X search queries in targets")

    try:
        signals = await asyncio.to_thread(
            scrape_x,
            provider="x-scraper",
            search_queries=queries,
            count=max(per_target_limit, 1),
        )
    except Exception as exc:  # noqa: BLE001
        raise ScrapeUnavailable(f"x-scraper failed: {exc}") from exc

    if not signals:
        raise ScrapeUnavailable("x-scraper returned no signals")
    return [asdict(s) for s in signals]


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
            waited = time.monotonic() - started
            if waited > POLL_TIMEOUT:
                raise ScrapeUnavailable(f"{platform} scrape exceeded {POLL_TIMEOUT:.0f}s")
            try:
                res = (await http.get(f"{base}{poll_url}", headers=headers)).json()
            except Exception as exc:  # noqa: BLE001
                raise ScrapeUnavailable(f"lost the scraper mid-job: {exc}") from exc

            if res.get("status") in ("running", "queued"):
                yield waited
                await asyncio.sleep(POLL_INTERVAL)
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
    if platform == "x":
        # x-scraper is a blocking subprocess — no poll loop, one heartbeat at start.
        yield HeartbeatEvent(elapsed_ms=int((time.monotonic() - started) * 1000))
        yield await _run_x_scraper(targets, per_target_limit)
        return

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


async def _platform_stream(
    platform: str,
    targets: ScrapeTargets,
    per_target_limit: int,
    allow_fixtures: bool,
    started: float,
) -> AsyncIterator[Any]:
    """One platform, start to finish: progress events, then one final
    (signals, degraded_notes, live) tuple."""
    stage = f"scrape_{platform}"
    yield StageEvent(stage=stage, status="running")  # type: ignore[arg-type]

    degraded: list[str] = []
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
        yield StageEvent(
            stage=stage,  # type: ignore[arg-type]
            status="done",
            detail=f"{len(got)} signals",
        )
        yield (got, degraded, True)
        return

    # Nothing live. Fall back, loudly.
    fixture = load_fixtures(platform) if allow_fixtures else []
    if not fixture:
        yield StageEvent(stage=stage, status="failed", detail="no signals")  # type: ignore[arg-type]
        yield ([], degraded, True)
        return
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
    yield (fixture, degraded, False)


async def harvest_stream(
    targets: ScrapeTargets,
    *,
    per_target_limit: int = 15,
    allow_fixtures: bool = True,
    scrape_x: bool = True,
    scrape_social: bool = True,
) -> AsyncIterator[Event]:
    """Scrape enabled platforms concurrently, then emit one HarvestEvent.

    ``scrape_x`` uses the local x-scraper (Playwright). ``scrape_social`` uses
    social-signals for Reddit/HN (fixtures when that service is down).
    """
    started = time.monotonic()
    signals: list[dict[str, Any]] = []
    degraded: list[str] = []
    any_live = False
    notes: list[str] = []

    platforms: list[str] = []
    if scrape_social:
        platforms.extend(p for p in ("reddit", "hackernews") if has_targets(p, targets))
    if scrape_x and has_targets("x", targets):
        platforms.append("x")

    if not platforms:
        yield HarvestEvent(
            harvest=Harvest(
                targets=targets,
                posts=[],
                live=False,
                source_note="no scrape platforms enabled or no targets selected",
                mapping_errors=[],
            )
        )
        return

    # The platform streams run as tasks feeding one queue, so their progress
    # events interleave on the wire in the order they actually happen.
    queue: asyncio.Queue[Event | None] = asyncio.Queue()

    async def pump(platform: str) -> tuple[list[dict[str, Any]], list[str], bool]:
        try:
            async for item in _platform_stream(
                platform, targets, per_target_limit, allow_fixtures and platform != "x", started
            ):
                if isinstance(item, tuple):
                    return item
                await queue.put(item)
            return [], [], True  # pragma: no cover - the stream always ends in a tuple
        finally:
            await queue.put(None)

    tasks = [asyncio.create_task(pump(p)) for p in platforms]
    remaining = len(tasks)
    while remaining:
        item = await queue.get()
        if item is None:
            remaining -= 1
        else:
            yield item

    for task in tasks:  # re-raise anything unexpected, in platform order
        got, platform_notes, was_live = await task
        signals += got
        degraded += platform_notes
        if was_live and got:
            any_live = True
            notes.append(f"live {len(got)}")
        elif got and not was_live:
            notes.append(f"recorded {len(got)}")

    yield StageEvent(stage="map_posts", status="running")
    posts, errors = to_posts(signals)
    yield StageEvent(stage="map_posts", status="done", detail=f"{len(posts)} posts")

    live = any_live and bool(posts)
    if live and all("recorded" not in n for n in notes):
        note = "live scrape"
    elif live:
        note = "mixed live + recorded signals — see mapping_errors for degraded sources"
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
