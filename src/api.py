"""Бэкенд-API TripOptimizer (Модуль 7A).

FastAPI-слой для фронтенда: приём задач подбора маршрута и трансляция
прогресса в реальном времени через Server-Sent Events (SSE).

Жизненный цикл ресурсов управляется через ``lifespan``: на старте поднимается
менеджер БД (Модуль 3) и фоновый воркер оркестратора (Модуль 4/7A), на
остановке — очередь дренируется, воркер гасится, пул БД закрывается.

Эндпоинты:
* ``POST /api/tasks``               — поставить задачу в очередь (202 + task_id);
* ``GET  /api/tasks/{id}``          — текущий снимок статуса/результата;
* ``GET  /api/tasks/{id}/stream``   — SSE-поток прогресса до завершения задачи.

Запуск (нужны PostgreSQL и валидный `.env`)::

    uvicorn src.api:app --reload
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from src.config import get_settings
from src.config.cities import get_city_catalog
from src.database import AdminRepository, DatabaseSessionManager, TaskRepository, TicketRepository
from src.orchestration import TaskOrchestrator
from src.schemas import SearchRequest

logger = logging.getLogger(__name__)

#: Корень проекта (на уровень выше пакета ``src``) — отсюда берутся каталоги
#: ``templates`` и ``static`` фронтенда (Модуль 7B).
_BASE_DIR = Path(__file__).resolve().parent.parent
_templates = Jinja2Templates(directory=str(_BASE_DIR / "templates"))

#: Период опроса реестра прогресса при формировании SSE-потока (секунды).
_STREAM_POLL_INTERVAL = 0.5
#: Финальные статусы, после которых поток закрывается.
_TERMINAL_STATUSES = {"COMPLETED", "FAILED"}


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Поднимает БД и фоновый воркер на старте, гасит их на остановке."""
    settings = get_settings()  # ранняя валидация прокси и DATABASE_URL
    db_manager = DatabaseSessionManager.from_settings(settings)
    orchestrator = TaskOrchestrator(db_manager, settings=settings)

    await orchestrator.start()
    app.state.db_manager = db_manager
    app.state.orchestrator = orchestrator
    logger.info("API запущено: оркестратор активен")

    try:
        yield
    finally:
        await orchestrator.stop(drain=True)
        await db_manager.dispose()
        logger.info("API остановлено: ресурсы освобождены")


app = FastAPI(title="TripOptimizer API", version="0.7.0", lifespan=lifespan)

# Статика фронтенда (Vanilla JS/CSS) — без сборщиков и node_modules.
app.mount(
    "/static",
    StaticFiles(directory=str(_BASE_DIR / "static")),
    name="static",
)


@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    """Отдаёт одностраничный интерфейс TripOptimizer (Jinja2-шаблон)."""
    return _templates.TemplateResponse("index.html", {"request": request})


@app.get("/api/cities")
async def search_cities(q: str = "") -> list[str]:
    """Возвращает до 10 названий городов, начинающихся с запроса ``q``."""
    return get_city_catalog().search(q.strip(), limit=10)


@app.get("/api/cities/coordinates")
async def city_coordinates(names: str = "") -> dict[str, list[float] | None]:
    """Возвращает ``{city: [lat, lon] | null}`` для переданного списка имён.

    Принимает ``names`` — названия городов, разделённые запятой. Используется
    фронтендом для отрисовки маршрута на карте мира; отдача списком (а не
    словарём по одному) — чтобы не делать N запросов на N точек маршрута.
    """
    catalog = get_city_catalog()
    out: dict[str, list[float] | None] = {}
    for raw in names.split(","):
        name = raw.strip()
        if not name:
            continue
        coords = catalog.coordinates(name)
        out[name] = list(coords) if coords is not None else None
    return out


@app.post("/api/tasks", status_code=202)
async def create_task(request: SearchRequest) -> dict[str, str]:
    """Создаёт задачу подбора маршрута и ставит её в очередь оркестратора.

    Возвращает идентификатор задачи (HTTP 202 Accepted) — обработка идёт в
    фоне, прогресс доступен через ``GET /api/tasks/{task_id}/stream``.
    """
    orchestrator: TaskOrchestrator = app.state.orchestrator
    task_id = await orchestrator.submit(request.to_user_inputs())
    return {"task_id": str(task_id), "status": "PENDING"}


@app.get("/api/tasks/{task_id}")
async def get_task(task_id: uuid.UUID) -> dict[str, Any]:
    """Возвращает текущий снимок состояния задачи (статус + прогресс + результат)."""
    return await _snapshot(app, task_id, allow_404=True)


@app.get("/api/tasks/{task_id}/stream")
async def stream_task(task_id: uuid.UUID) -> StreamingResponse:
    """SSE-поток: транслирует прогресс задачи в браузер до её завершения."""
    return StreamingResponse(
        _event_generator(task_id),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ====================================================================== #
# Админ-панель (Модуль 7C)
# ====================================================================== #
@app.get("/admin", response_class=HTMLResponse)
async def admin_page(request: Request) -> HTMLResponse:
    """Отдаёт страницу админ-панели (телеметрия, статусы, здоровье скраперов)."""
    return _templates.TemplateResponse("admin.html", {"request": request})


@app.get("/api/admin/overview")
async def admin_overview() -> dict[str, Any]:
    """Сводка для дашборда: метрики, последние задачи и здоровье скраперов."""
    db_manager: DatabaseSessionManager = app.state.db_manager
    orchestrator: TaskOrchestrator = app.state.orchestrator
    telemetry = orchestrator.telemetry.snapshot()

    async with db_manager.session() as session:
        repo = AdminRepository(session)
        total = await repo.count_total_tasks()
        active = await repo.count_active_tasks()
        cache_rows = await repo.count_cache_rows()
        tasks = await repo.recent_tasks(limit=20)

    return {
        "metrics": {
            "total_tasks": total,
            "active_tasks": active,
            "cache_hits": telemetry["cache_hits"],
            "cache_rows": cache_rows,
        },
        "tasks": tasks,
        "scrapers": telemetry["scrapers"],
    }


@app.post("/api/admin/cache/purge")
async def admin_purge_cache() -> dict[str, int]:
    """Удаляет из кэша билеты старше 24 часов (TTL). Возвращает число строк."""
    db_manager: DatabaseSessionManager = app.state.db_manager
    async with db_manager.session() as session:
        deleted = await TicketRepository(session).purge_expired()
    logger.info("Админ: очищено протухших записей кэша: %d", deleted)
    return {"deleted": deleted}


async def _event_generator(task_id: uuid.UUID) -> AsyncIterator[str]:
    """Генерирует SSE-пакеты: при каждом изменении прогресса отправляет JSON."""
    last_payload: str | None = None
    while True:
        snapshot = await _snapshot(app, task_id, allow_404=False)
        payload = json.dumps(snapshot, ensure_ascii=False)

        if payload != last_payload:
            last_payload = payload
            yield f"data: {payload}\n\n"

        if snapshot["status"] in _TERMINAL_STATUSES:
            break
        await asyncio.sleep(_STREAM_POLL_INTERVAL)


async def _snapshot(
    app: FastAPI, task_id: uuid.UUID, *, allow_404: bool
) -> dict[str, Any]:
    """Собирает единый снимок прогресса из in-memory реестра и БД.

    Реестр оркестратора — основной источник live-прогресса (плечи, проценты).
    БД — источник истины по статусу/результату (на случай рестарта процесса).
    """
    orchestrator: TaskOrchestrator = app.state.orchestrator
    db_manager: DatabaseSessionManager = app.state.db_manager

    progress = orchestrator.get_progress(task_id)

    status = progress.status.value if progress else None
    total_legs = progress.total_legs if progress else 0
    completed_legs = progress.completed_legs if progress else 0
    percentage = progress.percentage if progress else 0
    result = progress.result if progress else None

    # Дочитываем из БД статус/результат, если в памяти их нет (например, после
    # перезапуска сервиса) либо для подтверждения финального состояния.
    if progress is None or result is None:
        async with db_manager.session() as session:
            task = await TaskRepository(session).get_task(task_id)
            if task is None:
                if progress is None and allow_404:
                    raise HTTPException(status_code=404, detail="Задача не найдена")
            else:
                status = status or task.status.value
                result = result if result is not None else task.result_route

    return {
        "status": status or "PENDING",
        "progress_percentage": percentage,
        "total_legs": total_legs,
        "completed_legs": completed_legs,
        "result": result,
    }
