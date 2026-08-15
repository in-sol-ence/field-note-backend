from assets import Signal


async def analyze_signals(signals):                                                                                  
    findings = await extract_findings(signals)                                                                       
    merged = await merge_findings(findings)                                                                          
    validate(merged, signals)                                                                                        
    return rank(merged)  

def extract_issues(signals: list[Signal]) -> list[Issue]:
    for signal in signals:
        pass
    pass

def merge_issues(issues: list[Issue]) -> list[Issue]:
    pass

def validate_issues(issues: list[Issue], signals: list[Signal]) -> None:
    pass

def rank_issues(issues: list[Issue]) -> list[Issue]:
    pass