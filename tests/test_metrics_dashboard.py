import asyncio
from decimal import Decimal

import httpx
import pytest
from fastapi.testclient import TestClient

from audit_harness.api import create_app
from audit_harness.config import Settings
from audit_harness.dashboard_client import DashboardClient, DashboardError
from audit_harness.dashboard_data import decision_rows, demo_transactions, money, overview
from audit_harness.evaluation import evaluate_run
from audit_harness.harness import Harness
from audit_harness.llm import OfflineProvider
from audit_harness.metrics import (
    Pricing,
    TokenPrice,
    capture_provider_usage,
    distribution,
    measure_control,
    measure_model,
    summarize_metrics,
)
from audit_harness.models import ReviewRequest
from audit_harness.service import execute
from audit_harness.storage import Store


@pytest.fixture
def recorded_run(dataset):
    store = Store("sqlite:///:memory:")
    run = asyncio.run(execute(Settings(data_dir=dataset, _env_file=None), store))
    store.engine.dispose()
    return run


def test_measurements_and_costs_with_real_provider_contract(dataset, population, controls):
    class Model(OfflineProvider):
        name, model = "test-live", "priced-model"

        async def structured_generate(self, messages, output_schema):
            from audit_harness.observability import record_model_usage

            record_model_usage(
                {"prompt_tokens": 1000, "completion_tokens": 500, "total_tokens": 1500}, self.model
            )
            return await super().structured_generate(messages, output_schema)

    harness = Harness(Settings(data_dir=dataset, _env_file=None), Model())
    harness.pricing = Pricing(
        version="test-1",
        models={
            "priced-model": TokenPrice(
                input_per_million_usd=Decimal("1"), output_per_million_usd=Decimal("2")
            )
        },
    )
    result = asyncio.run(harness.evaluate(population[4], controls[2]))
    assert result.execution.llm_attempted and result.execution.llm_succeeded
    assert result.execution.estimated_cost_usd == Decimal("0.00200000")
    assert result.execution.input_tokens == 1000 and result.execution.total_tokens == 1500
    assert result.execution.pricing_version == "test-1"
    assert result.execution.duration_ms >= result.execution.llm_duration_ms >= 0


def test_failed_judgment_still_accounts_for_consumed_tokens(dataset, population, controls):
    class InvalidModel(OfflineProvider):
        name, model = "test-live", "priced-model"

        async def structured_generate(self, messages, output_schema):
            from audit_harness.observability import record_model_usage

            record_model_usage({"prompt_tokens": 100, "completion_tokens": 20}, self.model)
            raise ValueError("invalid structured response")

    harness = Harness(Settings(data_dir=dataset, _env_file=None), InvalidModel())
    result = asyncio.run(harness.evaluate(population[4], controls[2]))
    assert result.outcome == "NEEDS_HUMAN_REVIEW"
    assert result.execution.llm_attempted and not result.execution.llm_succeeded
    assert result.execution.input_tokens == 100
    assert result.execution.estimated_cost_usd is None  # No invented price.


def test_measurements_are_isolated_across_concurrent_calls():
    async def task(value):
        with measure_control(Pricing(version="empty")) as measurement:
            with measure_model(simulated=False):
                await asyncio.sleep(0)
                capture_provider_usage({"prompt_tokens": value}, "model")
        return measurement

    async def run():
        return await asyncio.gather(task(17), task(31))

    results = asyncio.run(run())
    assert [r.input_tokens for r in results] == [17, 31]


def test_unrepresentable_price_never_changes_control_execution():
    pricing = Pricing(
        version="extreme",
        models={
            "model": TokenPrice(
                input_per_million_usd=Decimal("1E+999999"), output_per_million_usd=Decimal(0)
            )
        },
    )
    with measure_control(pricing) as measurement:
        with measure_model(simulated=False):
            capture_provider_usage({"prompt_tokens": 1, "completion_tokens": 0}, "model")

    assert measurement.llm_succeeded
    assert measurement.estimated_cost_usd is None

    # The task-local context is reset even when cost estimation cannot be represented.
    capture_provider_usage({"prompt_tokens": 99}, "model")
    assert measurement.input_tokens == 1


def test_historical_data_is_unknown_and_offline_is_zero_cost(recorded_run):
    measured = summarize_metrics(recorded_run, successful_tasks=120)
    assert measured["measurement_coverage"] == {"recorded": 360, "total": 360}
    assert measured["llm"]["attempts"] == 0
    assert Decimal(measured["cost"]["estimated_total_usd"]) == 0
    for d in recorded_run["decisions"]:
        del d["execution"]
    historical = summarize_metrics(recorded_run)
    assert historical["llm"]["input_tokens"] is None
    assert historical["cost"]["estimated_total_usd"] is None
    assert historical["control_latency_ms"]["p95"] is None


def test_partial_pricing_does_not_pretend_total_cost_is_known():
    run = {
        "decisions": [
            {"execution": {"llm_attempted": True}},
            {"execution": {"estimated_cost_usd": "0.01"}},
        ]
    }
    report = summarize_metrics(run)
    assert report["cost"]["known_subtotal_usd"] == "0.01"
    assert report["cost"]["estimated_total_usd"] is None
    assert report["llm"]["input_tokens"] is None


def test_latency_percentiles_are_explicit():
    assert distribution([])["p95"] is None
    assert distribution([10])["p95"] == 10
    assert distribution([0, 100])["p95"] == 95


def test_dashboard_projections_keep_original_outcome(recorded_run):
    decision = next(
        d for d in recorded_run["decisions"] if d["control_id"] == "C3" and d["review_required"]
    )
    reviews = [{**decision, "revision": 1, "history": [{"outcome": "POLICY_EXCEPTION"}]}]
    rows = decision_rows(recorded_run, reviews)
    row = next(
        r for r in rows if r["Transaction"] == decision["transaction_id"] and r["Contrôle"] == "C3"
    )
    assert row["outcome"] == "NEEDS_HUMAN_REVIEW"
    assert row["Décision humaine"] == "Exception autorisée" and not row["pending"]
    assert overview(recorded_run, reviews)["resolved"] == 1
    assert overview(recorded_run, reviews)["simulated"]
    assert demo_transactions(recorded_run) == {
        "Conforme": "TX-0001",
        "Auto-approbation": "TX-0003",
        "Fournisseur récent": "TX-0005",
    }
    assert money("153850", "EUR") == "153 850,00 EUR"


def test_api_lists_runs_metrics_and_review_options(recorded_run, dataset, tmp_path):
    url = f"sqlite:///{tmp_path}/runs.db"
    store = Store(url)
    store.save(recorded_run)
    settings = Settings(data_dir=dataset, database_url=url, api_key="test-key", _env_file=None)
    with TestClient(create_app(settings)) as api:
        assert api.get("/runs").status_code == 401
        api.headers["X-API-Key"] = "test-key"
        listing = api.get("/runs").json()
        assert (
            listing[0]["run_id"] == recorded_run["run_id"] and listing[0]["decision_count"] == 360
        )
        assert "population" not in listing[0]
        assert api.get("/runs?limit=101").status_code == 422
        metrics = api.get(f"/runs/{recorded_run['run_id']}/metrics").json()
        assert metrics["control_latency_ms"]["count"] == 360
        assert api.get("/runs/missing/metrics").status_code == 404
        queue = api.get(f"/runs/{recorded_run['run_id']}/reviews").json()
        failure = next(d for d in queue if d["outcome"] == "FAIL")
        assert failure["allowed_outcomes"] == ["FAIL"]
        recent = next(d for d in queue if d["control_id"] == "C3")
        assert "POLICY_EXCEPTION" in recent["allowed_outcomes"]
        report = api.get(f"/runs/{recorded_run['run_id']}/evaluation").json()
        assert (
            report["evaluation_mode"] == "offline_simulator"
            and not report["llm_semantic_quality_evaluated"]
        )
        assert report["performance"]["cost"]["per_successful_task_usd"] == "0"
        result = api.post("/runs", json={"provider": "offline"})
        assert result.status_code == 201
        assert api.post("/runs", json={"provider": "unknown"}).status_code == 422
    store.engine.dispose()


@pytest.mark.parametrize("status", [401, 409, 503])
def test_api_client_handles_errors_without_exposing_keys(status):
    client = DashboardClient(
        "http://test",
        "PRIVATE-KEY",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(status, json={"detail": "private internal details"})
        ),
    )
    with pytest.raises(DashboardError) as error:
        client.list_runs()
    assert error.value.status_code == status
    assert "PRIVATE-KEY" not in str(error.value) and "private internal" not in str(error.value)


def test_api_client_sends_authenticated_review_with_revision():
    def handler(request):
        import json

        assert request.headers["X-API-Key"] == "test-key"
        body = json.loads(request.content)
        assert body["expected_revision"] == 2
        return httpx.Response(200, json={"revision": 3})

    client = DashboardClient("http://test", "test-key", transport=httpx.MockTransport(handler))
    request = ReviewRequest(
        reviewer_id="demo-reviewer",
        outcome="FAIL",
        rationale="Review backed by explicit supporting evidence.",
        citations=["E1"],
        expected_revision=2,
    )
    assert client.save_review("R1", "TX-1", "C1", request.model_dump())["revision"] == 3


def test_api_client_does_not_retry_ambiguous_timeout():
    attempts = []

    def handler(request):
        attempts.append(True)
        raise httpx.ReadTimeout("private-url-or-header")

    client = DashboardClient("http://test", "test-key", transport=httpx.MockTransport(handler))
    with pytest.raises(DashboardError, match="traitement peut continuer"):
        client.start_run("offline")
    assert attempts == [True]


def test_business_success_metric_does_not_claim_semantic_evaluation(recorded_run, dataset):
    report = evaluate_run(recorded_run, dataset / "ground_truth/labels.csv")
    assert report["accuracy"] == 1
    assert not report["llm_semantic_quality_evaluated"]


def test_cost_per_successful_task_includes_failed_task_spend():
    run = {
        "decisions": [
            {"execution": {"estimated_cost_usd": "0.01", "duration_ms": 10}},
            {"execution": {"estimated_cost_usd": "0.03", "duration_ms": 20}},
        ]
    }
    report = summarize_metrics(run, successful_tasks=1)
    assert Decimal(report["cost"]["estimated_total_usd"]) == Decimal("0.04")
    assert Decimal(report["cost"]["mean_per_control_usd"]) == Decimal("0.02")
    assert Decimal(report["cost"]["per_successful_task_usd"]) == Decimal("0.04")
    assert summarize_metrics(run, successful_tasks=0)["cost"]["per_successful_task_usd"] is None
    assert money("0.00001234", "USD", 8) == "0,00001234 USD"


def test_empty_evaluation_is_rejected(tmp_path):
    labels = tmp_path / "labels.csv"
    labels.write_text("transaction_id,control_id,expected_outcome\n")
    with pytest.raises(ValueError, match="cannot be empty"):
        evaluate_run({"run_id": "empty", "decisions": []}, labels)
