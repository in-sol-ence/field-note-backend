"""Hacker News read-only scrape via Algolia + Firebase APIs."""

from __future__ import annotations

import html
import re
import time
from typing import Any

import httpx

ALGOLIA_SEARCH = "https://hn.algolia.com/api/v1/search"
ALGOLIA_ITEM = "https://hn.algolia.com/api/v1/items/{item_id}"
HN_FIREBASE_ITEM = "https://hacker-news.firebaseio.com/v0/item/{}.json"

_WIH_TITLE_RE = re.compile(r"who is hiring", re.I)
_WIH_SKIP_RE = re.compile(r"freelancer|who wants to be hired|who wants to be hired", re.I)
_HTML_TAG_RE = re.compile(r"<[^>]+>")


def hit_to_item(hit: dict[str, Any]) -> dict[str, Any]:
    item_id = hit.get("objectID") or hit.get("story_id") or hit.get("id")
    url = hit.get("url") or (f"https://news.ycombinator.com/item?id={item_id}" if item_id else "")
    title = hit.get("title") or hit.get("story_title") or ""
    body = hit.get("story_text") or hit.get("comment_text") or hit.get("title") or ""
    return {
        "id": str(item_id or url),
        "url": url,
        "title": str(title)[:200],
        "body": str(body)[:1200],
        "author": hit.get("author") or hit.get("by") or "hn",
        "score": str(hit.get("points") or hit.get("score") or ""),
        "engagement": {
            "points": hit.get("points"),
            "num_comments": hit.get("num_comments"),
        },
        "scraped_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "raw": hit,
    }


def _strip_html(text: str) -> str:
    plain = _HTML_TAG_RE.sub(" ", text or "")
    return html.unescape(re.sub(r"\s+", " ", plain)).strip()


def find_latest_who_is_hiring_thread() -> dict[str, Any]:
    """Return the newest Ask HN: Who is hiring? monthly thread."""
    # author_whoishiring tag in Algolia is stale (stops ~2020); use recency filter instead.
    params_list = [
        {
            "query": "Ask HN: Who is hiring",
            "tags": "story",
            "hitsPerPage": 25,
            "numericFilters": "created_at_i>1700000000",
        },
        {"tags": "story,author_whoishiring", "hitsPerPage": 15},
        {"query": "Ask HN: Who is hiring", "tags": "story", "hitsPerPage": 10},
    ]

    candidates: list[dict[str, Any]] = []
    with httpx.Client(timeout=30.0) as client:
        for params in params_list:
            resp = client.get(ALGOLIA_SEARCH, params=params)
            resp.raise_for_status()
            for hit in resp.json().get("hits") or []:
                title = hit.get("title") or ""
                if not _WIH_TITLE_RE.search(title):
                    continue
                if _WIH_SKIP_RE.search(title):
                    continue
                if "who is hiring right now" in title.lower():
                    continue
                candidates.append(hit)
            if candidates:
                break

    if not candidates:
        raise RuntimeError("No Ask HN: Who is hiring thread found")

    candidates.sort(key=lambda h: int(h.get("created_at_i") or 0), reverse=True)
    return candidates[0]


def fetch_firebase_item(item_id: int | str) -> dict[str, Any]:
    with httpx.Client(timeout=30.0) as client:
        resp = client.get(HN_FIREBASE_ITEM.format(item_id))
        resp.raise_for_status()
        data = resp.json()
    return data if isinstance(data, dict) else {}


def _comment_matches_keywords(body: str, keywords: list[str]) -> bool:
    if not keywords:
        return True
    lower = body.lower()
    return any(kw.lower() in lower for kw in keywords if kw.strip())


def comment_to_job_item(comment: dict[str, Any], thread_id: str) -> dict[str, Any]:
    cid = comment.get("id")
    text = _strip_html(str(comment.get("text") or ""))
    title = text.split("\n", 1)[0][:200] if text else f"HN hiring comment {cid}"
    return {
        "id": f"hn-wih-{thread_id}-{cid}",
        "url": f"https://news.ycombinator.com/item?id={cid}",
        "title": title,
        "body": text[:2000],
        "author": comment.get("by") or "hn",
        "score": "",
        "engagement": {"thread_id": thread_id, "comment_id": cid},
        "scraped_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "raw": comment,
    }


def scrape_who_is_hiring(
    *,
    limit: int = 20,
    keywords: list[str] | None = None,
    max_scan: int = 120,
) -> list[dict[str, Any]]:
    """Top-level comments from the latest monthly Who is Hiring thread."""
    thread = find_latest_who_is_hiring_thread()
    thread_id = str(thread.get("objectID") or thread.get("story_id") or "")
    story = fetch_firebase_item(thread_id)
    kid_ids = story.get("kids") or []

    items: list[dict[str, Any]] = []
    for kid_id in kid_ids[:max_scan]:
        comment = fetch_firebase_item(kid_id)
        if not comment or comment.get("deleted") or comment.get("dead"):
            continue
        body = _strip_html(str(comment.get("text") or ""))
        if len(body) < 40:
            continue
        if not _comment_matches_keywords(body, keywords or []):
            continue
        items.append(comment_to_job_item(comment, thread_id))
        if len(items) >= limit:
            break

    if not items:
        thread_item = hit_to_item(thread)
        thread_item["title"] = f"Who is Hiring thread ({thread.get('title', '')})"
        items.append(thread_item)
    return items


def fetch_front_page(*, limit: int = 30, tag: str = "front_page") -> list[dict[str, Any]]:
    params = {"tags": tag, "hitsPerPage": limit}
    with httpx.Client(timeout=30.0) as client:
        resp = client.get(ALGOLIA_SEARCH, params=params)
        resp.raise_for_status()
        hits = resp.json().get("hits") or []
    return [hit_to_item(hit) for hit in hits[:limit]]


def fetch_item(item_id: str) -> dict[str, Any]:
    with httpx.Client(timeout=30.0) as client:
        resp = client.get(ALGOLIA_ITEM.format(item_id=item_id))
        resp.raise_for_status()
        data = resp.json()
    children = data.get("children") or []
    body_parts = [c.get("comment_text") or c.get("text") or "" for c in children[:5]]
    return {
        "id": str(data.get("id") or item_id),
        "url": f"https://news.ycombinator.com/item?id={item_id}",
        "title": (data.get("title") or f"HN item {item_id}")[:200],
        "body": "\n".join(p for p in body_parts if p)[:1200],
        "author": data.get("author") or "hn",
        "score": str(data.get("points") or ""),
        "engagement": {"points": data.get("points"), "comments": len(children)},
        "scraped_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "raw": data,
    }


def fetch_comments(item_id: str, *, limit: int = 15) -> list[dict[str, Any]]:
    """Top-level comments for an HN story, flattened and HTML-stripped.

    Algolia search hits carry no comment text, only counts — so a story pulled
    from search has no quotable evidence until this second call.
    """
    with httpx.Client(timeout=30.0) as client:
        resp = client.get(ALGOLIA_ITEM.format(item_id=item_id))
        resp.raise_for_status()
        data = resp.json()

    out: list[dict[str, Any]] = []
    for child in (data.get("children") or [])[:limit]:
        body = _strip_html(str(child.get("text") or child.get("comment_text") or ""))
        if len(body) < 20:
            continue
        out.append(
            {
                "author": child.get("author"),
                "body": body[:2000],
                "score": child.get("points"),
            }
        )
    return out


def enrich_with_comments(
    items: list[dict[str, Any]], *, top_n: int = 6, comment_limit: int = 15
) -> list[dict[str, Any]]:
    """Attach comment threads to the highest-scoring items. Returns new dicts."""
    ranked = sorted(items, key=lambda i: int(i.get("engagement", {}).get("points") or 0), reverse=True)
    targets = {i.get("id") for i in ranked[:top_n]}
    out: list[dict[str, Any]] = []
    for item in items:
        story_id = (item.get("raw") or {}).get("story_id") or (item.get("raw") or {}).get("objectID")
        if item.get("id") not in targets or not story_id:
            out.append(item)
            continue
        try:
            comments = fetch_comments(str(story_id), limit=comment_limit)
        except Exception as exc:  # noqa: BLE001 - one failure must not kill the run
            out.append({**item, "detail_error": str(exc)})
            continue
        raw = {**(item.get("raw") or {}), "comments": comments}
        out.append({**item, "raw": raw})
    return out


def scrape_feed(
    *,
    tags: list[str] | None = None,
    queries: list[str] | None = None,
    limit: int = 8,
    hiring_keywords: list[str] | None = None,
    fetch_comment_threads: bool = False,
    top_n_comments: int = 6,
    comment_limit: int = 15,
) -> list[dict[str, Any]]:
    tags = tags or ["front_page"]
    collected: list[dict[str, Any]] = []
    per_tag = max(2, limit // max(len(tags), 1))

    if "who_is_hiring" in tags:
        wih_limit = max(per_tag, limit // 2)
        collected.extend(
            scrape_who_is_hiring(limit=wih_limit, keywords=hiring_keywords or queries)
        )
        tags = [t for t in tags if t != "who_is_hiring"]

    # Queries run BEFORE tag feeds. Front-page items used to consume the whole
    # `limit` first, so a search for a product name returned only whatever HN
    # happened to be discussing that day.
    if queries:
        per_query = max(2, limit // max(len(queries), 1))
        with httpx.Client(timeout=30.0) as client:
            for query in queries:
                resp = client.get(
                    ALGOLIA_SEARCH, params={"query": query, "hitsPerPage": per_query}
                )
                resp.raise_for_status()
                for hit in resp.json().get("hits") or []:
                    collected.append(hit_to_item(hit))
                if len(collected) >= limit:
                    break

    # Pad with the front page only when no queries were given. With queries
    # set, an untargeted feed is pure noise for a product-signal run.
    if not queries:
        for tag in tags:
            collected.extend(fetch_front_page(limit=per_tag, tag=tag))
            if len(collected) >= limit:
                break

    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for item in collected:
        key = item.get("id") or item.get("url")
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
        if len(out) >= limit:
            break

    if fetch_comment_threads:
        out = enrich_with_comments(out, top_n=top_n_comments, comment_limit=comment_limit)
    return out


def scrape_url(url: str) -> dict[str, Any]:
    if "item?id=" in url:
        item_id = url.split("item?id=")[-1].split("&")[0]
        return fetch_item(item_id)
    if url.startswith("http") and "news.ycombinator.com" not in url:
        return {
            "id": url,
            "url": url,
            "title": url,
            "body": url,
            "author": "hn",
            "score": "",
            "engagement": {},
            "scraped_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
    return fetch_front_page(limit=1)[0]
