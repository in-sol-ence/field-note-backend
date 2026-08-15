import asyncio
import json
import re
from collections import defaultdict
from dataclasses import asdict, dataclass
from typing import Literal

from xai_sdk.tools import web_search, x_search

from assets import Issue, LovedFeature, RecommendedFeature, Signal, SignalFindings
from models import call_grok
from schema import ProductDossier

extract_signals_prompt = """
Analyze the single customer signal below and extract only clearly supported
product findings.

A finding can be:
- an issue: broken, unreliable, confusing, or harmful existing behavior;
- a recommended feature: an explicit request or clearly stated unmet need;
- a loved feature: existing behavior the user explicitly values.

Rules:
- Do not force a finding. Return empty arrays when the signal contains no clear,
  actionable product insight.
- Base every claim only on this signal; do not infer unsupported details.
- Put the supplied signal_id in every finding's signal_ids list.
- Evidence must contain short, exact quotes from the title, body, or comments.
- Do not classify engagement, popularity, or the author's identity as a finding.
- Return at most one finding in each category.
- Use the product dossier to disambiguate the product and its features. Ignore
  signals that refer to a namesake or a different product.
- Follow the provided structured output schema exactly.

Product dossier:
{dossier_json}

Signal:
{signal_json}
""".strip()

merge_signals_prompt = """
Merge duplicate findings in this batch. Two findings are duplicates only when
they describe the same underlying product behavior and user need.

Rules:
- Preserve every signal_id from merged findings.
- Never invent a signal_id or unsupported detail.
- Keep distinct root causes separate, even if they share a product area.
- Deduplicate evidence and keep only the strongest short quotes.
- Use a short canonical title and a one-sentence canonical summary.
- Return findings only in the supplied category; leave the other arrays empty.
- Keep findings scoped to the product described by the dossier.
- Follow the structured output schema exactly.

Product dossier:
{dossier_json}

Category: {category}
Product area: {product_area}
Findings:
{findings_json}
""".strip()

canonicalize_signals_prompt = """
Make this compact list globally unique within its category and product area.
Merge entries only when their canonical descriptions represent the same root
problem, requested capability, or valued behavior.

Rules:
- Preserve the union of all signal_ids.
- Never invent a signal_id.
- Keep genuinely different user needs separate.
- Produce a concise canonical title and one-sentence summary for every result.
- Return findings only in the supplied category; leave the other arrays empty.
- Keep findings scoped to the product described by the dossier.
- Follow the structured output schema exactly.

Product dossier:
{dossier_json}

Category: {category}
Product area: {product_area}
Canonical descriptions:
{findings_json}
""".strip()

validate_signals_prompt = """
Validate whether public source evidence actually supports this product finding.
You must use the available web or X search tools to inspect the supplied source
URLs and look for the original posts or reliable indexed copies.

Rules:
- Validate the specific claim, not merely whether the general topic exists.
- A source supports the finding only when its content expresses the same
  problem, requested capability, or valued behavior.
- Do not treat likes, scores, reposts, or general popularity as claim support.
- Return "unsupported" when accessible evidence contradicts or does not support
  the finding.
- Return "unverifiable" when the source cannot be accessed or reliably found.
  Lack of search results is not proof that the finding is false.
- Never invent sources or signal IDs.
- Include only URLs actually inspected through search in sources.
- Keep the explanation to one concise sentence.
- Confirm that the evidence concerns the product in the dossier, not a
  namesake or adjacent product.
- Follow the structured output schema exactly.

Product dossier:
{dossier_json}

Finding:
{finding_json}

Signals claimed as evidence:
{signals_json}
""".strip()

Finding = Issue | RecommendedFeature | LovedFeature
BucketKey = tuple[str, str]


@dataclass
class ValidationResult:
    finding_title: str
    verdict: Literal["supported", "unsupported", "unverifiable"]
    supported_signal_ids: list[str]
    explanation: str
    sources: list[str]


def _dossier_json(dossier: ProductDossier | None) -> str:
    """Serialize product context for every Grok stage."""
    if dossier is None:
        return '{"context":"No product dossier supplied by this legacy caller."}'
    return dossier.model_dump_json(exclude_none=True)


async def analyze_results(
    signals: list[Signal], dossier: ProductDossier
) -> SignalFindings:
    """Analyze scraper results in the context of the preprocessed product."""
    if not isinstance(signals, list) or any(not isinstance(s, Signal) for s in signals):
        raise TypeError("scraper results must be a list of assets.Signal objects")

    findings = await extract_issues(signals, dossier)
    merged = await merge_issues(findings, dossier)
    await validate_issues(merged, signals, dossier)
    return merged


async def analyze_signals(
    signals: list[Signal], dossier: ProductDossier | None = None
) -> SignalFindings:
    """Legacy entry point; new pipeline code should call ``analyze_results``."""
    if dossier is None:
        # Preserve the standalone fixture visualizer. Production orchestration
        # always uses analyze_results, where dossier is required.
        findings = await extract_issues(signals, None)
        merged = await merge_issues(findings, None)
        await validate_issues(merged, signals, None)
        return merged
    return await analyze_results(signals, dossier)


async def extract_issues(
    signals: list[Signal], dossier: ProductDossier | None = None
) -> SignalFindings:
    """Extract findings with one concurrent Grok call per signal."""
    # Drop bulky raw payloads before sending to the model.
    calls = []
    for signal in signals:
        payload = asdict(signal)
        payload["raw"] = {}
        calls.append(
            call_grok(
                extract_signals_prompt.format(
                    dossier_json=_dossier_json(dossier),
                    signal_json=json.dumps(payload, ensure_ascii=False),
                ),
                SignalFindings,
            )
        )
    extracted_findings = await asyncio.gather(*calls)

    findings = SignalFindings(
        issues=[],
        recommended_features=[],
        loved_features=[],
    )
    for extracted in extracted_findings:
        findings.issues.extend(extracted.issues)
        findings.recommended_features.extend(extracted.recommended_features)
        findings.loved_features.extend(extracted.loved_features)

    return findings


def _normalize_product_area(product_area: str) -> str:
    """Create a stable key from differently formatted product-area labels."""
    normalized = re.sub(r"[^a-z0-9]+", " ", product_area.casefold())
    return " ".join(normalized.split()) or "other"


def create_issue_buckets(
    findings: SignalFindings,
) -> dict[BucketKey, list[Finding]]:
    """Bucket findings by category and normalized product area in O(n)."""
    buckets: defaultdict[BucketKey, list[Finding]] = defaultdict(list)

    for finding in findings.issues:
        buckets[("issue", _normalize_product_area(finding.product_area))].append(
            finding
        )
    for finding in findings.recommended_features:
        buckets[("recommended_feature", _normalize_product_area(finding.product_area))].append(
            finding
        )
    for finding in findings.loved_features:
        buckets[("loved_feature", _normalize_product_area(finding.product_area))].append(
            finding
        )

    return dict(buckets)


def _compact_finding(finding: Finding) -> dict:
    """Keep final deduplication prompts small."""
    compact = {
        "title": finding.title,
        "summary": finding.summary,
        "product_area": finding.product_area,
        "signal_ids": finding.signal_ids,
        "evidence": finding.evidence[:2],
    }
    if isinstance(finding, Issue):
        compact.update(
            severity=finding.severity,
            suggested_action=finding.suggested_action,
        )
    elif isinstance(finding, RecommendedFeature):
        compact.update(
            priority=finding.priority,
            suggested_action=finding.suggested_action,
        )
    return compact


async def merge_issues(
    findings: SignalFindings, dossier: ProductDossier | None = None
) -> SignalFindings:
    """Merge batches of four, then deduplicate their canonical descriptions."""
    batch_calls = []
    for (category, product_area), bucket in create_issue_buckets(findings).items():
        for start in range(0, len(bucket), 4):
            batch = bucket[start : start + 4]
            batch_calls.append(
                call_grok(
                    merge_signals_prompt.format(
                        dossier_json=_dossier_json(dossier),
                        category=category,
                        product_area=product_area,
                        findings_json=json.dumps(
                            [asdict(finding) for finding in batch],
                            ensure_ascii=False,
                        ),
                    ),
                    SignalFindings,
                )
            )

    batch_results = await asyncio.gather(*batch_calls)
    first_pass = SignalFindings([], [], [])
    for result in batch_results:
        first_pass.issues.extend(result.issues)
        first_pass.recommended_features.extend(result.recommended_features)
        first_pass.loved_features.extend(result.loved_features)

    canonical_calls = [
        call_grok(
            canonicalize_signals_prompt.format(
                dossier_json=_dossier_json(dossier),
                category=category,
                product_area=product_area,
                findings_json=json.dumps(
                    [_compact_finding(finding) for finding in bucket],
                    ensure_ascii=False,
                ),
            ),
            SignalFindings,
        )
        for (category, product_area), bucket in create_issue_buckets(first_pass).items()
    ]

    canonical_results = await asyncio.gather(*canonical_calls)
    merged = SignalFindings([], [], [])
    for result in canonical_results:
        merged.issues.extend(result.issues)
        merged.recommended_features.extend(result.recommended_features)
        merged.loved_features.extend(result.loved_features)
    return merged


def _compact_signal(signal: Signal) -> dict:
    return {
        "platform": signal.platform,
        "signal_id": signal.signal_id,
        "url": signal.url,
        "title": signal.title,
        "body": signal.body,
        "author": signal.author,
    }


async def validate_issues(
    findings: SignalFindings,
    signals: list[Signal],
    dossier: ProductDossier | None = None,
) -> list[ValidationResult]:
    """Validate every finding concurrently with Grok's search tools."""
    signals_by_id = {signal.signal_id: signal for signal in signals}
    all_findings: list[Finding] = [
        *findings.issues,
        *findings.recommended_features,
        *findings.loved_features,
    ]

    calls = []
    for finding in all_findings:
        supporting_signals = [
            _compact_signal(signals_by_id[signal_id])
            for signal_id in finding.signal_ids
            if signal_id in signals_by_id
        ]
        calls.append(
            call_grok(
                validate_signals_prompt.format(
                    dossier_json=_dossier_json(dossier),
                    finding_json=json.dumps(asdict(finding), ensure_ascii=False),
                    signals_json=json.dumps(supporting_signals, ensure_ascii=False),
                ),
                ValidationResult,
                tools=[web_search(), x_search()],
            )
        )

    return list(await asyncio.gather(*calls))