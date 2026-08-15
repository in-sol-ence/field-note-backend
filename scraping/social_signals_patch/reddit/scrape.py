"""Reddit scrape + publish helpers (migrated from reddit-scraper/scraper.py)."""

from __future__ import annotations

import os
import random
import time
from typing import Any
from urllib.parse import urlencode, urljoin

from social_signals.core.browser_utils import wait

BASE = "https://www.reddit.com"

REDDIT_HTTP_ONLY = frozenset({"reddit_session", "token_v2", "session_tracker"})


def inject_cookies(context) -> int:
    from pathlib import Path

    from social_signals.core.cookie_import import (
        discover_cookies_file,
        inject_cookies_from_file,
        parse_netscape,
    )

    path = discover_cookies_file(
        "reddit", "REDDIT_COOKIES_FILE", "~/.social-signals-reddit-cookies.json"
    )
    if not path:
        return 0
    if path.suffix.lower() in {".txt", ".cookies"}:
        cookies = parse_netscape(path, http_only_names=REDDIT_HTTP_ONLY)
        existing = {(c.get("name"), c.get("domain")) for c in context.cookies()}
        to_add = [
            c
            for c in cookies
            if (c.get("name"), c.get("domain")) not in existing
            and c.get("name")
            and c.get("domain")
        ]
        if to_add:
            context.add_cookies(to_add)
        return len(to_add)
    return inject_cookies_from_file(context, Path(path))


def _sleep(seconds: float) -> None:
    time.sleep(seconds + random.uniform(0.3, 1.0))


def _scroll(page, steps: int = 3) -> None:
    for _ in range(steps):
        page.mouse.wheel(0, random.randint(800, 1200))
        wait(page, 700)


def _goto(page, url: str, *, attempts: int = 3, backoff: float = 8.0) -> None:
    """Navigate with backoff.

    Reddit answers a burst of listing requests with ERR_HTTP_RESPONSE_CODE_FAILURE
    (a 429 behind the scenes). One retry after a pause clears it; failing the
    whole target on the first refusal throws away the run.
    """
    last: Exception | None = None
    for attempt in range(attempts):
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=90_000)
            return
        except Exception as exc:  # noqa: BLE001 - retried below, re-raised if final
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


def _collect_posts(page, limit: int) -> list[dict[str, Any]]:
    """Scroll a listing and pull shreddit-post attributes until `limit`.

    Shared by subreddit listings and search results — both render the same
    shreddit-post custom element.
    """
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
    """Listing pages for a subreddit.

    `time_filter` only applies to sorts Reddit windows (top, controversial).
    "hot" surfaces what is busy right now; "top" over a month is what actually
    surfaces recurring complaints.
    """
    name = name.removeprefix("r/").removeprefix("/")
    url = f"{BASE}/r/{name}/{sort}/"
    if time_filter and sort in {"top", "controversial"}:
        url = f"{url}?{urlencode({'t': time_filter})}"
    _goto(page, url)
    _sleep(float(os.environ.get("PAGE_PAUSE", "5")))
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
    """Search Reddit for a query, optionally scoped to one subreddit.

    Subreddit listings only find products that already have a community.
    Search catches the complaints scattered across r/ChatGPT, r/SaaS, and
    anywhere else people happen to be talking.
    """
    params = urlencode({"q": query, "sort": sort, "t": time_filter})
    if subreddit:
        sub = subreddit.removeprefix("r/").removeprefix("/")
        url = f"{BASE}/r/{sub}/search/?{params}&restrict_sr=1"
    else:
        url = f"{BASE}/search/?{params}"
    _goto(page, url)
    _sleep(float(os.environ.get("PAGE_PAUSE", "5")))

    # Search results are NOT shreddit-post elements — they render inside
    # search-telemetry-tracker wrappers carrying only a permalink and a title.
    # Collect the links; the caller's detail pass fills in the rest.
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
    _sleep(float(os.environ.get("PAGE_PAUSE", "5")))

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
                // depth distinguishes a top-level complaint from a reply to
                // one; permalink makes a quoted comment citable on its own.
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


def _fill_title_field(page, title: str) -> None:
    page.evaluate(
        """(title) => {
        const ta = document.querySelector('faceplate-textarea-input[name="title"]')
            ?.shadowRoot?.querySelector('textarea');
        if (!ta) throw new Error('Title field not found');
        ta.focus();
        ta.value = title;
        ta.dispatchEvent(new Event('input', { bubbles: true }));
        ta.dispatchEvent(new Event('change', { bubbles: true }));
    }""",
        title,
    )


def _fill_richtext_field(page, aria_label: str, text: str) -> None:
    box = page.get_by_role("textbox", name=aria_label)
    box.wait_for(state="visible", timeout=30_000)
    box.click()
    wait(page, 400)
    page.evaluate(
        """(text) => {
        document.execCommand('insertText', false, text);
    }""",
        text,
    )


def _fill_visible_comment_body(page, text: str) -> None:
    idx = page.evaluate(
        """() => {
        const boxes = [...document.querySelectorAll('div[name="body"][role="textbox"]')];
        return boxes.findIndex((el) => {
            const r = el.getBoundingClientRect();
            return r.height > 0 && r.width > 0;
        });
    }"""
    )
    if idx < 0:
        raise RuntimeError("Comment editor not visible — open composer first.")
    box = page.locator('div[name="body"][role="textbox"]').nth(idx)
    box.click()
    wait(page, 400)
    page.evaluate(
        """(text) => {
        document.execCommand('insertText', false, text);
    }""",
        text,
    )


def _click_shadow_host_button(page, host_selector: str, label: str) -> None:
    clicked = page.evaluate(
        """([hostSelector, label]) => {
        const host = document.querySelector(hostSelector);
        if (!host?.shadowRoot) return false;
        for (const btn of host.shadowRoot.querySelectorAll('button')) {
            if (btn.innerText.trim() === label && !btn.disabled) {
                btn.click();
                return true;
            }
        }
        return false;
    }""",
        [host_selector, label],
    )
    if not clicked:
        raise RuntimeError(f"{label} button disabled or not found on {host_selector}.")


def _open_comment_composer(page) -> None:
    page.evaluate("window.scrollTo(0, Math.min(800, document.body.scrollHeight * 0.25))")
    _sleep(2)
    stubs = page.locator('faceplate-textarea-input[placeholder="Join the conversation"]')
    for i in range(stubs.count()):
        if stubs.nth(i).is_visible():
            stubs.nth(i).click()
            _sleep(2)
            return
    raise RuntimeError("Comment composer stub not found")


def perform_login(page) -> str:
    user = os.environ.get("REDDIT_USERNAME", "").strip()
    password = os.environ.get("REDDIT_PASSWORD", "").strip()
    if not user or not password:
        raise RuntimeError("Set REDDIT_USERNAME and REDDIT_PASSWORD.")

    page.goto(f"{BASE}/login/", wait_until="domcontentloaded", timeout=90_000)
    _sleep(8)

    filled_user = page.evaluate(
        """([username, password]) => {
        const fillNamed = (name, value) => {
            const walk = (root) => {
                const input = root.querySelector(`input[name="${name}"]`);
                if (input) {
                    input.focus();
                    input.value = value;
                    input.dispatchEvent(new Event('input', { bubbles: true }));
                    input.dispatchEvent(new Event('change', { bubbles: true }));
                    return true;
                }
                for (const el of root.querySelectorAll('*')) {
                    if (el.shadowRoot && walk(el.shadowRoot)) return true;
                }
                return false;
            };
            return walk(document);
        };
        return {
            username: fillNamed('username', username),
            password: fillNamed('password', password),
        };
    }""",
        [user, password],
    )
    if not filled_user.get("username") or not filled_user.get("password"):
        raise RuntimeError(f"Reddit login fields not found: {filled_user}")
    wait(page, 800)
    clicked = page.evaluate(
        """() => {
        const walk = (root) => {
            for (const btn of root.querySelectorAll('button')) {
                if (btn.innerText.trim() === 'Log In' && !btn.disabled) {
                    btn.click();
                    return true;
                }
            }
            for (const el of root.querySelectorAll('*')) {
                if (el.shadowRoot && walk(el.shadowRoot)) return true;
            }
            return false;
        };
        return walk(document);
    }"""
    )
    if not clicked:
        page.get_by_role("button", name="Log In", exact=True).click()

    page.wait_for_load_state("domcontentloaded", timeout=60_000)
    _sleep(5)

    if "/login" in page.url.lower() and page.locator('input[name="username"]').count():
        raise RuntimeError("Still on login page — check credentials or complete CAPTCHA.")
    if page.locator('[data-testid="user-drawer-button"], #expand-user-drawer-button').count() == 0:
        body = page.locator("body").inner_text().lower()
        if "incorrect" in body or "wrong password" in body:
            raise RuntimeError("Login failed — incorrect username or password.")
    return "ok"


def post_comment(page, post_url: str, text: str) -> None:
    """Post a comment and verify it actually persisted server-side.

    A click on the "Comment" button firing is NOT proof of success (confirmed
    2026-07-07: a real post reported "posted comment" — the button click
    genuinely happened — but the comment never existed on a fresh reload of
    either the thread or the account's own comment history). Reddit's spam
    filter can silently drop a comment from a new/low-karma account on a
    large default subreddit while the client-side UI updates optimistically.
    The only trustworthy check is a fresh reload + search for our own text.
    """
    page.goto(post_url, wait_until="domcontentloaded", timeout=90_000)
    _sleep(float(os.environ.get("PAGE_PAUSE", "6")))

    _open_comment_composer(page)
    _fill_visible_comment_body(page, text)
    wait(page, 1200)

    clicked = page.evaluate(
        """() => {
        const walk = (root) => {
            for (const btn of root.querySelectorAll('button')) {
                if (btn.innerText.trim() === 'Comment' && !btn.disabled) {
                    btn.click();
                    return true;
                }
            }
            for (const el of root.querySelectorAll('*')) {
                if (el.shadowRoot && walk(el.shadowRoot)) return true;
            }
            return false;
        };
        return walk(document);
    }"""
    )
    if not clicked:
        raise RuntimeError("Comment button disabled — karma, verification, or rate limit.")
    _sleep(5)

    # Ground-truth check: reload the thread fresh and look for our own text.
    # Matches on a distinctive prefix rather than the full body, since Reddit
    # can trim trailing whitespace/punctuation on render.
    needle = text.strip()[:60]
    page.goto(post_url, wait_until="domcontentloaded", timeout=90_000)
    _sleep(6)
    found = page.evaluate(
        """(needle) => {
        for (const c of document.querySelectorAll('shreddit-comment')) {
            const body = (c.querySelector('[slot="comment"]')?.innerText || '');
            if (body.includes(needle)) return true;
        }
        return false;
    }""",
        needle,
    )
    if not found:
        raise RuntimeError(
            "Comment button click registered but the comment is not present on "
            "reload — likely silently removed by Reddit's spam/quality filter "
            "(common for new/low-karma accounts on large default subreddits)."
        )


def _reddit_username(page) -> str | None:
    cached = os.environ.get("REDDIT_DISPLAY_USER", "").strip()
    if cached:
        return cached.removeprefix("u/")

    user = page.evaluate(
        """() => {
        const btn = document.querySelector('#expand-user-drawer-button, [data-testid="user-drawer-button"]');
        const label = btn?.getAttribute('aria-label') || '';
        const m = label.match(/u\\/([A-Za-z0-9_-]+)/);
        return m?.[1] || null;
    }"""
    )
    if user:
        return user

    drawer = page.locator('#expand-user-drawer-button, [data-testid="user-drawer-button"]').first
    if drawer.count():
        drawer.click()
        wait(page, 800)
        user = page.evaluate(
            """() => {
            const link = [...document.querySelectorAll('a[href^="/user/"]')].find((a) =>
                /^\\/user\\/[A-Za-z0-9_-]+\\/?$/.test(a.getAttribute('href') || '')
            );
            return link?.getAttribute('href')?.split('/')[2] || null;
        }"""
        )
        if user:
            return user
    return None


def submit_post(page, subreddit: str, title: str, body: str) -> str | None:
    sub = subreddit.removeprefix("r/")
    page.goto(f"{BASE}/r/{sub}/submit/?type=TEXT", wait_until="domcontentloaded", timeout=90_000)
    _sleep(5)

    page.locator('faceplate-textarea-input[name="title"]').wait_for(state="attached", timeout=30_000)
    _fill_title_field(page, title)
    wait(page, 800)

    _fill_richtext_field(page, "Post body text field", body)
    wait(page, 1200)

    _click_shadow_host_button(page, "#submit-post-button", "Post")
    _sleep(8)

    user = _reddit_username(page)
    if user:
        page.goto(f"{BASE}/user/{user}/submitted/", wait_until="domcontentloaded", timeout=90_000)
        _sleep(4)
        link = page.evaluate(
            """(title) => {
            for (const post of document.querySelectorAll('shreddit-post')) {
                if (post.getAttribute('post-title') === title) {
                    return post.getAttribute('permalink');
                }
            }
            return null;
        }""",
            title,
        )
        if link:
            return _full_url(link)
    return None
