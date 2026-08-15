"""T2 as a pipeline stage: harvested posts -> a stored dashboard report.

issues.py owns the model calls. This module owns the plumbing around them —
turning T1's Posts back into Signals, skipping what cannot be quoted, matching
validations to findings, and persisting the result so the dashboard has a URL.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from dataclasses import asdict
from typing import Any

import report_store
from assets import Signal
from enriched import Validation, build_report
from schema import ErrorEvent, Event, Harvest, ReportEvent, StageEvent


def post_to_signal(post: Any) -> dict[str, Any]:
    """A T1 Post back into the Signal shape T2 consumes.

    T1 normalizes scraped signals into Posts for the dashboard; T2 was written
    against Signals. Rather than fork T2, convert here — the raw payload is
    carried through, so nothing is lost.
    """
    data = post if isinstance(post, dict) else post.model_dump()
    raw = data.get("raw") or {}
    comments = [
        {
            "author": c.get("author") or "",
            "body": c.get("body") or "",
            "score": str(c.get("score") or ""),
        }
        for c in (data.get("comments") or [])
        if isinstance(c, dict) and (c.get("body") or "").strip()
    ]
    return {
        "platform": data.get("source") or data.get("platform") or "reddit",
        "signal_id": data.get("source_id") or data.get("id") or data.get("url") or "",
        "url": data.get("url") or "",
        "title": data.get("title") or "",
        "body": data.get("body") or "",
        "author": data.get("author") or "",
        "score": str(data.get("score") if data.get("score") is not None else ""),
        "engagement": {
            "score": str(data.get("score") if data.get("score") is not None else ""),
            "comments": comments,
        },
        "scraped_at": data.get("scraped_at") or "",
        "raw": {
            **raw,
            "created_at": data.get("created_at") or raw.get("created_at"),
            "subreddit": data.get("channel") or raw.get("subreddit"),
        },
    }


def is_quotable(signal: Signal) -> bool:
    """Whether there is anything a model could quote as evidence.

    A link post with no body and no fetched comments can only produce empty
    findings, so sending it spends a call for nothing.
    """
    if (signal.body or "").strip():
        return True
    return any((c.body or "").strip() for c in signal.engagement.comments)


async def run_t2_stream(
    harvest: Harvest, product: str, dossier: Any = None
) -> AsyncIterator[Event]:
    """Cluster the harvest into findings and store the dashboard report.

    The dossier is passed through so every model stage can tell the product
    from a namesake.
    """
    from issues import analyze_with_validation

    signals = [Signal.from_dict(post_to_signal(p)) for p in harvest.posts]
    quotable = [s for s in signals if is_quotable(s)]

    if not quotable:
        yield StageEvent(
            stage="extract_issues",
            status="failed",
            detail="no post carried a body or comments to quote",
        )
        return

    skipped = len(signals) - len(quotable)
    detail = f"{len(quotable)} signals" + (f", {skipped} unquotable skipped" if skipped else "")
    yield StageEvent(stage="extract_issues", status="running", detail=detail)

    started = time.monotonic()
    try:
        result = await analyze_with_validation(quotable, dossier)
    except Exception as exc:  # noqa: BLE001 - T2 failing must not lose T0/T1's work
        yield ErrorEvent(stage="extract_issues", detail=f"T2 failed: {exc}"[:400], fatal=False)
        return

    findings = result.findings
    validations = result.validations
    elapsed = int(time.monotonic() - started)
    yield StageEvent(
        stage="extract_issues",
        status="done",
        detail=f"{len(findings.issues)} issues, {len(findings.recommended_features)} requests "
        f"in {elapsed}s",
    )

    yield StageEvent(stage="build_report", status="running", detail=None)
    lookup = {
        v.finding_title: Validation(
            v.verdict, v.explanation, v.supported_signal_ids, v.sources
        )
        for v in validations
    }
    report = build_report(findings, quotable, product=product, validations=lookup)
    path = report_store.save(report)
    payload = asdict(report)

    yield StageEvent(
        stage="build_report",
        status="done",
        detail=f"health {report.health_score}/10 — {path}",
    )
    yield ReportEvent(report=payload)
