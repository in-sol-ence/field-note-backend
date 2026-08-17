"""Topic-first Substack harvest via unofficial public JSON endpoints.

Not an official API. Official surfaces (RSS, Publisher API, LinkedIn profile
lookup) cannot search articles+comments on a topic. These are the same JSON
routes the Substack web app uses; they can change without notice.
"""

from __future__ import annotations

import html
import re
import time
from typing import Any
from urllib.parse import quote, urlparse

import httpx

UA = "Mozilla/5.0 (compatible; Fieldnotes/1.0)"
SEARCH_URL = "https://substack.com/api/v1/publication/search"
_HTML_TAG_RE = re.compile(r"<[^>]+>")


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _strip_html(text: str) -> str:
    plain = _HTML_TAG_RE.sub(" ", text or "")
    return html.unescape(re.sub(r"\s+", " ", plain)).strip()


def _client(client: httpx.Client | None) -> httpx.Client:
    if client is not None:
        return client
    return httpx.Client(
        timeout=20.0,
        headers={"User-Agent": UA, "Accept": "application/json"},
        follow_redirects=True,
    )


def normalize_pub_base(value: str) -> str:
    """`platformer`, `https://platformer.substack.com/` → origin with no slash."""
    raw = (value or "").strip()
    if not raw:
        return ""
    if "://" not in raw:
        host = raw.removeprefix("@").split("/")[0]
        if "." not in host:
            host = f"{host}.substack.com"
        raw = f"https://{host}"
    parsed = urlparse(raw)
    if not parsed.scheme or not parsed.netloc:
        return ""
    return f"{parsed.scheme}://{parsed.netloc}"


def search_publications(
    topic: str,
    *,
    limit: int = 6,
    client: httpx.Client | None = None,
) -> list[dict[str, Any]]:
    """Publications matching a topic. Returns `{name, base_url, subdomain}`."""
    q = (topic or "").strip()
    if not q:
        return []
    own = client is not None
    http = _client(client)
    try:
        resp = http.get(f"{SEARCH_URL}?query={quote(q)}")
        resp.raise_for_status()
        payload = resp.json()
    finally:
        if not own:
            http.close()
    rows = payload.get("results") if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        return []
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        base = normalize_pub_base(str(row.get("base_url") or row.get("subdomain") or ""))
        if not base or base in seen:
            continue
        seen.add(base)
        out.append(
            {
                "name": row.get("name") or row.get("author_name") or base,
                "base_url": base,
                "subdomain": row.get("subdomain") or urlparse(base).hostname,
                "id": row.get("id"),
            }
        )
        if len(out) >= limit:
            break
    return out


def archive_search(
    base_url: str,
    topic: str,
    *,
    limit: int = 15,
    client: httpx.Client | None = None,
) -> list[dict[str, Any]]:
    """Posts in one publication matching `topic` (or latest if topic is empty)."""
    base = normalize_pub_base(base_url)
    if not base:
        return []
    n = min(max(limit, 1), 50)
    # Substack treats '+' (httpx params) as a different query than '%20'.
    if topic.strip():
        url = f"{base}/api/v1/archive?sort=new&limit={n}&offset=0&search={quote(topic.strip())}"
    else:
        url = f"{base}/api/v1/archive?sort=new&limit={n}&offset=0"
    own = client is not None
    http = _client(client)
    try:
        resp = http.get(url)
        resp.raise_for_status()
        payload = resp.json()
    finally:
        if not own:
            http.close()
    rows = payload if isinstance(payload, list) else payload.get("posts") if isinstance(payload, dict) else []
    if not isinstance(rows, list):
        return []
    return [r for r in rows if isinstance(r, dict)][:limit]


def fetch_comment_tree(
    base_url: str,
    post_id: int | str,
    *,
    client: httpx.Client | None = None,
) -> list[dict[str, Any]]:
    base = normalize_pub_base(base_url)
    if not base or not post_id:
        return []
    own = client is not None
    http = _client(client)
    try:
        resp = http.get(
            f"{base}/api/v1/post/{post_id}/comments",
            params={"all_comments": "true"},
        )
        if resp.status_code >= 400:
            return []
        payload = resp.json()
    except Exception:  # noqa: BLE001
        return []
    finally:
        if not own:
            http.close()
    rows = payload.get("comments") if isinstance(payload, dict) else payload
    return rows if isinstance(rows, list) else []


def flatten_comments(
    tree: list[dict[str, Any]],
    *,
    depth: int = 0,
    limit: int = 40,
) -> list[dict[str, Any]]:
    """Walk Substack's nested `children` into mapper-shaped comments."""
    out: list[dict[str, Any]] = []

    def walk(nodes: list[Any], d: int) -> None:
        for node in nodes:
            if len(out) >= limit:
                return
            if not isinstance(node, dict) or node.get("deleted"):
                continue
            body = _strip_html(str(node.get("body") or ""))
            if not body:
                kids = node.get("children") or []
                if isinstance(kids, list):
                    walk(kids, d + 1)
                continue
            cid = node.get("id")
            out.append(
                {
                    "author": node.get("handle") or node.get("name") or "substack",
                    "body": body[:2000],
                    "score": node.get("score") if isinstance(node.get("score"), int) else node.get("reaction_count"),
                    "depth": d,
                    "url": f"#comment-{cid}" if cid else None,
                    "is_op": bool((node.get("metadata") or {}).get("is_author")),
                }
            )
            kids = node.get("children") or []
            if isinstance(kids, list):
                walk(kids, d + 1)

    walk(tree, depth)
    return out


def _article_body(post: dict[str, Any]) -> str:
    for key in ("truncated_body_text", "description", "subtitle", "title"):
        val = post.get(key)
        if isinstance(val, str) and val.strip():
            return _strip_html(val)[:4000]
    return ""


def _post_url(post: dict[str, Any], base: str) -> str:
    url = post.get("canonical_url") or ""
    if url:
        return str(url)
    slug = post.get("slug")
    if slug:
        return f"{base}/p/{slug}"
    return base


def scrape_topic(
    topic: str,
    *,
    publications: list[str] | None = None,
    per_target_limit: int = 12,
    max_publications: int = 6,
    fetch_comments: bool = True,
    comment_limit: int = 25,
    client: httpx.Client | None = None,
    pause_s: float = 0.12,
) -> list[dict[str, Any]]:
    """Articles (and optional comments) about `topic`.

    Discovers publications via search, plus any explicit publication URLs.
    """
    topic = (topic or "").strip()
    pubs: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add_pub(name: str, base: str) -> None:
        base = normalize_pub_base(base)
        if not base or base in seen:
            return
        seen.add(base)
        pubs.append({"name": name or base, "base_url": base})

    for raw in publications or []:
        add_pub(raw, raw)

    own = client is not None
    http = _client(client)
    try:
        if topic:
            for hit in search_publications(topic, limit=max_publications, client=http):
                add_pub(str(hit.get("name") or ""), str(hit.get("base_url") or ""))
        pubs = pubs[:max_publications]
        if not pubs:
            return []

        items: list[dict[str, Any]] = []
        seen_posts: set[str] = set()
        for pub in pubs:
            base = pub["base_url"]
            try:
                posts = archive_search(base, topic, limit=per_target_limit, client=http)
                if topic and not posts:
                    # Archive search is picky; fall back to latest and filter.
                    latest = archive_search(base, "", limit=max(per_target_limit, 12), client=http)
                    needle = topic.casefold()
                    posts = [
                        p
                        for p in latest
                        if needle in " ".join(
                            str(p.get(k) or "") for k in ("title", "subtitle", "description", "truncated_body_text")
                        ).casefold()
                    ][:per_target_limit]
            except Exception:  # noqa: BLE001
                continue
            if pause_s:
                time.sleep(pause_s)
            for post in posts:
                pid = str(post.get("id") or "")
                url = _post_url(post, base)
                key = pid or url
                if not key or key in seen_posts:
                    continue
                seen_posts.add(key)
                comments: list[dict[str, Any]] = []
                comment_count = post.get("comment_count") or 0
                if fetch_comments and pid and comment_count:
                    comment_base = normalize_pub_base(url) or base
                    comments = flatten_comments(
                        fetch_comment_tree(comment_base, pid, client=http),
                        limit=comment_limit,
                    )
                    for c in comments:
                        if c.get("url") and str(c["url"]).startswith("#"):
                            c["url"] = f"{url}{c['url']}"
                    if pause_s:
                        time.sleep(pause_s)
                title = str(post.get("title") or "")[:200]
                bylines = post.get("publishedBylines")
                author = pub["name"]
                if isinstance(bylines, list) and bylines and isinstance(bylines[0], dict):
                    author = bylines[0].get("name") or author
                items.append(
                    {
                        "id": pid or url,
                        "url": url,
                        "title": title,
                        "body": _article_body(post),
                        "author": author,
                        "score": str((post.get("reactions") or {}).get("❤") or post.get("reaction_count") or ""),
                        "engagement": {
                            "comments": post.get("comment_count"),
                            "reactions": post.get("reaction_count"),
                            "restacks": post.get("restacks"),
                        },
                        "scraped_at": _now(),
                        "raw": {
                            **{k: post.get(k) for k in ("id", "slug", "post_date", "audience", "subtitle") if k in post},
                            "search_query": topic,
                            "publication": pub["name"],
                            "publication_url": base,
                            "created_at": post.get("post_date"),
                            "comments": comments,
                            "comments_count": post.get("comment_count") or len(comments),
                            "title": title,
                        },
                    }
                )
        return items
    finally:
        if not own:
            http.close()
