"""Tests for signal_to_post.

Each test encodes WHY the mapping matters, not just what it returns — the two
real hazards are stringly-typed Reddit counts and Reddit's missing post
timestamp, both of which corrupt downstream issue scoring if mishandled.
"""

import json
from pathlib import Path

import pytest

from mapper import SignalMappingError, signal_to_post, signals_to_posts

FIXTURES = Path(__file__).resolve().parent / "data" / "signals_testfixture.json"


@pytest.fixture(scope="module")
def signals():
    return json.loads(FIXTURES.read_text())


@pytest.fixture(scope="module")
def reddit(signals):
    return next(s for s in signals if s["platform"] == "reddit")


@pytest.fixture(scope="module")
def tweet(signals):
    return next(s for s in signals if s["platform"] == "x")


def test_reddit_counts_become_ints(reddit):
    """Reddit returns "412" as a string. The dashboard sorts issues by score,
    and string sort puts "9" above "412"."""
    post = signal_to_post(reddit)
    assert post["score"] == 412
    assert post["num_comments"] == 87
    assert isinstance(post["score"], int)


def test_reddit_carries_real_post_time(reddit):
    """The scraper now reads shreddit-post's created-timestamp. Falling back to
    scrape time would make every post look like it was posted today, which
    collapses Issue.trend to 'everything is rising'."""
    post = signal_to_post(reddit)
    assert post["created_at"] == "2026-08-11T14:22:09.412000+0000"
    assert post["created_at_is_scrape_time"] is False


def test_scrape_time_fallback_still_flagged():
    """If a scraper regresses and stops emitting created_at, the T2 agent must
    be able to tell — silently substituting scrape time corrupts trend math."""
    post = signal_to_post(
        {
            "platform": "reddit",
            "url": "https://www.reddit.com/r/x/comments/zz1/t/",
            "scraped_at": "2026-08-15T00:00:00Z",
            "raw": {},
        }
    )
    assert post["created_at"] == "2026-08-15T00:00:00Z"
    assert post["created_at_is_scrape_time"] is True


def test_x_keeps_real_created_at(tweet):
    """X stores its post time under `time`, not `created_at`. Reading only
    created_at silently backdated every tweet to scrape time."""
    post = signal_to_post(tweet)
    assert post["created_at"] == "2026-08-13T18:05:30Z"
    assert post["created_at_is_scrape_time"] is False


def test_x_aria_label_counts_parse(tweet):
    """The X scraper reads counts out of ARIA labels: "4050 Likes. Like".
    Treating that as a number yields None, so every tweet ranked last."""
    post = signal_to_post(tweet)
    assert post["score"] == 3140
    assert post["num_comments"] == 196


def test_x_source_id_falls_back_to_signal_id(tweet):
    """The X scraper emits no id field; signal_id is the tweet id."""
    assert signal_to_post(tweet)["source_id"] == "1954480000000000001"


def test_x_title_is_dropped(tweet):
    """social-signals sets X title to text[:120] — a truncated body. Rendering
    that as a headline shows users a sentence cut mid-word."""
    post = signal_to_post(tweet)
    assert post["title"] is None
    assert post["body"].endswith("going back to plain search")


def test_id_is_stable_and_derived_from_url(reddit):
    """signal_id on Reddit is the subreddit URL, not unique per post. Dedup
    across scrape passes depends on Post.id being per-post and repeatable."""
    assert signal_to_post(reddit)["id"] == signal_to_post(reddit)["id"]
    assert signal_to_post(reddit)["id"].startswith("post_rd_")


def test_reddit_source_id_extracted_from_permalink(reddit):
    post = signal_to_post(reddit)
    assert post["source_id"] == "t3_1a2b3c"


def test_authors_are_platform_prefixed(reddit, tweet):
    assert signal_to_post(reddit)["author"] == "u/Typical_Yogurt_9500"
    assert signal_to_post(tweet)["author"] == "@devanshu_b"


def test_unsupported_platform_raises():
    with pytest.raises(SignalMappingError):
        signal_to_post({"platform": "myspace", "url": "https://x"})


def test_missing_url_raises():
    with pytest.raises(SignalMappingError):
        signal_to_post({"platform": "reddit", "signal_id": "abc"})


def test_batch_survives_one_bad_signal(signals):
    """A single malformed signal must not abort a scrape run mid-demo."""
    posts, errors = signals_to_posts([*signals, {"platform": "reddit"}])
    assert len(posts) == len(signals)
    assert len(errors) == 1


def test_hn_url_points_at_the_thread_not_the_article(signals):
    """HN signals carry the submitted article's URL, but the complaints are in
    the HN comments. Citing the article as evidence sends a reader to a page
    that contains none of the quoted text."""
    hn = next(s for s in signals if s["platform"] == "hackernews")
    post = signal_to_post(hn)
    assert post["url"] == "https://news.ycombinator.com/item?id=49301188"
    assert post["source_id"] == "49301188"


def test_hn_score_comes_from_points(signals):
    hn = next(s for s in signals if s["platform"] == "hackernews")
    post = signal_to_post(hn)
    assert post["score"] == 284
    assert post["num_comments"] == 137
    assert post["created_at"] == "2026-08-12T15:31:08Z"


@pytest.mark.parametrize(
    "value,expected",
    [
        ("412", 412),
        ("1.2K", 1200),
        ("4050 Likes. Like", 4050),
        ("1.2K reposts. Repost", 1200),
        ("3,140", 3140),
        (1486, 1486),
        ("", None),
        ("no digits", None),
    ],
)
def test_count_coercion(value, expected):
    """Every count shape the three scrapers actually emit."""
    post = signal_to_post(
        {"platform": "reddit", "url": "https://r/x/comments/a1/t/", "score": value, "raw": {}}
    )
    assert post["score"] == expected


def test_every_fixture_signal_maps(signals):
    posts, errors = signals_to_posts(signals)
    assert errors == []
    assert len(posts) == 10
    assert {p["source"] for p in posts} == {"reddit", "x", "hackernews"}
    assert all(p["created_at"] for p in posts)
    assert not any(p["created_at_is_scrape_time"] for p in posts)


def test_x_zero_engagement_is_zero_not_unknown():
    """X omits the number from the ARIA label at zero engagement. Mapping that
    to None makes a real tweet indistinguishable from a scrape failure, and
    None sorts unpredictably when the dashboard ranks by score."""
    post = signal_to_post(
        {
            "platform": "x",
            "signal_id": "123",
            "url": "https://x.com/a/status/123",
            "score": "Like",
            "engagement": {"likes": "Like", "replies": "Reply"},
            "raw": {"handle": "a", "time": "2026-08-15T00:00:00Z"},
        }
    )
    assert post["score"] == 0
    assert post["num_comments"] == 0


def test_reddit_missing_score_stays_unknown():
    """Unlike X, a Reddit post with no score means the scrape failed. Coercing
    it to 0 would hide the failure and rank a broken post as unpopular."""
    post = signal_to_post(
        {"platform": "reddit", "url": "https://r/x/comments/a1/t/", "raw": {}}
    )
    assert post["score"] is None


def test_x_author_is_the_handle_not_the_display_name():
    """signal.author holds the display name ("Justin Merrlles"), which is not
    clickable and not unique. Citations need the handle."""
    post = signal_to_post(
        {
            "platform": "x",
            "signal_id": "123",
            "url": "https://x.com/J_Merrlles/status/123",
            "author": "Justin Merrlles",
            "raw": {"handle": "J_Merrlles", "time": "2026-08-15T00:00:00Z"},
        }
    )
    assert post["author"] == "@J_Merrlles"
    assert post["channel"] == "@J_Merrlles"


def test_listing_comment_count_is_not_mistaken_for_a_thread():
    """A Reddit listing puts a count string ("87") under the same `comments`
    key the detail page uses for the comment list. Trusting it produced a
    thread of single characters and crashed evidence extraction."""
    post = signal_to_post(
        {
            "platform": "reddit",
            "url": "https://www.reddit.com/r/x/comments/a1/t/",
            "raw": {"comments": "87", "comments_count": "87"},
        }
    )
    assert post["comments"] == []
    assert post["num_comments"] == 87


def test_reddit_comment_thread_is_carried_through():
    """Comments are the quotable evidence an Issue cites. Dropping them leaves
    the T2 agent clustering headlines with nothing to quote."""
    post = signal_to_post(
        {
            "platform": "reddit",
            "url": "https://www.reddit.com/r/x/comments/a1/t/",
            "raw": {
                "comments": [
                    {"author": "jakegh", "body": "chatgpt is better at searching", "score": "12"},
                    {"author": "nobody", "body": "", "score": "3"},
                ]
            },
        }
    )
    assert len(post["comments"]) == 1
    assert post["comments"][0]["author"] == "jakegh"
    assert post["comments"][0]["score"] == 12
    assert post["num_comments"] == 1


def test_hn_comment_threads_are_carried_through():
    """HN comments arrive via a second Algolia call. Hardcoding them to [] made
    HN posts unusable as evidence even after the fetch was added."""
    post = signal_to_post(
        {
            "platform": "hackernews",
            "signal_id": "1",
            "url": "https://example.com/a",
            "raw": {
                "objectID": "1",
                "comments": [{"author": "pg", "body": "this is a real complaint", "score": 9}],
            },
        }
    )
    assert len(post["comments"]) == 1
    assert post["comments"][0]["author"] == "pg"


def test_search_provenance_is_preserved():
    """A post found by a targeted search is stronger evidence than one that
    merely appeared in a feed. T2 needs to tell them apart."""
    hit = signal_to_post(
        {
            "platform": "reddit",
            "url": "https://www.reddit.com/r/Revolut/comments/a1/t/",
            "raw": {"search_query": "perplexity billing"},
        }
    )
    ambient = signal_to_post(
        {"platform": "reddit", "url": "https://www.reddit.com/r/x/comments/b2/t/", "raw": {}}
    )
    assert hit["search_query"] == "perplexity billing"
    assert ambient["search_query"] is None


def test_comment_depth_and_permalink_are_kept():
    """A depth-0 comment answers the post; a depth-3 comment is usually a side
    argument. T2 ranks evidence by that, and a per-comment URL lets a cited
    quote be verified without opening the whole thread."""
    post = signal_to_post(
        {
            "platform": "reddit",
            "url": "https://www.reddit.com/r/x/comments/a1/t/",
            "author": "op_user",
            "raw": {
                "comments": [
                    {
                        "author": "op_user",
                        "body": "clarifying my own post here",
                        "depth": "0",
                        "permalink": "/r/x/comments/a1/t/c1/",
                        "is_op": True,
                    },
                    {"author": "other", "body": "deep tangent about pricing", "depth": "3"},
                ]
            },
        }
    )
    first, second = post["comments"]
    assert first["depth"] == 0
    assert first["is_op"] is True
    assert first["url"] == "https://www.reddit.com/r/x/comments/a1/t/c1/"
    assert second["depth"] == 3
    assert second["is_op"] is False
    assert second["url"] is None
