import json
import logging
import sys
from contextlib import contextmanager
from datetime import UTC, datetime

from opentelemetry import trace

logger = logging.getLogger("audit_harness")
tracer = trace.get_tracer("audit_harness", "0.1.0")


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
    """Applications can attach an OTLP exporter to the standard global tracer provider."""
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(JsonFormatter())
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False
    # Preserve a provider installed by deployment auto-instrumentation.
    if isinstance(trace.get_tracer_provider(), trace.ProxyTracerProvider):
        from opentelemetry.sdk.trace import TracerProvider

        trace.set_tracer_provider(TracerProvider())


@contextmanager
def observation(name: str, attributes: dict, langfuse_enabled: bool = False):
    """Metadata only; optional export failures cannot change a control decision."""
    with tracer.start_as_current_span(name, attributes=attributes):
        manager = None
        if langfuse_enabled:
            try:
                from langfuse import get_client

                candidate = get_client().start_as_current_observation(
                    name=name, metadata=attributes
                )
                candidate.__enter__()
                manager = candidate
            except Exception:
                logger.warning("langfuse_observation_unavailable")
        try:
            yield
        finally:
            if manager is not None:
                try:
                    manager.__exit__(*sys.exc_info())
                except Exception:
                    logger.warning("langfuse_export_unavailable")
            logger.info(
                "audit_event",
                extra={
                    "audit": {"event": name, **attributes},
                    "trace_id": format(
                        trace.get_current_span().get_span_context().trace_id, "032x"
                    ),
                },
            )
