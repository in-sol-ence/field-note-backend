"""Run watch scrapes and map to Signal-shaped dicts."""

from __future__ import annotations

import os
import sys
import traceback
from datetime import datetime, timezone
from typing import Any

from social_signals_lite import hackernews as hn
from social_signals_lite import reddit_scrape as reddit
from social_signals_lite import substack as substack_scrape


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _signal(
    *,
    platform: str,
    signal_id: str,
    url: str,
    title: str,
    body: str,
    author: str,
    score: str = "",
    engagement: dict[str, Any] | None = None,
    scraped_at: str | None = None,
    raw: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "platform": platform,
        "signal_id": str(signal_id),
        "url": url or "",
        "title": (title or "")[:200],
        "body": body or "",
        "author": author or platform,
        "score": str(score or ""),
        "engagement": engagement or {},
        "scraped_at": scraped_at or _now(),
        "raw": raw or {},
    }


def _as_int(value: Any) -> int:
    try:
        return int(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return 0


_LISTING_WINS = frozenset(
    {"url", "permalink", "created_at", "score", "flair", "domain", "search_query"}
)


def watch_hackernews(targets: dict[str, Any], per_target_limit: int) -> list[dict[str, Any]]:
    queries = targets.get("search_queries") or targets.get("queries") or []
    tags = targets.get("tags") or (["front_page"] if not queries else [])
    items = hn.scrape_feed(
        tags=tags,
        queries=queries,
        limit=per_target_limit,
        hiring_keywords=targets.get("hiring_keywords") or queries,
        fetch_comment_threads=bool(targets.get("fetch_comments", True)),
        top_n_comments=int(targets.get("fetch_comments_limit", 6)),
        comment_limit=int(targets.get("comment_limit", 15)),
    )
    out: list[dict[str, Any]] = []
    for item in items:
        url = item.get("url") or ""
        sid = item.get("id") or url or "hn"
        out.append(
            _signal(
                platform="hackernews",
                signal_id=str(sid),
                url=url,
                title=item.get("title") or "",
                body=item.get("body") or "",
                author=item.get("author") or "hn",
                score=str(item.get("score") or ""),
                engagement=item.get("engagement") or {},
                scraped_at=item.get("scraped_at"),
                raw=item.get("raw") or item,
            )
        )
    return out


def _map_reddit_posts(posts: list[dict[str, Any]], source: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for p in posts:
        url = p.get("url") or p.get("post_url") or ""
        sid = p.get("id") or url
        out.append(
            _signal(
                platform="reddit",
                signal_id=str(sid),
                url=url,
                title=p.get("title") or "",
                body=p.get("selftext") or p.get("body") or "",
                author=p.get("author") or source,
                score=str(p.get("score") or ""),
                engagement={"comments": p.get("comments"), "score": p.get("score")},
                scraped_at=p.get("scraped_at"),
                raw=p,
            )
        )
    return out


def _enrich_posts(
    page,
    posts: list[dict[str, Any]],
    bodies_limit: int,
    comment_limit: int,
) -> list[dict[str, Any]]:
    ranked = sorted(posts, key=lambda p: _as_int(p.get("score")), reverse=True)
    targets = {p.get("url") for p in ranked[:bodies_limit] if p.get("url")}
    out: list[dict[str, Any]] = []
    for post in posts:
        url = post.get("url")
        if url not in targets:
            out.append(post)
            continue
        try:
            detail = reddit.scrape_post(page, url, comment_limit=comment_limit)
        except Exception as exc:  # noqa: BLE001
            out.append({**post, "detail_error": str(exc)})
            continue
        keep = {k: v for k, v in post.items() if v and k in _LISTING_WINS}
        out.append({**detail, **keep})
    return out


def watch_reddit(targets: dict[str, Any], per_target_limit: int) -> list[dict[str, Any]]:
    from playwright.sync_api import sync_playwright

    subs = targets.get("subreddits") or []
    queries = targets.get("search_queries") or []
    sort = targets.get("sort", "hot")
    time_filter = targets.get("time_filter")
    fetch_bodies = bool(targets.get("fetch_bodies", True))
    bodies_limit = int(targets.get("fetch_bodies_limit", 10))
    comment_limit = int(targets.get("comment_limit", 15))
    search_sort = targets.get("search_sort", "relevance")
    search_time = targets.get("search_time", "month")

    # Keep demo runs bounded unless PAGE_PAUSE is set explicitly.
    os.environ.setdefault("PAGE_PAUSE", "4")
    os.environ.setdefault("SCROLL_PAUSE", "2")

    signals: list[dict[str, Any]] = []
    seen_urls: set[str] = set()

    def fresh(posts: list[dict[str, Any]]) -> list[dict[str, Any]]:
        out = []
        for post in posts:
            url = post.get("url")
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)
            out.append(post)
        return out

    headed = os.environ.get("REDDIT_HEADED", "").lower() in {"1", "true", "yes"}
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not headed)
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 900},
        )
        n_cookies = reddit.inject_cookies(context)
        print(f"[reddit] injected {n_cookies} cookies", file=sys.stderr)
        page = context.new_page()

        for sub in subs:
            label = f"r/{sub}"
            try:
                posts = fresh(
                    reddit.scrape_subreddit(
                        page, sub, sort=sort, limit=per_target_limit, time_filter=time_filter
                    )
                )
                if fetch_bodies:
                    posts = _enrich_posts(page, posts, bodies_limit, comment_limit)
                signals.extend(_map_reddit_posts(posts, label))
            except Exception as exc:  # noqa: BLE001
                print(
                    f"[reddit] target {label} failed: {exc.__class__.__name__}: {exc}",
                    file=sys.stderr,
                )
                traceback.print_exc(file=sys.stderr)

        for query in queries:
            label = f"search:{query}"
            try:
                posts = fresh(
                    reddit.scrape_search(
                        page,
                        query,
                        limit=per_target_limit,
                        sort=search_sort,
                        time_filter=search_time,
                    )
                )
                # Search hits are title+permalink only. Detail pass is expensive;
                # skip it when the caller asked for listings (fetch_bodies=false).
                if fetch_bodies and posts:
                    posts = _enrich_posts(page, posts, min(len(posts), bodies_limit), comment_limit)
                signals.extend(_map_reddit_posts(posts, label))
            except Exception as exc:  # noqa: BLE001
                print(
                    f"[reddit] target {label} failed: {exc.__class__.__name__}: {exc}",
                    file=sys.stderr,
                )
                traceback.print_exc(file=sys.stderr)

        browser.close()

    if not signals and (subs or queries):
        raise RuntimeError(
            "reddit scrape returned no signals — try uploading cookies via "
            "POST /v1/sessions/reddit/cookies or set REDDIT_COOKIES_FILE"
        )
    return signals


def watch_substack(targets: dict[str, Any], per_target_limit: int) -> list[dict[str, Any]]:
    topics = [t for t in (targets.get("topics") or targets.get("search_queries") or []) if str(t).strip()]
    publications = [p for p in (targets.get("publications") or []) if str(p).strip()]
    fetch_comments = bool(targets.get("fetch_comments", True))
    comment_limit = int(targets.get("comment_limit", 25))
    max_pubs = int(targets.get("max_publications", 6))

    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    # Topic is the primary key. Publications without a topic still harvest
    # their latest archive (empty topic string).
    queries = topics or ([""] if publications else [])
    for topic in queries:
        batch = substack_scrape.scrape_topic(
            topic,
            publications=publications,
            per_target_limit=per_target_limit,
            max_publications=max_pubs,
            fetch_comments=fetch_comments,
            comment_limit=comment_limit,
        )
        for item in batch:
            key = str(item.get("id") or item.get("url") or "")
            if not key or key in seen:
                continue
            seen.add(key)
            items.append(item)

    out: list[dict[str, Any]] = []
    for item in items:
        url = item.get("url") or ""
        sid = item.get("id") or url or "substack"
        out.append(
            _signal(
                platform="substack",
                signal_id=str(sid),
                url=url,
                title=item.get("title") or "",
                body=item.get("body") or "",
                author=item.get("author") or "substack",
                score=str(item.get("score") or ""),
                engagement=item.get("engagement") or {},
                scraped_at=item.get("scraped_at"),
                raw=item.get("raw") or item,
            )
        )
    return out


def run_watch(
    platform: str,
    targets: dict[str, Any],
    *,
    per_target_limit: int = 15,
) -> list[dict[str, Any]]:
    platform = platform.lower().strip()
    plat_targets = targets.get(platform) or targets
    if platform == "hackernews":
        return watch_hackernews(plat_targets, per_target_limit)
    if platform == "reddit":
        return watch_reddit(plat_targets, per_target_limit)
    if platform == "substack":
        return watch_substack(plat_targets, per_target_limit)
    raise ValueError(f"unsupported platform: {platform}")
