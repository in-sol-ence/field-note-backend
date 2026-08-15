"""FastAPI surface for field-note.

HTTP concerns only: request validation, SSE framing, status codes. All T0
research logic lives in preprocess.py and is reachable without going through
the network.
"""

from collections.abc import AsyncIterator

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse

from preprocess import MissingCredentials, preprocess_stream, require_keys
from schema import ErrorEvent, Event, PreprocessRequest

app = FastAPI(
    title="Cursor Hackathon API",
    version="0.1.0",
)


@app.get("/")
async def root() -> dict[str, str]:
    """Return a simple greeting."""
    return {"message": "Hello from cursor-hackathon!"}


@app.get("/health")
async def health() -> dict[str, str]:
    """Report whether the API is available."""
    return {"status": "ok"}


def _frame(event: Event) -> str:
    return f"event: {event.event}\ndata: {event.model_dump_json()}\n\n"


async def as_sse(events: AsyncIterator[Event]) -> AsyncIterator[str]:
    """Frame events as SSE, converting a mid-stream blowup into a fatal event.

    Once the response has started we can no longer change the status code, so
    the client learns about failures through the stream itself.
    """
    try:
        async for event in events:
            yield _frame(event)
    except Exception as exc:  # noqa: BLE001
        yield _frame(ErrorEvent(detail=f"{type(exc).__name__}: {exc}"[:500], fatal=True))


@app.post(
    "/preprocess",
    openapi_extra={
        "responses": {
            "200": {
                "description": (
                    "Server-sent events. `stage` events report progress, "
                    "non-fatal `error` events report degraded sources, and a "
                    "final `result` event carries the ProductDossier."
                ),
                "content": {"text/event-stream": {"schema": {"type": "string"}}},
            }
        }
    },
)
async def run_preprocess(req: PreprocessRequest) -> StreamingResponse:
    """Run T0 product understanding, streaming progress as it goes."""
    if not req.website or not req.website.strip():
        raise HTTPException(status_code=422, detail="website is required")
    try:
        require_keys()  # fail before any spend, while we can still set a status
    except MissingCredentials as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    return StreamingResponse(
        as_sse(preprocess_stream(req.website, req.repo, req.form, req.name)),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
