# social-signals-lite

Drop-in stand-in for the private `social-signals` service so Fieldnotes T1 can
scrape **live Hacker News** (Algolia) and **live Reddit** (Playwright) without
that monorepo.

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
