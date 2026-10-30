"""The business interface accesses runs and reviews exclusively through the API."""

import httpx
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class DashboardSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="AUDIT_DASHBOARD_", env_file=".env", extra="ignore"
    )
    api_url: str = "http://127.0.0.1:8000"
    timeout_seconds: float = Field(default=180, gt=0)


class DashboardError(Exception):
    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class DashboardClient:
    def __init__(self, api_url: str, api_key: str, timeout_seconds: float = 180, *, transport=None):
        self.api_url, self._api_key = api_url.rstrip("/"), api_key
        self.timeout_seconds, self.transport = timeout_seconds, transport

    def request(self, method: str, path: str, **kwargs):
        try:
            with httpx.Client(
                base_url=self.api_url,
                headers={"X-API-Key": self._api_key},
                timeout=httpx.Timeout(self.timeout_seconds, connect=5),
                transport=self.transport,
                follow_redirects=False,
            ) as client:
                response = client.request(method, path, **kwargs)
        except httpx.TimeoutException as exc:
            raise DashboardError(
                "Délai dépassé. Le traitement peut continuer côté serveur ; actualisez avant de relancer."
            ) from exc
        except httpx.RequestError as exc:
            raise DashboardError(
                "Backend inaccessible. Démarrez le serveur avec audit-harness serve."
            ) from exc
        if not response.is_success:
            message = {
                401: "Clé API refusée. Vérifiez AUDIT_API_KEY dans le fichier .env du serveur et du dashboard.",
                404: "Exécution ou décision introuvable. Actualisez la liste.",
                409: "Une modification concurrente a été détectée. Rechargez la revue avant d’enregistrer.",
                503: "Le backend n’est pas configuré. Vérifiez AUDIT_API_KEY.",
            }.get(response.status_code, "La requête a été refusée par le backend.")
            if response.status_code == 422:
                detail = response.json().get("detail")
                if isinstance(detail, str):
                    message = detail
            raise DashboardError(message, response.status_code)
        try:
            return response.json()
        except ValueError as exc:
            raise DashboardError(
                "Réponse API illisible. Vérifiez AUDIT_DASHBOARD_API_URL."
            ) from exc

    def list_runs(self):
        return self.request("GET", "/runs", params={"limit": 50})

    def get_run(self, run_id: str):
        return self.request("GET", f"/runs/{run_id}")

    def reviews(self, run_id: str):
        return self.request("GET", f"/runs/{run_id}/reviews")

    def metrics(self, run_id: str):
        return self.request("GET", f"/runs/{run_id}/metrics")

    def evaluation(self, run_id: str):
        return self.request("GET", f"/runs/{run_id}/evaluation")

    def start_run(self, provider: str):
        return self.request("POST", "/runs", json={"provider": provider})

    def save_review(self, run_id: str, transaction_id: str, control_id: str, request: dict):
        return self.request(
            "POST", f"/runs/{run_id}/reviews/{transaction_id}/{control_id}", json=request
        )
