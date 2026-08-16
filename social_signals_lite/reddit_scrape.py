"""Reddit Playwright scrape (standalone — no private social_signals package)."""

from __future__ import annotations

import json
import os
import random
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urljoin

BASE = "https://www.reddit.com"

_POST_ATTRS_JS = """() => [...document.querySelectorAll('shreddit-post')].map(p => ({
    title: p.getAttribute('post-title'),
    author: p.getAttribute('author'),
    subreddit: p.getAttribute('subreddit-prefixed-name'),
    score: p.getAttribute('score'),
    comments: p.getAttribute('comment-count'),
    created_at: p.getAttribute('created-timestamp')
        || p.querySelector('time[datetime]')?.getAttribute('datetime'),
    permalink: p.getAttribute('permalink'),
    flair: p.getAttribute('flair-text'),
    domain: p.getAttribute('domain'),
    content_href: p.getAttribute('content-href'),
}))"""


def _sleep(seconds: float) -> None:
    time.sleep(seconds + random.uniform(0.3, 1.0))


def _wait(page, ms: int = 700) -> None:
    page.wait_for_timeout(ms)


def _scroll(page, steps: int = 3) -> None:
    for _ in range(steps):
        page.mouse.wheel(0, random.randint(800, 1200))
        _wait(page, 700)


def _goto(page, url: str, *, attempts: int = 3, backoff: float = 8.0) -> None:
    last: Exception | None = None
    for attempt in range(attempts):
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=90_000)
            # Reddit often runs a JS challenge; give it time to settle.
            page.wait_for_timeout(int(float(os.environ.get("PAGE_PAUSE", "5")) * 1000))
            return
        except Exception as exc:  # noqa: BLE001
            last = exc
            if attempt < attempts - 1:
                _sleep(backoff * (attempt + 1))
    raise last  # type: ignore[misc]


def _full_url(path: str | None) -> str | None:
    if not path:
        return None
    if path.startswith("http"):
        return path
    return urljoin(BASE, path)


def cookie_paths() -> list[Path]:
    """Ordered discovery for Reddit cookie files."""
    candidates: list[Path] = []
    env = os.environ.get("REDDIT_COOKIES_FILE")
    if env:
        candidates.append(Path(env).expanduser())
    here = Path(__file__).resolve().parent / "data" / "reddit-cookies.json"
    candidates.append(here)
    candidates.append(Path("~/.social-signals-reddit-cookies.json").expanduser())
    return candidates


def inject_cookies(context) -> int:
    for path in cookie_paths():
        if not path.is_file():
            continue
        raw = path.read_text(encoding="utf-8").strip()
        if not raw:
            continue
        cookies = json.loads(raw)
        if not isinstance(cookies, list):
            continue
        cleaned = []
        for c in cookies:
            if not isinstance(c, dict) or not c.get("name") or not c.get("domain"):
                continue
            entry = {
                "name": c["name"],
                "value": c.get("value") or "",
                "domain": c["domain"],
                "path": c.get("path") or "/",
            }
            if "expires" in c and c["expires"] is not None:
                entry["expires"] = c["expires"]
            if "httpOnly" in c:
                entry["httpOnly"] = bool(c["httpOnly"])
            if "secure" in c:
                entry["secure"] = bool(c["secure"])
            if "sameSite" in c and c["sameSite"] in ("Strict", "Lax", "None"):
                entry["sameSite"] = c["sameSite"]
            cleaned.append(entry)
        if cleaned:
            context.add_cookies(cleaned)
            return len(cleaned)
    return 0


def _collect_posts(page, limit: int) -> list[dict[str, Any]]:
    seen: set[str] = set()
    posts: list[dict[str, Any]] = []
    stagnant = 0

    while len(posts) < limit and stagnant < 6:
        batch = page.evaluate(_POST_ATTRS_JS)
        added = 0
        for item in batch:
            link = _full_url(item.get("permalink"))
            if not link or link in seen or not item.get("title"):
                continue
            seen.add(link)
            item["url"] = link
            posts.append(item)
            added += 1
            if len(posts) >= limit:
                break
        if len(posts) >= limit:
            break
        stagnant = stagnant + 1 if added == 0 else 0
        _scroll(page, 1)
        _sleep(float(os.environ.get("SCROLL_PAUSE", "3")))

    for p in posts:
        p["scraped_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    return posts[:limit]


def scrape_subreddit(
    page,
    name: str,
    sort: str = "hot",
    limit: int = 25,
    time_filter: str | None = None,
) -> list[dict[str, Any]]:
    name = name.removeprefix("r/").removeprefix("/")
    url = f"{BASE}/r/{name}/{sort}/"
    if time_filter and sort in {"top", "controversial"}:
        url = f"{url}?{urlencode({'t': time_filter})}"
    _goto(page, url)
    return _collect_posts(page, limit)


def scrape_search(
    page,
    query: str,
    *,
    limit: int = 25,
    sort: str = "relevance",
    time_filter: str = "month",
    subreddit: str | None = None,
) -> list[dict[str, Any]]:
    params = urlencode({"q": query, "sort": sort, "t": time_filter})
    if subreddit:
        sub = subreddit.removeprefix("r/").removeprefix("/")
        url = f"{BASE}/r/{sub}/search/?{params}&restrict_sr=1"
    else:
        url = f"{BASE}/search/?{params}"
    _goto(page, url)

    seen: set[str] = set()
    posts: list[dict[str, Any]] = []
    stagnant = 0
    while len(posts) < limit and stagnant < 6:
        found = page.evaluate(
            """() => [...document.querySelectorAll('a[href*="/comments/"]')]
                .map(a => ({
                    permalink: a.getAttribute('href'),
                    title: (a.innerText || '').trim().split('\\n')[0],
                }))
                .filter(x => x.permalink)"""
        )
        added = 0
        for item in found:
            link = _full_url(item.get("permalink"))
            if not link or link in seen:
                continue
            seen.add(link)
            posts.append(
                {
                    "url": link,
                    "title": item.get("title") or "",
                    "search_query": query,
                    "scraped_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                }
            )
            added += 1
            if len(posts) >= limit:
                break
        if len(posts) >= limit:
            break
        stagnant = stagnant + 1 if added == 0 else 0
        _scroll(page, 1)
        _sleep(float(os.environ.get("SCROLL_PAUSE", "3")))
    return posts[:limit]


def scrape_post(page, url: str, comment_limit: int = 30) -> dict[str, Any]:
    if not url.startswith("http"):
        url = _full_url(url) or url
    _goto(page, url)
    data = page.evaluate(
        """(limit) => {
        const post = document.querySelector('shreddit-post');
        const bodyEl = document.querySelector('[slot="text-body"]')
            || document.querySelector('.md')
            || document.querySelector('[data-testid="post-content"]');
        const comments = [];
        for (const c of document.querySelectorAll('shreddit-comment')) {
            if (comments.length >= limit) break;
            const body = c.querySelector('[slot="comment"]')?.innerText?.trim()
                || c.querySelector('.md')?.innerText?.trim();
            if (!body) continue;
            comments.push({
                author: c.getAttribute('author'),
                score: c.getAttribute('score'),
                body: body.slice(0, 2000),
                depth: Number(c.getAttribute('depth') || 0),
                permalink: c.getAttribute('permalink'),
                is_op: c.getAttribute('author') === post?.getAttribute('author'),
            });
        }
        return {
            url: location.href,
            title: post?.getAttribute('post-title'),
            author: post?.getAttribute('author'),
            subreddit: post?.getAttribute('subreddit-prefixed-name'),
            score: post?.getAttribute('score'),
            comments_count: post?.getAttribute('comment-count'),
            created_at: post?.getAttribute('created-timestamp')
                || post?.querySelector('time[datetime]')?.getAttribute('datetime'),
            body: bodyEl?.innerText?.trim()?.slice(0, 8000) || null,
            comments,
        };
    }""",
        comment_limit,
    )
    data["scraped_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    return data
