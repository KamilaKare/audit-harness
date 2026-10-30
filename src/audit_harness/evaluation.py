"""Task-level metrics against independently authored scenario labels."""

import csv
from collections import Counter
from pathlib import Path

from .storage import digest


def evaluate_run(run: dict, labels_path: Path) -> dict:
    with labels_path.open() as stream:
        labels = list(csv.DictReader(stream))
    expected = {(r["transaction_id"], r["control_id"]): r["expected_outcome"] for r in labels}
    actual = {(d["transaction_id"], d["control_id"]): d for d in run["decisions"]}
    if len(expected) != len(labels) or len(actual) != len(run["decisions"]):
        raise ValueError("Duplicate evaluation keys")
    if expected.keys() != actual.keys():
        raise ValueError("Ground truth and run populations do not match exactly")
    confusion, mismatches = Counter(), []
    correct, unsafe, critical, caught, cited, evidence_checks = 0, 0, 0, 0, 0, 0
    for key, label in expected.items():
        decision = actual[key]
        prediction = decision["outcome"]
        confusion[f"{label} -> {prediction}"] += 1
        correct += label == prediction
        unsafe += label != "PASS" and prediction == "PASS"
        critical += label == "FAIL"
        caught += label == "FAIL" and prediction == "FAIL"
        if label != prediction:
            mismatches.append(
                {
                    "transaction_id": key[0],
                    "control_id": key[1],
                    "expected": label,
                    "actual": prediction,
                }
            )
        ids = {e["evidence_id"] for e in decision["evidence"]}
        for test in decision["tests"]:
            evidence_checks += 1
            cited += bool(test["citations"]) and set(test["citations"]) <= ids
    tx_ids = {key[0] for key in expected}
    successful = sum(
        all(actual[k]["outcome"] == expected[k] for k in expected if k[0] == tx) for tx in tx_ids
    )
    return {
        "run_id": run["run_id"],
        "labels_sha256": digest({"labels": labels}),
        "decisions": len(expected),
        "accuracy": correct / len(expected),
        "business_task_success_rate": successful / len(tx_ids),
        "unsafe_passes": unsafe,
        "critical_failure_recall": caught / critical if critical else None,
        "test_citation_coverage": cited / evidence_checks if evidence_checks else 1,
        "review_rate": sum(d["review_required"] for d in actual.values()) / len(actual),
        "confusion_matrix": dict(sorted(confusion.items())),
        "mismatches": mismatches,
        "quality_gate_passed": correct == len(expected) and unsafe == 0,
        "limitations": "Synthetic task correctness only; does not measure live model quality or confidence calibration.",
    }
