import hashlib
import json
import time
from datetime import UTC, datetime
from uuid import uuid4

from .config import Settings, load_controls
from .evidence import load_population
from .harness import Harness
from .llm import PROMPT_VERSION, LLMProvider, get_provider
from .observability import observation
from .storage import Store


async def execute(settings: Settings, store: Store, provider: LLMProvider | None = None) -> dict:
    controls, config_hash = load_controls(settings.controls_dir)
    transactions = load_population(settings.data_dir)
    provider = provider or get_provider(settings)
    harness = Harness(settings, provider)
    run_id = str(uuid4())
    with observation(
        "audit.run",
        {
            "run_id": run_id,
            "controls_sha256": config_hash,
            "provider": provider.name,
            "model": provider.model,
        },
        settings.langfuse_enabled,
    ):
        started = time.monotonic()
        decisions = []
        for tx in transactions:
            for control in controls:
                decisions.append((await harness.evaluate(tx, control)).model_dump(mode="json"))
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
            "decisions": decisions,
        }
        store.save(payload)
        return payload
