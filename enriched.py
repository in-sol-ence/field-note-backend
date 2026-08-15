"""Enriched findings: what the dashboard (T3) and code-matching (T4) consume.

Two layers, deliberately:

  assets.Issue        what the MODEL returns. Judgment only — title, summary,
                      product_area, severity, quotes, suggested_action. Every
                      field here is a field the model must fill, so it stays
                      minimal.

  enriched.RichIssue  what CODE builds from that plus the signals. IDs,
                      timestamps, counts, per-quote provenance, validation
                      verdicts. All deterministic; asking the model for them
                      invites hallucinated URLs and invented dates.

Keeping them separate means the extraction schema can stay stable while the
dashboard's needs grow.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

from assets import Issue, LovedFeature, RecommendedFeature, Signal, SignalFindings

Category = Literal["issue", "recommended_feature", "loved_feature"]
Severity = Literal["low", "medium", "high", "critical"]
Verdict = Literal["supported", "unsupported", "unverifiable"]

_SEVERITY_WEIGHT = {"low": 1, "medium": 2, "high": 3, "critical": 4}


@dataclass(frozen=True)
class EvidenceQuote:
    """One quote, resolved back to the signal it came from.

    The model returns bare strings. A bare quote cannot be clicked, verified,
    or attributed, so the dashboard needs the source attached — which code can
    resolve because it holds the signals.
    """

    quote: str
    signal_id: str
    url: str
    author: str | None = None
    platform: str | None = None
    channel: str | None = None
    created_at: str | None = None
    #  False when the quote does not appear verbatim in its signal — a cheap,
    #  network-free hallucination check.
    verbatim: bool = True


@dataclass(frozen=True)
class Validation:
    verdict: Verdict
    explanation: str
    supported_signal_ids: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class CodeRef:
    """Filled by T4. Empty until a repo is connected."""

    path: str
    symbol: str | None = None
    line: int | None = None
    reason: str | None = None


@dataclass
class RichIssue:
    id: str
    category: Category
    title: str
    summary: str
    product_area: str
    suggested_action: str

    signal_ids: list[str]
    evidence: list[EvidenceQuote]

    # Derived from the signals, never from the model.
    mention_count: int
    reach: int
    first_seen: str | None
    last_seen: str | None
    channels: list[str]

    severity: Severity | None = None
    priority: Literal["low", "medium", "high"] | None = None
    validation: Validation | None = None

    # T4 / T5. Empty until a repo is connected and fixes are suggested.
    related_code: list[CodeRef] = field(default_factory=list)
    suggested_fix: str | None = None

    @property
    def rank(self) -> tuple[int, int, int]:
        """Sort key for the dashboard: severity, then how many people, then reach."""
        return (
            _SEVERITY_WEIGHT.get(self.severity or "", 0),
            self.mention_count,
            self.reach,
        )


@dataclass
class Report:
    """The whole T3 payload."""

    product: str
    generated_at: str
    health_score: float
    issues: list[RichIssue]
    recommended_features: list[RichIssue]
    loved_features: list[RichIssue]
    signals_analyzed: int


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _issue_id(category: str, title: str) -> str:
    """Stable across runs so a re-scrape updates an issue instead of cloning it."""
    digest = hashlib.sha1(f"{category}:{title.casefold()}".encode()).hexdigest()[:10]
    return f"{category[:3]}_{digest}"


def _as_int(value: object) -> int:
    try:
        return int(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return 0


def _citation_url(signal: Signal) -> str:
    """Where a reader can actually find the quote.

    A HackerNews signal's url is the submitted article, but the comments quoted
    as evidence live on the HN item page. Citing the article sends the reader
    to a page that does not contain the quote.
    """
    if signal.platform == "hackernews":
        item = signal.raw.get("objectID") or signal.raw.get("story_id") or signal.signal_id
        if item:
            return f"https://news.ycombinator.com/item?id={item}"
    return signal.url


def _quote_appears_in(quote: str, signal: Signal) -> bool:
    haystack = " ".join(
        [signal.title or "", signal.body or ""]
        + [c.body or "" for c in signal.engagement.comments]
    ).casefold()
    return quote.casefold().strip() in haystack


def _resolve_evidence(
    quotes: list[str], signal_ids: list[str], signals_by_id: dict[str, Signal]
) -> list[EvidenceQuote]:
    """Attach each quote to whichever cited signal actually contains it."""
    cited = [signals_by_id[sid] for sid in signal_ids if sid in signals_by_id]
    resolved: list[EvidenceQuote] = []
    for quote in quotes:
        match = next((s for s in cited if _quote_appears_in(quote, s)), None)
        source = match or (cited[0] if cited else None)
        if source is None:
            continue
        resolved.append(
            EvidenceQuote(
                quote=quote,
                signal_id=source.signal_id,
                url=_citation_url(source),
                author=source.author,
                platform=source.platform,
                channel=source.raw.get("subreddit") or source.raw.get("handle"),
                created_at=source.raw.get("created_at") or source.scraped_at,
                verbatim=match is not None,
            )
        )
    return resolved


def _timespan(signals: list[Signal]) -> tuple[str | None, str | None]:
    stamps = sorted(
        s.raw.get("created_at") or s.scraped_at for s in signals if s.raw or s.scraped_at
    )
    return (stamps[0], stamps[-1]) if stamps else (None, None)


def enrich(
    finding: Issue | RecommendedFeature | LovedFeature,
    category: Category,
    signals_by_id: dict[str, Signal],
    validation: Validation | None = None,
) -> RichIssue:
    cited = [signals_by_id[sid] for sid in finding.signal_ids if sid in signals_by_id]
    first_seen, last_seen = _timespan(cited)
    return RichIssue(
        id=_issue_id(category, finding.title),
        category=category,
        title=finding.title,
        summary=finding.summary,
        product_area=finding.product_area,
        suggested_action=getattr(finding, "suggested_action", ""),
        signal_ids=list(finding.signal_ids),
        evidence=_resolve_evidence(finding.evidence, finding.signal_ids, signals_by_id),
        # How many distinct posts mention it — the honest count. Upvotes are
        # reach, not incidence, and conflating them overstates rare-but-loud
        # complaints.
        mention_count=len(cited),
        reach=sum(_as_int(s.score) for s in cited),
        first_seen=first_seen,
        last_seen=last_seen,
        channels=sorted({s.raw.get("subreddit") or s.platform for s in cited}),
        severity=getattr(finding, "severity", None),
        priority=getattr(finding, "priority", None),
        validation=validation,
    )


def health_score(issues: list[RichIssue]) -> float:
    """0 (critical) to 10 (healthy).

    Weighted by severity and by how many distinct people hit each issue, so one
    loud thread cannot tank the score on its own.
    """
    if not issues:
        return 10.0
    penalty = sum(
        _SEVERITY_WEIGHT.get(i.severity or "low", 1) * min(i.mention_count, 5)
        for i in issues
    )
    return round(max(0.0, 10.0 - penalty / 4), 1)


def build_report(
    findings: SignalFindings,
    signals: list[Signal],
    *,
    product: str,
    validations: dict[str, Validation] | None = None,
    generated_at: str | None = None,
) -> Report:
    """SignalFindings + Signals -> the dashboard payload.

    `validations` is keyed by finding title. Merge rewrites titles, so this is
    matched after merge, never carried across it.
    """
    by_id = {s.signal_id: s for s in signals}
    lookup = validations or {}

    def convert(items, category: Category) -> list[RichIssue]:
        rich = [enrich(f, category, by_id, lookup.get(f.title)) for f in items]
        return sorted(rich, key=lambda i: i.rank, reverse=True)

    issues = convert(findings.issues, "issue")
    return Report(
        product=product,
        generated_at=generated_at or datetime.now().astimezone().isoformat(),
        health_score=health_score(issues),
        issues=issues,
        recommended_features=convert(findings.recommended_features, "recommended_feature"),
        loved_features=convert(findings.loved_features, "loved_feature"),
        signals_analyzed=len(signals),
    )
