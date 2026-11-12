import asyncio
import logging
import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query

from optimizer.config import Settings
from optimizer.models import AnalysisResult, Recommendation, RecommendationStatus, WorkloadsResponse
from optimizer.observe import ObservationState, Observer, observe_loop
from optimizer.recommend import ModelCall
from optimizer.service import AnalysisBusy, ObservationUnavailable, RecommendationService
from optimizer.store import Store

logger = logging.getLogger(__name__)


def create_app(
    settings: Settings | None = None, observer: Observer | None = None,
    *, store: Store | None = None, model_call: ModelCall | None = None,
) -> FastAPI:
    """Share one cache/scheduler; allow injected clients for offline tests."""
    settings = settings or Settings()
    state = ObservationState()
    database = store or Store(settings.database_path)
    service = RecommendationService(settings, state, database, model_call)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        await asyncio.to_thread(database.initialize)
        await asyncio.to_thread(service.restore_history)
        reader = observer or Observer.connect(settings)
        stop = asyncio.Event()
        task = asyncio.create_task(observe_loop(reader, state, stop, service.observe))
        try:
            yield
        finally:
            # Finish in-flight work before closing clients, even if cleanup fails.
            stop.set()
            try:
                await task
            finally:
                try:
                    await service.close()
                finally:
                    reader.close()

    app = FastAPI(title="Agentic Cloud Optimizer", lifespan=lifespan)

    @app.get("/health")
    async def health() -> dict[str, str]:
        if state.error is not None or state.snapshot is None:
            raise HTTPException(status_code=503, detail=state.error or "Waiting for observation")
        analysis_error = service.storage_error or service.error
        if analysis_error is not None:
            raise HTTPException(status_code=503, detail=analysis_error)
        return {"status": "healthy"}

    @app.get("/workloads", response_model=WorkloadsResponse)
    async def workloads() -> WorkloadsResponse:
        if state.snapshot is None:
            raise HTTPException(status_code=503, detail=state.error or "Waiting for observation")
        return WorkloadsResponse(**state.snapshot.model_dump(), error=state.error)

    @app.post("/analyze", response_model=AnalysisResult)
    async def analyze() -> AnalysisResult:
        try:
            result = await service.analyze()
        except AnalysisBusy as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ObservationUnavailable as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except sqlite3.Error as exc:
            logger.exception("Unable to read recommendation history")
            raise HTTPException(status_code=503, detail="Unable to read SQLite history") from exc
        if result.status == "failed":
            raise HTTPException(status_code=502, detail=result.model_dump(mode="json"))
        return result

    @app.get("/recommendations", response_model=list[Recommendation])
    async def recommendations(
        status: RecommendationStatus | None = None,
        limit: int = Query(default=100, ge=1, le=1000),
        offset: int = Query(default=0, ge=0),
    ) -> list[Recommendation]:
        try:
            return await asyncio.to_thread(database.recommendations, status, limit, offset)
        except sqlite3.Error as exc:
            logger.exception("Unable to read recommendations")
            raise HTTPException(status_code=503, detail="Unable to read SQLite history") from exc

    return app


app = create_app()
