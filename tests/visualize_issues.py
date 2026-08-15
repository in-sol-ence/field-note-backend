#!/usr/bin/env python3
"""Run a signal JSON fixture through issues.py and write an HTML report.

Example:
    uv run python tests/visualize_issues.py data/signals_perplexity_x.json

The default limit keeps model usage modest. Pass ``--limit 0`` to process every
signal in the file.
"""

from __future__ import annotations

import argparse
import asyncio
import html
import json
import os
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from assets import Signal, SignalFindings  # noqa: E402
from issues import analyze_signals  # noqa: E402
from x_signals import tweets_to_signals  # noqa: E402


def _records(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        records = payload
    elif isinstance(payload, dict):
        for key in ("signals", "tweets", "data", "results"):
            if isinstance(payload.get(key), list):
                records = payload[key]
                break
        else:
            records = [payload]
    else:
        raise ValueError("JSON root must be an object or an array")
    if not all(isinstance(item, dict) for item in records):
        raise ValueError("Every signal must be a JSON object")
    return records


def load_signals(path: Path) -> list[Signal]:
    """Load either canonical Signal objects or raw x-scraper tweet objects."""
    records = _records(json.loads(path.read_text(encoding="utf-8")))
    if not records:
        return []
    if all("signal_id" in item and "platform" in item for item in records):
        return [Signal.from_dict(item) for item in records]
    if all("id" in item for item in records):
        return tweets_to_signals(records)
    raise ValueError(
        "Unrecognized records: expected canonical signals (signal_id/platform) "
        "or raw X tweets (id)"
    )


def _finding_card(kind: str, finding: Any, signals: dict[str, Signal]) -> str:
    data = asdict(finding)
    level = data.get("severity") or data.get("priority") or ""
    evidence = "".join(f"<li>{html.escape(str(quote))}</li>" for quote in data.get("evidence", []))
    sources = []
    for signal_id in data.get("signal_ids", []):
        signal = signals.get(signal_id)
        label = html.escape(signal_id)
        sources.append(f'<a href="{html.escape(signal.url, quote=True)}">{label}</a>' if signal and signal.url else label)
    action = data.get("suggested_action")
    return f"""
      <article class="card {html.escape(kind)}">
        <div class="meta"><span>{html.escape(kind.replace('_', ' ').title())}</span>{f'<b>{html.escape(level)}</b>' if level else ''}</div>
        <h2>{html.escape(data.get('title', 'Untitled'))}</h2>
        <p>{html.escape(data.get('summary', ''))}</p>
        <p><strong>Area:</strong> {html.escape(data.get('product_area', ''))}</p>
        {f'<p><strong>Suggested action:</strong> {html.escape(action)}</p>' if action else ''}
        <strong>Evidence</strong><ul>{evidence or '<li>None supplied</li>'}</ul>
        <p class="sources"><strong>Signals:</strong> {', '.join(sources) or 'None'}</p>
      </article>"""


def render_html(findings: SignalFindings, source_signals: list[Signal], input_name: str) -> str:
    """Create a dependency-free report suitable for opening in a browser."""
    groups = (
        ("issue", findings.issues),
        ("recommended_feature", findings.recommended_features),
        ("loved_feature", findings.loved_features),
    )
    cards = "".join(_finding_card(kind, item, {s.signal_id: s for s in source_signals}) for kind, items in groups for item in items)
    counts = "".join(f"<div><strong>{len(items)}</strong><span>{kind.replace('_', ' ')}</span></div>" for kind, items in groups)
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>Signal findings — {html.escape(input_name)}</title>
<style>
:root {{ color-scheme: dark; font-family: Inter, system-ui, sans-serif; background:#10131a; color:#e9eef8 }}
body {{ max-width:1100px; margin:auto; padding:40px 20px }} h1 {{ margin-bottom:4px }} .sub {{ color:#9aa8bd }}
.stats {{ display:flex; gap:12px; margin:28px 0; flex-wrap:wrap }} .stats div {{ background:#1b2130; border:1px solid #30394d; padding:14px 22px; border-radius:12px }}
.stats strong {{ font-size:28px; display:block }} .stats span {{ color:#9aa8bd }} .grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(320px,1fr)); gap:16px }}
.card {{ background:#171c28; border:1px solid #30394d; border-top:4px solid #ef6b73; border-radius:12px; padding:20px }}
.card.recommended_feature {{ border-top-color:#63a9ff }} .card.loved_feature {{ border-top-color:#65d69e }} .meta {{ display:flex; justify-content:space-between; color:#9aa8bd }}
.meta b {{ text-transform:uppercase; color:#f5c96a }} a {{ color:#79b8ff }} li {{ margin:7px 0 }} .sources {{ font-size:13px; color:#9aa8bd }}
</style></head><body>
<h1>Signal findings</h1><div class="sub">{html.escape(input_name)} · {len(source_signals)} source signals</div>
<section class="stats">{counts}</section><main class="grid">{cards or '<p>No findings were extracted.</p>'}</main>
</body></html>"""


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="Signal or raw X JSON file")
    parser.add_argument("--output", type=Path, default=ROOT / "tests" / "issues_report.html")
    parser.add_argument("--limit", type=int, default=10, help="Signals to process; 0 means all (default: 10)")
    return parser.parse_args()


async def _main() -> None:
    args = _parse_args()
    try:
        from dotenv import load_dotenv

        load_dotenv(ROOT / ".env")
    except ImportError:
        pass
    if not os.getenv("XAI_API_KEY"):
        raise SystemExit("XAI_API_KEY is required (set it in the environment or project .env)")
    signals = load_signals(args.input)
    if args.limit < 0:
        raise SystemExit("--limit cannot be negative")
    selected = signals[: args.limit] if args.limit else signals
    if not selected:
        raise SystemExit("The input contains no signals")
    print(f"Analyzing {len(selected)} of {len(signals)} signals from {args.input} …")
    findings = await analyze_signals(selected)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(render_html(findings, selected, args.input.name), encoding="utf-8")
    print(f"Wrote {args.output.resolve()}")


if __name__ == "__main__":
    asyncio.run(_main())
