import asyncio
from pathlib import Path

import issues
from assets import Issue, SignalFindings
from tests.visualize_issues import load_signals, render_html


FIXTURE = Path(__file__).parents[1] / "data" / "signals_perplexity_x.json"


def _findings(signal_id: str) -> SignalFindings:
    return SignalFindings(
        issues=[
            Issue(
                title="Citation mismatch",
                summary="An answer cites the wrong source.",
                product_area="answers",
                severity="high",
                signal_ids=[signal_id, "unknown-id"],
                evidence=["the citation is wrong"],
                suggested_action="Verify citation entailment.",
            )
        ],
        recommended_features=[],
        loved_features=[],
    )


def test_branch_fixture_can_flow_through_issues_and_render(monkeypatch) -> None:
    signals = load_signals(FIXTURE)[:1]

    async def fake_grok(_prompt, output_type, **_kwargs):
        if output_type is issues.ValidationResult:
            return issues.ValidationResult(
                finding_title="Citation mismatch",
                verdict="supported",
                supported_signal_ids=[signals[0].signal_id],
                explanation="The source supports the claim.",
                sources=[signals[0].url],
            )
        return _findings(signals[0].signal_id)

    monkeypatch.setattr(issues, "call_grok", fake_grok)
    findings = asyncio.run(issues.analyze_signals(signals))
    report = render_html(findings, signals, FIXTURE.name)

    assert findings.issues[0].signal_ids == [signals[0].signal_id, "unknown-id"]
    assert "Citation mismatch" in report
    assert signals[0].url in report
    assert "1 source signals" in report


def test_loader_accepts_raw_x_scraper_records(tmp_path: Path) -> None:
    raw_file = tmp_path / "raw.json"
    raw_file.write_text(
        '[{"id":"42","full_text":"broken workflow","user":{"username":"sam"},'
        '"metrics":{"favorite_count":3,"reply_count":1,"retweet_count":0}}]'
    )

    signal = load_signals(raw_file)[0]

    assert signal.signal_id == "42"
    assert signal.body == "broken workflow"
    assert signal.engagement.likes == 3
