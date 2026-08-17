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

**T1 still scrapes live** in demo mode when a `:8899` scraper and `x-scraper`
are up — only T0 is canned. See `notes-gregory.md` §11 for the checklist to go
**fully live** (real dossier + live scrapes).

### Scrape stand-ins (replace these)

**social-signals-lite (this repo).** `social_signals_lite/` +
`scripts/run_social_signals_lite.sh` expose a `:8899`-compatible watch API so
T1 can get live Reddit/HN without the private **social-signals** checkout.
This is a **temporary substitute**. When you have the private social-signals
repo, run that instead and point `SOCIAL_SIGNALS_URL` / `FIELDNOTE_SCRAPER` at
it; retire lite.

**X via x-scraper.** Live X today goes through the Playwright `x-scraper`
puppet (`X_SCRAPER_ROOT` / `FIELDNOTE_X_SCRAPER`). That is **not** an official
X integration. Replace it with a client that uses a **real X API key**. Do
not ship the puppet as the production X path.

## Fully live (real dossier)

1. Copy `.env.example` → `.env` and set `XAI_API_KEY`, `FIRECRAWL_API_KEY`, `EXA_API_KEY`.
2. Start scrapers: prefer the **private social-signals** service on `:8899`;
   otherwise `./scripts/run_social_signals_lite.sh`. For X, prefer a real X
   API client; the Playwright puppet is `~/x-scraper`.
3. Run `main:app` (not demo), e.g. via the CLI **without** `--demo`:

```bash
uv run uvicorn main:app --port 8001
# or from cursor-grok-hackathon:
# FIELDNOTE_BACKEND_DIR=../field-note-backend FIELDNOTE_PORT=8001 ./run.sh --repo getcursor/cursor --url https://cursor.com
```

`/health` must report `"mode":"live"`.

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
- `POST /preprocess` — runs the pipeline (**T0** then **T1**), streamed as SSE

### `POST /preprocess`

Body: `{"website": "...", "name": "...", "repo": "owner/repo", "form": "...",
"stop_after": "t0"}`. Only `website` is required; the repo is deliberately
optional so users need not expose private repositories at onboarding.
`stop_after: "t0"` returns the dossier without scraping.

Responds with `text/event-stream`:

- `stage` — progress per pipeline stage
- `error` — a degraded source (`fatal: false`) or an aborted run (`fatal: true`)
- `heartbeat` — emitted while a scrape runs, so a multi-minute silence does not
  look like a hang or get a connection dropped by a proxy
- `dossier` — T0's `ProductDossier`, published as soon as it lands rather than
  held until the end, so a client has something to render during the scrape
- `harvest` — T1's chosen targets and scraped posts
- `result` — the terminal event, exactly one per run, carrying the whole
  `FieldNote`

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
| `preprocess.py` | T0 — product understanding |
| `sources.py` | T1 — which sources to scrape |
| `harvest.py` | T1 — scraping them, and the fixture fallback |
| `schema.py` | Contracts |

Every route goes through `pipeline.py`, so `main.py` never imports a stage
directly. Adding T1-T5 is a change in `pipeline.py` alone — their events join
the same stream and neither the HTTP layer nor the CLI has to change to pick
them up.

```python
from pipeline import run_stream      # -> AsyncIterator[Event], the whole pipeline
from pipeline import run_t0          # -> ProductDossier
from pipeline import run_t0_stream   # -> AsyncIterator[Event], T0 only
from pipeline import preflight       # credential check, before any spend
```

Every run ends with exactly one `ResultEvent` carrying a `FieldNote`. Each stage
fills in its own field of it — T0 the dossier, T1 the harvest — so adding T2 is
one more `async for` in `pipeline.py` and one more field on `FieldNote`.

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

## How T1 works

Two halves, deliberately split: choosing where to look is cheap, reversible and
worth testing; the scrape itself is slow and external.

### `sources.py` — where to look

One Grok call turns the dossier into subreddits and search queries. This is
where T0's disambiguation work is finally spent: the collisions, the negative
signals and the no-namesake jargon all go into the prompt, so a product with a
crowded name never gets a bare-name query.

Search queries matter more than subreddits. In the reference scrape 12 of 27
Reddit signals came from site-wide search, in subreddits nobody would have
listed up front — a subreddit-only scraper finds none of them.

The caps (4 subreddits, 4 Reddit queries, 3 HN queries) are applied to the
answer rather than asked for in the prompt, because a model told "at most four"
still returns seven. If the call fails, `fallback_targets` builds a weaker set
from the dossier alone — a weak scrape beats no scrape.

### `harvest.py` — doing the scrape

Drives social-signals over `POST /v1/jobs/watch`, polls it, and normalizes the
result through `scraping/mapper.py` into the frozen `Post` contract. Routing
through the mapper rather than `assets.Signal` is deliberate: the mapper
handles HackerNews, `assets.py` still does not.

Reddit takes 3-5 minutes per target and needs live browser cookies, which makes
it the least reliable thing in the pipeline. When it fails, the recorded
signals in `scraping/data/` are substituted rather than ending the run — but
never silently:

- `Harvest.live` goes `false`
- a non-fatal `error` event names the substitution
- the reason lands in `mapping_errors`

**Those recordings are about Perplexity.** A fallback run is for proving the
plumbing or rehearsing a demo, not for reading a real product's feedback. Pass
`allow_fixtures=False` to a caller that would rather have nothing than the
wrong product's data.

### Pointing at a scraper

```bash
SOCIAL_SIGNALS_URL=http://127.0.0.1:8899
SOCIAL_SIGNALS_API_KEY=demo-key
```

Neither is checked by `preflight` — an unreachable scraper degrades a run, it
does not stop one. See `scraping/README.md` for standing the service up.
