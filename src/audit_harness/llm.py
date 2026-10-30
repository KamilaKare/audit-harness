"""Provider boundary. No provider is allowed to decide control outcomes."""

import json
from typing import Protocol, TypeVar

import httpx
from pydantic import BaseModel

from .config import Settings
from .models import StrictModel

T = TypeVar("T", bound=BaseModel)
PROMPT_VERSION = "vendor-context-1.0"


class Message(StrictModel):
    role: str
    content: str


class LLMProvider(Protocol):
    name: str
    model: str

    async def structured_generate(self, messages: list[Message], output_schema: type[T]) -> T: ...


class OfflineProvider:
    """Deterministic contract simulator; not an actual language model or calibrated confidence."""

    name = "offline"
    model = "context-simulator-1.0"

    async def structured_generate(self, messages: list[Message], output_schema: type[T]) -> T:
        payload = json.loads(messages[-1].content)
        evidence = payload["context_evidence"]
        return output_schema.model_validate(
            {
                "recommendation": "UNCERTAIN",
                "confidence": 0.5,
                "rationale": "Offline simulator cannot establish a legitimate business exception. Human assessment required.",
                "citations": [e["evidence_id"] for e in evidence],
            }
        )


class OpenAICompatibleProvider:
    """JSON Schema HTTP adapter. Also supports compatible gateways; no business rules here."""

    name = "openai-compatible"

    def __init__(self, settings: Settings):
        if not settings.llm_api_key:
            raise ValueError("AUDIT_LLM_API_KEY is required")
        self.settings, self.model = settings, settings.model

    async def structured_generate(self, messages: list[Message], output_schema: type[T]) -> T:
        async with httpx.AsyncClient(timeout=self.settings.llm_timeout_seconds) as client:
            response = await client.post(
                self.settings.llm_base_url.rstrip("/") + "/chat/completions",
                headers={"Authorization": f"Bearer {self.settings.llm_api_key}"},
                json={
                    "model": self.model,
                    "temperature": 0,
                    "messages": [m.model_dump() for m in messages],
                    "response_format": {
                        "type": "json_schema",
                        "json_schema": {
                            "name": output_schema.__name__,
                            "strict": True,
                            "schema": output_schema.model_json_schema(),
                        },
                    },
                },
            )
            response.raise_for_status()
            choice = response.json()["choices"][0]
            if choice.get("finish_reason") != "stop" or choice["message"].get("refusal"):
                raise ValueError("Incomplete or refused model response")
            return output_schema.model_validate_json(choice["message"]["content"])


def get_provider(settings: Settings) -> LLMProvider:
    return (
        OfflineProvider() if settings.provider == "offline" else OpenAICompatibleProvider(settings)
    )
