"""Execution measurements and explicitly configured cost estimates, independent of tracing."""

import math
import time
from contextlib import contextmanager
from contextvars import ContextVar
from decimal import Decimal, DecimalException
from pathlib import Path
from statistics import mean, median

import yaml
from pydantic import Field

from .models import ExecutionMetrics, StrictModel


class TokenPrice(StrictModel):
    input_per_million_usd: Decimal = Field(ge=0)
    output_per_million_usd: Decimal = Field(ge=0)


class Pricing(StrictModel):
    version: str = Field(min_length=1)
    models: dict[str, TokenPrice] = Field(default_factory=dict)


_current: ContextVar[ExecutionMetrics | None] = ContextVar("control_metrics", default=None)


def load_pricing(path: Path) -> Pricing:
    if not path.exists():
        return Pricing(version="unconfigured")
    return Pricing.model_validate(yaml.safe_load(path.read_text()))


@contextmanager
def measure_control(pricing: Pricing):
    measurement = ExecutionMetrics()
    token, started = _current.set(measurement), time.perf_counter()
    try:
        yield measurement
    finally:
        try:
            measurement.duration_ms = round((time.perf_counter() - started) * 1000, 3)
            if not measurement.llm_attempted:
                measurement.estimated_cost_usd = Decimal(0)
            elif (price := pricing.models.get(measurement.model or "")) is not None:
                if measurement.input_tokens is not None and measurement.output_tokens is not None:
                    try:
                        measurement.estimated_cost_usd = (
                            (
                                measurement.input_tokens * price.input_per_million_usd
                                + measurement.output_tokens * price.output_per_million_usd
                            )
                            / Decimal(1_000_000)
                        ).quantize(Decimal("0.00000001"))
                        measurement.pricing_version = pricing.version
                    except DecimalException:
                        # Telemetry and cost estimates must never alter a control outcome.
                        measurement.estimated_cost_usd = None
        finally:
            _current.reset(token)


@contextmanager
def measure_model(simulated: bool):
    measurement = _current.get()
    started = time.perf_counter()
    if measurement is not None and not simulated:
        measurement.llm_attempted = True
    try:
        yield
        if measurement is not None and not simulated:
            measurement.llm_succeeded = True
    finally:
        if measurement is not None and not simulated:
            measurement.llm_duration_ms = round((time.perf_counter() - started) * 1000, 3)


def capture_provider_usage(usage: dict, model: str):
    measurement = _current.get()
    if measurement is None or not isinstance(usage, dict):
        return
    measurement.model = model if isinstance(model, str) else None
    for source, field in (
        ("prompt_tokens", "input_tokens"),
        ("completion_tokens", "output_tokens"),
        ("total_tokens", "total_tokens"),
    ):
        value = usage.get(source)
        if type(value) is int and value >= 0:
            setattr(measurement, field, value)


def distribution(values: list[float]) -> dict:
    if not values:
        return {"count": 0, "mean": None, "p50": None, "p95": None, "max": None}
    ordered = sorted(values)
    position = (len(ordered) - 1) * 0.95
    low, high = math.floor(position), math.ceil(position)
    p95 = ordered[low] + (ordered[high] - ordered[low]) * (position - low)
    return {
        "count": len(values),
        "mean": round(mean(values), 3),
        "p50": round(median(values), 3),
        "p95": round(p95, 3),
        "max": max(values),
    }


def summarize_metrics(run: dict, successful_tasks: int | None = None) -> dict:
    decisions = run["decisions"]
    measured = [
        ExecutionMetrics.model_validate(d["execution"]) for d in decisions if d.get("execution")
    ]
    calls = [m for m in measured if m.llm_attempted]
    priced = [m for m in measured if m.estimated_cost_usd is not None]
    complete = len(priced) == len(decisions) and bool(decisions)
    subtotal = sum((m.estimated_cost_usd for m in priced), Decimal(0))
    token_complete = all(m.input_tokens is not None and m.output_tokens is not None for m in calls)
    # Older runs did not persist these measurements. Absence must never appear as zero cost.
    coverage = len(measured) == len(decisions) and bool(decisions)
    return {
        "measurement_coverage": {"recorded": len(measured), "total": len(decisions)},
        "control_latency_ms": distribution([m.duration_ms for m in measured]),
        "llm_latency_ms": distribution(
            [m.llm_duration_ms for m in calls if m.llm_duration_ms is not None]
        ),
        "llm": {
            "attempts": len(calls) if coverage else None,
            "succeeded": sum(m.llm_succeeded for m in calls) if coverage else None,
            "failed": sum(not m.llm_succeeded for m in calls) if coverage else None,
            "input_tokens": sum(m.input_tokens or 0 for m in calls)
            if coverage and token_complete
            else None,
            "output_tokens": sum(m.output_tokens or 0 for m in calls)
            if coverage and token_complete
            else None,
        },
        "cost": {
            "currency": "USD",
            "estimated": True,
            "priced_controls": len(priced),
            "known_subtotal_usd": str(subtotal) if priced else None,
            "estimated_total_usd": str(subtotal) if complete else None,
            "mean_per_control_usd": str(subtotal / len(decisions)) if complete else None,
            "per_successful_task_usd": str(subtotal / successful_tasks)
            if complete and successful_tasks
            else None,
            "pricing_versions": sorted({m.pricing_version for m in priced if m.pricing_version}),
        },
    }
