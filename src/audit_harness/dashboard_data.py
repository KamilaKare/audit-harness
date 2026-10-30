"""Presentation projections. No business control or review policies live here."""

from collections import Counter
from decimal import Decimal

OUTCOMES = {
    "PASS": "Conforme",
    "FAIL": "Non conforme",
    "INSUFFICIENT_EVIDENCE": "Preuves insuffisantes",
    "NEEDS_HUMAN_REVIEW": "Revue requise",
    "POLICY_EXCEPTION": "Exception autorisée",
}
EVIDENCE_NAMES = {
    "invoice": "Facture",
    "payment_record": "Paiement",
    "approval_record": "Approbation",
    "user_identity": "Identités",
    "vendor_record": "Fournisseur",
    "approval_message": "Justification",
}
FIELD_NAMES = {
    "transaction_id": "Transaction",
    "vendor_id": "Identifiant fournisseur",
    "invoice_id": "Facture",
    "invoice_amount": "Montant facturé",
    "payment_amount": "Montant payé",
    "currency": "Devise",
    "payment_creator_id": "Initiateur du paiement",
    "approver_id": "Approbateur",
    "approval_timestamp": "Approbation le",
    "payment_timestamp": "Paiement exécuté le",
    "payment_date": "Date du paiement",
    "vendor_creation_date": "Fournisseur créé le",
    "status": "Statut de la source",
    "custodian": "Source déclarée",
}


def money(value, currency="EUR", precision=2):
    if value is None:
        return "Non disponible"
    return (
        f"{Decimal(str(value)):,.{precision}f}".replace(",", " ").replace(".", ",") + f" {currency}"
    )


def decision_rows(run: dict, reviews: list[dict]) -> list[dict]:
    population = {t["transaction_id"]: t for t in run.get("population", [])}
    history = {(r["transaction_id"], r["control_id"]): r for r in reviews}
    rows = []
    for decision in run["decisions"]:
        tx = population.get(decision["transaction_id"], {})
        review = history.get((decision["transaction_id"], decision["control_id"]), {})
        events = review.get("history", [])
        rows.append(
            {
                "Transaction": decision["transaction_id"],
                "Fournisseur": tx.get("vendor_name", "—"),
                "Contrôle": decision["control_id"],
                "Montant": money(tx.get("payment_amount"), tx.get("currency", "EUR")),
                "Entité": tx.get("business_unit", "—"),
                "Résultat automatique": OUTCOMES[decision["outcome"]],
                "Périmètre": "Applicable" if decision["in_scope"] else "Hors périmètre",
                "Revue": "Traitée"
                if events
                else "À traiter"
                if decision["review_required"]
                else "Non requise",
                "Décision humaine": OUTCOMES[events[-1]["outcome"]] if events else "—",
                "outcome": decision["outcome"],
                "pending": decision["review_required"] and not events,
            }
        )
    return rows


def overview(run: dict, reviews: list[dict]) -> dict:
    rows = decision_rows(run, reviews)
    judgments = [d["judgment"] for d in run["decisions"] if d.get("judgment")]
    return {
        "transactions": len({d["transaction_id"] for d in run["decisions"]}),
        "controls": len(rows),
        "pending": sum(r["pending"] for r in rows),
        "resolved": sum(r["Revue"] == "Traitée" for r in rows),
        "out_of_scope": sum(not d["in_scope"] for d in run["decisions"]),
        "outcomes": dict(Counter(d["outcome"] for d in run["decisions"])),
        "model_responses": len(judgments),
        "llm_errors": sum(
            any(e.startswith("LLM analysis unavailable:") for e in d.get("errors", []))
            for d in run["decisions"]
        ),
        "simulated": run.get("provider") == "offline",
    }


def demo_transactions(run: dict) -> dict:
    grouped = {}
    for decision in run["decisions"]:
        grouped.setdefault(decision["transaction_id"], []).append(decision)
    clean = next(
        (
            tx
            for tx, ds in grouped.items()
            if all(d["outcome"] == "PASS" and d["in_scope"] for d in ds)
        ),
        None,
    )
    self_approval = next(
        (
            d["transaction_id"]
            for d in run["decisions"]
            if d["control_id"] == "C2" and d["outcome"] == "FAIL"
        ),
        None,
    )
    recent = next(
        (
            d["transaction_id"]
            for d in run["decisions"]
            if d["control_id"] == "C3"
            and d.get("judgment", {})
            and d["judgment"]["recommendation"] == "JUSTIFIED"
        ),
        None,
    )
    if recent is None:
        recent = next(
            (
                d["transaction_id"]
                for d in run["decisions"]
                if d["control_id"] == "C3" and d.get("judgment")
            ),
            None,
        )
    return {"Conforme": clean, "Auto-approbation": self_approval, "Fournisseur récent": recent}
