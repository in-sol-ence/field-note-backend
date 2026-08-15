"""Map raw X/Reddit scrape payloads into assets.Signal."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from assets import Engagement, Signal


def _iso_from_scraped_at(value: Any) -> str:
    if isinstance(value, str) and value.strip():
        return value
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=timezone.utc).isoformat()
    return datetime.now(tz=timezone.utc).isoformat()


def tweet_to_signal(tweet: dict[str, Any]) -> Signal:
    """Convert one x-scraper tweet dict into a pipeline Signal."""
    user = tweet.get("user") or {}
    metrics = tweet.get("metrics") or {}
    tweet_id = str(tweet.get("id") or "")
    username = (user.get("username") or "").lstrip("@")
    text = tweet.get("full_text") or tweet.get("text") or ""
    url = tweet.get("url") or (
        f"https://x.com/{username}/status/{tweet_id}" if username and tweet_id else (
            f"https://x.com/i/status/{tweet_id}" if tweet_id else ""
        )
    )
    likes = metrics.get("favorite_count")
    replies = metrics.get("reply_count")
    retweets = metrics.get("retweet_count")

    return Signal(
        platform="x",
        signal_id=tweet_id,
        url=url,
        title="",  # X posts have no title; body carries the text
        body=text,
        author=username or str(user.get("id") or ""),
        score=str(likes if likes is not None else ""),
        engagement=Engagement(
            likes=int(likes) if likes is not None else None,
            replies=int(replies) if replies is not None else None,
            retweets=int(retweets) if retweets is not None else None,
        ),
        scraped_at=_iso_from_scraped_at(tweet.get("scraped_at")),
        raw=tweet,
    )


def tweets_to_signals(tweets: list[dict[str, Any]]) -> list[Signal]:
    return [tweet_to_signal(t) for t in tweets if t.get("id")]
