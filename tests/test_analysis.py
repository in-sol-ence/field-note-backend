"""T2 plumbing: Post -> Signal, quotability, and validations reaching the report.

No network. issues.analyze_with_validation is stubbed, because what is under
test is the wiring around the model calls, not the model.
"""

from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import analysis  # noqa: E402
from assets import Comment, Engagement, Issue, Signal, SignalFindings  # noqa: E402
from enriched import build_report  # noqa: E402
from schema import ReportEvent, StageEvent  # noqa: E402


def reddit_post(**overrides) -> dict:
    post = {
        "id": "post_rd_1",
        "source": "reddit",
        "source_id": "t3_abc",
        "url": "https://www.reddit.com/r/x/comments/abc/t/",
        "title": "Billing keeps charging after cancel",
        "body": "Cancelled in March and was billed twice since.",
        "author": "u/someone",
        "channel": "r/x",
        "created_at": "2026-08-01T00:00:00Z",
        "score": 120,
        "num_comments": 2,
        "scraped_at": "2026-08-15T00:00:00Z",
        "comments": [{"author": "b", "body": "same here", "score": 5}],
        "raw": {"subreddit": "r/x"},
    }
    return {**post, **overrides}


def signal(body: str = "", comments: list[Comment] | None = None) -> Signal:
    return Signal(
        platform="reddit",
        signal_id="s1",
        url="https://example.com/1",
        title="t",
        body=body,
        author="a",
        score="1",
        engagement=Engagement(score="1", comments=comments or []),
        scraped_at="2026-08-15T00:00:00Z",
        raw={},
    )


# ---------------------------------------------------------------------------
# post_to_signal
# ---------------------------------------------------------------------------


def test_post_converts_to_a_loadable_signal():
    """T1 hands the pipeline Posts; T2 was written against Signals. If the
    conversion drops a required field the whole stage dies at from_dict."""
    converted = Signal.from_dict(analysis.post_to_signal(reddit_post()))
    assert converted.platform == "reddit"
    assert converted.signal_id == "t3_abc"
    assert converted.body.startswith("Cancelled in March")


def test_comments_survive_the_conversion():
    """Comments are the quotable evidence. Losing them leaves T2 clustering
    headlines with nothing to cite."""
    converted = Signal.from_dict(analysis.post_to_signal(reddit_post()))
    assert [c.body for c in converted.engagement.comments] == ["same here"]


def test_blank_comments_are_dropped():
    """A comment with no body cannot be quoted and only inflates the prompt."""
    post = reddit_post(comments=[{"author": "a", "body": "   ", "score": 1}])
    converted = Signal.from_dict(analysis.post_to_signal(post))
    assert converted.engagement.comments == []


def test_created_at_is_carried_into_raw():
    """enriched reads raw.created_at for first_seen/last_seen. Dropping it
    makes every issue look like it appeared today."""
    converted = Signal.from_dict(analysis.post_to_signal(reddit_post()))
    assert converted.raw["created_at"] == "2026-08-01T00:00:00Z"


def test_numeric_score_becomes_a_string():
    """Post.score is an int, Signal.score is a str. Without the cast the
    dataclass silently holds the wrong type and str comparisons misbehave."""
    converted = Signal.from_dict(analysis.post_to_signal(reddit_post(score=120)))
    assert converted.score == "120"


# ---------------------------------------------------------------------------
# is_quotable
# ---------------------------------------------------------------------------


def test_body_alone_is_quotable():
    assert analysis.is_quotable(signal(body="something broke"))


def test_comments_alone_are_quotable():
    """Link posts have no body, but their comment threads carry the complaint."""
    assert analysis.is_quotable(signal(comments=[Comment("a", "it broke", "3")]))


def test_nothing_to_quote_is_skipped():
    """A link post with no body and no fetched comments can only produce empty
    findings, so sending it spends a model call for nothing."""
    assert not analysis.is_quotable(signal())
    assert not analysis.is_quotable(signal(comments=[Comment("a", "  ", "1")]))


# ---------------------------------------------------------------------------
# run_t2_stream
# ---------------------------------------------------------------------------


@dataclass
class FakeValidation:
    finding_title: str
    verdict: str
    supported_signal_ids: list
    explanation: str
    sources: list


@dataclass
class FakeAnalysis:
    findings: SignalFindings
    validations: list


def collect(agen) -> list:
    async def run():
        return [event async for event in agen]

    return asyncio.run(run())


class FakeHarvest:
    def __init__(self, posts):
        self.posts = posts


@pytest.fixture
def stub_analyze(monkeypatch):
    """Replace the model call with a fixed finding plus its verdict."""
    issue = Issue(
        title="Billing keeps charging after cancel",
        summary="Users are billed after cancelling.",
        product_area="billing",
        severity="high",
        signal_ids=["t3_abc"],
        evidence=["Cancelled in March and was billed twice since."],
        suggested_action="Honour cancellations.",
    )
    result = FakeAnalysis(
        findings=SignalFindings(issues=[issue], recommended_features=[], loved_features=[]),
        validations=[
            FakeValidation(
                finding_title=issue.title,
                verdict="supported",
                supported_signal_ids=["t3_abc"],
                explanation="The cited post says exactly this.",
                sources=["https://www.reddit.com/r/x/comments/abc/t/"],
            )
        ],
    )

    async def fake(signals, dossier=None):
        return result

    import issues

    monkeypatch.setattr(issues, "analyze_with_validation", fake)
    return result


def test_verdicts_reach_the_report(stub_analyze, monkeypatch, tmp_path):
    """The whole point of this stage: a computed verdict must land on the
    finding. It used to be awaited and thrown away, so every issue reached the
    dashboard marked unvalidated."""
    monkeypatch.setattr("report_store.REPORT_DIR", tmp_path)
    events = collect(analysis.run_t2_stream(FakeHarvest([reddit_post()]), "Acme"))
    report = next(e for e in events if isinstance(e, ReportEvent)).report
    assert report["issues"][0]["validation"]["verdict"] == "supported"


def test_report_is_persisted_for_the_dashboard(stub_analyze, monkeypatch, tmp_path):
    monkeypatch.setattr("report_store.REPORT_DIR", tmp_path)
    collect(analysis.run_t2_stream(FakeHarvest([reddit_post()]), "Acme"))
    assert (tmp_path / "acme.json").is_file()


def test_unquotable_harvest_fails_the_stage_without_calling_the_model(monkeypatch, tmp_path):
    """Nothing quotable means no findings are possible. The stage should say so
    rather than spend calls and report an empty success."""
    monkeypatch.setattr("report_store.REPORT_DIR", tmp_path)
    bare = reddit_post(body="", comments=[])
    events = collect(analysis.run_t2_stream(FakeHarvest([bare]), "Acme"))
    stages = [e for e in events if isinstance(e, StageEvent)]
    assert stages[0].status == "failed"
    assert not any(isinstance(e, ReportEvent) for e in events)


def test_t2_failure_does_not_lose_earlier_stages(monkeypatch, tmp_path):
    """T0 and T1 already cost minutes and money. A T2 blowup must surface as a
    non-fatal error, not take the run down."""
    monkeypatch.setattr("report_store.REPORT_DIR", tmp_path)

    async def boom(signals, dossier=None):
        raise RuntimeError("model exploded")

    import issues

    monkeypatch.setattr(issues, "analyze_with_validation", boom)
    events = collect(analysis.run_t2_stream(FakeHarvest([reddit_post()]), "Acme"))
    errors = [e for e in events if getattr(e, "event", "") == "error"]
    assert errors and errors[0].fatal is False


# ---------------------------------------------------------------------------
# enriched
# ---------------------------------------------------------------------------


def test_hackernews_evidence_cites_the_thread_not_the_article():
    """An HN signal's url is the submitted article, but the quoted comments
    live on the item page. Citing the article sends readers somewhere the
    quote does not appear."""
    hn = Signal(
        platform="hackernews",
        signal_id="4242",
        url="https://github.com/someone/project",
        title="Show HN: project",
        body="",
        author="pg",
        score="500",
        engagement=Engagement(comments=[Comment("pg", "it crashes on startup", "9")]),
        scraped_at="2026-08-15T00:00:00Z",
        raw={"objectID": "4242"},
    )
    findings = SignalFindings(
        issues=[
            Issue(
                title="Crashes on startup",
                summary="It crashes.",
                product_area="runtime",
                severity="high",
                signal_ids=["4242"],
                evidence=["it crashes on startup"],
                suggested_action="Fix it.",
            )
        ],
        recommended_features=[],
        loved_features=[],
    )
    report = build_report(findings, [hn], product="Project")
    quote = report.issues[0].evidence[0]
    assert quote.url == "https://news.ycombinator.com/item?id=4242"
    assert quote.verbatim is True


def test_a_quote_missing_from_its_source_is_flagged():
    """A free hallucination check: if the model paraphrased instead of quoting,
    the citation should be marked rather than silently trusted."""
    sig = signal(body="the export button does nothing")
    findings = SignalFindings(
        issues=[
            Issue(
                title="Export broken",
                summary="Export does nothing.",
                product_area="export",
                severity="medium",
                signal_ids=["s1"],
                evidence=["users report that exporting silently fails"],
                suggested_action="Fix export.",
            )
        ],
        recommended_features=[],
        loved_features=[],
    )
    report = build_report(findings, [sig], product="Acme")
    assert report.issues[0].evidence[0].verbatim is False
