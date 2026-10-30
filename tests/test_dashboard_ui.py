"""Real Streamlit interaction tests, backed by the real API and a temporary database."""

import asyncio
from pathlib import Path

import httpx
import pytest

pytest.importorskip("streamlit", reason="Install the dashboard extra for UI interaction tests")

from fastapi.testclient import TestClient
from streamlit.testing.v1 import AppTest

from audit_harness.api import create_app
from audit_harness.config import Settings
from audit_harness.dashboard_client import DashboardClient
from audit_harness.service import execute
from audit_harness.storage import Store

APP = Path(__file__).resolve().parents[1] / "src/audit_harness/dashboard.py"


@pytest.fixture
def ui_backend(monkeypatch, dataset, tmp_path):
    from audit_harness import dashboard_client
    from audit_harness.observability import logger

    monkeypatch.setattr(logger, "info", lambda *a, **kw: None)
    settings = Settings(
        data_dir=dataset,
        database_url=f"sqlite:///{tmp_path}/ui.db",
        api_key="test-key",
        _env_file=None,
    )
    store = Store(settings.database_url)
    run = asyncio.run(execute(settings, store))
    api = TestClient(create_app(settings))
    api.__enter__()

    def gateway(request):
        response = api.request(
            request.method,
            request.url.raw_path.decode(),
            headers={
                "X-API-Key": "test-key",
                "Content-Type": request.headers.get("Content-Type", "application/json"),
            },
            content=request.content,
        )
        if response.status_code >= 400:
            client.last_failure = (response.status_code, response.json())
        return httpx.Response(response.status_code, json=response.json())

    client = DashboardClient("http://test", "test-key", transport=httpx.MockTransport(gateway))
    monkeypatch.setattr(dashboard_client, "DashboardClient", lambda *a, **kw: client)
    yield run, store, client
    api.__exit__(None, None, None)
    store.engine.dispose()


def test_dashboard_opens_all_three_demo_dossiers(ui_backend):
    run, _, _ = ui_backend
    app = AppTest.from_file(str(APP), default_timeout=20).run()
    assert not app.exception
    assert app.metric[0].value == "120" and app.metric[1].value == "360"
    app.button(key="demo_Auto-approbation").click().run()
    assert not app.exception
    assert app.radio(key="page").value == "Revue des transactions"
    assert app.selectbox(key="selected_tx").value == "TX-0003"
    assert app.radio(key=f"control_{run['run_id']}_TX-0003").value == "C2"
    assert any("Non conforme" in error.value for error in app.error)
    app.radio(key="page").set_value("Tableau de bord").run()
    app.button(key="demo_Conforme").click().run()
    assert app.selectbox(key="selected_tx").value == "TX-0001"
    assert any("Conforme" in element.value for element in app.success)
    app.radio(key="page").set_value("Tableau de bord").run()
    app.button(key="demo_Fournisseur récent").click().run()
    assert not app.exception
    assert app.selectbox(key="selected_tx").value == "TX-0005"
    assert app.radio(key=f"control_{run['run_id']}_TX-0005").value == "C3"


def test_review_is_saved_through_api_without_replacing_machine_result(ui_backend):
    run, store, client = ui_backend
    app = AppTest.from_file(str(APP), default_timeout=20).run()
    app.button(key="demo_Fournisseur récent").click().run()
    prefix = f"review_{run['run_id']}_TX-0005_C3"
    app.selectbox(key=prefix + "_outcome").select("POLICY_EXCEPTION")
    app.text_area(key=prefix + "_rationale").input(
        "Justification vérifiée dans les pièces du dossier synthétique."
    )
    app.multiselect(key=prefix + "_citations").select("TX-0005:approval_message")
    next(button for button in app.button if button.label == "Enregistrer la décision").click().run()
    assert not app.exception
    assert not app.error, getattr(client, "last_failure", [e.value for e in app.error])
    history = store.review_history(run["run_id"], "TX-0005", "C3")
    assert history["revision"] == 1 and history["history"][0]["outcome"] == "POLICY_EXCEPTION"
    original = next(
        d
        for d in store.get(run["run_id"])["decisions"]
        if d["transaction_id"] == "TX-0005" and d["control_id"] == "C3"
    )
    assert original["outcome"] == "NEEDS_HUMAN_REVIEW"


def test_concurrent_review_requires_explicit_reload(ui_backend):
    run, store, _ = ui_backend
    from audit_harness.models import ReviewRequest

    app = AppTest.from_file(str(APP), default_timeout=20).run()
    app.button(key="demo_Fournisseur récent").click().run()
    store.review(
        run["run_id"],
        "TX-0005",
        "C3",
        ReviewRequest(
            reviewer_id="other-reviewer",
            outcome="FAIL",
            rationale="Une autre revue a examiné les preuves du dossier.",
            citations=["TX-0005:approval_message"],
            expected_revision=0,
        ),
    )
    app.run()
    assert not app.exception
    submit = next(button for button in app.button if button.label == "Enregistrer la décision")
    assert submit.disabled
    assert any("depuis son ouverture" in warning.value for warning in app.warning)
    prefix = f"review_{run['run_id']}_TX-0005_C3"
    app.button(key=prefix + "_reload").click().run()
    assert not next(
        button for button in app.button if button.label == "Enregistrer la décision"
    ).disabled


def test_filters_and_performance_screen(ui_backend):
    app = AppTest.from_file(str(APP), default_timeout=20).run()
    app.radio(key="page").set_value("Revue des transactions").run()
    app.multiselect(key="filter_control").select("C2")
    app.checkbox(key="filter_pending").check().run()
    frame = app.dataframe[0].value
    assert set(frame["Contrôle"]) == {"C2"}
    assert set(frame["Revue"]) == {"À traiter"}
    app.radio(key="page").set_value("Performance & coût").run()
    assert not app.exception
    assert any(metric.label == "Appels LLM réels" and metric.value == "0" for metric in app.metric)
    assert any(
        metric.label == "Décisions correspondant aux labels" and metric.value == "100%"
        for metric in app.metric
    )


def test_backend_unavailable_has_readable_empty_state(monkeypatch):
    from audit_harness import dashboard_client
    from audit_harness.dashboard_client import DashboardError

    class Unavailable:
        def list_runs(self):
            raise DashboardError("Backend inaccessible")

    monkeypatch.setattr(dashboard_client, "DashboardClient", lambda *a, **kw: Unavailable())
    app = AppTest.from_file(str(APP), default_timeout=10).run()
    assert not app.exception and "Backend inaccessible" in app.error[0].value


def test_new_offline_run_is_created_from_sidebar(ui_backend):
    _, store, _ = ui_backend
    app = AppTest.from_file(str(APP), default_timeout=20).run()
    next(button for button in app.button if button.label == "Lancer les contrôles").click().run()
    assert not app.exception and not app.error
    assert len(store.list_runs()) == 2
    assert app.selectbox(key="run_selector").value == store.list_runs()[0]["run_id"]


def test_historical_run_does_not_display_missing_tokens_as_zero(ui_backend):
    import copy
    from datetime import UTC, datetime

    run, store, _ = ui_backend
    historical = copy.deepcopy(run)
    historical["run_id"] = "historical-run"
    historical["created_at"] = datetime.now(UTC).isoformat()
    for decision in historical["decisions"]:
        decision.pop("execution", None)
    store.save(historical)
    app = AppTest.from_file(str(APP), default_timeout=20).run()
    app.radio(key="page").set_value("Performance & coût").run()
    assert not app.exception
    assert any(
        metric.label == "Tokens d’entrée" and metric.value == "Non enregistrés"
        for metric in app.metric
    )
    assert any(
        metric.label == "Total estimé" and metric.value == "Non disponible" for metric in app.metric
    )
