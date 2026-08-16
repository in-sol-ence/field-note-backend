---
title: Gregory — Fieldnotes session notes
author: gwild
date: 2026-08-15
tags:
  - fieldnotes
  - hackathon
  - scrape
  - x-scraper
aliases:
  - notes-gregory
---

# Fieldnotes — Gregory notes (2026-08-15)

Working notes for the team. Same plain markdown style as `Cursor Hackathon.md` / `SYSTEM.md` (readable in Obsidian or GitHub). **Do not paste API keys here.**

---

## 1. Plan vs execution (brief)

### Plan (from team brief)

| Stage | Intent |
| --- | --- |
| T0 | Product understanding from website (+ optional repo) |
| T1 | Choose sources; scrape Reddit / X / GitHub → shared post/signal objects |
| T2–T3 | Analyze → issues / features / love + dashboard health score |
| T4–T5 | Map to repo code → suggested fixes / MCP (deferred) |

Scrape bet in the room: **avoid paid X API** — Puppeteer / reverse-engineer. Shared contract: **`assets.Signal`**, model judgment vs code-built provenance (`enriched.RichIssue`).

### What executed today (Gregory’s lane)

| Goal | Outcome |
| --- | --- |
| Pick open-source X scraper, adapt as submodule | `gwild/x-scraper` → `field-note-backend/x-scraper` |
| Tweet → `assets.Signal` → Post | `x_signals` + `POST /scrape/x` |
| Wire into frontend console | Connect default source **x**; fixtures for openclaw/perplexity; force-live → Playwright |
| Keep demo alive when live path flakes | Fixtures, durable Next `/api/backend` proxy, API without `--reload` mid-scrape |
| HN / Reddit in Connect | Fixture-only via `POST /scrape/social` (live harvest exists in pipeline, not console) |

**North star** (web feedback → analyze repo → PR) is still T4/T5. **Today’s wedge:** scrape → Signal → UI themes, with one **real** live source (X).

---

## 2. Blow-by-blow — Gregory’s work today

One-page sequence (console + backend + scraper).

1. **Adopt scraper** — Evaluated open X scrapers; forked/adapted Proxidize lineage as **`gwild/x-scraper`**, wired as git submodule under field-note.
2. **Login / scrape reliability** — Playwright login hardening, DOM fallback when GraphQL is stale, session cookie save, `-n` count respected as session cap.
3. **Signal contract** — `tweets_to_signals` / fixtures → validated `assets.Signal`; mapper → Post (`scraping.mapper`).
4. **API** — `POST /scrape/x` (fixture \| x-scraper \| social-signals); later `POST /scrape/social` for HN/Reddit **fixtures**.
5. **Console** — Connect product + sources; default platforms include **x**; sweep → inbox from live Signals (not only canned demo themes).
6. **Regression** — HN/Reddit UI work briefly dropped X; restored X as default + guard comments/`DEFAULT_SCRAPE_PLATFORMS`.
7. **“Scrapes then fails”** — Root cause: uvicorn `--reload` watching backend tree; scraper writes (cookies/data) killed worker → Next `socket hang up` / Internal Server Error.
8. **Stabilizers** — Prefer scraper checkout **outside** watch tree (`~/x-scraper`); `run_api.sh` **without** `--reload`; scrape off event loop (`asyncio.to_thread`); Next **route handler** proxy with long timeout instead of short rewrite.
9. **Ship** — Commits/pushes on `crispy-pancake` (private) and `field-note-backend` (public), plus public `gwild/x-scraper`.
10. **Demo ops** — Fresh stack scripts; openclaw/perplexity fast path = fixtures unless force-live.

**Repos:** [field-note-backend](https://github.com/in-sol-ence/field-note-backend) · [crispy-pancake](https://github.com/anudeepadi/crispy-pancake) · [gwild/x-scraper](https://github.com/gwild/x-scraper)

---

## 3. One-pager — Sources: what’s live?

| Source | Console Connect | Backend capability |
| --- | --- | --- |
| **X** | On by default | Live Playwright via x-scraper **or** fixtures |
| **Force live** | Checkbox | Skips openclaw/perplexity fixtures → always x-scraper |
| **Hacker News** | Opt-in | **Fixtures only** in `/scrape/social` |
| **Reddit** | Opt-in (slow) | **Fixtures only** in `/scrape/social` |

Pipeline `harvest.py` can drive **live** Reddit/HN via private **social-signals** (`/v1/jobs/watch`) with fixture fallback — that path is **not** what Connect calls today.

**Takeaway:** Only X force-live (or non-fixture products on X) is “real” scrape in the UI. HN/Reddit UI = recorded demo data.

**Eric CLI (`fieldnote`):** T1 can use **one or both** backends via flags:
- `--scrape-x` (default true) → live X via local `x-scraper`
- `--scrape-social` (default true) → Reddit/HN via social-signals `:8899` (fixtures if down)
- Examples: `--scrape-social=false` (X only), `--scrape-x=false` (social only)

---

## 4. One-pager — Is X fully integrated for assets?

**Yes**, for the contract we care about:

```
Playwright search → tweet JSON
       ↓ tweets_to_signals
assets.Signal
       ↓ signals_to_posts
Post (+ mapping_errors)
       ↓ console signalsToConsoleThemes
Inbox themes / quotes / draft issue shells
```

Same shape as fixture Signals. Remaining risk is **ops** (cookies, timeouts, proxy), not missing mapping.

---

## 5. One-pager — HN/Reddit: near operational?

**Code:** near ready (patches under `scraping/social_signals_patch/`, mapper, harvest stream, heartbeats, loud degrade).

**Live ops (2026-08-16):** `social_signals_lite/` serves the harvest watch API on `:8899` (HN Algolia + Reddit Playwright). Start with `./scripts/run_social_signals_lite.sh`; Eric’s `run.sh` auto-starts it when the port is down. Verified: harvest `live=True` / `source_note=live scrape` for Reddit+HN.

**Still missing for Connect:**

1. Swap `/scrape/social` from `load_fixtures` → live watch (or job + poll/SSE)  
2. UI for 3–5 min Reddit (progress), not one blocking POST  

So: **pipeline live; console still fixture path for HN/Reddit.**

---

## 6. One-pager — Original prompting vs what we did

**Their design DNA (from harvest/README):** external social-signals service; field-note orchestrates; fixtures save the demo; T1 stream with heartbeats; per-request targets from T0.

**What we optimized for:** one **console-native** live source we own (x-scraper submodule); direct `POST /scrape/x`; fixtures as fast demo; force-live for real X.

| | Team harvest path | Gregory X path |
| --- | --- | --- |
| Live process | Separate service | Subprocess from field-note |
| Console | Not primary | Primary |
| Failover | Harvest → fixtures | Fixture default / force-live |
| Contract | Signal → Post | Same |

Same spine, different ops model.

---

## 7. One-pager — Use cases

**North star:** web feedback → Signals → issues → map to repo → PR. Ambitious; T4/T5 empty by design today.

**Ready wedge:**

- Product listening / research desk (especially live X)  
- Competitive / launch monitoring  
- Support triage from public complaints  
- Custom research + AI analysis over real Signals  
- Vertical reuse: change queries, keep Signal contract  

**Natural extensions:** image/visual as another `platform` on the same Signal model; full multi-source live; clustering → repo → PR.

---

## 8. One-pager — Strengths / weaknesses / completeness / scale

**Strengths:** shared Signal contract; pragmatic live X; demo survivability; clear team lanes; real failure-mode fixes (reload/proxy).

**Weaknesses:** two scrape worlds (console vs harvest); thin clustering in UI; live X babysitting; config sprawl.

**Completeness:** strong **vertical slice** (X → UI); weak **horizontal** multi-source live; T4–T5 not started.

**Scalability:** good bones for a job queue + workers; **won’t scale** as sync Playwright inside one uvicorn + long Next wait. Next step: async jobs, dedicated scrapers, cache/dedupe by signal id.

---

## 9. One-pager — Team bones

Same-day trio (repos created 2026-08-15):

| Person | Lane (from commits) |
| --- | --- |
| **Anudeep** (`anudeepadi`) | Frontend (crispy-pancake); T2/T3/report touches |
| **Eric** (`ericjypark`) | Backend pipeline, streaming, merges |
| **Gregory** (`gwild`) | X scrape, submodule, `/scrape/x`, console X wiring, scrape stability |
| **Sol** (`in-sol-ence`) | Org / early T2 / dossier merges |

**Verdict:** good execution chemistry and a real technical spine; not yet a durable org. Risk = parallel console vs pipeline unless someone owns the join.

---

## 10. One-pager — Security / repo visibility

| Repo | Visibility |
| --- | --- |
| `field-note-backend` | **Public** |
| `crispy-pancake` | **Private** |
| `gwild/x-scraper` | **Public** |
| `ericjypark/cursor-grok-hackathon` | **Public** |

Checked: **no real API secrets in public git trees** (`.env` ignored; `.env.example` placeholders; cookies/`config.ini` ignored). Local `Cursor Hackathon.md` has had keys in notes — **untracked; do not commit**. Hackathon security posture is expectedly thin; don’t rotate mid-demo if keys aren’t published.

---

## 11. Open follow-ups (if we continue)

- [x] Live Reddit/HN without private social-signals: `social_signals_lite/` on `:8899` (`./scripts/run_social_signals_lite.sh`); `run.sh` auto-starts it  
- [ ] Point Connect HN/Reddit at harvest / social-signals (or document fixtures-only clearly in UI)  
- [ ] Feed console themes from T2/T3 reports, not only one-row-per-signal  
- [ ] Job queue for live scrapes (status + SSE)  
- [ ] T4: `related_code` from connected repo  
- [ ] Keep secrets out of markdown forever  

---

*End of notes — Gregory / gwild — 2026-08-15 (lite scraper 2026-08-16)*
