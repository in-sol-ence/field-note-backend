"""HTTP surface: SSE framing and status codes. No research logic here."""

import json

from fastapi.testclient import TestClient

import main
from main import app
from preprocess import MissingCredentials, Settings
from schema import (
    Disambiguation,
    Identity,
    ProductDossier,
    Provenance,
    ResultEvent,
    StageEvent,
    Vocabulary,
    What,
)
from datetime import datetime, timezone

client = TestClient(app)


def _dossier() -> ProductDossier:
    return ProductDossier(
        identity=Identity(canonical_name="Acme", slug="acme"),
        what=What(),
        vocabulary=Vocabulary(),
        disambiguation=Disambiguation(ambiguity_score=0.25),
        provenance=Provenance(generated_at=datetime.now(timezone.utc), runtime_ms=5),
    )


def _ok_keys():
    return Settings(firecrawl_api_key="k", exa_api_key="k", xai_api_key="k")


def _parse(body: str) -> list[tuple[str, dict]]:
    events = []
    for chunk in body.strip().split("\n\n"):
        if not chunk.strip():
            continue
        lines = dict(line.split(": ", 1) for line in chunk.splitlines())
        events.append((lines["event"], json.loads(lines["data"])))
    return events


def test_stream_frames_stage_events_then_result(monkeypatch) -> None:
    async def fake_stream(website, repo, form, name):
        yield StageEvent(stage="map", status="done", detail="3 pages found")
        yield ResultEvent(dossier=_dossier())

    monkeypatch.setattr(main, "require_keys", _ok_keys)
    monkeypatch.setattr(main, "preprocess_stream", fake_stream)

    with client.stream("POST", "/preprocess", json={"website": "https://acme.dev"}) as r:
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/event-stream")
        events = _parse("".join(r.iter_text()))

    assert [name for name, _ in events] == ["stage", "result"]
    assert events[0][1]["detail"] == "3 pages found"
    assert events[1][1]["dossier"]["identity"]["canonical_name"] == "Acme"


def test_midstream_failure_becomes_a_fatal_event(monkeypatch) -> None:
    async def boom(website, repo, form, name):
        yield StageEvent(stage="map", status="running")
        raise RuntimeError("grok fell over")

    monkeypatch.setattr(main, "require_keys", _ok_keys)
    monkeypatch.setattr(main, "preprocess_stream", boom)

    with client.stream("POST", "/preprocess", json={"website": "https://acme.dev"}) as r:
        events = _parse("".join(r.iter_text()))

    kind, payload = events[-1]
    assert kind == "error"
    assert payload["fatal"] is True
    assert "grok fell over" in payload["detail"]


def test_missing_credentials_returns_503(monkeypatch) -> None:
    def missing():
        raise MissingCredentials("missing required env var(s): EXA_API_KEY")

    monkeypatch.setattr(main, "require_keys", missing)

    response = client.post("/preprocess", json={"website": "https://acme.dev"})

    assert response.status_code == 503
    assert "EXA_API_KEY" in response.json()["detail"]


def test_website_is_required() -> None:
    response = client.post("/preprocess", json={})

    assert response.status_code == 422
