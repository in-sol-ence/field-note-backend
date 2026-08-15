"""Pipeline behaviour with every network source mocked out."""

import asyncio

import pytest

import preprocess
from preprocess import MissingCredentials, Settings, preprocess_stream
from schema import (
    DisambiguationDraft,
    ErrorEvent,
    Identity,
    NameCollision,
    ResultEvent,
    StageEvent,
    SynthesisDraft,
    Vocabulary,
    What,
)

SITE = "https://acme.dev"
BARE_HITS = [
    ("https://acme.dev/", "Acme", "the product"),
    ("https://en.wikipedia.org/wiki/Acme_Corporation", "Acme Corporation", "cartoon"),
    ("https://acmebank.example/", "Acme Bank", "a bank"),
    ("https://acmepaint.example/", "Acme Paint", "paint"),
]

REAL_COLLISION = NameCollision(
    name="Acme Corporation",
    what_it_is="fictional cartoon company",
    evidence_url="https://en.wikipedia.org/wiki/Acme_Corporation",
)
FABRICATED = NameCollision(
    name="Acme Airlines",
    what_it_is="invented from thin air",
    evidence_url="https://never-fetched.example/airlines",
)


def _draft(*collisions: NameCollision) -> SynthesisDraft:
    return SynthesisDraft(
        identity=Identity(canonical_name="Acme", slug="acme", homepage=SITE),
        what=What(category="developer tool", description="does things"),
        vocabulary=Vocabulary(feature_jargon=["acmeql"]),
        disambiguation=DisambiguationDraft(name_collisions=list(collisions)),
        field_confidence={"what.category": 0.9},
    )


@pytest.fixture
def wired(monkeypatch):
    """Patch every network edge. Returns a dict tests can tweak."""
    state = {"draft": _draft(REAL_COLLISION), "map": ["https://acme.dev/pricing"]}

    async def fake_map(url, limit=150):
        value = state["map"]
        if isinstance(value, Exception):
            raise value
        return value

    async def fake_scrape_meta(url):
        return "# Acme\nAcme is a developer tool.", {"description": "developer tool for teams"}

    async def fake_scrape(url):
        return f"content of {url}"

    async def fake_search(query, n=10, exclude_domains=None):
        state.setdefault("searches", []).append((query, exclude_domains))
        if exclude_domains:
            # the rival pass: product-owned hits are filtered out server-side
            owned = set(exclude_domains)
            return [h for h in BARE_HITS if not any(o in h[0] for o in owned)]
        return BARE_HITS

    async def fake_similar(url, n=10):
        return [("https://rival.example/", "Rival", "competitor")]

    async def fake_manifest(repo):
        return (f"https://raw.githubusercontent.com/{repo}/HEAD/package.json", '{"name":"acme"}')

    async def fake_synth(prompt):
        state["prompt"] = prompt
        return state["draft"]

    monkeypatch.setattr(preprocess, "require_keys", lambda: Settings(
        firecrawl_api_key="k", exa_api_key="k", xai_api_key="k"
    ))
    monkeypatch.setattr(preprocess, "_map_site", fake_map)
    monkeypatch.setattr(preprocess, "_scrape_meta", fake_scrape_meta)
    monkeypatch.setattr(preprocess, "_scrape", fake_scrape)
    monkeypatch.setattr(preprocess, "_exa_search", fake_search)
    monkeypatch.setattr(preprocess, "_exa_similar", fake_similar)
    monkeypatch.setattr(preprocess, "_fetch_manifest", fake_manifest)
    monkeypatch.setattr(preprocess, "_synthesize", fake_synth)
    return state


def _run(**kwargs):
    async def go():
        return [e async for e in preprocess_stream(**kwargs)]

    return asyncio.run(go())


def _dossier(events):
    return next(e.dossier for e in events if isinstance(e, ResultEvent))


def test_happy_path_produces_a_dossier(wired) -> None:
    events = _run(website=SITE, repo="acme/acme", name="Acme")
    dossier = _dossier(events)

    assert dossier.identity.canonical_name == "Acme"
    assert dossier.what.category == "developer tool"
    assert dossier.provenance.runtime_ms >= 0
    assert dossier.provenance.field_confidence == {"what.category": 0.9}


def test_ambiguity_reflects_unowned_share_of_the_namespace(wired) -> None:
    dossier = _dossier(_run(website=SITE, name="Acme"))

    # 3 of the 4 bare-name hits sit on domains Acme does not own. This must be
    # measured on the UNFILTERED search: scoring the rival pass would report
    # ~1.0 for every product on earth.
    assert dossier.disambiguation.ambiguity_score == 0.75


def test_collision_probe_runs_both_an_open_and_a_domain_excluded_search(wired) -> None:
    _run(website=SITE, name="Acme")

    bare_name = [s for s in wired["searches"] if s[0] == "Acme"]
    assert len(bare_name) == 2, "expected an unfiltered pass and a rival pass"
    assert {s[1] is None for s in bare_name} == {True, False}
    assert ["acme.dev"] in [s[1] for s in bare_name]


def test_rival_hits_are_citable_so_the_guard_does_not_drop_them(wired) -> None:
    real = NameCollision(
        name="Acme Bank",
        what_it_is="a bank",
        evidence_url="https://acmebank.example/",
    )
    wired["draft"] = _draft(real)

    dossier = _dossier(_run(website=SITE, name="Acme"))

    assert [c.name for c in dossier.disambiguation.name_collisions] == ["Acme Bank"]


def test_stream_reports_stages_and_ends_with_result(wired) -> None:
    events = _run(website=SITE, repo="acme/acme", name="Acme")

    assert isinstance(events[-1], ResultEvent)
    done = {e.stage for e in events if isinstance(e, StageEvent) and e.status == "done"}
    assert {"map", "scrape_site", "search_collisions", "synthesize"} <= done


def test_map_failure_degrades_instead_of_dying(wired) -> None:
    wired["map"] = RuntimeError("firecrawl exploded")

    events = _run(website=SITE, name="Acme")
    dossier = _dossier(events)

    assert any(isinstance(e, ErrorEvent) and not e.fatal for e in events)
    assert any("firecrawl exploded" in d for d in dossier.provenance.degraded_sources)
    assert dossier.identity.canonical_name == "Acme"


def test_fabricated_collision_never_reaches_the_dossier(wired) -> None:
    wired["draft"] = _draft(REAL_COLLISION, FABRICATED)

    dossier = _dossier(_run(website=SITE, name="Acme"))

    names = [c.name for c in dossier.disambiguation.name_collisions]
    assert names == ["Acme Corporation"]
    assert any("Acme Airlines" in d for d in dossier.provenance.degraded_sources)


def test_operator_form_is_passed_to_synthesis(wired) -> None:
    _run(website=SITE, name="Acme", form="We are a CI tool for monorepos.")

    assert "CI tool for monorepos" in wired["prompt"]


def test_runs_without_a_repo(wired) -> None:
    dossier = _dossier(_run(website=SITE, name="Acme"))

    assert dossier.identity.canonical_name == "Acme"


def test_missing_credentials_fails_before_any_spend(monkeypatch) -> None:
    monkeypatch.setattr(preprocess, "get_settings", lambda: Settings(
        firecrawl_api_key="", exa_api_key="", xai_api_key=""
    ))

    with pytest.raises(MissingCredentials) as exc:
        _run(website=SITE, name="Acme")

    assert "FIRECRAWL_API_KEY" in str(exc.value)
