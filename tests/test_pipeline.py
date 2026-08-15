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
    DossierEvent,
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
        yield DossierEvent(dossier=_dossier())

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
    assert isinstance(events[-1], DossierEvent)


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


# --------------------------------------------------------------------------
# T0 -> T1 chaining
# --------------------------------------------------------------------------


@pytest.fixture
def stub_t1(monkeypatch):
    """Replace both halves of T1 so we test the chain, not the scraper."""
    from schema import Harvest, HarvestEvent, RedditTargets, ScrapeTargets

    picked = ScrapeTargets(reddit=RedditTargets(subreddits=["acme"]))

    async def fake_select(dossier):
        return picked

    async def fake_harvest(targets, **kw):
        yield StageEvent(stage="scrape_reddit", status="done", detail="2 signals")
        yield HarvestEvent(
            harvest=Harvest(targets=targets, posts=[], live=True, source_note="live scrape")
        )

    monkeypatch.setattr(pipeline, "select_targets", fake_select)
    monkeypatch.setattr(pipeline, "harvest_stream", fake_harvest)
    return picked


def _kinds(events):
    return [e.event for e in events]


def test_a_full_run_ends_with_one_result_carrying_both_stages(stub_t0, stub_t1) -> None:
    from schema import ResultEvent

    events = asyncio.run(_collect(REQ))

    assert _kinds(events).count("result") == 1
    assert isinstance(events[-1], ResultEvent)
    note = events[-1].note
    assert note.dossier.identity.canonical_name == "Acme"
    assert note.harvest is not None
    assert note.harvest.targets == stub_t1


def test_the_dossier_is_published_before_the_scrape_starts(stub_t0, stub_t1) -> None:
    """T1 takes minutes. Holding a finished dossier until the end would leave
    the CLI with nothing to render while it waits."""
    kinds = _kinds(asyncio.run(_collect(REQ)))
    assert kinds.index("dossier") < kinds.index("harvest")


def test_stop_after_t0_skips_the_scrape_entirely(stub_t0, stub_t1) -> None:
    events = asyncio.run(_collect(REQ.model_copy(update={"stop_after": "t0"})))

    assert "harvest" not in _kinds(events)
    assert events[-1].note.harvest is None
    assert events[-1].note.dossier.identity.canonical_name == "Acme"


def test_source_selection_failing_degrades_to_fallback_targets(stub_t0, stub_t1, monkeypatch) -> None:
    """A dead LLM call costs the run its best targets, not the run."""
    from schema import ErrorEvent

    async def boom(dossier):
        raise RuntimeError("xai timed out")

    monkeypatch.setattr(pipeline, "select_targets", boom)
    events = asyncio.run(_collect(REQ))

    warnings = [e for e in events if isinstance(e, ErrorEvent)]
    assert warnings and not any(w.fatal for w in warnings)
    assert events[-1].note.harvest is not None, "the scrape still ran"


def test_a_t0_that_never_produces_a_dossier_ends_the_run_fatally(monkeypatch, stub_t1) -> None:
    from schema import ErrorEvent

    async def empty(website, repo, form, name):
        yield StageEvent(stage="map", status="failed")

    monkeypatch.setattr(pipeline, "preprocess_stream", empty)
    events = asyncio.run(_collect(REQ))

    assert isinstance(events[-1], ErrorEvent)
    assert events[-1].fatal is True


async def _collect(req):
    return [e async for e in pipeline.run_stream(req)]
