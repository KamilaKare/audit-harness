import json
import logging
import sys
from types import SimpleNamespace

import pytest

from audit_harness.observability import JsonFormatter, observation


def test_json_logging_excludes_unset_data():
    record = logging.LogRecord("audit_harness", logging.INFO, "", 0, "audit_event", (), None)
    record.audit = {"control_id": "C1"}
    result = json.loads(JsonFormatter().format(record))
    assert result["audit"] == {"control_id": "C1"}
    assert result["message"] == "audit_event"


def test_optional_tracing_failure_does_not_fail_business_logic(monkeypatch):
    def broken_client():
        raise RuntimeError("secret provider details")

    monkeypatch.setitem(sys.modules, "langfuse", SimpleNamespace(get_client=broken_client))
    with observation("test", {"control_id": "C1"}, langfuse_enabled=True):
        result = "business operation completed"
    assert result == "business operation completed"
    with pytest.raises(ValueError, match="business error"):
        with observation("test", {}, langfuse_enabled=True):
            raise ValueError("business error")
