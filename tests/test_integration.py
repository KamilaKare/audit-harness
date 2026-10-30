import asyncio

import pytest

pytest.importorskip(
    "langgraph", reason="Full dependency installation required for graph integration"
)
pytest.importorskip("pandas", reason="Full dependency installation required for population loading")

from fastapi.testclient import TestClient

from audit_harness.api import create_app
from audit_harness.config import Settings
from audit_harness.evaluation import evaluate_run
from audit_harness.harness import Harness
from audit_harness.llm import OfflineProvider
from audit_harness.models import Judgment
from audit_harness.service import execute
from audit_harness.storage import Store


def test_complete_graph_run_and_api(dataset, tmp_path):
    settings = Settings(
        data_dir=dataset,
        database_url=f"sqlite:///{tmp_path}/audit.db",
        api_key="test-key",
        _env_file=None,
    )
    store = Store(settings.database_url)
    run = asyncio.run(execute(settings, store))
    assert len(run["decisions"]) == 360
    assert evaluate_run(run, dataset / "ground_truth/labels.csv")["quality_gate_passed"]
    with TestClient(create_app(settings)) as client:
        assert client.get("/health").status_code == 200
        assert client.get("/controls").status_code == 401
        client.headers["X-API-Key"] = "test-key"
        assert client.get("/controls").status_code == 200
        assert client.get(f"/runs/{run['run_id']}").json() == run
        assert client.get(f"/runs/{run['run_id']}/reviews").json()
        assert client.get(f"/runs/{run['run_id']}/evaluation").json()["quality_gate_passed"]
        assert client.get("/runs/unknown").status_code == 404
        assert client.post("/runs").status_code == 201


@pytest.mark.parametrize("failure", ["timeout", "citations", "schema"])
def test_model_failures_escalate(dataset, population, controls, failure):
    class BrokenProvider(OfflineProvider):
        async def structured_generate(self, messages, output_schema):
            if failure == "timeout":
                raise TimeoutError()
            if failure == "schema":
                raise ValueError("secret raw document")
            return Judgment(
                recommendation="JUSTIFIED",
                confidence=1,
                rationale="Unsupported",
                citations=["invented"],
            )

    settings = Settings(data_dir=dataset, _env_file=None)
    decision = asyncio.run(Harness(settings, BrokenProvider()).evaluate(population[4], controls[2]))
    assert decision.outcome == "NEEDS_HUMAN_REVIEW"
    assert decision.judgment is None and decision.errors
    assert "secret" not in str(decision.errors)


def test_confident_llm_does_not_override_deterministic_failure(dataset, population, controls):
    class NeverCall(OfflineProvider):
        async def structured_generate(self, *args):
            raise AssertionError("C1 must not call the LLM")

    result = asyncio.run(
        Harness(Settings(data_dir=dataset), NeverCall()).evaluate(population[1], controls[0])
    )
    assert result.outcome == "FAIL"
