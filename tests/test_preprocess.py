"""Pipeline behaviour with every network source mocked out."""

import asyncio
from types import SimpleNamespace

import pytest

import preprocess
from preprocess import MissingCredentials, Settings, preprocess_stream
from schema import (
    DisambiguationDraft,
    ErrorEvent,
    Identity,
    NameCollision,
    DossierEvent,
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

    async def fake_synth(prompt, blocks):
        state["prompt"] = prompt
        yield f"fake-model · drafting from {blocks} evidence blocks"
        yield state["draft"]

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
    return next(e.dossier for e in events if isinstance(e, DossierEvent))


def test_happy_path_produces_a_dossier(wired) -> None:
    events = _run(website=SITE, repo="acme/acme", name="Acme")
    dossier = _dossier(events)

    assert dossier.identity.canonical_name == "Acme"
    assert dossier.what.category == "developer tool"
    assert dossier.provenance.runtime_ms >= 0
    assert dossier.provenance.field_confidence == {"what.category": 0.9}


def test_repo_alone_produces_a_dossier(wired) -> None:
    events = _run(website=None, repo="acme/acme")
    dossier = _dossier(events)

    assert dossier.identity.canonical_name == "Acme"
    stages = {e.stage for e in events if isinstance(e, StageEvent)}
    # nothing site-shaped is attempted without a site
    assert "map" not in stages and "find_similar" not in stages
    assert "scrape_repo" in stages and "search_collisions" in stages
    # the repo name seeds the collision hunt, and no host is excluded
    assert ("acme", None) in wired["searches"]
    assert "WEBSITE:" not in wired["prompt"]
    assert "GITHUB REPO: acme/acme" in wired["prompt"]


def test_ambiguity_counts_only_hits_on_identified_rivals(wired) -> None:
    dossier = _dossier(_run(website=SITE, name="Acme"))

    # One collision, on wikipedia.org, matching 1 of the 4 bare-name hits.
    # Measured on the UNFILTERED search: scoring the rival pass, which excludes
    # the product's own domain by construction, would inflate every product.
    assert dossier.disambiguation.ambiguity_score == 0.25


def test_ambiguity_is_zero_when_no_collision_survives(wired) -> None:
    wired["draft"] = _draft()  # model found nothing sharing the name

    dossier = _dossier(_run(website=SITE, name="Acme"))

    assert dossier.disambiguation.ambiguity_score == 0.0
    assert dossier.disambiguation.name_collisions == []


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

    assert isinstance(events[-1], DossierEvent)
    done = {e.stage for e in events if isinstance(e, StageEvent) and e.status == "done"}
    assert {"map", "scrape_site", "search_collisions", "synthesize"} <= done


def test_synthesis_reports_what_it_is_waiting_on(wired) -> None:
    """Synthesis is the run's longest single wait, so it has to say more than
    its own name while the model is thinking."""
    events = _run(website=SITE, name="Acme")

    notes = [
        e.detail
        for e in events
        if isinstance(e, StageEvent) and e.stage == "synthesize" and e.status == "running"
    ]
    assert any(n and "drafting" in n for n in notes), notes


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


# ---- synthesis, streamed -------------------------------------------------


def _fake_llm(text: str, size: int = 9):
    """A client whose completion streams `text` back in fixed-size chunks."""

    async def create(**kwargs):
        assert kwargs["stream"], "synthesis must stream or it cannot report progress"

        async def chunks():
            for i in range(0, len(text), size):
                yield SimpleNamespace(
                    choices=[SimpleNamespace(delta=SimpleNamespace(content=text[i : i + size]))]
                )
            # Providers close with a usage-only chunk carrying no choices.
            yield SimpleNamespace(choices=[])

        return chunks()

    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))


def _synth(monkeypatch, text: str):
    monkeypatch.setattr(preprocess, "_llm", lambda: _fake_llm(text))
    monkeypatch.setattr(preprocess, "get_settings", lambda: Settings(
        firecrawl_api_key="k", exa_api_key="k", xai_api_key="k"
    ))

    async def go():
        return [step async for step in preprocess._synthesize(text, blocks=12)]

    return asyncio.run(go())


def test_synthesis_narrates_the_draft_as_the_model_writes_it(monkeypatch) -> None:
    """The run's longest wait has to keep saying what it is on. Streaming is
    what makes that possible: the sub-steps are the draft's own sections."""
    steps = _synth(monkeypatch, _draft(REAL_COLLISION).model_dump_json())

    assert isinstance(steps[-1], SynthesisDraft), "the draft still has to arrive whole"
    notes = [s for s in steps if isinstance(s, str)]
    assert notes[0] == "grok-4.6 · reading 12 evidence blocks"
    assert any("naming the product" in n for n in notes), notes
    assert any("sorting out name collisions" in n for n in notes), notes
    # The point of the exercise: the line the operator reads keeps changing.
    assert len(set(notes)) >= 4, notes


def test_a_malformed_draft_says_it_is_repairing_rather_than_going_quiet(monkeypatch) -> None:
    with pytest.raises(ValueError, match="after repair"):
        _synth(monkeypatch, '{"identity": {"canonical_name": "Acme"}}')


def test_the_repair_pass_is_announced(monkeypatch) -> None:
    notes = []
    monkeypatch.setattr(preprocess, "_llm", lambda: _fake_llm('{"identity": {}}'))
    monkeypatch.setattr(preprocess, "get_settings", lambda: Settings(
        firecrawl_api_key="k", exa_api_key="k", xai_api_key="k"
    ))

    async def go():
        async for step in preprocess._synthesize("prompt", blocks=3):
            notes.append(step)

    with pytest.raises(ValueError):
        asyncio.run(go())
    assert any("repairing the schema" in n for n in notes), notes


def _no_stream_llm(text: str, fail_after: int = 0):
    """A client that refuses to stream, or dies `fail_after` chunks in, and
    answers the plain request normally."""

    async def create(**kwargs):
        if not kwargs.get("stream"):
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content=text))]
            )

        async def chunks():
            for i in range(fail_after):
                yield SimpleNamespace(
                    choices=[SimpleNamespace(delta=SimpleNamespace(content=text[i : i + 1]))]
                )
            raise RuntimeError("stream_unsupported: response_format with stream")

        if fail_after:
            return chunks()
        raise RuntimeError("stream_unsupported: response_format with stream")

    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))


def _settings(monkeypatch) -> None:
    monkeypatch.setattr(preprocess, "get_settings", lambda: Settings(
        firecrawl_api_key="k", exa_api_key="k", xai_api_key="k"
    ))


def test_a_provider_that_will_not_stream_still_produces_a_dossier(monkeypatch) -> None:
    """The notes are a nicety, the dossier is the product: losing progress text
    must not lose the run."""
    text = _draft(REAL_COLLISION).model_dump_json()
    monkeypatch.setattr(preprocess, "_llm", lambda: _no_stream_llm(text))
    _settings(monkeypatch)

    async def go():
        return [step async for step in preprocess._synthesize(text, blocks=12)]

    steps = asyncio.run(go())

    assert isinstance(steps[-1], SynthesisDraft)
    notes = [s for s in steps if isinstance(s, str)]
    # And it says so, with the reason — a silent fallback is the blind minute
    # this whole change exists to remove.
    assert any("drafting blind" in n and "stream_unsupported" in n for n in notes), notes


def test_a_stream_that_dies_mid_draft_is_not_retried_blind(monkeypatch) -> None:
    # Half a draft already cost half the tokens. Paying again for the same
    # call is worse than surfacing the failure.
    text = _draft(REAL_COLLISION).model_dump_json()
    monkeypatch.setattr(preprocess, "_llm", lambda: _no_stream_llm(text, fail_after=20))
    _settings(monkeypatch)

    async def go():
        return [step async for step in preprocess._synthesize(text, blocks=12)]

    with pytest.raises(RuntimeError, match="stream_unsupported"):
        asyncio.run(go())


def test_page_reads_do_not_wait_for_the_slowest_stage_a_job(wired, monkeypatch) -> None:
    """The deep read depends only on the sitemap, so it must start while the
    other stage-A jobs are still in flight. find_similar refuses to finish
    until a page has been scraped: under a stage barrier this deadlocks."""
    page_scraped = asyncio.Event()

    async def fake_scrape(url):
        page_scraped.set()
        return f"content of {url}"

    async def fake_similar(url, n=10):
        await asyncio.wait_for(page_scraped.wait(), timeout=2)
        return [("https://rival.example/", "Rival", "competitor")]

    monkeypatch.setattr(preprocess, "_scrape", fake_scrape)
    monkeypatch.setattr(preprocess, "_exa_similar", fake_similar)

    events = _run(website=SITE, name="Acme")

    assert isinstance(events[-1], DossierEvent)
    done = {e.stage for e in events if isinstance(e, StageEvent) and e.status == "done"}
    assert "find_similar" in done


def test_contextual_search_still_runs_when_the_homepage_scrape_fails(
    wired, monkeypatch
) -> None:
    async def broken_scrape_meta(url):
        raise RuntimeError("homepage unreachable")

    monkeypatch.setattr(preprocess, "_scrape_meta", broken_scrape_meta)

    events = _run(website=SITE, name="Acme")

    done = {e.stage for e in events if isinstance(e, StageEvent) and e.status == "done"}
    assert "search_context" in done
    # with no metadata the query falls back to the bare name
    assert ("Acme Acme", None) in wired["searches"]
