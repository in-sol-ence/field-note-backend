"""Substack topic harvest: unofficial JSON → Signal → Post."""

from __future__ import annotations

import httpx

from harvest import has_targets, product_targets, watch_payload
from schema import ScrapeTargets, SubstackTargets
from scraping.mapper import signal_to_post
from social_signals_lite.substack import flatten_comments, normalize_pub_base, scrape_topic
from social_signals_lite.watch import watch_substack


def test_normalize_pub_accepts_subdomain_or_url() -> None:
    assert normalize_pub_base("platformer") == "https://platformer.substack.com"
    assert normalize_pub_base("https://platformer.substack.com/") == "https://platformer.substack.com"


def test_flatten_comments_walks_children_and_skips_deleted() -> None:
    tree = [
        {
            "id": 1,
            "body": "top",
            "handle": "alice",
            "score": 3,
            "deleted": False,
            "metadata": {},
            "children": [
                {
                    "id": 2,
                    "body": "reply",
                    "name": "bob",
                    "score": 1,
                    "deleted": False,
                    "children": [],
                    "metadata": {"is_author": True},
                },
                {"id": 3, "body": "gone", "deleted": True, "children": []},
            ],
        }
    ]
    got = flatten_comments(tree)
    assert [c["body"] for c in got] == ["top", "reply"]
    assert got[0]["depth"] == 0
    assert got[1]["depth"] == 1
    assert got[1]["is_op"] is True


def test_topic_targets_fill_substack_without_requiring_a_product_subreddit() -> None:
    got = product_targets("", topic="AI coding assistants")
    assert got.topic == "AI coding assistants"
    assert got.substack.topics == ["AI coding assistants"]
    assert got.reddit.subreddits == []
    assert has_targets("substack", got)
    body = watch_payload("substack", got, 8)
    assert body["platform"] == "substack"
    assert body["targets"]["substack"]["topics"] == ["AI coding assistants"]
    assert body["targets"]["substack"]["fetch_comments"] is True


def test_umbrella_topic_counts_as_substack_targets() -> None:
    empty = ScrapeTargets(topic="climate")
    assert has_targets("substack", empty)
    assert watch_payload("substack", empty, 5)["targets"]["substack"]["topics"] == ["climate"]


def test_mapper_keeps_substack_comments_on_the_article() -> None:
    post = signal_to_post(
        {
            "platform": "substack",
            "signal_id": "99",
            "url": "https://example.substack.com/p/hello",
            "title": "Hello",
            "body": "essay",
            "author": "Casey",
            "score": "4",
            "engagement": {"comments": 2},
            "scraped_at": "2026-08-16T00:00:00Z",
            "raw": {
                "id": 99,
                "title": "Hello",
                "publication": "Example",
                "created_at": "2026-08-01T00:00:00Z",
                "search_query": "AI coding assistants",
                "comments": [
                    {"author": "alice", "body": "agree", "score": 2, "depth": 0},
                    {"author": "bob", "body": "nope", "score": 1, "depth": 1},
                ],
            },
        }
    )
    assert post["source"] == "substack"
    assert post["channel"] == "Example"
    assert post["num_comments"] == 2
    assert [c["body"] for c in post["comments"]] == ["agree", "nope"]
    assert post["search_query"] == "AI coding assistants"


def test_scrape_topic_uses_search_archive_and_comments(monkeypatch) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        calls.append(url)
        if "publication/search" in url:
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "name": "Platformer",
                            "base_url": "https://platformer.substack.com",
                            "subdomain": "platformer",
                        }
                    ]
                },
            )
        if "/api/v1/archive" in url:
            return httpx.Response(
                200,
                json=[
                    {
                        "id": 11,
                        "slug": "hello",
                        "title": "Hello AI",
                        "canonical_url": "https://platformer.substack.com/p/hello",
                        "truncated_body_text": "body text",
                        "post_date": "2026-01-01T00:00:00Z",
                        "comment_count": 1,
                        "publishedBylines": [{"name": "Casey"}],
                    }
                ],
            )
        if "/api/v1/post/11/comments" in url:
            return httpx.Response(
                200,
                json={
                    "comments": [
                        {
                            "id": 7,
                            "body": "good take",
                            "handle": "reader",
                            "score": 4,
                            "deleted": False,
                            "children": [],
                            "metadata": {},
                        }
                    ]
                },
            )
        return httpx.Response(404, json={})

    transport = httpx.MockTransport(handler)
    client = httpx.Client(transport=transport)
    items = scrape_topic(
        "AI coding assistants",
        per_target_limit=5,
        fetch_comments=True,
        client=client,
        pause_s=0,
    )
    assert len(items) == 1
    assert items[0]["title"] == "Hello AI"
    assert items[0]["raw"]["comments"][0]["body"] == "good take"
    assert any("publication/search" in u for u in calls)
    assert any("/archive" in u for u in calls)
    assert any("/comments" in u for u in calls)


def test_publication_search_encodes_spaces_as_percent_twenty() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, json={"results": []})

    from social_signals_lite.substack import search_publications

    search_publications("AI coding", client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert seen and "%20" in seen[0]
    assert "+" not in seen[0].split("query=")[-1]


def test_watch_substack_maps_mocked_scrape(monkeypatch) -> None:
    monkeypatch.setattr(
        "social_signals_lite.watch.substack_scrape.scrape_topic",
        lambda *a, **k: [
            {
                "id": "1",
                "url": "https://ex.substack.com/p/a",
                "title": "A",
                "body": "b",
                "author": "ann",
                "score": "1",
                "engagement": {"comments": 0},
                "scraped_at": "2026-01-01T00:00:00Z",
                "raw": {"id": 1, "title": "A", "publication": "Ex", "comments": []},
            }
        ],
    )
    got = watch_substack({"topics": ["climate"]}, 3)
    assert len(got) == 1
    assert got[0]["platform"] == "substack"
    assert got[0]["url"].endswith("/p/a")
