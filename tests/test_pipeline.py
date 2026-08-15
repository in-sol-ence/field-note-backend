"""The orchestration seam: main.py -> pipeline.py -> preprocess.py."""

import asyncio

import pytest

import main
import pipeline
import preprocess
from assets import Engagement, Signal, SignalFindings
from pipeline import MissingCredentials, preflight, run_analysis, run_t0, run_t0_stream
from schema import (
    Disambiguation,
    Identity,
    PreprocessRequest,
    ProductDossier,
    Provenance,
    ResultEvent,
    StageEvent,
    Vocabulary,
    What,
)
from datetime import datetime, timezone

REQ = PreprocessRequest(
    website="https://acme.dev", name="Acme", repo="acme/acme", form="a CI tool"
)


def _dossier() -> ProductDossier:
    return ProductDossier(
        identity=Identity(canonical_name="Acme", slug="acme"),
        what=What(),
        vocabulary=Vocabulary(),
        disambiguation=Disambiguation(ambiguity_score=0.4),
        provenance=Provenance(generated_at=datetime.now(timezone.utc), runtime_ms=3),
    )


@pytest.fixture
def stub_t0(monkeypatch):
    """Replace the T0 stage so we test routing, not research."""
    seen = {}

    async def fake_stream(website, repo, form, name):
        seen.update(website=website, repo=repo, form=form, name=name)
        yield StageEvent(stage="map", status="done")
        yield ResultEvent(dossier=_dossier())

    monkeypatch.setattr(pipeline, "preprocess_stream", fake_stream)
    return seen


def test_pipeline_forwards_every_request_field_to_t0(stub_t0) -> None:
    async def go():
        return [e async for e in run_t0_stream(REQ)]

    asyncio.run(go())

    assert stub_t0 == {
        "website": "https://acme.dev",
        "repo": "acme/acme",
        "form": "a CI tool",
        "name": "Acme",
    }


def test_pipeline_passes_stage_events_through_untouched(stub_t0) -> None:
    async def go():
        return [e async for e in run_t0_stream(REQ)]

    events = asyncio.run(go())

    assert isinstance(events[0], StageEvent)
    assert isinstance(events[-1], ResultEvent)


def test_run_t0_collects_just_the_dossier(stub_t0) -> None:
    dossier = asyncio.run(run_t0(REQ))

    assert dossier.identity.canonical_name == "Acme"


def test_run_analysis_passes_dossier_to_scraper_and_analyzer(stub_t0, monkeypatch) -> None:
    signal = Signal(
        platform="x",
        signal_id="1",
        url="https://x.com/acme/status/1",
        title="",
        body="Acme crashes",
        author="user",
        score="0",
        engagement=Engagement(),
        scraped_at="2026-01-01T00:00:00Z",
        raw={},
    )
    seen = {}

    def fake_scrape_x(**kwargs):
        seen["scraper_dossier"] = kwargs["dossier"]
        return [signal]

    async def fake_analyze_results(signals, dossier):
        seen["analysis"] = (signals, dossier)
        return SignalFindings([], [], [])

    monkeypatch.setattr(pipeline, "scrape_x", fake_scrape_x)
    monkeypatch.setattr(pipeline, "analyze_results", fake_analyze_results)

    result = asyncio.run(run_analysis(REQ))

    assert seen["scraper_dossier"] is result.dossier
    assert seen["analysis"] == ([signal], result.dossier)
    assert result.signals == [signal]


def test_run_analysis_stops_on_non_signal_scraper_output(stub_t0, monkeypatch) -> None:
    monkeypatch.setattr(pipeline, "scrape_x", lambda **_kwargs: [{"not": "a signal"}])

    with pytest.raises(TypeError, match="assets.Signal"):
        asyncio.run(run_analysis(REQ))


def test_run_t0_raises_when_no_dossier_arrives(monkeypatch) -> None:
    async def only_stages(website, repo, form, name):
        yield StageEvent(stage="map", status="done")

    monkeypatch.setattr(pipeline, "preprocess_stream", only_stages)

    with pytest.raises(RuntimeError, match="without producing a dossier"):
        asyncio.run(run_t0(REQ))


def test_preflight_surfaces_missing_credentials(monkeypatch) -> None:
    monkeypatch.setattr(preprocess, "get_settings", lambda: preprocess.Settings(
        firecrawl_api_key="", exa_api_key="", xai_api_key=""
    ))

    with pytest.raises(MissingCredentials):
        preflight()


def test_http_layer_does_not_reach_past_pipeline() -> None:
    """main.py must not import stage internals directly."""
    source = (main.__file__ and open(main.__file__).read()) or ""

    assert "from preprocess import" not in source
    assert "from pipeline import" in source
