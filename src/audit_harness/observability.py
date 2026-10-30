"""Explicit, metadata-only telemetry. No automatic argument or exception capture."""

import json
import logging
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from functools import lru_cache

from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode
from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger("audit_harness")
tracer = trace.get_tracer("audit_harness", "0.1.0")
_active_generation: ContextVar = ContextVar("audit_generation", default=None)

# Deliberately exclude free-text evidence, rationale, request headers and settings.
SAFE_KEYS = frozenset(
    {
        "run_id",
        "transaction_id",
        "control_id",
        "control_version",
        "controls_sha256",
        "provider",
        "model",
        "prompt_version",
        "simulated",
        "transaction_count",
        "decision_count",
        "evidence_count",
        "evidence_id",
        "sha256",
        "kind",
        "evidence",
        "tests",
        "test_id",
        "status",
        "outcome",
        "in_scope",
        "review_required",
        "review_count",
        "outcomes",
        "PASS",
        "FAIL",
        "INSUFFICIENT_EVIDENCE",
        "NEEDS_HUMAN_REVIEW",
        "POLICY_EXCEPTION",
        "recommendation",
        "confidence",
        "citation_count",
        "error_count",
        "error_type",
        "scope",
        "duration_seconds",
        "input",
        "output",
        "total",
        "version",
        "harness_version",
        "min_confidence",
    }
)


class LangfuseSettings(BaseSettings):
    # Pydantic's app Settings does not export .env into os.environ. Load these explicitly.
    model_config = SettingsConfigDict(env_prefix="LANGFUSE_", env_file=".env", extra="ignore")
    public_key: SecretStr = SecretStr("")
    secret_key: SecretStr = SecretStr("")
    base_url: str = "https://cloud.langfuse.com"
    tracing_environment: str = "development"
    release: str = "audit-harness@0.1.0"
    sample_rate: float = Field(default=1, ge=0, le=1)


def safe_data(value):
    if isinstance(value, dict):
        return {key: safe_data(item) for key, item in value.items() if key in SAFE_KEYS}
    if isinstance(value, (list, tuple)):
        return [safe_data(item) for item in value if isinstance(item, dict)]
    if isinstance(value, (str, bool, int, float)) or value is None:
        return value
    return "[omitted]"


def mask_export(*, params):
    """Defense in depth on exported SDK spans (compatible with SDK v4 and v5)."""
    from langfuse.types import MaskOtelSpansResult, OtelSpanPatch

    patches = {}
    for identifier, span in params.spans.items():
        remove, replace = [], {}
        for key, value in span.attributes.items():
            if key.startswith(("exception.", "gen_ai.prompt", "gen_ai.completion")):
                remove.append(key)
            elif key in {"langfuse.observation.input", "langfuse.observation.output"}:
                try:
                    decoded = json.loads(value)
                except (TypeError, ValueError):
                    decoded = {}
                replace[key] = json.dumps(safe_data(decoded) if isinstance(decoded, dict) else {})
            elif ".metadata." in key and key.rsplit(".", 1)[-1] not in SAFE_KEYS:
                remove.append(key)
        if remove or replace:
            patches[identifier] = OtelSpanPatch(delete_attributes=remove, set_attributes=replace)
    return MaskOtelSpansResult(span_patches=patches)


@lru_cache(maxsize=1)
def get_langfuse_client():
    from langfuse import Langfuse
    from langfuse.span_filter import is_langfuse_span

    settings = LangfuseSettings()
    if not settings.public_key.get_secret_value() or not settings.secret_key.get_secret_value():
        raise ValueError("Configure Langfuse project keys in .env")
    return Langfuse(
        public_key=settings.public_key.get_secret_value(),
        secret_key=settings.secret_key.get_secret_value(),
        base_url=settings.base_url,
        environment=settings.tracing_environment,
        release=settings.release,
        sample_rate=settings.sample_rate,
        should_export_span=is_langfuse_span,
        mask_otel_spans=mask_export,
    )


class JsonFormatter(logging.Formatter):
    def format(self, record):
        return json.dumps(
            {
                "timestamp": datetime.now(UTC).isoformat(),
                "level": record.levelname,
                "message": record.getMessage(),
                "trace_id": getattr(record, "trace_id", None),
                "audit": getattr(record, "audit", {}),
            }
        )


def configure_observability():
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(JsonFormatter())
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False
    if isinstance(trace.get_tracer_provider(), trace.ProxyTracerProvider):
        from opentelemetry.sdk.trace import TracerProvider

        trace.set_tracer_provider(TracerProvider())


class Observation:
    def __init__(self, sdk_span=None):
        self.sdk_span = sdk_span

    def update(self, **values):
        if self.sdk_span is not None:
            try:
                self.sdk_span.update(
                    **{
                        key: safe_data(value)
                        if key in {"input", "output", "metadata", "usage_details"}
                        else value
                        for key, value in values.items()
                    }
                )
            except Exception:
                logger.warning("langfuse_update_unavailable")


def record_model_usage(usage: dict, model: str):
    """Provider telemetry stays task-local, including concurrent async invocations."""
    from .metrics import capture_provider_usage

    capture_provider_usage(usage, model)
    active = _active_generation.get()
    if active is None or not isinstance(usage, dict):
        return
    counts = {}
    for source, target in (
        ("prompt_tokens", "input"),
        ("completion_tokens", "output"),
        ("total_tokens", "total"),
    ):
        value = usage.get(source)
        if type(value) is int and value >= 0:
            counts[target] = value
    active.update(usage_details=counts, model=model)


def flush_observability(enabled: bool):
    if enabled:
        try:
            get_langfuse_client().flush()
        except Exception:
            logger.warning("langfuse_flush_unavailable")


@contextmanager
def observation(
    name: str,
    attributes: dict,
    langfuse_enabled: bool = False,
    *,
    as_type: str = "span",
    input: dict | None = None,
    model: str | None = None,
    version: str | None = None,
    session_id: str | None = None,
):
    """One span per step, never a duplicate OTel parent plus a Langfuse child."""
    attributes = safe_data(attributes)
    manager, propagation, handle = None, None, Observation()
    if langfuse_enabled:
        try:
            from langfuse import propagate_attributes

            manager = get_langfuse_client().start_as_current_observation(
                name=name,
                as_type=as_type,
                metadata=attributes,
                input=safe_data(input or {}),
                model=model,
                version=version,
            )
            handle = Observation(manager.__enter__())
            if session_id:
                propagation = propagate_attributes(
                    session_id=session_id,
                    trace_name=name,
                    tags=["purchase-to-pay", "internal-controls"],
                    metadata=attributes,
                )
                propagation.__enter__()
        except Exception:
            logger.warning("langfuse_observation_unavailable")
    fallback = None
    if handle.sdk_span is None:
        fallback = tracer.start_as_current_span(
            name,
            attributes=attributes,
            record_exception=False,
            set_status_on_exception=False,
        )
        fallback.__enter__()
    token = _active_generation.set(handle) if as_type == "generation" else None
    try:
        yield handle
    except BaseException as exc:
        # SDK automatic exception recording could export document text or API response bodies.
        error_type = type(exc).__name__
        handle.update(level="ERROR", status_message=error_type, output={"error_type": error_type})
        trace.get_current_span().set_status(Status(StatusCode.ERROR, error_type))
        raise
    finally:
        trace_id = format(trace.get_current_span().get_span_context().trace_id, "032x")
        if token is not None:
            _active_generation.reset(token)
        for context in (propagation, manager, fallback):
            if context is not None:
                try:
                    context.__exit__(None, None, None)
                except Exception:
                    logger.warning("langfuse_export_unavailable")
        logger.info(
            "audit_event", extra={"audit": {"event": name, **attributes}, "trace_id": trace_id}
        )


def current_trace():
    context = trace.get_current_span().get_span_context()
    return format(context.trace_id, "032x") if context.is_valid else None
