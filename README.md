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

## Running without API keys

`demo_server.py` serves the real app with only `preprocess_stream` swapped for
a canned Cursor dossier — genuine routing, preflight, SSE framing and pydantic
models, no credits spent. Useful for checking the plumbing, and for rehearsing
a demo without depending on three external APIs.

```bash
uv run uvicorn demo_server:app --port 8000
```

It returns the same dossier whatever you ask for, so never point a real run at it.

## Test

```bash
uv run pytest
```

## Configure

T0 needs three keys. Copy `.env.example` to `.env` and fill them in — the
service refuses to start a run without all three rather than spending credits
on a pipeline that cannot finish.

```bash
cp .env.example .env
```

## Endpoints

- `GET /` — returns a greeting
- `GET /health` — returns API health
- `POST /preprocess` — **T0 product understanding**, streamed as SSE

### `POST /preprocess`

Body: `{"website": "...", "name": "...", "repo": "owner/repo", "form": "..."}`.
Only `website` is required; the repo is deliberately optional so users need not
expose private repositories at onboarding.

Responds with `text/event-stream`:

- `stage` — progress per pipeline stage
- `error` — a degraded source (`fatal: false`) or an aborted run (`fatal: true`)
- `result` — the finished `ProductDossier`

```bash
curl -N -X POST localhost:8000/preprocess \
  -H 'content-type: application/json' \
  -d '{"website":"https://cursor.com","repo":"getcursor/cursor"}'
```

## Layering

| File | Owns |
|---|---|
| `main.py` | HTTP: validation, SSE framing, status codes |
| `pipeline.py` | Which stages run, in what order |
| `preprocess.py` | The T0 implementation |
| `schema.py` | Contracts |

Every route goes through `pipeline.py`, so `main.py` never imports a stage
directly. Adding T1-T5 is a change in `pipeline.py` alone — their events join
the same stream and neither the HTTP layer nor the CLI has to change to pick
them up.

```python
from pipeline import run_t0          # -> ProductDossier
from pipeline import run_t0_stream   # -> AsyncIterator[Event]
from pipeline import preflight       # credential check, before any spend
```

## How T0 works

`preprocess.py` owns the whole stage and knows nothing about HTTP, so it stays
callable directly in tests and notebooks:

```python
from preprocess import preprocess          # -> ProductDossier
from preprocess import preprocess_stream   # -> AsyncIterator[Event]
```

Pipeline, budgeted to finish in about a minute:

1. **Ground truth**, all in parallel — Firecrawl maps the site and scrapes the
   homepage and repo page; a dependency manifest is read straight off
   `raw.githubusercontent.com`; Exa searches the bare product name and finds
   semantically adjacent pages.
2. **Deep read** — the sitemap is ranked (`/pricing` and `/docs` win, dated blog
   archives lose) and the best few pages are scraped.
3. **Synthesis** — one `grok-4.6` call over the gathered evidence.

Any single source failing degrades the run instead of ending it; what broke is
recorded in `provenance.degraded_sources`.

### Collisions are observed, not recalled

Asking a model "what else is called Foo?" invites confabulation, and a
fabricated namesake would misdirect every downstream scrape. So the bare-name
search results are partitioned by domain, and whatever the product doesn't own
becomes the collision set. `ambiguity_score` is arithmetic over that partition.

As a backstop, any collision citing a URL we never actually fetched is dropped
before the dossier is returned, and the drop is recorded in
`provenance.degraded_sources`.

## Regenerating the Go client's types

The `/preprocess` route streams, so it declares no response model and the
dossier schema would never reach `components.schemas` on its own. This exports
it straight from the pydantic models instead:

```bash
uv run python scripts/export_openapi.py > openapi.json
```
