import csv
import hashlib
import json
from datetime import timedelta

import pytest
from pydantic import ValidationError

from audit_harness.config import Control, load_controls
from audit_harness.evidence import collect
from audit_harness.models import Judgment, Outcome, Transaction
from audit_harness.policy import decide
from audit_harness.rules import run_tests, select_scope
from audit_harness.synthetic import generate


def assess(tx, control, directory):
    evidence, errors = collect(directory, tx)
    scope = select_scope(tx, control)
    tests = run_tests(tx, control, evidence) if scope == "in" else []
    return decide(tx, control, evidence, tests, scope, None, errors)


def test_all_360_scenario_labels(dataset, population, controls):
    with (dataset / "ground_truth/labels.csv").open() as stream:
        truth = {
            (r["transaction_id"], r["control_id"]): r["expected_outcome"]
            for r in csv.DictReader(stream)
        }
    for tx in population:
        for control in controls:
            result = assess(tx, control, dataset)
            assert result.outcome == truth[(tx.transaction_id, control.control_id)], (tx, result)
            ids = {e.evidence_id for e in result.evidence}
            assert all(set(t.citations) <= ids for t in result.tests)
            for ev in result.evidence:
                assert hashlib.sha256((dataset / ev.source).read_bytes()).hexdigest() == ev.sha256


@pytest.mark.parametrize(
    "amount,scope", [("49999.99", "out"), ("50000", "out"), ("50000.01", "in")]
)
def test_strict_threshold(population, controls, amount, scope):
    tx = Transaction.model_validate({**population[0].model_dump(), "payment_amount": amount})
    assert select_scope(tx, controls[0]) == scope


def test_equal_approval_timestamp_fails(dataset, population, controls):
    tx = population[0].model_copy(update={"approval_timestamp": population[0].payment_timestamp})
    evidence, _ = collect(dataset, population[0])
    for ev in evidence:
        if ev.kind == "approval_record":
            ev.content["approval_timestamp"] = tx.approval_timestamp.isoformat()
    assert run_tests(tx, controls[0], evidence)[0].status == "FAIL"


def test_subday_vendor_age_triggers(dataset, population, controls):
    tx = population[0].model_copy(
        update={"vendor_creation_date": population[0].payment_date - timedelta(days=6, hours=23)}
    )
    evidence, _ = collect(dataset, population[0])
    next(e for e in evidence if e.kind == "vendor_record").content["vendor_creation_date"] = (
        tx.vendor_creation_date.isoformat()
    )
    assert run_tests(tx, controls[2], evidence)[0].status == "FAIL"


@pytest.mark.parametrize("confidence", [0.1, 0.85, 1])
def test_model_cannot_authorize_exception(dataset, population, controls, confidence):
    tx, control = population[4], controls[2]
    evidence, errors = collect(dataset, tx)
    tests = run_tests(tx, control, evidence)
    judgment = Judgment(
        recommendation="JUSTIFIED",
        confidence=confidence,
        rationale="Plausible emergency",
        citations=[f"{tx.transaction_id}:approval_message"],
    )
    result = decide(tx, control, evidence, tests, "in", judgment, errors)
    assert result.outcome == Outcome.NEEDS_HUMAN_REVIEW
    assert result.review_required
    assert ("below policy" in result.reason) == (confidence < 0.85)


def test_corrupt_and_cross_transaction_evidence(tmp_path, population):
    root = tmp_path / "evidence" / population[0].transaction_id
    root.mkdir(parents=True)
    (root / "invoice.json").write_text("not-json")
    (root / "payment_record.json").write_text(json.dumps({"transaction_id": "OTHER"}))
    evidence, errors = collect(tmp_path, population[0])
    assert not evidence and len(errors) == 2


def test_symlink_escape_rejected(tmp_path, population):
    outside = tmp_path.parent / "outside-evidence.json"
    outside.write_text(json.dumps({"transaction_id": population[0].transaction_id}))
    root = tmp_path / "evidence" / population[0].transaction_id
    root.mkdir(parents=True)
    (root / "invoice.json").symlink_to(outside)
    evidence, errors = collect(tmp_path, population[0])
    assert not evidence and "escapes" in errors[0]


def test_dynamic_config_and_unknown_rule_rejected(tmp_path, controls):
    import yaml

    config = controls[0].model_dump()
    config["population"]["payment_amount_gt"] = 100
    config["version"] = "2.0"
    (tmp_path / "control.yaml").write_text(yaml.safe_dump(config))
    loaded, digest = load_controls(tmp_path)
    assert loaded[0].version == "2.0" and len(digest) == 64
    config["tests"][0]["id"] = "execute_arbitrary_python"
    with pytest.raises(ValidationError):
        Control.model_validate(config)


def test_malformed_identity_routes_to_unknown(dataset, population, controls):
    evidence, _ = collect(dataset, population[0])
    next(e for e in evidence if e.kind == "user_identity").content["identities"] = []
    assert run_tests(population[0], controls[1], evidence)[0].status == "UNKNOWN"


def test_dataset_reproducible_and_refuses_overwrite(tmp_path, dataset):
    generate(tmp_path)
    for path in dataset.rglob("*"):
        if path.is_file():
            assert path.read_bytes() == (tmp_path / path.relative_to(dataset)).read_bytes()
    with pytest.raises(ValueError):
        generate(tmp_path)


def test_timezone_and_traversal_ids_rejected(population):
    for change in ({"transaction_id": "../secret"}, {"payment_timestamp": "2026-01-01T00:00:00"}):
        with pytest.raises(ValidationError):
            Transaction.model_validate({**population[0].model_dump(), **change})


@pytest.mark.parametrize("missing", [["approval_record"], ["payment_record", "user_identity"]])
def test_missing_required_evidence_blocks_self_approval_conclusion(
    dataset, population, controls, missing
):
    tx, control = population[2], controls[1]
    evidence, errors = collect(dataset, tx)
    evidence = [e for e in evidence if e.kind not in missing]
    tests = run_tests(tx, control, evidence)
    result = decide(tx, control, evidence, tests, "in", None, errors)
    assert tests[0].status == "UNKNOWN"
    assert result.outcome == "INSUFFICIENT_EVIDENCE"


def test_known_sod_failure_survives_error_loading_optional_invoice(
    tmp_path, population, controls, dataset
):
    import shutil

    tx, control = population[2], controls[1]
    shutil.copytree(
        dataset / "evidence" / tx.transaction_id, tmp_path / "evidence" / tx.transaction_id
    )
    (tmp_path / "evidence" / tx.transaction_id / "invoice.json").write_text("malformed")
    evidence, errors = collect(tmp_path, tx)
    tests = run_tests(tx, control, evidence)
    result = decide(tx, control, evidence, tests, "in", None, errors)
    assert errors and tests[0].status == "FAIL"
    assert result.outcome == "FAIL"


def test_conflicting_required_payment_prevents_unfounded_sod_failure(dataset, population, controls):
    tx, control = population[2], controls[1]
    evidence, errors = collect(dataset, tx)
    next(e for e in evidence if e.kind == "payment_record").content["payment_creator_id"] = (
        "SOMEONE-ELSE"
    )
    tests = run_tests(tx, control, evidence)
    result = decide(tx, control, evidence, tests, "in", None, errors)
    assert tests[0].status == "UNKNOWN" and result.outcome == "INSUFFICIENT_EVIDENCE"


def test_established_critical_failure_precedes_unknown_test_and_collection_errors(
    dataset, population, controls
):
    from audit_harness.models import TestResult

    tx, control = population[3], controls[0]
    evidence, _ = collect(dataset, tx)
    known_failure = run_tests(tx, control, evidence)[0]
    unknown = TestResult(
        test_id="different_people", status="UNKNOWN", reason="Missing identities", citations=[]
    )
    result = decide(
        tx, control, evidence, [known_failure, unknown], "in", None, ["invoice: OSError"]
    )
    assert known_failure.status == "FAIL" and result.outcome == "FAIL"
