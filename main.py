from fastapi import FastAPI

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
