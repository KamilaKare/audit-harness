"""Local Streamlit business interface. Decisions and writes remain behind FastAPI."""

import json

import pandas as pd
import streamlit as st

from audit_harness.config import Settings
from audit_harness.dashboard_client import DashboardClient, DashboardError, DashboardSettings
from audit_harness.dashboard_data import (
    EVIDENCE_NAMES,
    FIELD_NAMES,
    OUTCOMES,
    decision_rows,
    demo_transactions,
    money,
    overview,
)

TEST_NAMES = {
    "approval_before_payment": "Approbation avant le paiement",
    "different_people": "Séparation des personnes",
    "recent_vendor": "Ancienneté du fournisseur",
}

PAGES = ["Tableau de bord", "Revue des transactions", "Performance & coût"]


def choose_demo(transaction_id, control_id):
    st.session_state["selected_tx"] = transaction_id
    st.session_state["page"] = PAGES[1]
    run_id = st.session_state.get("previous_run")
    if run_id:
        st.session_state[f"control_{run_id}_{transaction_id}"] = control_id


def show_overview(run, reviews):
    summary = overview(run, reviews)
    st.subheader("Une population, des décisions traçables")
    columns = st.columns(4)
    for col, label, value in zip(
        columns,
        ["Transactions", "Contrôles exécutés", "Revues à traiter", "Revues enregistrées"],
        [summary["transactions"], summary["controls"], summary["pending"], summary["resolved"]],
        strict=True,
    ):
        col.metric(label, value)
    st.caption(
        f"{summary['out_of_scope']} décisions sont hors périmètre. Leur statut PASS ne constitue pas une validation des preuves."
    )
    left, right = st.columns([2, 1])
    with left:
        st.markdown("#### Résultats automatiques par contrôle")
        frame = pd.DataFrame(
            [
                {"Contrôle": d["control_id"], "Résultat": OUTCOMES[d["outcome"]]}
                for d in run["decisions"]
            ]
        )
        counts = frame.groupby(["Contrôle", "Résultat"]).size().unstack(fill_value=0)
        st.bar_chart(counts, height=270)
    with right:
        st.markdown("#### Intervention du modèle")
        st.metric(
            "Réponses du simulateur" if summary["simulated"] else "Analyses LLM reçues",
            summary["model_responses"],
        )
        st.metric("Erreurs d’analyse LLM", summary["llm_errors"])
        st.caption(
            "Simulation déterministe : aucune qualité de modèle réel n’est mesurée."
            if summary["simulated"]
            else "L’avis du modèle reste consultatif. Une justification plausible n’autorise pas une exception."
        )
    st.markdown("#### Trois dossiers pour comprendre le harness")
    descriptions = {
        "Conforme": "Les contrôles applicables sont satisfaits.",
        "Auto-approbation": "La même personne initie et approuve le paiement.",
        "Fournisseur récent": "Une justification à interpréter, puis une décision humaine.",
    }
    for column, (label, tx) in zip(st.columns(3), demo_transactions(run).items(), strict=True):
        with column:
            with st.container(border=True):
                st.markdown(f"**{label}**")
                st.caption(descriptions[label])
                st.button(
                    f"Ouvrir {tx or 'le dossier'}",
                    key=f"demo_{label}",
                    disabled=tx is None,
                    on_click=choose_demo,
                    args=(
                        tx,
                        {"Conforme": "C1", "Auto-approbation": "C2", "Fournisseur récent": "C3"}[
                            label
                        ],
                    ),
                    width="stretch",
                )


def show_evidence(decision):
    evidence = decision["evidence"]
    if not evidence:
        st.info("Aucune preuve collectée pour ce contrôle.")
        return
    by_id = {e["evidence_id"]: e for e in evidence}
    selected = st.selectbox(
        "Document",
        list(by_id),
        format_func=lambda eid: EVIDENCE_NAMES.get(by_id[eid]["kind"], by_id[eid]["kind"]),
        key=f"evidence_{decision['transaction_id']}_{decision['control_id']}",
    )
    ev = by_id[selected]
    st.markdown(f"**{EVIDENCE_NAMES.get(ev['kind'], ev['kind'])}**")
    content = ev["content"]
    if "text" in content:
        st.text(content["text"])
    elif isinstance(content.get("identities"), dict):
        st.table(
            pd.DataFrame(
                [
                    {"Compte": account, "Personne de référence": person}
                    for account, person in content["identities"].items()
                ]
            )
        )
    else:
        st.table(
            pd.DataFrame(
                [
                    {
                        "Champ": FIELD_NAMES.get(k, k),
                        "Valeur": str(v) if v is not None else "Non renseigné",
                    }
                    for k, v in content.items()
                ]
            )
        )
    with st.expander("Provenance et intégrité"):
        st.text(f"Référence : {ev['evidence_id']}\nSource déclarée : {ev['source']}")
        st.code(ev["sha256"], language=None)
        st.caption(
            "L’empreinte identifie la version utilisée. Elle ne prouve pas à elle seule l’authenticité de la source."
        )


def show_judgment(run, decision):
    judgment = decision.get("judgment")
    if judgment is None:
        st.info("Aucune analyse contextuelle enregistrée pour ce contrôle.")
        if decision.get("errors"):
            for error in decision["errors"]:
                st.text(error)
        return
    st.caption(
        "Simulation déterministe"
        if run.get("provider") == "offline"
        else f"Modèle : {(decision.get('execution') or {}).get('model') or run.get('model', 'Non renseigné')}"
    )
    columns = st.columns(2)
    columns[0].metric(
        "Recommandation",
        {"JUSTIFIED": "Justification plausible", "SUSPICIOUS": "Suspect", "UNCERTAIN": "Incertain"}[
            judgment["recommendation"]
        ],
    )
    columns[1].metric("Confiance déclarée", f"{judgment['confidence']:.0%}")
    st.caption(
        "Score déclaré par le modèle, non calibré ; ce n’est pas une probabilité de conformité vérifiée."
    )
    st.text(judgment["rationale"])
    st.markdown("**Preuves citées**")
    for citation in judgment["citations"]:
        st.text(citation)
    st.info("La décision du harness et la revue humaine restent distinctes de cet avis.")


def show_review(client, run_id, decision, reviews):
    txid, control_id = decision["transaction_id"], decision["control_id"]
    context = next(
        (r for r in reviews if r["transaction_id"] == txid and r["control_id"] == control_id), None
    )
    if context is None:
        st.info("Ce résultat ne nécessite pas de revue humaine.")
        return
    history = context["history"]
    if history:
        latest = history[-1]
        st.success(
            f"Décision humaine actuelle : {OUTCOMES[latest['outcome']]} · révision {context['revision']}"
        )
        st.text(latest["rationale"])
        with st.expander(f"Historique des {len(history)} intervention(s)"):
            for event in reversed(history):
                st.markdown(f"**{OUTCOMES[event['outcome']]}**")
                st.caption(f"{event['timestamp']} · {event['reviewer_id']}")
                st.text(event["rationale"])
    else:
        st.warning("Une revue reste à enregistrer pour ce contrôle.")
    st.caption("Réviseur déclaratif — démonstration locale sur données synthétiques.")
    prefix = f"review_{run_id}_{txid}_{control_id}"
    revision_key = prefix + "_revision"
    if revision_key not in st.session_state:
        st.session_state[revision_key] = context["revision"]
    stale = st.session_state[revision_key] != context["revision"]
    if stale:
        st.warning(
            "La revue a changé depuis son ouverture. Rechargez-la avant de soumettre votre décision."
        )
        if st.button("Recharger la revue", key=prefix + "_reload"):
            st.session_state[revision_key] = context["revision"]
            st.rerun()
    options = context.get("allowed_outcomes", [])
    if not options:
        st.info("Aucune décision n’est disponible. Vérifiez que le backend est à jour.")
        return
    if options == ["FAIL"]:
        st.info(
            "Une violation déterministe confirmée peut être reconnue, mais ne peut pas être transformée en conformité. Une correction des preuves nécessite une nouvelle exécution."
        )
    evidence_ids = [e["evidence_id"] for e in decision["evidence"]]
    with st.form(prefix):
        reviewer = st.text_input(
            "Réviseur (déclaratif)", value="demo-auditeur", key=prefix + "_reviewer"
        )
        outcome = st.selectbox(
            "Décision humaine",
            options,
            format_func=lambda value: OUTCOMES[value],
            key=prefix + "_outcome",
        )
        rationale = st.text_area(
            "Motif de la décision",
            help="Expliquez votre conclusion et les éléments examinés.",
            key=prefix + "_rationale",
        )
        citations = st.multiselect("Preuves examinées", evidence_ids, key=prefix + "_citations")
        submitted = st.form_submit_button(
            "Enregistrer la décision", disabled=stale or not evidence_ids
        )
    if not evidence_ids:
        st.caption(
            "Complétez les preuves et relancez le contrôle avant d’enregistrer une revue étayée."
        )
    if submitted:
        if len(reviewer.strip()) < 3 or len(rationale.strip()) < 20 or not citations:
            st.error(
                "Renseignez le réviseur, un motif explicite d’au moins 20 caractères et au moins une preuve."
            )
            return
        try:
            result = client.save_review(
                run_id,
                txid,
                control_id,
                {
                    "reviewer_id": reviewer.strip(),
                    "outcome": outcome,
                    "rationale": rationale.strip(),
                    "citations": citations,
                    "expected_revision": st.session_state[revision_key],
                },
            )
        except DashboardError as error:
            st.error(str(error))
            return
        st.session_state[revision_key] = result["revision"]
        st.session_state["review_notice"] = (
            "Décision enregistrée. Le résultat automatique d’origine est conservé."
        )
        st.rerun()


def show_transactions(client, run, reviews):
    st.subheader("Explorer les résultats et instruire une revue")
    rows = pd.DataFrame(decision_rows(run, reviews))
    columns = st.columns(4)
    control_filter = columns[0].multiselect(
        "Contrôles", sorted(rows["Contrôle"].unique()), key="filter_control"
    )
    outcome_filter = columns[1].multiselect(
        "Résultats", list(OUTCOMES), format_func=lambda x: OUTCOMES[x], key="filter_outcome"
    )
    entity_filter = columns[2].multiselect(
        "Entités", sorted(rows["Entité"].unique()), key="filter_entity"
    )
    pending_only = columns[3].checkbox("Revues à traiter uniquement", key="filter_pending")
    query = st.text_input("Rechercher une transaction ou un fournisseur", key="filter_query")
    filtered = rows
    for column, values in (
        ("Contrôle", control_filter),
        ("outcome", outcome_filter),
        ("Entité", entity_filter),
    ):
        if values:
            filtered = filtered[filtered[column].isin(values)]
    if pending_only:
        filtered = filtered[filtered["pending"]]
    if query:
        filtered = filtered[
            filtered["Transaction"].str.contains(query, case=False, regex=False)
            | filtered["Fournisseur"].str.contains(query, case=False, regex=False)
        ]
    st.caption(f"{len(filtered)} résultat(s) dans la sélection")
    visible = filtered.drop(columns=["outcome", "pending"])
    event = st.dataframe(
        visible,
        hide_index=True,
        width="stretch",
        height=250,
        on_select="rerun",
        selection_mode="single-row",
        key="result_table",
    )
    if event.selection.rows:
        row = filtered.iloc[event.selection.rows[0]]
        selected_key = f"{run['run_id']}/{row['Transaction']}/{row['Contrôle']}"
        if st.session_state.get("last_table_selection") != selected_key:
            st.session_state["selected_tx"] = row["Transaction"]
            st.session_state[f"control_{run['run_id']}_{row['Transaction']}"] = row["Contrôle"]
            st.session_state["last_table_selection"] = selected_key
    txs = {t["transaction_id"]: t for t in run["population"]}
    choices = list(txs)
    if not choices:
        st.info("Aucune transaction dans cette exécution.")
        return
    if st.session_state.get("selected_tx") not in choices:
        st.session_state["selected_tx"] = choices[0]
    selected = st.selectbox(
        "Ouvrir une transaction",
        choices,
        format_func=lambda key: f"{key} · {txs[key]['vendor_name']}",
        key="selected_tx",
    )
    tx = txs[selected]
    st.markdown(f"### {selected}")
    st.text(tx["vendor_name"])
    columns = st.columns(3)
    columns[0].metric("Montant du paiement", money(tx["payment_amount"], tx["currency"]))
    columns[1].metric("Entité", tx["business_unit"])
    columns[2].metric("Centre de coût", tx["cost_center"])
    st.caption(
        f"Fournisseur créé : {tx['vendor_creation_date'][:10]} · Paiement : {tx['payment_date'][:10]} · Initiateur : {tx['payment_creator_id']} · Approbateur : {tx.get('approver_id') or 'Non renseigné'}"
    )
    decisions = {d["control_id"]: d for d in run["decisions"] if d["transaction_id"] == selected}
    choice = st.radio(
        "Contrôle à examiner",
        list(decisions),
        horizontal=True,
        key=f"control_{run['run_id']}_{selected}",
    )
    decision = decisions[choice]
    tabs = st.tabs(["Résultat et tests", "Preuves", "Avis du modèle", "Revue humaine"])
    with tabs[0]:
        if not decision["in_scope"]:
            st.info("Hors périmètre — ce PASS ne valide pas les preuves de la transaction.")
        else:
            display = {"PASS": st.success, "FAIL": st.error}.get(decision["outcome"], st.warning)
            display(f"Résultat automatique : {OUTCOMES[decision['outcome']]}")
        st.text(decision["reason"])
        for test in decision["tests"]:
            with st.container(border=True):
                st.markdown(
                    f"**{TEST_NAMES.get(test['test_id'], test['test_id'])} · {test['status']}**"
                )
                st.text(test["reason"])
                st.caption("Preuves : " + ", ".join(test["citations"]))
        st.caption(
            "Pour C3, un test FAIL signale le déclenchement de l’alerte. La décision finale reste une revue humaine."
        )
    with tabs[1]:
        show_evidence(decision)
    with tabs[2]:
        show_judgment(run, decision)
    with tabs[3]:
        show_review(client, run["run_id"], decision, reviews)
    with st.expander("Données techniques du dossier"):
        st.json({"transaction": tx, "decision": decision})


def show_performance(client, run):
    st.subheader("Mesurer l’exécution et son coût")
    performance = client.metrics(run["run_id"])
    coverage = performance["measurement_coverage"]
    st.caption(
        f"Mesures enregistrées pour {coverage['recorded']} contrôles sur {coverage['total']}."
    )
    columns = st.columns(4)
    columns[0].metric("Durée de l’exécution", f"{run.get('duration_seconds', 0):.2f} s")
    llm = performance["llm"]
    columns[1].metric(
        "Appels LLM réels", llm["attempts"] if llm["attempts"] is not None else "Non enregistré"
    )
    columns[2].metric(
        "Tokens d’entrée",
        llm["input_tokens"] if llm["input_tokens"] is not None else "Non enregistrés",
    )
    columns[3].metric(
        "Tokens de sortie",
        llm["output_tokens"] if llm["output_tokens"] is not None else "Non enregistrés",
    )
    latency = []
    for label, key in (
        ("Contrôle complet", "control_latency_ms"),
        ("Analyse LLM", "llm_latency_ms"),
    ):
        data = performance[key]
        latency.append(
            {
                "Mesure": label,
                "Observations": data["count"],
                "Moyenne (ms)": data["mean"],
                "Médiane (ms)": data["p50"],
                "95e percentile (ms)": data["p95"],
                "Maximum (ms)": data["max"],
            }
        )
    st.markdown("#### Latences observées")
    st.dataframe(pd.DataFrame(latency), hide_index=True, width="stretch")
    cost = performance["cost"]
    st.markdown("#### Coût estimé du modèle")
    columns = st.columns(2)
    columns[0].metric("Total estimé", money(cost["estimated_total_usd"], "USD", 8))
    columns[1].metric("Moyenne par contrôle", money(cost["mean_per_control_usd"], "USD", 8))
    if cost["estimated_total_usd"] is None:
        st.info(
            "Le coût total n’est pas disponible : il manque des mesures ou un tarif configuré. Aucun montant n’est inventé."
        )
    st.caption(
        "Estimation des appels modèle uniquement, selon les tarifs versionnés configurés. Elle exclut le travail humain, l’infrastructure et les remises particulières du fournisseur."
    )
    st.markdown("#### Évaluation des décisions")
    try:
        report = client.evaluation(run["run_id"])
    except DashboardError:
        st.info("Aucun jeu de référence correspondant n’est disponible pour cette exécution.")
        return
    columns = st.columns(3)
    columns[0].metric("Décisions correspondant aux labels", f"{report['accuracy']:.0%}")
    columns[1].metric(
        "Transactions entièrement conformes aux labels",
        f"{report['business_task_success_rate']:.0%}",
    )
    columns[2].metric("PASS dangereux", report["unsafe_passes"])
    st.caption(
        "Concordance avec les labels synthétiques. Ce score n’évalue ni la qualité sémantique des justifications du LLM, ni sa fiabilité en production."
    )
    success_cost = report.get("performance", {}).get("cost", {}).get("per_successful_task_usd")
    st.metric("Coût modèle par transaction correctement traitée", money(success_cost, "USD", 8))


def main():
    st.set_page_config(page_title="Audit · Purchase-to-Pay", page_icon="✓", layout="wide")
    st.title("Audit · Purchase-to-Pay")
    st.caption("Contrôles métier, preuves et décisions gouvernées · Démonstration locale")
    app_settings, dashboard_settings = Settings(), DashboardSettings()
    client = DashboardClient(
        dashboard_settings.api_url, app_settings.api_key, dashboard_settings.timeout_seconds
    )
    with st.sidebar:
        st.markdown("### Centre de contrôle")
        st.caption("Les données et les revues proviennent du backend FastAPI.")
        st.button("Actualiser les exécutions", key="refresh")
        with st.expander("Nouvelle exécution"):
            with st.form("new_run"):
                provider = st.radio(
                    "Mode d’analyse",
                    ["offline", "openai-compatible"],
                    format_func=lambda p: (
                        "Simulation sans LLM" if p == "offline" else "LLM configuré sur le serveur"
                    ),
                )
                start = st.form_submit_button("Lancer les contrôles")
            if start:
                try:
                    with st.spinner("Contrôles en cours…"):
                        result = client.start_run(provider)
                    st.session_state["run_selector"] = result["run_id"]
                    st.rerun()
                except DashboardError as error:
                    st.error(str(error))
    try:
        runs = client.list_runs()
    except DashboardError as error:
        st.error(str(error))
        st.info(
            "Démarrez le backend dans un autre Terminal : uv run --extra observability audit-harness serve"
        )
        return
    if not runs:
        st.info(
            "Aucune exécution enregistrée. Lancez les contrôles depuis le menu de gauche pour créer votre première population de résultats."
        )
        return
    ids = [r["run_id"] for r in runs]
    descriptions = {
        r[
            "run_id"
        ]: f"{(r.get('created_at') or '')[:16].replace('T', ' ')} · {r['model']} · {r['run_id'][:8]}"
        for r in runs
    }
    if st.session_state.get("run_selector") not in ids:
        st.session_state["run_selector"] = ids[0]
    with st.sidebar:
        selected = st.selectbox(
            "Exécution", ids, format_func=lambda key: descriptions[key], key="run_selector"
        )
        st.caption("Liste des 50 dernières exécutions.")
    try:
        run, reviews = client.get_run(selected), client.reviews(selected)
        if st.session_state.get("previous_run") != selected:
            st.session_state["previous_run"] = selected
            st.session_state.pop("selected_tx", None)
            st.session_state.pop("last_table_selection", None)
            for name in (
                "filter_control",
                "filter_outcome",
                "filter_entity",
                "filter_pending",
                "filter_query",
            ):
                st.session_state.pop(name, None)
        st.caption(f"Exécution {run['run_id']} · Modèle {run.get('model', 'Non renseigné')}")
        if message := st.session_state.pop("review_notice", None):
            st.success(message)
        page = st.radio("Navigation", PAGES, horizontal=True, key="page")
        if page == PAGES[0]:
            show_overview(run, reviews)
        elif page == PAGES[1]:
            show_transactions(client, run, reviews)
        else:
            show_performance(client, run)
        st.download_button(
            "Exporter le dossier et les revues",
            json.dumps({"run": run, "reviews": reviews}, indent=2),
            file_name=f"audit-{run['run_id']}.json",
            mime="application/json",
        )
    except DashboardError as error:
        st.error(str(error))


if __name__ == "__main__":
    main()
