import hashlib
from pathlib import Path
from typing import Literal

import yaml
from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .models import StrictModel

RULES = {"approval_before_payment", "different_people", "recent_vendor"}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AUDIT_", env_file=".env", extra="ignore")
    data_dir: Path = Path("data")
    controls_dir: Path = Path("controls")
    database_url: str = "sqlite:///./audit.db"
    provider: Literal["offline", "openai-compatible"] = "offline"
    model: str = "gpt-4.1-mini"
    llm_api_key: str = ""
    llm_base_url: str = "https://api.openai.com/v1"
    llm_timeout_seconds: float = Field(default=20, gt=0, le=120)
    api_key: str = ""
    langfuse_enabled: bool = False


class Population(StrictModel):
    currency: str = "EUR"
    payment_amount_gt: float = Field(ge=0, allow_inf_nan=False)


class TestDefinition(StrictModel):
    id: str
    type: Literal["deterministic"] = "deterministic"
    critical: bool = True


class Policy(StrictModel):
    min_confidence: float = Field(default=0.85, ge=0, le=1)
    suspicious_requires_human: Literal[True] = True
    require_evidence_citations: Literal[True] = True


class Control(StrictModel):
    control_id: str
    name: str
    version: str
    objective: str
    population: Population
    evidence_required: list[str] = Field(min_length=1)
    tests: list[TestDefinition] = Field(min_length=1)
    recent_vendor_days: int = Field(default=7, gt=0)
    llm_context: bool = False
    decision_policy: Policy

    @model_validator(mode="after")
    def known_rules(self):
        if any(test.id not in RULES for test in self.tests):
            raise ValueError("Unknown deterministic test")
        if len({t.id for t in self.tests}) != len(self.tests):
            raise ValueError("Duplicate tests")
        dependencies = {
            "approval_before_payment": {"invoice", "payment_record", "approval_record"},
            "different_people": {"payment_record", "approval_record", "user_identity"},
            "recent_vendor": {"payment_record", "vendor_record"},
        }
        for test in self.tests:
            if not dependencies[test.id] <= set(self.evidence_required):
                raise ValueError("Rule prerequisites must be declared as required evidence")
        if self.llm_context and not any(t.id == "recent_vendor" for t in self.tests):
            raise ValueError("Context analysis is only defined for recent vendor testing")
        return self


def load_controls(directory: Path) -> tuple[list[Control], str]:
    files = sorted(directory.glob("*.yaml"))
    if not files:
        raise ValueError("No control definitions found")
    controls = [Control.model_validate(yaml.safe_load(p.read_text())) for p in files]
    if len({c.control_id for c in controls}) != len(controls):
        raise ValueError("Duplicate control IDs")
    digest = hashlib.sha256(b"".join(p.read_bytes() for p in files)).hexdigest()
    return controls, digest
