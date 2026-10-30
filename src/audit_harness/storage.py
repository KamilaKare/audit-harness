import hashlib
import json
from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    Column,
    Integer,
    MetaData,
    String,
    Table,
    create_engine,
    insert,
    select,
    update,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.pool import StaticPool

from .models import ReviewRequest

metadata = MetaData()
runs = Table(
    "runs",
    metadata,
    Column("run_id", String, primary_key=True),
    Column("payload", JSON, nullable=False),
    Column("sha256", String, nullable=False),
)
reviews = Table(
    "reviews",
    metadata,
    Column("key", String, primary_key=True),
    Column("revision", Integer, nullable=False),
    Column("history", JSON, nullable=False),
)


def digest(value: dict) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class Store:
    def __init__(self, url: str):
        kwargs = {}
        if url.startswith("sqlite"):
            kwargs["connect_args"] = {"check_same_thread": False}
            if ":memory:" in url:
                kwargs["poolclass"] = StaticPool
        self.engine = create_engine(url, **kwargs)
        metadata.create_all(self.engine)

    def list_runs(self, limit: int = 50, offset: int = 0) -> list[dict]:
        with self.engine.connect() as conn:
            rows = (
                conn.execute(
                    select(runs)
                    .order_by(runs.c.payload["created_at"].as_string().desc(), runs.c.run_id.desc())
                    .limit(limit)
                    .offset(offset)
                )
                .mappings()
                .all()
            )
        result = []
        for row in rows:
            payload = row["payload"]
            if digest(payload) != row["sha256"]:
                raise ValueError("Stored run integrity check failed")
            result.append(
                {
                    "run_id": row["run_id"],
                    "created_at": payload.get("created_at"),
                    "provider": payload.get("provider", "unknown"),
                    "model": payload.get("model", "unknown"),
                    "decision_count": len(payload["decisions"]),
                }
            )
        return result

    @staticmethod
    def review_options(decision: dict) -> list[str]:
        if not decision["review_required"]:
            return []
        if decision["outcome"] == "FAIL":
            return ["FAIL"]
        return (
            ["PASS", "FAIL", "POLICY_EXCEPTION"]
            if decision["control_id"] == "C3"
            else ["PASS", "FAIL"]
        )

    def save(self, payload: dict):
        with self.engine.begin() as conn:
            conn.execute(
                insert(runs).values(
                    run_id=payload["run_id"], payload=payload, sha256=digest(payload)
                )
            )

    def get(self, run_id: str) -> dict:
        with self.engine.connect() as conn:
            row = conn.execute(select(runs).where(runs.c.run_id == run_id)).mappings().first()
        if not row:
            raise KeyError(run_id)
        if digest(row["payload"]) != row["sha256"]:
            raise ValueError("Stored run integrity check failed")
        return row["payload"]

    def review_history(self, run_id: str, transaction_id: str, control_id: str) -> dict:
        key = f"{run_id}/{transaction_id}/{control_id}"
        with self.engine.connect() as conn:
            row = conn.execute(select(reviews).where(reviews.c.key == key)).mappings().first()
        return (
            {"revision": row["revision"], "history": row["history"]}
            if row
            else {"revision": 0, "history": []}
        )

    def queue(self, run_id: str) -> list[dict]:
        payload = self.get(run_id)
        result = []
        for decision in payload["decisions"]:
            if decision["review_required"]:
                history = self.review_history(
                    run_id, decision["transaction_id"], decision["control_id"]
                )
                result.append(
                    {
                        **decision,
                        **history,
                        "resolved": bool(history["revision"]),
                        "allowed_outcomes": self.review_options(decision),
                    }
                )
        return result

    def review(
        self, run_id: str, transaction_id: str, control_id: str, request: ReviewRequest
    ) -> dict:
        run = self.get(run_id)
        decision = next(
            (
                d
                for d in run["decisions"]
                if d["transaction_id"] == transaction_id and d["control_id"] == control_id
            ),
            None,
        )
        if not decision:
            raise KeyError("Decision not found")
        if not decision["review_required"]:
            raise ValueError("Decision does not require review")
        if request.outcome == "POLICY_EXCEPTION" and control_id != "C3":
            raise ValueError("Only C3 permits policy exceptions")
        if not set(request.citations) <= {e["evidence_id"] for e in decision["evidence"]}:
            raise ValueError("Unknown evidence citation")
        # Confirmed deterministic failures remain failures; remediation requires a fresh run.
        if decision["outcome"] == "FAIL" and request.outcome != "FAIL":
            raise ValueError("Cannot override a confirmed deterministic failure")
        if request.outcome not in self.review_options(decision):
            raise ValueError("Outcome is not permitted for this review")
        history = self.review_history(run_id, transaction_id, control_id)
        if history["revision"] != request.expected_revision:
            raise RuntimeError("Review revision conflict")
        event = {
            **request.model_dump(),
            "timestamp": datetime.now(UTC).isoformat(),
            "previous_hash": digest(history),
            "original_outcome": decision["outcome"],
        }
        value = {"revision": history["revision"] + 1, "history": history["history"] + [event]}
        key = f"{run_id}/{transaction_id}/{control_id}"
        try:
            with self.engine.begin() as conn:
                if history["revision"] == 0:
                    conn.execute(insert(reviews).values(key=key, **value))
                else:
                    changed = conn.execute(
                        update(reviews)
                        .where(
                            reviews.c.key == key, reviews.c.revision == request.expected_revision
                        )
                        .values(**value)
                    )
                    if changed.rowcount != 1:
                        raise RuntimeError("Review revision conflict")
        except IntegrityError as exc:
            raise RuntimeError("Review revision conflict") from exc
        return value
