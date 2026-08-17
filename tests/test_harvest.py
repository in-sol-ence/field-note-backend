"""T1 scraping: payload shape, mapping, and the fixture fallback.

The fallback is the part that matters most — it is what stands between a dead
Reddit session and a dead demo — so it is tested from both directions.
"""

import asyncio
import json
import time

import harvest
from harvest import (
    FIXTURES,
    ScrapeUnavailable,
    harvest_stream,
    has_targets,
    load_fixtures,
    to_posts,
    watch_payload,
)
from schema import (
    ErrorEvent,
    HackerNewsTargets,
    HarvestEvent,
    HeartbeatEvent,
    RedditTargets,
    ScrapeTargets,
    StageEvent,
)

TARGETS = ScrapeTargets(
    reddit=RedditTargets(subreddits=["perplexity_ai"], search_queries=["perplexity billing"]),
    hackernews=HackerNewsTargets(search_queries=["perplexity"]),
)


def _drain(targets=TARGETS, **kw):
    async def go():
        return [e async for e in harvest_stream(targets, **kw)]

    return asyncio.run(go())


def _harvest(events):
    return next(e.harvest for e in events if isinstance(e, HarvestEvent))


# --- payload ---------------------------------------------------------------


def test_watch_payload_carries_t1_targets_to_the_scraper() -> None:
    body = watch_payload("reddit", TARGETS, per_target_limit=15)
    assert body["targets"]["reddit"]["subreddits"] == ["perplexity_ai"]
    assert body["targets"]["reddit"]["search_queries"] == ["perplexity billing"]
    assert body["per_target_limit"] == 15
    assert body["include_signals"] is True


def test_watch_payload_enables_hackernews_explicitly() -> None:
    """HackerNews ships disabled in the base vertical config, so a request that
    does not turn it on silently scrapes nothing."""
    assert watch_payload("hackernews", TARGETS, 15)["enable_platforms"] == ["hackernews"]


def test_has_targets_skips_a_platform_with_nothing_to_scrape() -> None:
    empty = ScrapeTargets()
    assert not has_targets("reddit", empty)
    assert not has_targets("hackernews", empty)
    assert not has_targets("x", empty)
    assert not has_targets("substack", empty)
    assert has_targets("reddit", TARGETS)


# --- mapping ---------------------------------------------------------------


def test_recorded_signals_map_to_posts() -> None:
    posts, errors = to_posts(load_fixtures("reddit"))
    assert posts and not errors
    assert all(p.url.startswith("http") for p in posts)
    assert all(p.source == "reddit" for p in posts)
    assert any(p.comments for p in posts)


def test_hackernews_fixtures_map_even_though_assets_cannot_load_them() -> None:
    """assets.Signal rejects platform 'hackernews'; mapper.py handles it. T1
    routes through mapper for exactly this reason."""
    posts, errors = to_posts(load_fixtures("hackernews"))
    assert posts and not errors
    assert all(p.url.startswith("https://news.ycombinator.com/item?id=") for p in posts)


def test_one_bad_signal_does_not_discard_the_batch() -> None:
    signals = load_fixtures("reddit")[:2] + [{"platform": "myspace"}]
    posts, errors = to_posts(signals)
    assert len(posts) == 2
    assert len(errors) == 1


# --- live path -------------------------------------------------------------


def _fake_watch(signals_by_platform, *, waits=()):
    async def run(platform, targets, per_target_limit, **_kwargs):
        for w in waits:
            yield w
        yield signals_by_platform.get(platform, [])

    return run


REDDIT_ONLY = ScrapeTargets(reddit=RedditTargets(subreddits=["perplexity_ai"]))


def test_a_live_scrape_is_reported_as_live(monkeypatch) -> None:
    monkeypatch.setattr(
        harvest, "_run_watch", _fake_watch({"reddit": load_fixtures("reddit")[:3]})
    )
    got = _harvest(_drain(REDDIT_ONLY))
    assert got.live is True
    assert len(got.posts) == 3
    assert got.source_note == "live scrape"


def test_mixed_live_and_fixture_is_reported_as_mixed(monkeypatch) -> None:
    """Reddit live + HN fixture fallback stays ``live`` but the source note
    must say mixed so operators do not treat Perplexity fixtures as product
    feedback."""
    monkeypatch.setattr(
        harvest, "_run_watch", _fake_watch({"reddit": load_fixtures("reddit")[:3]})
    )
    got = _harvest(_drain())  # TARGETS covers both platforms
    assert got.live is True
    assert "mixed" in got.source_note
    assert len(got.posts) > 3  # reddit's live posts plus HN's recorded ones


def test_heartbeats_are_emitted_while_reddit_takes_its_time(monkeypatch) -> None:
    """Reddit runs 3-5 minutes. Without heartbeats the client watches a frozen
    spinner and a proxy is free to drop the connection."""
    monkeypatch.setattr(
        harvest,
        "_run_watch",
        _fake_watch({"reddit": load_fixtures("reddit")[:1]}, waits=(1.0, 15.0, 40.0)),
    )
    events = _drain(REDDIT_ONLY)
    assert sum(isinstance(e, HeartbeatEvent) for e in events) == 2  # 1.0s is too soon


# --- fallback --------------------------------------------------------------


def _dead_scraper(platform, targets, per_target_limit, **_kwargs):
    async def run():
        raise ScrapeUnavailable("connection refused")
        yield  # pragma: no cover - makes this an async generator

    return run()


def test_an_unreachable_scraper_falls_back_to_recorded_signals(monkeypatch) -> None:
    monkeypatch.setattr(harvest, "_run_watch", _dead_scraper)
    events = _drain()
    got = _harvest(events)

    assert got.live is False
    assert got.posts, "the run still produces posts"
    assert "recorded signals" in got.source_note


def test_the_fallback_is_announced_and_never_silent(monkeypatch) -> None:
    """Substituting Perplexity data for another product would misrepresent the
    result, so the substitution has to be visible in the stream and on the
    harvest, not just in a log line."""
    monkeypatch.setattr(harvest, "_run_watch", _dead_scraper)
    events = _drain()
    warnings = [e for e in events if isinstance(e, ErrorEvent)]

    assert warnings, "a fallback emits a non-fatal error event"
    assert all(not w.fatal for w in warnings)
    assert any("Perplexity" in w.detail for w in warnings)
    assert any("recorded" in note for note in _harvest(events).mapping_errors)


def test_fallback_can_be_refused(monkeypatch) -> None:
    """A caller that would rather have nothing than the wrong product's data."""
    monkeypatch.setattr(harvest, "_run_watch", _dead_scraper)
    got = _harvest(_drain(allow_fixtures=False))
    assert got.posts == []
    assert got.live is False
    assert "no signals" in got.source_note


def test_an_empty_live_result_also_falls_back(monkeypatch) -> None:
    """A scraper that answers with zero signals is as useless as one that is
    down, and Reddit returns empty when the session cookie has expired."""
    monkeypatch.setattr(harvest, "_run_watch", _fake_watch({}))
    got = _harvest(_drain())
    assert got.live is False
    assert got.posts


def test_non_perplexity_targets_do_not_get_perplexity_fixtures(monkeypatch) -> None:
    """Cursor (etc.) must not inherit recorded Perplexity complaints on failure."""
    monkeypatch.setattr(harvest, "_run_watch", _dead_scraper)
    cursor = ScrapeTargets(
        reddit=RedditTargets(subreddits=["cursor"], search_queries=["cursor composer broken"]),
        hackernews=HackerNewsTargets(search_queries=["Cursor Anysphere"]),
    )
    got = _harvest(_drain(cursor))
    assert got.posts == []
    assert any("skipped Perplexity fixtures" in n for n in got.mapping_errors)


def test_platforms_without_targets_are_never_scraped(monkeypatch) -> None:
    calls = []

    async def run(platform, targets, per_target_limit, **_kwargs):
        calls.append(platform)
        yield []

    monkeypatch.setattr(harvest, "_run_watch", run)
    reddit_only = ScrapeTargets(reddit=RedditTargets(subreddits=["cursor"]))
    _drain(reddit_only)
    assert calls == ["reddit"]


def test_the_targets_used_are_reported_back(monkeypatch) -> None:
    monkeypatch.setattr(harvest, "_run_watch", _fake_watch({"reddit": []}))
    assert _harvest(_drain()).targets == TARGETS


# --- ordering -----------------------------------------------------------


def test_platforms_run_http_before_browsers(monkeypatch) -> None:
    """HN (HTTP) before X/Reddit (Chromium) — dual Playwright was the demo failure."""
    order: list[str] = []

    async def run(platform, targets, per_target_limit, **_kwargs):
        order.append(platform)
        yield load_fixtures(platform)[:1] if platform in FIXTURES else []

    monkeypatch.setattr(harvest, "_run_watch", run)
    # Force an X query so the ordering includes all three.
    from schema import XTargets

    targets = TARGETS.model_copy(
        update={"x": XTargets(search_queries=["cursor bug"])}
    )

    async def fake_x(targets, per_target_limit):
        order.append("x")
        return load_fixtures("hackernews")[:1]

    monkeypatch.setattr(harvest, "_run_x_scraper", fake_x)
    _drain(targets)
    assert order == ["hackernews", "x", "reddit"]


def test_a_finished_job_is_not_taxed_with_a_poll_interval(monkeypatch) -> None:
    """The first poll happens immediately; the sleep only follows a poll that
    saw the job still running."""

    class _Resp:
        def __init__(self, data):
            self._data = data
            self.text = json.dumps(data)

        def raise_for_status(self):
            pass

        def json(self):
            return self._data

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, **kw):
            return _Resp({"poll_url": "/v1/jobs/1"})

        async def get(self, url, **kw):
            return _Resp({"status": "done", "result": {"signals": []}})

    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _Client())

    async def go():
        return [item async for item in harvest._run_watch("hackernews", TARGETS, 5)]

    t0 = time.monotonic()
    items = asyncio.run(go())
    assert items == [[]]
    assert time.monotonic() - t0 < harvest.POLL_INTERVAL
