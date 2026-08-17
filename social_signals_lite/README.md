# social-signals-lite

**Temporary substitute — replace this.** Drop-in stand-in for the private
`social-signals` service so Fieldnotes T1 can scrape **live Hacker News**
(Algolia) and **live Reddit** (Playwright) without that monorepo.

When you have access to the private social-signals repo, run **that** on
`:8899` (or point `SOCIAL_SIGNALS_URL` / `FIELDNOTE_SCRAPER` at it) and stop
using lite. Do not treat this package as the long-term scraper.

## Substack (topic-first)

Unofficial public JSON — not an official API. Harvests **articles and
comments** for a topic:

```bash
curl -sS http://127.0.0.1:8899/v1/jobs/watch \
  -H 'Authorization: Bearer demo-key' -H 'Content-Type: application/json' \
  -d '{"platform":"substack","per_target_limit":8,"targets":{"substack":{"topics":["AI coding assistants"]}},"include_signals":true}'
```

Or via the backend:

```bash
curl -sS -X POST http://127.0.0.1:8000/scrape/social \
  -H 'content-type: application/json' \
  -d '{"platforms":["substack"],"topic":"AI coding assistants","per_target_limit":8}'
```

`topic` is the primary key. Optional `publications` pin known newsletters.
Paid post bodies are empty without a session; comments are public threads.

## Run

```bash
./scripts/run_social_signals_lite.sh
# → http://127.0.0.1:8899/health
```

Env:

| Variable | Default |
| --- | --- |
| `SOCIAL_SIGNALS_API_KEY` | `demo-key` |
| `SOCIAL_SIGNALS_PORT` | `8899` |
| `REDDIT_COOKIES_FILE` | optional Playwright JSON cookie array |
| `PAGE_PAUSE` / `SCROLL_PAUSE` | `4` / `2` (seconds) |

`run.sh` in `cursor-grok-hackathon` auto-starts this when `:8899` is down.

## Contract

Same subset harvest needs:

- `GET /health`
- `POST /v1/jobs/watch` + `GET /v1/jobs/{id}` (Bearer auth)
- optional `POST /v1/sessions/reddit/cookies`

## Cookies

Reddit works anonymously here after the JS challenge, but logged-in cookies
improve reliability. Upload Playwright JSON:

```bash
curl -X POST http://127.0.0.1:8899/v1/sessions/reddit/cookies \
  -H 'Authorization: Bearer demo-key' -H 'Content-Type: application/json' \
  -d "{\"content\": $(jq -c -Rs . < ~/.social-signals-reddit-cookies.json)}"
```
