# Cursor Hackathon API

A small FastAPI service managed with [uv](https://docs.astral.sh/uv/).

## Install

```bash
uv sync
```

## Run

```bash
uv run uvicorn main:app --reload
```

The API is available at <http://127.0.0.1:8000>. Interactive documentation is at <http://127.0.0.1:8000/docs>.

## Test

```bash
uv run pytest
```

## Endpoints

- `GET /` — returns a greeting
- `GET /health` — returns API health
