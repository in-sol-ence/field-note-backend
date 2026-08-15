# x-scraper patch

Patches on top of [proxidize/x-scraper](https://github.com/proxidize/x-scraper) that field-note's
`POST /scrape/x` (`provider: x-scraper`) depends on.

Copy over a checkout of x-scraper:

```
x_scraper_patch/src/playwright_scraper.py -> src/playwright_scraper.py
x_scraper_patch/src/scraper.py            -> src/scraper.py
x_scraper_patch/cli/user.py               -> cli/user.py
```

What changed vs upstream:

1. Honor `-n/--count` as the session tweet cap (upstream ignored it when
   `max_tweets_per_session` was unset in config).
2. Louder `[scrape]` progress + GraphQL op logging when X renames endpoints.
3. DOM fallback when GraphQL timeline parse returns nothing.
4. Manual-login cookie capture when automated X login is blocked.
5. Linux-friendly browser fingerprint defaults.

Point `X_SCRAPER_ROOT` at that checkout (default: sibling `../x-scraper`).
