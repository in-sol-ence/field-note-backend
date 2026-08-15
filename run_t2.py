"""Run T2 end to end over a scraped signal file and save the findings.

    uv run python run_t2.py scraping/data/signals_reddit.json out/findings.json
"""

import asyncio
import json
import sys
import time
from dataclasses import asdict
from pathlib import Path

from assets import Signal
from issues import analyze_with_validation


def is_quotable(signal: Signal) -> bool:
    """Whether there is anything the model could quote as evidence.

    A link post with no body and no fetched comments can only produce empty
    arrays, so sending it costs a call and returns nothing.
    """
    if (signal.body or "").strip():
        return True
    return any((c.body or "").strip() for c in signal.engagement.comments)


def load_signals(path: Path) -> list[Signal]:
    signals = [Signal.from_dict(d) for d in json.loads(path.read_text())]
    quotable = [s for s in signals if is_quotable(s)]
    skipped = len(signals) - len(quotable)
    if skipped:
        print(f"skipping {skipped} signals with no quotable content")
    return quotable


async def main() -> None:
    src = Path(sys.argv[1] if len(sys.argv) > 1 else "scraping/data/signals_reddit.json")
    dest = Path(sys.argv[2] if len(sys.argv) > 2 else "out/findings.json")

    signals = load_signals(src)
    print(f"analyzing {len(signals)} signals from {src}")

    started = time.monotonic()
    result = await analyze_with_validation(signals)
    elapsed = time.monotonic() - started

    findings = result.findings
    validations = result.validations
    print(
        f"\n{elapsed:.0f}s | issues={len(findings.issues)} "
        f"features={len(findings.recommended_features)} "
        f"loved={len(findings.loved_features)}"
    )

    verdicts: dict[str, str] = {v.finding_title: v.verdict for v in validations}
    for issue in sorted(findings.issues, key=lambda i: i.severity):
        print(f"  [{issue.severity:<8}] {issue.title}  ({verdicts.get(issue.title, '?')})")

    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(
        json.dumps(
            {
                "findings": asdict(findings),
                "validations": [asdict(v) for v in validations],
            },
            indent=2,
            ensure_ascii=False,
        )
        + "\n"
    )
    print(f"\nwrote {dest}")


if __name__ == "__main__":
    asyncio.run(main())
