import hashlib
import json
import time
from collections import Counter
from datetime import UTC, datetime
from uuid import uuid4

from .config import Settings, load_controls
from .evidence import load_population
from .harness import Harness
from .llm import PROMPT_VERSION, LLMProvider, get_provider
from .observability import current_trace, observation
from .storage import Store


async def execute(settings: Settings, store: Store, provider: LLMProvider | None = None) -> dict:
    controls, config_hash = load_controls(settings.controls_dir)
    transactions = load_population(settings.data_dir)
    provider = provider or get_provider(settings)
    harness = Harness(settings, provider)
    run_id, started = str(uuid4()), time.monotonic()
    decisions, traces = [], []
    # A transaction is a self-contained business task. A run groups its traces in one session.
    for tx in transactions:
        with observation(
            "audit.test-transaction",
            {
                "run_id": run_id,
                "transaction_id": tx.transaction_id,
                "controls_sha256": config_hash,
                "provider": provider.name,
                "model": provider.model,
            },
            settings.langfuse_enabled,
            input={"transaction_id": tx.transaction_id, "decision_count": len(controls)},
            session_id=run_id,
        ) as span:
            results = [await harness.evaluate(tx, control) for control in controls]
            decisions.extend(result.model_dump(mode="json") for result in results)
            span.update(
                output={
                    "outcomes": dict(Counter(d.outcome.value for d in results)),
                    "review_count": sum(d.review_required for d in results),
                }
            )
            traces.append({"transaction_id": tx.transaction_id, "trace_id": current_trace()})
    population = [tx.model_dump(mode="json") for tx in transactions]
    payload = {
        "run_id": run_id,
        "created_at": datetime.now(UTC).isoformat(),
        "harness_version": "0.1.0",
        "controls_sha256": config_hash,
        "controls": [c.model_dump(mode="json") for c in controls],
        "population_sha256": hashlib.sha256(
            json.dumps(population, sort_keys=True).encode()
        ).hexdigest(),
        "population": population,
        "provider": provider.name,
        "model": provider.model,
        "prompt_version": PROMPT_VERSION,
        "duration_seconds": round(time.monotonic() - started, 4),
        "traces": traces,
        "decisions": decisions,
    }
    with observation(
        "audit.persist-run",
        {"run_id": run_id},
        settings.langfuse_enabled,
        session_id=run_id,
        input={"decision_count": len(decisions)},
    ) as span:
        store.save(payload)
        span.update(output={"decision_count": len(decisions)})
    return payload
