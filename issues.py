import asyncio
import json
from dataclasses import asdict

from assets import Signal, SignalFindings
from models import call_grok

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
- Follow the provided structured output schema exactly.

Signal:
{signal_json}
""".strip()

merge_signals_prompt = ""
validate_signals_prompt = ""

async def analyze_signals(signals):                                                                                  
    findings: SignalFindings = await extract_findings(signals)                                                                       
    merged: SignalFindings = await merge_findings(findings)                                                                          
    await validate(merged, signals)                                                                                        
    return await rank(merged)  

async def extract_issues(signals: list[Signal]) -> SignalFindings:
    """Extract findings with one concurrent Grok call per signal."""
    calls = [
        call_grok(
            extract_signals_prompt.format(
                signal_json=json.dumps(asdict(signal), ensure_ascii=False)
            ),
            SignalFindings,
        )
        for signal in signals
    ]
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


def merge_issues(issues: SignalFindings) -> SignalFindings:
    pass

def validate_issues(issues: SignalFindings, signals: list[Signal]) -> None:
    pass

def rank_issues(issues: SignalFindings) -> SignalFindings:
    pass