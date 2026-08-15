"""X scrape providers for field-note.

Every provider returns ``list[assets.Signal]`` — the same objects ``issues.py``
and the rest of the pipeline consume. Raw tweet JSON never leaves this module
without going through ``x_signals.to_signals``.

Providers:
- ``x-scraper`` — local Playwright checkout at ``X_SCRAPER_ROOT`` (default)
- ``social-signals`` — remote HTTP watch job
- ``fixture`` — Signal or tweet JSON on disk (demo / offline)
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from assets import Signal
from schema import ProductDossier
from x_signals import to_signals, tweets_to_signals

# Repo root: field-note-backend/
_BACKEND_ROOT = Path(__file__).resolve().parents[2]


def default_x_scraper_root() -> Path:
    env = os.environ.get("X_SCRAPER_ROOT", "").strip()
    if env:
        return Path(env).expanduser().resolve()
    # Prefer the git submodule checked out inside this repo.
    bundled = _BACKEND_ROOT / "x-scraper"
    if (bundled / "main.py").is_file():
        return bundled.resolve()
    sibling = _BACKEND_ROOT.parent / "x-scraper"
    if (sibling / "main.py").is_file():
        return sibling.resolve()
    return Path.home() / "x-scraper"


def _python_bin(root: Path) -> Path:
    env = os.environ.get("X_SCRAPER_PYTHON", "").strip()
    if env:
        return Path(env).expanduser().resolve()
    for candidate in (root / ".venv" / "bin" / "python", root / "venv" / "bin" / "python"):
        if candidate.is_file():
            return candidate
    return Path("python3")


def _complaint_query(product: str, extra: str | None = None) -> str:
    """Default complaint-oriented X search for a product name."""
    base = (
        f'("{product}") (bug OR broken OR crash OR error OR "not working" OR '
        f"sucks OR issue OR complaint OR abandoned OR frustrat*) -is:retweet"
    )
    if extra:
        return f"{base} {extra}"
    return base


def dossier_to_search_queries(dossier: ProductDossier) -> list[str]:
    """Build focused X queries from T0's identity and disambiguation context."""
    identity = dossier.identity
    names = [identity.canonical_name, *identity.aliases]
    names = list(dict.fromkeys(name.strip() for name in names if name.strip()))
    if not names:
        raise ValueError("dossier identity has no product name")

    # Search the canonical name and up to two useful aliases independently so
    # providers can allocate results across the ways customers name a product.
    queries = [_complaint_query(name) for name in names[:3]]
    jargon = next(
        (
            term.strip()
            for term in dossier.vocabulary.feature_jargon
            if term.strip() and term.casefold() not in {name.casefold() for name in names}
        ),
        None,
    )
    if jargon:
        queries.append(_complaint_query(identity.canonical_name, f'"{jargon}"'))
    return list(dict.fromkeys(queries))


def load_fixture_signals(path: str | Path) -> list[Signal]:
    """Load a fixture file as validated assets.Signal objects."""
    raw = json.loads(Path(path).expanduser().read_text())
    if isinstance(raw, dict) and "tweets" in raw:
        return tweets_to_signals(list(raw["tweets"] or []))
    if isinstance(raw, dict) and "signals" in raw:
        raw = raw["signals"]
    if not isinstance(raw, list):
        raise ValueError(f"fixture must be a list or {{tweets|signals: [...]}}: {path}")
    return to_signals(raw)


def run_x_scraper_search(
    query: str,
    *,
    count: int = 20,
    result_type: str = "Latest",
    root: Path | None = None,
    timeout_sec: int = 300,
) -> list[Signal]:
    """Run local x-scraper keyword search; return assets.Signal objects."""
    root = (root or default_x_scraper_root()).resolve()
    if not (root / "main.py").is_file():
        raise FileNotFoundError(
            f"x-scraper not found at {root}. Set X_SCRAPER_ROOT to the checkout."
        )

    python = _python_bin(root)
    with tempfile.TemporaryDirectory(prefix="fn-xscrape-") as tmp:
        out = Path(tmp) / "result.json"
        cmd = [
            str(python),
            "main.py",
            "search",
            "-q",
            query,
            "-n",
            str(count),
            "-t",
            result_type,
            "--no-analyze",
            "-o",
            str(out),
        ]
        proc = subprocess.run(
            cmd,
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=timeout_sec,
            check=False,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                "x-scraper failed "
                f"(exit {proc.returncode}): {proc.stderr[-2000:] or proc.stdout[-2000:]}"
            )
        if not out.is_file():
            raise RuntimeError(
                "x-scraper exited 0 but wrote no output file. "
                f"stdout tail: {proc.stdout[-1000:]}"
            )
        payload = json.loads(out.read_text())
        tweets = payload.get("tweets") or []
        return tweets_to_signals(tweets, search_query=query)


def run_social_signals_x(
    targets: dict[str, Any],
    *,
    base_url: str | None = None,
    api_key: str | None = None,
    per_target_limit: int | None = None,
    poll_sec: float = 3.0,
    timeout_sec: int = 600,
) -> list[Signal]:
    """Call social-signals ``POST /v1/jobs/watch``; normalize to assets.Signal."""
    base = (base_url or os.environ.get("SOCIAL_SIGNALS_URL") or "http://127.0.0.1:8899").rstrip(
        "/"
    )
    key = api_key or os.environ.get("SOCIAL_SIGNALS_API_KEY") or ""
    headers = {"Authorization": f"Bearer {key}"} if key else {}

    body: dict[str, Any] = {
        "platform": "x",
        "include_signals": True,
        "targets": {"x": targets},
    }
    if per_target_limit is not None:
        body["per_target_limit"] = per_target_limit

    with httpx.Client(timeout=60.0) as client:
        job = client.post(f"{base}/v1/jobs/watch", headers=headers, json=body)
        job.raise_for_status()
        job_id = job.json()["job_id"]
        poll_url = job.json().get("poll_url") or f"/v1/jobs/{job_id}"
        if not poll_url.startswith("http"):
            poll_url = f"{base}{poll_url}"

        deadline = time.time() + timeout_sec
        while time.time() < deadline:
            res = client.get(poll_url, headers=headers)
            res.raise_for_status()
            payload = res.json()
            status = payload.get("status")
            if status == "running":
                time.sleep(poll_sec)
                continue
            if status not in {"succeeded", "success", "completed", "done"}:
                raise RuntimeError(f"social-signals job failed: {payload}")
            result = payload.get("result") or {}
            return to_signals(list(result.get("signals") or []))
        raise TimeoutError(f"social-signals watch timed out after {timeout_sec}s")


def scrape_x(
    *,
    provider: str | None = None,
    search_queries: list[str] | None = None,
    product: str | None = None,
    dossier: ProductDossier | None = None,
    count: int = 20,
    result_type: str = "Latest",
    fixture_path: str | None = None,
    accounts: list[str] | None = None,
    x_scraper_root: str | None = None,
    social_signals_url: str | None = None,
) -> list[Signal]:
    """Scrape X into assets.Signal objects via the selected provider."""
    provider = (provider or os.environ.get("X_SCRAPE_PROVIDER") or "x-scraper").lower()

    if provider == "fixture":
        path = fixture_path or os.environ.get("X_SCRAPE_FIXTURE")
        if not path:
            raise ValueError("fixture provider requires fixture_path or X_SCRAPE_FIXTURE")
        return load_fixture_signals(path)[:count]

    queries = list(search_queries or [])
    if not queries and dossier is not None:
        queries = dossier_to_search_queries(dossier)
    if not queries and product:
        queries = [_complaint_query(product)]
    if not queries:
        raise ValueError("search_queries or product is required")

    if provider == "x-scraper":
        root = Path(x_scraper_root).expanduser() if x_scraper_root else None
        signals: list[Signal] = []
        seen: set[str] = set()
        per_query = max(1, count // len(queries))
        remainder = count
        for i, query in enumerate(queries):
            n = remainder if i == len(queries) - 1 else min(per_query, remainder)
            if n <= 0:
                break
            batch = run_x_scraper_search(
                query,
                count=n,
                result_type=result_type,
                root=root,
            )
            for sig in batch:
                if sig.signal_id and sig.signal_id not in seen:
                    seen.add(sig.signal_id)
                    signals.append(sig)
            remainder = count - len(signals)
        return signals[:count]

    if provider in {"social-signals", "social_signals"}:
        targets: dict[str, Any] = {
            "accounts": accounts or [],
            "search_queries": queries,
        }
        return run_social_signals_x(
            targets,
            base_url=social_signals_url,
            per_target_limit=count,
        )[:count]

    raise ValueError(
        f"unknown X scrape provider {provider!r}; "
        "use x-scraper, social-signals, or fixture"
    )


def website_to_product_hint(website: str) -> str:
    """Rough product token from a website URL for default query building."""
    host = urlparse(website).netloc or website
    host = host.removeprefix("www.")
    return host.split(".")[0] if host else website
