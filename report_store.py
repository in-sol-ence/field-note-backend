"""Persisted reports, keyed by product slug.

The dashboard needs a URL it can poll, not a file path. A run writes its report
here; `GET /report/{slug}` reads it back. Flat JSON files on disk — a hackathon
does not need a database, and the whole payload is a few hundred KB.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict
from pathlib import Path
from typing import Any

from enriched import Report

REPORT_DIR = Path("out/reports")


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.casefold()).strip("-")
    return slug or "product"


def save(report: Report) -> Path:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    path = REPORT_DIR / f"{slugify(report.product)}.json"
    path.write_text(
        json.dumps(asdict(report), indent=2, ensure_ascii=False, default=str) + "\n"
    )
    return path


def load(slug: str) -> dict[str, Any] | None:
    path = REPORT_DIR / f"{slugify(slug)}.json"
    if not path.exists():
        return None
    return json.loads(path.read_text())


def list_slugs() -> list[str]:
    if not REPORT_DIR.exists():
        return []
    return sorted(p.stem for p in REPORT_DIR.glob("*.json"))
