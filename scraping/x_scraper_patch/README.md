# x-scraper patch notes

Live X scraping uses the **`x-scraper` git submodule** at the repo root
([gwild/x-scraper](https://github.com/gwild/x-scraper)) — Fieldnotes' patched
fork of Proxidize.

```bash
git submodule update --init --recursive
cd x-scraper && python -m venv .venv && . .venv/bin/activate
pip install -e . && playwright install chromium
cp config.ini.template config.ini
```

`X_SCRAPER_ROOT` defaults to that submodule. The files in this folder are a
snapshot of the same patches for reference if the submodule is missing; prefer
the submodule checkout for running scrapes.
