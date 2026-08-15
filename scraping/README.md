# scraping

T1/T2 input: real scraped signals plus the scraper patches that produced them.

Everything in `data/` is **real data scraped on 2026-08-15**, not synthetic.
Product under analysis: Perplexity.

## Data files

| File | Signals | Comments | Loads via `assets.Signal.from_dict` |
|---|---|---|---|
| `data/signals_reddit.json` | 27 | 334 | yes |
| `data/signals_sample.json` | 5 | 60 | yes |
| `data/signals_hackernews.json` | 8 | 88 | **no — see below** |

```python
import json
from assets import Signal

signals = [Signal.from_dict(d) for d in json.load(open("scraping/data/signals_reddit.json"))]
```

### HackerNews needs two lines in `assets.py`

`signals_hackernews.json` will not load today. Two reasons:

```python
# assets.py
class Signal:
    platform: Literal["reddit", "x"]           # needs "hackernews"

class Engagement:
    points: int | None = None                  # HN uses points/num_comments
    num_comments: int | None = None            # instead of score/comments
```

Left unmodified so this folder does not conflict with in-flight edits to
`assets.py`. Add those fields and the file loads.

## What is in a signal

Reddit signals carry the full comment thread, which is the quotable evidence
an `Issue` cites:

- `engagement.comments[]` — `{author, body, score}`, matching `assets.Comment`
- `raw.comments[]` — the same comments plus `depth`, `permalink`, `is_op`

`depth: 0` means a direct reply to the post — the strongest evidence.
`is_op: true` means the original poster clarifying their own complaint.
Per-comment `permalink` lets a quoted line be verified without opening the
thread.

Other fields worth knowing:

- `raw.created_at` — real post time, not scrape time
- `raw.search_query` — which search surfaced this post; absent means it came
  from a subreddit listing. A targeted hit is stronger evidence than an
  ambient one.
- `raw.subreddit` — 12 of the 27 Reddit signals come from search, in
  `r/Revolut`, `r/Perplexity`, `r/sideload`, `r/LETTERSET`. A subreddit-only
  scraper finds none of them.

### Shapes the consumer must handle

Real data is uneven. All of these appear in `signals_reddit.json`:

- posts with no `body` (link posts — title only)
- posts with an empty comment list (outside the detail-fetch limit)
- `score` and `comments_count` are **strings**, not ints
- `score` can be `"•"` on hidden-score posts

## `mapper.py`

Optional normalizer: `Signal` dict -> a flat `Post` dict with ints coerced,
platform-specific fields unified, and per-source quirks handled. 32 tests in
`test_mapper.py`:

```bash
cd scraping && python3 -m pytest -q
```

Use it or don't — the raw signal files are the contract, `mapper.py` is a
convenience.

## Regenerating the data

Scraping runs in the private `social-signals` repo, not here. `social_signals_patch/`
holds the modified files; copy them over a checkout of that repo:

```
social_signals_patch/reddit/scrape.py       -> social_signals/platforms/reddit/scrape.py
social_signals_patch/reddit/adapter.py      -> social_signals/platforms/reddit/adapter.py
social_signals_patch/hackernews/scrape.py   -> social_signals/platforms/hackernews/scrape.py
social_signals_patch/hackernews/adapter.py  -> social_signals/platforms/hackernews/adapter.py
social_signals_patch/config/crispy-pancake.yaml -> config/verticals/crispy-pancake.yaml
```

Then:

```bash
./run.sh --config crispy-pancake watch --platform reddit
./run.sh --config crispy-pancake watch --platform hackernews
```

Output lands in `data/output/watch/`. Requires a logged-in Reddit browser
session (cookies). **No Reddit API key** — Reddit now requires pre-approval
for all Data API access, so this scrapes the site directly.

### What the patches fix

Reddit:

1. `created-timestamp` read off `shreddit-post` — real post dates
2. detail pass per permalink for body + comment threads
3. listing/detail merge allowlist — the listing's `comments` is a *count
   string* and was overwriting the detail page's comment *list*
4. site-wide search via `search-telemetry-tracker` links (search results are
   not `shreddit-post` elements)
5. `top` + `t=month` sorting
6. comment `depth` / `permalink` / `is_op`
7. cross-target dedup by permalink
8. per-target error isolation — one failed target no longer discards the run
9. navigation retry with backoff for Reddit's 429s
10. guardrail refusals and navigation timeouts are no longer recorded as
    platform errors — doing so re-armed a 60-minute cooldown on every blocked
    target and locked the platform out permanently

HackerNews:

1. accepts `search_queries` as well as `queries` (the key mismatch made every
   configured search silently no-op)
2. queries run before tag feeds — the front page used to consume the whole
   result limit, so searches returned nothing
3. `fetch_comments` second call, since Algolia search hits carry comment
   counts but no comment text

## Operational note

Reddit rate-limits after sustained scraping (`ERR_HTTP_RESPONSE_CODE_FAILURE`
= a 429). Retry with backoff absorbs a single one. **Run once during a demo**
and fall back to these files if a re-run is requested.
