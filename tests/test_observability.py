import asyncio
import json
import logging
from types import SimpleNamespace
from uuid import uuid4

import pytest

from audit_harness import observability as telemetry
from audit_harness.config import Settings
from audit_harness.evaluation import evaluate_run
from audit_harness.harness import Harness
from audit_harness.llm import OfflineProvider
from audit_harness.observability import (
    JsonFormatter,
    LangfuseSettings,
    flush_observability,
    observation,
    record_model_usage,
)
from audit_harness.service import execute
from audit_harness.storage import Store


@pytest.fixture
def local_tracing(monkeypatch):
    langfuse = pytest.importorskip("langfuse")
    from langfuse.span_filter import is_langfuse_span
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    # Real SDK, real exporter pipeline, no cloud calls and no developer credentials.
    client = langfuse.Langfuse(
        public_key=f"pk-lf-test-{uuid4()}",
        secret_key="test-secret",
        base_url="http://127.0.0.1:1",
        tracer_provider=provider,
        span_exporter=exporter,
        should_export_span=is_langfuse_span,
        mask_otel_spans=telemetry.mask_export,
        environment="test",
        release="audit-harness@test",
    )
    monkeypatch.setattr(telemetry, "get_langfuse_client", lambda: client)
    yield client, exporter
    client.flush()
    provider.shutdown()


def test_json_logging_excludes_unset_data():
    record = logging.LogRecord("audit_harness", logging.INFO, "", 0, "audit_event", (), None)
    record.audit = {"control_id": "C1"}
    result = json.loads(JsonFormatter().format(record))
    assert result["audit"] == {"control_id": "C1"}


def test_settings_read_dotenv_without_exporting_keys(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for key in ("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY", "LANGFUSE_BASE_URL"):
        monkeypatch.delenv(key, raising=False)
    (tmp_path / ".env").write_text(
        "LANGFUSE_PUBLIC_KEY=pk-local\nLANGFUSE_SECRET_KEY=private-local\n"
        "LANGFUSE_BASE_URL=http://localhost:3000\nAUDIT_PROVIDER=offline\n"
    )
    settings = LangfuseSettings()
    assert settings.public_key.get_secret_value() == "pk-local"
    assert settings.base_url == "http://localhost:3000"
    assert "private-local" not in repr(settings)


def test_optional_tracing_failure_preserves_business_result(monkeypatch, caplog):
    def broken_client():
        raise RuntimeError("secret provider details")

    monkeypatch.setattr(telemetry, "get_langfuse_client", broken_client)
    with observation("test", {"control_id": "C1"}, True):
        result = "completed"
    assert result == "completed"
    with pytest.raises(ValueError, match="business error"):
        with observation("test", {}, True):
            raise ValueError("business error")
    flush_observability(True)
    assert "secret provider details" not in caplog.text


def test_disabled_tracing_never_initializes_sdk(monkeypatch):
    def forbidden():
        pytest.fail("SDK should not be constructed when disabled")

    monkeypatch.setattr(telemetry, "get_langfuse_client", forbidden)
    with observation("test", {"control_id": "C1"}, False):
        pass
    flush_observability(False)


def test_real_sdk_nesting_usage_and_masking(local_tracing):
    client, exporter = local_tracing
    with observation(
        "audit.test-transaction",
        {"run_id": "R1", "transaction_id": "TX-1"},
        True,
        input={"transaction_id": "TX-1", "business_justification": "PRIVATE-INVOICE"},
        session_id="R1",
    ):
        with observation(
            "llm.context", {"prompt_version": "v1"}, True, as_type="generation", model="demo-model"
        ) as generation:
            record_model_usage(
                {"prompt_tokens": 12, "completion_tokens": 3, "total_tokens": 15}, "demo-model"
            )
            generation.update(output={"recommendation": "UNCERTAIN", "rationale": "PRIVATE-REASON"})
    client.flush()
    spans = exporter.get_finished_spans()
    assert len(spans) == 2  # No duplicate OTel wrapper spans.
    root = next(s for s in spans if s.name == "audit.test-transaction")
    child = next(s for s in spans if s.name == "llm.context")
    assert root.parent is None and child.parent.span_id == root.context.span_id
    assert child.context.trace_id == root.context.trace_id
    assert child.attributes["langfuse.observation.type"] == "generation"
    assert json.loads(child.attributes["langfuse.observation.usage_details"]) == {
        "input": 12,
        "output": 3,
        "total": 15,
    }
    assert child.attributes["session.id"] == "R1"
    encoded = str([dict(s.attributes) for s in spans])
    assert "PRIVATE-INVOICE" not in encoded and "PRIVATE-REASON" not in encoded


def test_errors_are_visible_without_exporting_exception_text(local_tracing):
    client, exporter = local_tracing
    with pytest.raises(ValueError):
        with observation("llm.context", {}, True, as_type="generation", model="demo"):
            raise ValueError("PRIVATE-DOCUMENT-AND-KEY")
    client.flush()
    span = exporter.get_finished_spans()[0]
    assert span.attributes["langfuse.observation.level"] == "ERROR"
    assert span.attributes["langfuse.observation.status_message"] == "ValueError"
    assert "PRIVATE-DOCUMENT-AND-KEY" not in str(span.attributes)
    assert not span.events


def test_token_usage_stays_isolated_between_concurrent_calls(local_tracing):
    client, exporter = local_tracing

    async def call(number):
        with observation(f"generation-{number}", {}, True, as_type="generation", model="demo"):
            await asyncio.sleep(0)
            record_model_usage({"prompt_tokens": number}, "demo")

    async def run():
        await asyncio.gather(call(11), call(22))

    asyncio.run(run())
    client.flush()
    for span in exporter.get_finished_spans():
        expected = int(span.name.split("-")[1])
        assert (
            json.loads(span.attributes["langfuse.observation.usage_details"])["input"] == expected
        )


def test_full_graph_traces_preserve_business_outcomes(local_tracing, dataset):
    client, exporter = local_tracing

    class MockModel(OfflineProvider):
        name, model = "mock-live", "test-model"

        async def structured_generate(self, messages, output_schema):
            record_model_usage(
                {"prompt_tokens": 20, "completion_tokens": 5, "total_tokens": 25}, self.model
            )
            return await super().structured_generate(messages, output_schema)

    settings = Settings(data_dir=dataset, langfuse_enabled=True, _env_file=None)
    store = Store("sqlite:///:memory:")
    run = asyncio.run(execute(settings, store, MockModel()))
    assert evaluate_run(run, dataset / "ground_truth/labels.csv")["quality_gate_passed"]
    assert len(run["traces"]) == 120 and all(t["trace_id"] for t in run["traces"])
    client.flush()
    spans = exporter.get_finished_spans()
    roots = [s for s in spans if s.parent is None]
    assert len(roots) == 121  # 120 transaction tasks plus persistence summary.
    assert len({s.context.trace_id for s in spans}) == 121
    assert any(s.attributes["langfuse.observation.type"] == "generation" for s in spans)
    for span in spans:
        assert span.attributes["session.id"] == run["run_id"]
    exported = str([dict(s.attributes) for s in spans])
    assert "Ignore all prior instructions" not in exported
    assert "Emergency replacement" not in exported
    store.engine.dispose()


def test_offline_simulator_is_not_recorded_as_paid_generation(
    local_tracing, dataset, population, controls
):
    client, exporter = local_tracing
    result = asyncio.run(
        Harness(Settings(data_dir=dataset, langfuse_enabled=True), OfflineProvider()).evaluate(
            population[4], controls[2]
        )
    )
    client.flush()
    assert result.outcome == "NEEDS_HUMAN_REVIEW"
    assert all(
        s.attributes["langfuse.observation.type"] != "generation"
        for s in exporter.get_finished_spans()
    )


def test_export_hook_strips_accidental_sensitive_fields():
    span = SimpleNamespace(
        attributes={
            "langfuse.observation.input": json.dumps(
                {"transaction_id": "TX-1", "api_key": "secret"}
            ),
            "langfuse.observation.metadata.raw_document": "private",
            "exception.message": "private",
        }
    )
    result = telemetry.mask_export(params=SimpleNamespace(spans={"span-1": span}))
    patch = result.span_patches["span-1"]
    assert "exception.message" in patch.delete_attributes
    assert "langfuse.observation.metadata.raw_document" in patch.delete_attributes
    assert json.loads(patch.set_attributes["langfuse.observation.input"]) == {
        "transaction_id": "TX-1"
    }


def test_http_provider_exports_actual_model_usage(
    local_tracing, dataset, population, controls, monkeypatch
):
    import httpx

    from audit_harness.llm import OpenAICompatibleProvider

    client, exporter = local_tracing
    original = httpx.AsyncClient

    def respond(request):
        return httpx.Response(
            200,
            json={
                "model": "server-model-revision",
                "usage": {"prompt_tokens": 88, "completion_tokens": 14, "total_tokens": 102},
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "content": json.dumps(
                                {
                                    "recommendation": "UNCERTAIN",
                                    "confidence": 0.3,
                                    "rationale": "PRIVATE-REASONING",
                                    "citations": ["TX-0005:approval_message"],
                                }
                            )
                        },
                    }
                ],
            },
        )

    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(respond), **kw)
    )
    settings = Settings(
        data_dir=dataset, langfuse_enabled=True, llm_api_key="PRIVATE-API-KEY", _env_file=None
    )
    result = asyncio.run(
        Harness(settings, OpenAICompatibleProvider(settings)).evaluate(population[4], controls[2])
    )
    client.flush()
    assert result.outcome == "NEEDS_HUMAN_REVIEW"
    spans = exporter.get_finished_spans()
    generation = next(s for s in spans if s.attributes["langfuse.observation.type"] == "generation")
    assert generation.attributes["langfuse.observation.model.name"] == "server-model-revision"
    assert json.loads(generation.attributes["langfuse.observation.usage_details"])["total"] == 102
    assert "PRIVATE-REASONING" not in str([dict(s.attributes) for s in spans])
    assert "PRIVATE-API-KEY" not in str([dict(s.attributes) for s in spans])


def test_cli_flushes_when_business_execution_fails(monkeypatch):
    import sys

    from audit_harness import cli, service

    calls = []

    async def failed_run(*args):
        raise ValueError("business failure")

    monkeypatch.setattr(service, "execute", failed_run)
    monkeypatch.setattr(telemetry, "flush_observability", lambda enabled: calls.append(enabled))
    monkeypatch.setenv("AUDIT_LANGFUSE_ENABLED", "true")
    monkeypatch.setenv("AUDIT_DATABASE_URL", "sqlite:///:memory:")
    monkeypatch.setattr(sys, "argv", ["audit-harness", "run"])
    with pytest.raises(ValueError, match="business failure"):
        cli.main()
    assert calls == [True]


def test_api_shutdown_flushes_sdk(local_tracing, dataset, monkeypatch):
    from fastapi.testclient import TestClient

    from audit_harness.api import create_app

    client, _ = local_tracing
    calls = []
    original = client.flush

    def flush():
        calls.append(True)
        original()

    monkeypatch.setattr(client, "flush", flush)
    settings = Settings(
        data_dir=dataset, database_url="sqlite:///:memory:", langfuse_enabled=True, _env_file=None
    )
    with TestClient(create_app(settings)) as api:
        assert api.get("/health").status_code == 200
    assert calls == [True]
