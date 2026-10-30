import asyncio
import json

import httpx
import pytest

from audit_harness.config import Settings
from audit_harness.llm import Message, OfflineProvider, OpenAICompatibleProvider
from audit_harness.models import Judgment


def test_offline_provider_is_structured_and_uncertain():
    response = asyncio.run(
        OfflineProvider().structured_generate(
            [
                Message(
                    role="user",
                    content=json.dumps(
                        {"context_evidence": [{"evidence_id": "E1", "content": "untrusted"}]}
                    ),
                )
            ],
            Judgment,
        )
    )
    assert response.confidence == 0.5 and response.citations == ["E1"]


def test_remote_provider_requires_key():
    with pytest.raises(ValueError):
        OpenAICompatibleProvider(Settings(llm_api_key="", _env_file=None))


def test_remote_provider_structured_response(monkeypatch):
    original = httpx.AsyncClient

    def respond(request):
        body = json.loads(request.content)
        assert body["response_format"]["json_schema"]["strict"]
        assert request.headers["Authorization"] == "Bearer test-key"
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "content": json.dumps(
                                {
                                    "recommendation": "UNCERTAIN",
                                    "confidence": 0.3,
                                    "rationale": "Insufficient context",
                                    "citations": ["E1"],
                                }
                            )
                        },
                    }
                ]
            },
        )

    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(respond), **kw)
    )
    provider = OpenAICompatibleProvider(Settings(llm_api_key="test-key", _env_file=None))
    response = asyncio.run(
        provider.structured_generate([Message(role="user", content="context")], Judgment)
    )
    assert response.recommendation == "UNCERTAIN"
