"""Map raw X scrape payloads into assets.Signal — the pipeline contract.

Shape matches what ``scraping/mapper.signal_to_post`` expects for platform
``x`` (same keys as ``scraping/data/signals_testfixture.json``): ``raw.handle``,
``raw.time``, engagement reply/like counts, and a handle-based ``author``.

Every X ingest path (live x-scraper, fixture, social-signals) must exit through
``to_signals`` / ``tweets_to_signals`` so downstream code only ever sees
``assets.Signal``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

from assets import Engagement, Signal


def _iso_from_scraped_at(value: Any) -> str:
    if isinstance(value, str) and value.strip():
        return value
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=timezone.utc).isoformat()
    return datetime.now(tz=timezone.utc).isoformat()


def _tweet_time_iso(tweet: dict[str, Any]) -> str | None:
    """Prefer social-signals ``time``; else parse Twitter ``created_at``."""
    for key in ("time",):
        value = tweet.get(key)
        if isinstance(value, str) and value.strip():
            return value
    created = tweet.get("created_at")
    if isinstance(created, str) and created.strip():
        try:
            dt = parsedate_to_datetime(created)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        except (TypeError, ValueError, IndexError):
            return created
    return None


def tweet_to_signal(
    tweet: dict[str, Any],
    *,
    search_query: str | None = None,
) -> Signal:
    """Convert one x-scraper tweet dict into a pipeline Signal.

    ``raw`` keeps the full scraper payload and overlays the flat social-signals
    keys (``handle``, ``time``, ``likes``, …) that ``signal_to_post`` reads.
    """
    user = tweet.get("user") or {}
    metrics = tweet.get("metrics") or {}
    tweet_id = str(tweet.get("id") or "")
    username = (user.get("username") or tweet.get("handle") or "").lstrip("@")
    text = tweet.get("full_text") or tweet.get("text") or ""
    url = tweet.get("url") or (
        f"https://x.com/{username}/status/{tweet_id}" if username and tweet_id else (
            f"https://x.com/i/status/{tweet_id}" if tweet_id else ""
        )
    )
    likes = metrics.get("favorite_count", tweet.get("likes"))
    replies = metrics.get("reply_count", tweet.get("replies"))
    retweets = metrics.get("retweet_count", tweet.get("retweets"))

    def _count(value: Any) -> int | None:
        if value is None or value == "":
            return None
        if isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            return int(value)
        # ARIA labels from social-signals: "196 Replies. Reply"
        text_v = str(value).strip().split()[0].replace(",", "")
        try:
            return int(float(text_v))
        except ValueError:
            return None

    likes_i = _count(likes)
    replies_i = _count(replies)
    retweets_i = _count(retweets)
    time_iso = _tweet_time_iso(tweet)

    raw = dict(tweet)
    if username:
        raw["handle"] = username
    raw["text"] = text
    if time_iso:
        raw["time"] = time_iso
    if likes is not None:
        raw["likes"] = likes
    if replies is not None:
        raw["replies"] = replies
    if retweets is not None:
        raw["retweets"] = retweets
    if url:
        raw["url"] = url
    query = search_query or tweet.get("search_query")
    if query:
        raw["search_query"] = query

    return Signal(
        platform="x",
        signal_id=tweet_id,
        url=url,
        # social-signals fills title with text[:120]; mapper drops it for Posts.
        title=text[:120] if text else "",
        body=text,
        author=username or str(user.get("id") or ""),
        score=str(likes_i if likes_i is not None else likes if likes is not None else "0"),
        engagement=Engagement(
            likes=likes_i,
            replies=replies_i,
            retweets=retweets_i,
        ),
        scraped_at=_iso_from_scraped_at(tweet.get("scraped_at")),
        raw=raw,
    )


def tweets_to_signals(
    tweets: list[dict[str, Any]],
    *,
    search_query: str | None = None,
) -> list[Signal]:
    return [
        tweet_to_signal(t, search_query=search_query)
        for t in tweets
        if t.get("id")
    ]


def _looks_like_signal(row: dict[str, Any]) -> bool:
    return "platform" in row and "signal_id" in row and "engagement" in row


def _looks_like_tweet(row: dict[str, Any]) -> bool:
    return bool(row.get("id")) and (
        "full_text" in row or "text" in row or "user" in row or "metrics" in row
    )


def _normalize_signal_dict(row: dict[str, Any]) -> dict[str, Any]:
    """Ensure Signal-shaped rows carry mapper-required raw overlays."""
    payload = dict(row)
    raw = dict(payload.get("raw") or {})
    user = raw.get("user") if isinstance(raw.get("user"), dict) else {}
    handle = (
        raw.get("handle")
        or (user.get("username") if isinstance(user, dict) else "")
        or ""
    )
    handle = str(handle).lstrip("@")
    if handle:
        raw["handle"] = handle
        payload["author"] = handle
    elif "handle" in raw and not raw["handle"]:
        del raw["handle"]
    if not raw.get("time"):
        time_iso = _tweet_time_iso(raw)
        if time_iso:
            raw["time"] = time_iso
    if "text" not in raw and payload.get("body"):
        raw["text"] = payload["body"]
    metrics = raw.get("metrics") if isinstance(raw.get("metrics"), dict) else {}
    eng = payload.get("engagement") or {}
    if "likes" not in raw and (metrics.get("favorite_count") is not None or eng.get("likes") is not None):
        raw["likes"] = metrics.get("favorite_count", eng.get("likes"))
    if "replies" not in raw and (metrics.get("reply_count") is not None or eng.get("replies") is not None):
        raw["replies"] = metrics.get("reply_count", eng.get("replies"))
    if "retweets" not in raw and (metrics.get("retweet_count") is not None or eng.get("retweets") is not None):
        raw["retweets"] = metrics.get("retweet_count", eng.get("retweets"))
    payload["raw"] = raw
    payload["platform"] = "x"
    return payload


def to_signals(rows: list[dict[str, Any]]) -> list[Signal]:
    """Normalize tweet dicts or Signal dicts into assets.Signal objects.

    Raises ValueError if a row cannot be interpreted as either shape.
    """
    out: list[Signal] = []
    for i, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ValueError(f"row {i}: expected dict, got {type(row).__name__}")
        if _looks_like_signal(row):
            if row.get("platform") not in (None, "x"):
                raise ValueError(
                    f"row {i}: expected platform 'x', got {row.get('platform')!r}"
                )
            # Prefer remapping from embedded tweet when present so overlays match.
            raw = row.get("raw") or {}
            if _looks_like_tweet(raw) or raw.get("id"):
                tweet = dict(raw)
                tweet.setdefault("id", row.get("signal_id") or raw.get("id"))
                tweet.setdefault("scraped_at", row.get("scraped_at"))
                if row.get("url"):
                    tweet.setdefault("url", row["url"])
                out.append(
                    tweet_to_signal(tweet, search_query=raw.get("search_query"))
                )
            else:
                out.append(Signal.from_dict(_normalize_signal_dict(row)))
        elif _looks_like_tweet(row):
            out.append(tweet_to_signal(row))
        else:
            raise ValueError(
                f"row {i}: not an assets.Signal or x-scraper tweet "
                f"(keys={sorted(row.keys())[:12]})"
            )
    return out
