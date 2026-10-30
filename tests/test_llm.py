import asyncio
import json

import httpx
import pytest

from audit_harness.config import Settings
from audit_harness.llm import (
    Message,
    MissingLLMKeyError,
    OfflineProvider,
    OpenAICompatibleProvider,
)
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


@pytest.mark.parametrize("key", ["", "   "])
def test_remote_provider_requires_key(key):
    with pytest.raises(MissingLLMKeyError, match="Set AUDIT_LLM_API_KEY"):
        OpenAICompatibleProvider(Settings(llm_api_key=key, _env_file=None))


def test_live_cli_explains_missing_key_without_traceback(monkeypatch, tmp_path, dataset, capsys):
    from pathlib import Path

    from audit_harness.cli import main

    monkeypatch.setenv("AUDIT_CONTROLS_DIR", str(Path("controls").resolve()))
    monkeypatch.setenv("AUDIT_DATA_DIR", str(dataset))
    monkeypatch.setenv("AUDIT_DATABASE_URL", "sqlite:///:memory:")
    monkeypatch.setenv("AUDIT_PROVIDER", "openai-compatible")
    monkeypatch.setenv("AUDIT_LLM_API_KEY", "")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.argv", ["audit-harness", "run"])
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 2
    stderr = capsys.readouterr().err
    assert "Configuration error: Set AUDIT_LLM_API_KEY" in stderr
    assert "Traceback" not in stderr


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
