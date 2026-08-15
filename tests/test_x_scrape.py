"""X scrape → assets.Signal + Post contract (no live browser)."""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from assets import Signal
from main import app
from scraping.mapper import signal_to_post, signals_to_posts
from scraping.x_providers import load_fixture_signals, scrape_x
from x_signals import to_signals, tweet_to_signal

ROOT = Path(__file__).resolve().parents[1]
OPENCLAW = ROOT / "data" / "signals_openclaw_x.json"
client = TestClient(app)


@pytest.mark.skipif(not OPENCLAW.is_file(), reason="openclaw fixture missing")
def test_load_fixture_returns_asset_signals() -> None:
    signals = load_fixture_signals(OPENCLAW)
    assert len(signals) >= 1
    assert all(isinstance(s, Signal) for s in signals)
    assert signals[0].platform == "x"
    assert signals[0].signal_id
    assert signals[0].body or signals[0].title
    again = Signal.from_dict(asdict(signals[0]))
    assert again.signal_id == signals[0].signal_id


@pytest.mark.skipif(not OPENCLAW.is_file(), reason="openclaw fixture missing")
def test_openclaw_signals_map_to_posts() -> None:
    """Repo expectation: Signal → Post via scraping.mapper with real post time."""
    signals = load_fixture_signals(OPENCLAW)[:10]
    posts, errors = signals_to_posts([asdict(s) for s in signals])
    assert errors == []
    assert len(posts) == 10
    for post in posts:
        assert post["source"] == "x"
        assert post["source_id"]
        assert post["url"].startswith("https://x.com/")
        assert post["created_at"]
        assert post["created_at_is_scrape_time"] is False
        assert post["title"] is None
        assert isinstance(post["score"], int)
        assert isinstance(post["num_comments"], int)


def test_tweet_to_signal_matches_social_signals_keys() -> None:
    sig = tweet_to_signal(
        {
            "id": "111",
            "full_text": "OpenClaw keeps crashing on save",
            "created_at": "Sat Aug 15 12:00:00 +0000 2026",
            "url": "https://x.com/u/status/111",
            "user": {"username": "u", "id": "9"},
            "metrics": {"favorite_count": 2, "reply_count": 1, "retweet_count": 0},
            "scraped_at": "2026-08-15T12:00:00+00:00",
        },
        search_query="openclaw crash",
    )
    assert sig.author == "u"
    assert sig.raw["handle"] == "u"
    assert sig.raw["time"] == "2026-08-15T12:00:00Z"
    assert sig.raw["search_query"] == "openclaw crash"
    post = signal_to_post(asdict(sig))
    assert post["author"] == "@u"
    assert post["channel"] == "@u"
    assert post["source_id"] == "111"
    assert post["created_at"] == "2026-08-15T12:00:00Z"
    assert post["created_at_is_scrape_time"] is False
    assert post["score"] == 2
    assert post["num_comments"] == 1
    assert post["search_query"] == "openclaw crash"
    assert post["title"] is None


@pytest.mark.skipif(not OPENCLAW.is_file(), reason="openclaw fixture missing")
def test_scrape_x_fixture_provider() -> None:
    signals = scrape_x(
        provider="fixture",
        fixture_path=str(OPENCLAW),
        count=3,
        search_queries=["unused"],
    )
    assert len(signals) == 3
    assert all(isinstance(s, Signal) for s in signals)


def test_scrape_x_tweets_fixture(tmp_path: Path) -> None:
    payload = {
        "tweets": [
            {
                "id": "111",
                "full_text": "OpenClaw keeps crashing on save",
                "url": "https://x.com/u/status/111",
                "user": {"username": "u"},
                "metrics": {"favorite_count": 2, "reply_count": 1, "retweet_count": 0},
                "scraped_at": "2026-08-15T12:00:00+00:00",
            }
        ]
    }
    path = tmp_path / "tweets.json"
    path.write_text(json.dumps(payload))
    signals = scrape_x(provider="fixture", fixture_path=str(path), count=5)
    assert len(signals) == 1
    assert isinstance(signals[0], Signal)
    assert signals[0].signal_id == "111"
    assert "crashing" in signals[0].body


def test_to_signals_rejects_garbage() -> None:
    with pytest.raises(ValueError, match="not an assets.Signal"):
        to_signals([{"foo": "bar"}])


@pytest.mark.skipif(not OPENCLAW.is_file(), reason="openclaw fixture missing")
def test_scrape_x_endpoint_returns_signals_and_posts() -> None:
    res = client.post(
        "/scrape/x",
        json={
            "provider": "fixture",
            "fixture_path": str(OPENCLAW),
            "count": 2,
        },
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["count"] == 2
    loaded = [Signal.from_dict(row) for row in body["signals"]]
    assert all(s.platform == "x" for s in loaded)
    assert len(body["posts"]) == 2
    assert body["mapping_errors"] == []
    assert body["posts"][0]["source"] == "x"
    assert body["posts"][0]["title"] is None


def test_scrape_x_endpoint_requires_query() -> None:
    res = client.post("/scrape/x", json={"provider": "x-scraper", "count": 1})
    assert res.status_code == 422
