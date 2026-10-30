import pytest
from sqlalchemy import update
from test_controls import assess

from audit_harness.evaluation import evaluate_run
from audit_harness.models import ReviewRequest
from audit_harness.storage import Store, runs


@pytest.fixture
def run(dataset, population, controls):
    return {
        "run_id": "test-run",
        "decisions": [
            assess(tx, c, dataset).model_dump(mode="json") for tx in population for c in controls
        ],
    }


def test_task_evaluation_and_population_mismatch(run, dataset):
    report = evaluate_run(run, dataset / "ground_truth/labels.csv")
    assert report["accuracy"] == report["business_task_success_rate"] == 1
    assert report["critical_failure_recall"] == 1
    assert report["unsafe_passes"] == 0
    assert report["quality_gate_passed"]
    run["decisions"].pop()
    with pytest.raises(ValueError):
        evaluate_run(run, dataset / "ground_truth/labels.csv")


def test_evaluation_detects_unsafe_pass(run, dataset):
    decision = next(d for d in run["decisions"] if d["outcome"] == "FAIL")
    decision["outcome"] = "PASS"
    report = evaluate_run(run, dataset / "ground_truth/labels.csv")
    assert report["unsafe_passes"] == 1 and not report["quality_gate_passed"]


def test_persistence_review_history_and_conflict(run, tmp_path):
    store = Store(f"sqlite:///{tmp_path}/audit.db")
    store.save(run)
    reopened = Store(f"sqlite:///{tmp_path}/audit.db")
    assert reopened.get("test-run") == run
    request = ReviewRequest(
        reviewer_id="auditor-1",
        outcome="POLICY_EXCEPTION",
        rationale="Emergency purchase corroborated by supporting evidence.",
        citations=["TX-0005:approval_message"],
        expected_revision=0,
    )
    result = reopened.review("test-run", "TX-0005", "C3", request)
    assert result["revision"] == 1
    assert result["history"][0]["original_outcome"] == "NEEDS_HUMAN_REVIEW"
    with pytest.raises(RuntimeError):
        reopened.review("test-run", "TX-0005", "C3", request)
    revised = request.model_copy(update={"expected_revision": 1, "outcome": "FAIL"})
    assert reopened.review("test-run", "TX-0005", "C3", revised)["revision"] == 2
    assert reopened.get("test-run") == run  # Original machine decision remains intact.
    assert any(item["resolved"] for item in reopened.queue("test-run"))
    store.engine.dispose()
    reopened.engine.dispose()


def test_reviews_reject_unknown_citations_and_failure_override(run):
    store = Store("sqlite:///:memory:")
    store.save(run)
    request = ReviewRequest(
        reviewer_id="auditor-1",
        outcome="PASS",
        rationale="Reviewed supporting evidence thoroughly.",
        citations=["invented-evidence"],
        expected_revision=0,
    )
    with pytest.raises(ValueError, match="citation"):
        store.review("test-run", "TX-0005", "C3", request)
    request.citations = ["TX-0002:approval_record"]
    with pytest.raises(ValueError, match="override"):
        store.review("test-run", "TX-0002", "C1", request)
    request.outcome = "POLICY_EXCEPTION"
    with pytest.raises(ValueError, match="Only C3"):
        store.review("test-run", "TX-0002", "C1", request)


def test_integrity_check_detects_mutation(run):
    store = Store("sqlite:///:memory:")
    store.save(run)
    with store.engine.begin() as conn:
        conn.execute(update(runs).values(payload={"run_id": "tampered"}))
    with pytest.raises(ValueError, match="integrity"):
        store.get("test-run")
