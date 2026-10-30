import hashlib
import json
from pathlib import Path

from .models import Evidence, Transaction

KINDS = {
    "invoice",
    "payment_record",
    "approval_record",
    "user_identity",
    "vendor_record",
    "approval_message",
}


def load_population(directory: Path) -> list[Transaction]:
    import pandas as pd

    frame = pd.read_csv(directory / "transactions.csv", dtype=str, keep_default_na=False)
    records = frame.to_dict(orient="records")
    transactions = [
        Transaction.model_validate({k: v if v != "" else None for k, v in r.items()})
        for r in records
    ]
    if len({t.transaction_id for t in transactions}) != len(transactions):
        raise ValueError("Duplicate transaction IDs")
    return transactions


def collect(directory: Path, transaction: Transaction) -> tuple[list[Evidence], list[str]]:
    """Fixed paths only; evidence text never selects tools or file paths."""
    evidence, errors = [], []
    root = directory.resolve()
    for kind in sorted(KINDS):
        path = root / "evidence" / transaction.transaction_id / f"{kind}.json"
        if not path.exists():
            continue
        if not path.resolve().is_relative_to(root):
            errors.append(f"{kind}: source escapes evidence root")
            continue
        try:
            if path.stat().st_size > 100_000:
                raise ValueError("Evidence too large")
            raw = path.read_bytes()
            content = json.loads(raw)
            if (
                not isinstance(content, dict)
                or content.get("transaction_id") != transaction.transaction_id
            ):
                raise ValueError("Evidence transaction mismatch")
            evidence.append(
                Evidence(
                    evidence_id=f"{transaction.transaction_id}:{kind}",
                    transaction_id=transaction.transaction_id,
                    kind=kind,
                    sha256=hashlib.sha256(raw).hexdigest(),
                    source=str(path.relative_to(root)),
                    content=content,
                )
            )
        except (ValueError, OSError) as exc:
            errors.append(f"{kind}: {type(exc).__name__}")
    return evidence, errors
