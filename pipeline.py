"""Stage orchestration for the field-note pipeline.

main.py talks only to this module. Individual stages — preprocess.py for T0,
sources.py + harvest.py for T1, and T2-T5 as they land — are reached through
here, so adding or reordering a stage is a change in this file rather than in
the HTTP layer.

Layering:

    main.py       HTTP: validation, SSE framing, status codes
    pipeline.py   which stages run, in what order      <- you are here
    preprocess.py T0 implementation
    sources.py    T1 source selection
    harvest.py    T1 scraping
    schema.py     contracts

Every run ends with exactly one ResultEvent carrying a FieldNote. Stages fill
in their own field of it: T0 the dossier, T1 the harvest.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from harvest import harvest_stream
from preprocess import MissingCredentials, preprocess_stream, require_keys
from schema import (
    DossierEvent,
    ErrorEvent,
    Event,
    FieldNote,
    Harvest,
    HarvestEvent,
    PreprocessRequest,
    ProductDossier,
    ResultEvent,
    StageEvent,
)
from sources import fallback_targets, select_targets

__all__ = [
    "MissingCredentials",
    "preflight",
    "run_stream",
    "run_t0",
    "run_t0_stream",
]


def preflight() -> None:
    """Check credentials before a run starts.

    Called while the HTTP layer can still choose a status code — once the SSE
    response has begun, failures can only be reported inside the stream.
    """
    require_keys()


async def run_t0_stream(req: PreprocessRequest) -> AsyncIterator[Event]:
    """T0 — product understanding, streamed."""
    async for event in preprocess_stream(
        website=req.website,
        repo=req.repo,
        form=req.form,
        name=req.name,
    ):
        yield event


async def run_t1_stream(dossier: ProductDossier) -> AsyncIterator[Event]:
    """T1 — pick sources from the dossier, then scrape them.

    Source selection failing is not fatal: the dossier alone is enough to build
    a weaker set of targets, and a weak scrape beats no scrape.
    """
    yield StageEvent(stage="select_sources", status="running")
    try:
        targets = await select_targets(dossier)
    except Exception as exc:  # noqa: BLE001 - degrade, don't die
        yield ErrorEvent(stage="select_sources", detail=str(exc)[:300], fatal=False)
        targets = fallback_targets(dossier)

    detail = (
        f"{len(targets.reddit.subreddits)} subreddits, "
        f"{len(targets.reddit.search_queries) + len(targets.hackernews.search_queries)} queries"
    )
    yield StageEvent(stage="select_sources", status="done", detail=detail)

    async for event in harvest_stream(targets):
        yield event


async def run_stream(req: PreprocessRequest) -> AsyncIterator[Event]:
    """The whole pipeline. T1's events follow T0's on the same stream.

    Adding T2 is another `async for` below and one more field on FieldNote —
    neither main.py nor the CLI has to change to carry it.
    """
    dossier: ProductDossier | None = None
    harvest: Harvest | None = None

    async for event in run_t0_stream(req):
        if isinstance(event, DossierEvent):
            dossier = event.dossier
        yield event

    if dossier is None:
        yield ErrorEvent(detail="T0 ended without producing a dossier", fatal=True)
        return

    if req.stop_after != "t0":
        async for event in run_t1_stream(dossier):
            if isinstance(event, HarvestEvent):
                harvest = event.harvest
            yield event

    yield ResultEvent(note=FieldNote(dossier=dossier, harvest=harvest))


async def run_t0(req: PreprocessRequest) -> ProductDossier:
    """T0 without progress reporting, for callers that just want the dossier."""
    async for event in run_t0_stream(req):
        if isinstance(event, DossierEvent):
            return event.dossier
    raise RuntimeError("pipeline ended without producing a dossier")
