"""FastAPI surface for field-note.

HTTP concerns only: request validation, SSE framing, status codes. All T0
research logic lives in preprocess.py and is reachable without going through
the network.
"""

from collections.abc import AsyncIterator

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse

from pipeline import MissingCredentials, preflight, run_stream
from schema import ErrorEvent, Event, PreprocessRequest
from scraping.routes import router as scrape_router

app = FastAPI(
    title="Cursor Hackathon API",
    version="0.1.0",
)
app.include_router(scrape_router)

# demo_server.py flips this to "demo". Reported by /health so a client can tell
# a real backend from a canned one before trusting its output.
MODE = "live"


@app.get("/")
async def root() -> dict[str, str]:
    """Return a simple greeting."""
    return {"message": "Hello from cursor-hackathon!"}


@app.get("/health")
async def health() -> dict[str, str]:
    """Report whether the API is available, and whether it returns real data."""
    return {"status": "ok", "mode": MODE}


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
                    "non-fatal `error` events report degraded sources, "
                    "`heartbeat` keeps a long scrape alive, `dossier` and "
                    "`harvest` carry each stage's output as it lands, and a "
                    "final `result` event carries the whole FieldNote."
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
        preflight()  # fail before any spend, while we can still set a status
    except MissingCredentials as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    return StreamingResponse(
        as_sse(run_stream(req)),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
