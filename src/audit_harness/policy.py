from .config import Control
from .models import Decision, Evidence, Judgment, Outcome, TestResult, Transaction


def decide(
    tx: Transaction,
    control: Control,
    evidence: list[Evidence],
    tests: list[TestResult],
    scope: str,
    judgment: Judgment | None,
    errors: list[str],
) -> Decision:
    if scope == "unsupported_currency":
        outcome, reason = (
            Outcome.NEEDS_HUMAN_REVIEW,
            "Currency is not covered; no implicit FX conversion.",
        )
    elif scope == "out":
        outcome, reason = (
            Outcome.PASS,
            "Outside amount threshold; no assertion about evidence compliance.",
        )
    else:
        critical = [t for t in tests if t.critical]
        failures = [t for t in critical if t.status == "FAIL"]
        unknowns = [t for t in critical if t.status == "UNKNOWN"]
        suspicious = any(t.test_id == "recent_vendor" for t in tests)
        if failures and not suspicious:
            outcome, reason = Outcome.FAIL, "A critical deterministic test failed."
        elif unknowns or errors:
            outcome = Outcome.NEEDS_HUMAN_REVIEW if suspicious else Outcome.INSUFFICIENT_EVIDENCE
            reason = "Evidence is incomplete, conflicting, or analysis is unavailable."
        elif failures:
            outcome, reason = (
                Outcome.NEEDS_HUMAN_REVIEW,
                "Recent vendor payment requires human disposition.",
            )
            if judgment and judgment.confidence < control.decision_policy.min_confidence:
                reason += " Model confidence is below policy threshold."
            elif judgment:
                reason += f" Model recommendation: {judgment.recommendation}; it cannot authorize exceptions."
        elif not critical:
            outcome, reason = Outcome.NEEDS_HUMAN_REVIEW, "No critical tests defined."
        else:
            outcome, reason = Outcome.PASS, "All critical deterministic tests passed."
    return Decision(
        transaction_id=tx.transaction_id,
        control_id=control.control_id,
        control_version=control.version,
        outcome=outcome,
        in_scope=scope != "out",
        reason=reason,
        tests=tests,
        evidence=evidence,
        judgment=judgment,
        review_required=outcome != Outcome.PASS,
        errors=errors,
    )
