from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Outcome(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
    NEEDS_HUMAN_REVIEW = "NEEDS_HUMAN_REVIEW"
    POLICY_EXCEPTION = "POLICY_EXCEPTION"


class Transaction(StrictModel):
    transaction_id: str = Field(pattern=r"^[A-Za-z0-9_-]+$")
    vendor_id: str
    vendor_name: str
    vendor_creation_date: datetime
    invoice_id: str
    invoice_amount: Decimal = Field(gt=0)
    payment_amount: Decimal = Field(gt=0)
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    invoice_date: datetime
    payment_date: datetime
    payment_creator_id: str
    approver_id: str | None = None
    approval_timestamp: datetime | None = None
    payment_timestamp: datetime
    business_unit: str
    cost_center: str
    purchase_order_id: str
    business_justification: str

    @model_validator(mode="after")
    def timezone_required(self):
        for key in (
            "vendor_creation_date",
            "invoice_date",
            "payment_date",
            "payment_timestamp",
            "approval_timestamp",
        ):
            value = getattr(self, key)
            if value is not None and value.utcoffset() is None:
                raise ValueError(f"{key} must have a timezone")
        return self


class Evidence(StrictModel):
    evidence_id: str
    transaction_id: str
    kind: str
    sha256: str
    source: str
    content: dict


class TestResult(StrictModel):
    test_id: str
    status: Literal["PASS", "FAIL", "UNKNOWN"]
    reason: str
    citations: list[str]
    critical: bool = True


class Judgment(StrictModel):
    recommendation: Literal["JUSTIFIED", "SUSPICIOUS", "UNCERTAIN"]
    confidence: float = Field(ge=0, le=1)
    rationale: str = Field(min_length=1, max_length=4000)
    citations: list[str] = Field(min_length=1)


class Decision(StrictModel):
    transaction_id: str
    control_id: str
    control_version: str
    outcome: Outcome
    in_scope: bool
    reason: str
    tests: list[TestResult]
    evidence: list[Evidence]
    judgment: Judgment | None = None
    review_required: bool
    errors: list[str] = Field(default_factory=list)


class ReviewRequest(StrictModel):
    reviewer_id: str = Field(min_length=3, max_length=100)
    outcome: Literal["PASS", "FAIL", "POLICY_EXCEPTION"]
    rationale: str = Field(min_length=20, max_length=4000)
    citations: list[str] = Field(min_length=1)
    expected_revision: int = Field(ge=0)
