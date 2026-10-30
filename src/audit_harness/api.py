import asyncio
import secrets
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, Query

from .config import Settings, load_controls
from .evaluation import evaluate_run
from .llm import MissingLLMKeyError
from .metrics import summarize_metrics
from .models import ReviewRequest, RunRequest
from .observability import flush_observability
from .service import execute
from .storage import Store


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    store = Store(settings.database_url)
    lock = asyncio.Lock()

    @asynccontextmanager
    async def lifespan(app):
        from .observability import configure_observability

        configure_observability()
        try:
            yield
        finally:
            await asyncio.to_thread(flush_observability, settings.langfuse_enabled)
            store.engine.dispose()

    app = FastAPI(title="Purchase-to-Pay Audit Harness", version="0.1.0", lifespan=lifespan)

    def authenticate(x_api_key: Annotated[str | None, Header()] = None):
        if not settings.api_key:
            raise HTTPException(503, "Configure AUDIT_API_KEY before using the API")
        if not x_api_key or not secrets.compare_digest(x_api_key, settings.api_key):
            raise HTTPException(401, "Invalid API key")

    protected = [Depends(authenticate)]

    @app.get("/health")
    def health():
        return {"status": "ok", "version": "0.1.0"}

    @app.get("/controls", dependencies=protected)
    def controls():
        items, digest = load_controls(settings.controls_dir)
        return {"sha256": digest, "controls": items}

    @app.get("/runs", dependencies=protected)
    def list_runs(
        limit: int = Query(default=50, ge=1, le=100), offset: int = Query(default=0, ge=0)
    ):
        return store.list_runs(limit, offset)

    @app.post("/runs", dependencies=protected, status_code=201)
    async def run(request: RunRequest | None = None):
        # Bounded local execution. For larger populations use a durable worker queue.
        if lock.locked():
            raise HTTPException(409, "A run is already executing in this worker")
        async with lock:
            try:
                run_settings = (
                    settings.model_copy(update={"provider": request.provider})
                    if request and request.provider
                    else settings
                )
                result = await execute(run_settings, store)
                return {"run_id": result["run_id"], "decisions": len(result["decisions"])}
            except MissingLLMKeyError as exc:
                raise HTTPException(422, str(exc)) from exc
            except (ValueError, OSError) as exc:
                raise HTTPException(422, "Invalid control configuration or dataset") from exc

    def get(run_id: str):
        try:
            return store.get(run_id)
        except KeyError as exc:
            raise HTTPException(404, "Run not found") from exc

    @app.get("/runs/{run_id}", dependencies=protected)
    def get_run(run_id: str):
        return get(run_id)

    @app.get("/runs/{run_id}/reviews", dependencies=protected)
    def queue(run_id: str):
        get(run_id)
        return store.queue(run_id)

    @app.post("/runs/{run_id}/reviews/{transaction_id}/{control_id}", dependencies=protected)
    def review(run_id: str, transaction_id: str, control_id: str, request: ReviewRequest):
        try:
            return store.review(run_id, transaction_id, control_id, request)
        except KeyError as exc:
            raise HTTPException(404, "Decision not found") from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.get("/runs/{run_id}/metrics", dependencies=protected)
    def metrics(run_id: str):
        return summarize_metrics(get(run_id))

    @app.get("/runs/{run_id}/evaluation", dependencies=protected)
    def evaluate(run_id: str):
        try:
            return evaluate_run(get(run_id), settings.data_dir / "ground_truth" / "labels.csv")
        except (ValueError, OSError) as exc:
            raise HTTPException(422, "Ground truth is unavailable or does not match run") from exc

    return app
