from datetime import datetime
from decimal import Decimal

from .config import Control
from .models import Evidence, TestResult, Transaction


def reconcile(tx: Transaction, evidence: list[Evidence]) -> list[str]:
    errors = []
    fields = {
        "payment_record": [
            "payment_amount",
            "currency",
            "payment_creator_id",
            "payment_timestamp",
            "payment_date",
            "vendor_id",
            "invoice_id",
        ],
        "invoice": ["invoice_id", "invoice_amount", "vendor_id"],
        "approval_record": ["approver_id", "approval_timestamp"],
        "vendor_record": ["vendor_id", "vendor_creation_date"],
    }
    for ev in evidence:
        for field in fields.get(ev.kind, []):
            expected, actual = getattr(tx, field), ev.content.get(field)
            try:
                if isinstance(expected, datetime):
                    matches = datetime.fromisoformat(actual) == expected
                elif isinstance(expected, Decimal):
                    matches = Decimal(str(actual)) == expected
                else:
                    matches = field in ev.content and actual == expected
                if not matches:
                    errors.append(f"{ev.kind}.{field}: missing or conflicting value")
            except (ValueError, TypeError, ArithmeticError):
                errors.append(f"{ev.kind}.{field}: invalid value")
    return errors


def run_tests(tx: Transaction, control: Control, evidence: list[Evidence]) -> list[TestResult]:
    by_kind = {e.kind: e for e in evidence}
    citations = [e.evidence_id for e in evidence if e.kind in control.evidence_required]
    missing = set(control.evidence_required) - by_kind.keys()
    conflicts = reconcile(tx, evidence)
    results = []
    for test in control.tests:
        status, reason = "UNKNOWN", ""
        if missing:
            reason = f"Unavailable evidence: {', '.join(sorted(missing))}"
        elif conflicts:
            reason = "; ".join(conflicts)
        elif test.id == "approval_before_payment":
            approval = by_kind["approval_record"].content
            if (
                approval.get("status") == "absent"
                and tx.approver_id is None
                and tx.approval_timestamp is None
            ):
                status, reason = "FAIL", "Evidence custodian explicitly records no approval."
            elif (
                approval.get("status") != "approved"
                or not tx.approver_id
                or not tx.approval_timestamp
            ):
                reason = "Approval record is incomplete or ambiguous."
            elif tx.approval_timestamp >= tx.payment_timestamp:
                status, reason = "FAIL", "Approval did not precede payment execution."
            else:
                status, reason = "PASS", "Approval precedes payment execution."
        elif (
            test.id == "different_people"
            and by_kind["approval_record"].content.get("status") != "approved"
        ):
            reason = "No unambiguous completed approval to identify an approver."
        elif test.id == "different_people":
            identities = by_kind["user_identity"].content.get("identities", {})
            identities = identities if isinstance(identities, dict) else {}
            creator = identities.get(tx.payment_creator_id)
            approver = identities.get(tx.approver_id)
            if (
                not creator
                or not approver
                or not isinstance(creator, str)
                or not isinstance(approver, str)
            ):
                reason = "Cannot resolve both accounts to canonical people."
            else:
                status = "PASS" if creator != approver else "FAIL"
                reason = (
                    "Canonical person IDs differ."
                    if status == "PASS"
                    else "Same person created and approved payment."
                )
        elif test.id == "recent_vendor":
            age = (tx.payment_date - tx.vendor_creation_date).total_seconds() / 86400
            if tx.payment_date.date() != tx.payment_timestamp.date():
                reason = "Payment date conflicts with execution timestamp."
            elif age < 0:
                reason = "Vendor creation is after payment."
            else:
                status = "FAIL" if age < control.recent_vendor_days else "PASS"
                reason = f"Vendor age at payment: {age:g} days; review trigger is < {control.recent_vendor_days}."
        results.append(
            TestResult(
                test_id=test.id,
                status=status,
                reason=reason,
                citations=citations,
                critical=test.critical,
            )
        )
    return results


def select_scope(tx: Transaction, control: Control) -> str:
    if tx.currency != control.population.currency:
        return "unsupported_currency"
    if tx.payment_amount <= Decimal(str(control.population.payment_amount_gt)):
        return "out"
    return "in"
