"""Map social-signals scrape output to the frozen Post contract.

Input shape: fixtures/signals.json (what `./run.sh watch|scrape` emits).
Output shape: fixtures/posts.json (what the T2 issue agent consumes).
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

SUPPORTED_PLATFORMS = frozenset({"reddit", "x", "hackernews", "github", "substack"})

_ID_PREFIX = {"reddit": "rd", "x": "x", "hackernews": "hn", "github": "gh", "substack": "ss"}

HN_ITEM = "https://news.ycombinator.com/item?id={}"

# Reddit permalinks look like /r/<sub>/comments/<id>/<slug>/
_REDDIT_POST_ID = re.compile(r"/comments/([a-z0-9]+)", re.IGNORECASE)

# X scrapes counts out of ARIA labels: "4050 Likes. Like", "1.2K reposts. Repost"
_LEADING_COUNT = re.compile(r"^\s*([\d.,]+)\s*([KMkm])?")


class SignalMappingError(ValueError):
    """Raised when a signal cannot be mapped to a Post."""


def _to_int(value: Any) -> int | None:
    """Coerce social-signals' stringly-typed counts to int.

    Three real shapes come back from the scrapers:
      Reddit  "412"                 plain numeric string
      X       "4050 Likes. Like"    ARIA label with the count in front
      HN      1486                  already an int
    Abbreviated forms ("1.2K") appear on both Reddit and X once a post is big.
    """
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return int(value)
    match = _LEADING_COUNT.match(str(value))
    if not match:
        return None
    number, suffix = match.groups()
    multiplier = {"k": 1_000, "m": 1_000_000}.get((suffix or "").lower(), 1)
    try:
        return int(float(number.replace(",", "")) * multiplier)
    except ValueError:
        return None


def _stable_id(platform: str, url: str) -> str:
    digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:8]
    return f"post_{_ID_PREFIX.get(platform, platform)}_{digest}"


def _source_id(platform: str, url: str, raw: dict[str, Any], signal_id: str | None) -> str | None:
    if platform == "reddit":
        match = _REDDIT_POST_ID.search(url)
        return f"t3_{match.group(1)}" if match else None
    if platform == "hackernews":
        return str(raw.get("objectID") or signal_id or "") or None
    if platform == "substack":
        return str(raw.get("id") or signal_id or "") or None
    # X: social-signals often omits raw.id; x-scraper puts the tweet id there.
    # signal_id is always the tweet id in both shapes.
    return str(raw.get("id") or signal_id or "") or None


def _score(platform: str, signal: dict[str, Any], engagement: dict[str, Any]) -> int | None:
    raw = signal.get("raw") or {}
    metrics = raw.get("metrics") if isinstance(raw.get("metrics"), dict) else {}
    value = _to_int(
        signal.get("score")
        or engagement.get("score")
        or engagement.get("points")
        or engagement.get("likes")
        or raw.get("likes")
        or metrics.get("favorite_count")
    )
    # A tweet with no like count has zero likes; a Reddit post with no score
    # genuinely failed to scrape, so leave that one unknown.
    if value is None and platform == "x":
        return 0
    return value


def _created_at(raw: dict[str, Any]) -> str | None:
    # social-signals X uses `time`; x-scraper / Twitter use `created_at`.
    # Prefer `time` when present so remapped Signals win over raw Twitter strings.
    return raw.get("time") or raw.get("created_at")


def _canonical_url(platform: str, signal: dict[str, Any], raw: dict[str, Any]) -> str:
    """The URL a reader should land on to see the discussion.

    HN signals carry the submitted article's URL, but the complaints live in
    the HN thread — cite the thread, not the article.
    """
    if platform == "hackernews":
        item = raw.get("objectID") or raw.get("story_id") or signal.get("signal_id")
        if item:
            return HN_ITEM.format(item)
    return signal.get("url") or ""


def _comments(value: Any) -> list[dict[str, Any]]:
    """Normalize a comment thread. Reddit listings put a count string under the
    same key the detail page uses for the list, so type-check before trusting.
    """
    if not isinstance(value, list):
        return []
    out = []
    for c in value:
        if not isinstance(c, dict) or not c.get("body"):
            continue
        permalink = c.get("permalink")
        out.append(
            {
                "author": c.get("author"),
                "body": c.get("body"),
                "score": _to_int(c.get("score")),
                # depth 0 is a direct reply to the post — the strongest
                # evidence. Deeper comments are often tangents.
                "depth": _to_int(c.get("depth")) or 0,
                "url": f"https://www.reddit.com{permalink}" if permalink else None,
                "is_op": bool(c.get("is_op")),
            }
        )
    return out


def _reddit_fields(raw: dict[str, Any], engagement: dict[str, Any]) -> dict[str, Any]:
    comments = _comments(raw.get("comments")) or _comments(engagement.get("comments"))
    return {
        "channel": raw.get("subreddit"),
        "title": raw.get("title"),
        "num_comments": _to_int(raw.get("comments_count")) or len(comments),
        "comments": comments,
    }


def _x_handle(raw: dict[str, Any], author: Any = None) -> str:
    """Resolve @handle from social-signals or x-scraper raw payloads."""
    user = raw.get("user") if isinstance(raw.get("user"), dict) else {}
    handle = raw.get("handle") or user.get("username") or author or ""
    return str(handle).lstrip("@")


def _x_fields(
    raw: dict[str, Any],
    engagement: dict[str, Any],
    author: Any = None,
) -> dict[str, Any]:
    # X has no real title — social-signals fills it with text[:120]. Drop it
    # rather than let the dashboard render a truncated body as a headline.
    handle = _x_handle(raw, author)
    metrics = raw.get("metrics") if isinstance(raw.get("metrics"), dict) else {}
    replies = (
        engagement.get("replies")
        or raw.get("replies")
        or metrics.get("reply_count")
    )
    return {
        "channel": f"@{handle}" if handle else None,
        "title": None,
        "comments": [],
        # X omits the count from the ARIA label when it is zero, so an
        # unparseable reply label means no replies — not unknown.
        "num_comments": _to_int(replies) or 0,
    }


def _hackernews_fields(raw: dict[str, Any], engagement: dict[str, Any]) -> dict[str, Any]:
    return {
        "channel": "news.ycombinator.com",
        "title": raw.get("title"),
        "comments": _comments(raw.get("comments")),
        "num_comments": _to_int(engagement.get("num_comments") or raw.get("num_comments")),
    }


def _substack_fields(raw: dict[str, Any], engagement: dict[str, Any]) -> dict[str, Any]:
    comments = _comments(raw.get("comments"))
    channel = raw.get("publication") or raw.get("publication_url")
    return {
        "channel": channel,
        "title": raw.get("title"),
        "comments": comments,
        "num_comments": _to_int(
            engagement.get("comments") or raw.get("comments_count") or len(comments)
        ),
    }


_PLATFORM_FIELDS = {
    "reddit": _reddit_fields,
    "hackernews": _hackernews_fields,
    "substack": _substack_fields,
}


def signal_to_post(signal: dict[str, Any]) -> dict[str, Any]:
    """Convert one social-signals Signal into a Post. Returns a new dict."""
    platform = signal.get("platform")
    if platform not in SUPPORTED_PLATFORMS:
        raise SignalMappingError(f"unsupported platform: {platform!r}")

    raw = signal.get("raw") or {}
    engagement = signal.get("engagement") or {}
    scraped_at = signal.get("scraped_at") or raw.get("scraped_at")

    url = _canonical_url(platform, signal, raw)
    if not url:
        raise SignalMappingError(f"signal {signal.get('signal_id')!r} has no url")

    author = signal.get("author")
    if platform == "reddit" and author:
        author = f"u/{author}"
    elif platform == "x":
        # Prefer raw.handle / user.username; fall back to signal.author (id ok).
        handle = _x_handle(raw, author)
        author = f"@{handle}" if handle else None

    if platform == "x":
        platform_fields = _x_fields(raw, engagement, signal.get("author"))
    else:
        platform_fields = _PLATFORM_FIELDS[platform](raw, engagement)

    return {
        "id": _stable_id(platform, url),
        "source": platform,
        "source_id": _source_id(platform, url, raw, signal.get("signal_id")),
        "url": url,
        "body": signal.get("body") or "",
        "author": author,
        # Post time key differs per scraper: Reddit and HN use created_at,
        # X uses time. The flag lets the T2 agent tell a genuine post date from
        # a scrape-time fallback if a scraper regresses mid-run.
        "created_at": _created_at(raw) or scraped_at,
        "created_at_is_scrape_time": not _created_at(raw),
        "score": _score(platform, signal, engagement),
        "scraped_at": scraped_at,
        "relevance": None,  # filled by the T2 agent
        "language": "en",
        # Which search surfaced this post, if any. Null for feed/listing hits.
        # Lets T2 tell a targeted hit from an ambient one.
        "search_query": raw.get("search_query"),
        **platform_fields,
    }


def signals_to_posts(signals: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[str]]:
    """Map a batch. Returns (posts, errors) — one bad signal never kills a run."""
    posts: list[dict[str, Any]] = []
    errors: list[str] = []
    for signal in signals:
        try:
            posts.append(signal_to_post(signal))
        except SignalMappingError as exc:
            errors.append(str(exc))
    return posts, errors
