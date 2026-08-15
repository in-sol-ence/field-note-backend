"""Stage orchestration for the field-note pipeline.

main.py talks only to this module. Individual stages — preprocess.py for T0,
and T1-T5 as they land — are reached through here, so adding or reordering a
stage is a change in this file rather than in the HTTP layer.

Layering:

    main.py       HTTP: validation, SSE framing, status codes
    pipeline.py   which stages run, in what order      <- you are here
    preprocess.py T0 implementation
    schema.py     contracts
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from preprocess import MissingCredentials, preprocess_stream, require_keys
from schema import Event, PreprocessRequest, ProductDossier, ResultEvent

__all__ = [
    "MissingCredentials",
    "preflight",
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
    """T0 — product understanding, streamed.

    When T1 lands its events follow T0's on this same stream, so neither
    main.py nor the CLI needs to change to pick them up.
    """
    async for event in preprocess_stream(
        website=req.website,
        repo=req.repo,
        form=req.form,
        name=req.name,
    ):
        yield event


async def run_t0(req: PreprocessRequest) -> ProductDossier:
    """T0 without progress reporting, for callers that just want the dossier."""
    async for event in run_t0_stream(req):
        if isinstance(event, ResultEvent):
            return event.dossier
    raise RuntimeError("pipeline ended without producing a dossier")
