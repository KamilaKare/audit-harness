import csv
from pathlib import Path

import pytest

from audit_harness.config import load_controls
from audit_harness.models import Transaction
from audit_harness.synthetic import generate


@pytest.fixture(scope="session")
def dataset(tmp_path_factory):
    path = tmp_path_factory.mktemp("synthetic")
    generate(path)
    return path


@pytest.fixture(scope="session")
def population(dataset):
    with (dataset / "transactions.csv").open() as stream:
        return [
            Transaction.model_validate({k: v or None for k, v in row.items()})
            for row in csv.DictReader(stream)
        ]


@pytest.fixture
def controls():
    return load_controls(Path("controls"))[0]


@pytest.fixture(autouse=True)
def isolate_external_services(monkeypatch):
    """A developer's .env must never cause network calls during unit tests."""
    monkeypatch.setenv("AUDIT_LANGFUSE_ENABLED", "false")
    monkeypatch.setenv("AUDIT_PROVIDER", "offline")
