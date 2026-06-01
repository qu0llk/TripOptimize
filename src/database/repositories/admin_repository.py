"""Репозиторий агрегатов для админ-панели (Модуль 7C).

Содержит только read-only аналитические запросы поверх :class:`SearchTask` и
:class:`TicketCache`: счётчики для карточек телеметрии и выборка последних
задач для таблицы статусов. Как и прочие репозитории, работает в рамках
переданной сессии и не управляет транзакцией.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.database.models import SearchTask, TaskStatus, TicketCache

#: Статусы, считающиеся «активными» (задача ещё в работе).
_ACTIVE_STATUSES = (TaskStatus.PENDING, TaskStatus.SCRAPING, TaskStatus.OPTIMIZING)


class AdminRepository:
    """Аналитические запросы для дашборда администратора."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def count_total_tasks(self) -> int:
        """Всего созданных задач подбора маршрута."""
        return await self._scalar(select(func.count()).select_from(SearchTask))

    async def count_active_tasks(self) -> int:
        """Задачи в незавершённых статусах (PENDING/SCRAPING/OPTIMIZING)."""
        stmt = (
            select(func.count())
            .select_from(SearchTask)
            .where(SearchTask.status.in_(_ACTIVE_STATUSES))
        )
        return await self._scalar(stmt)

    async def count_cache_rows(self) -> int:
        """Всего строк в кэше билетов :class:`TicketCache`."""
        return await self._scalar(select(func.count()).select_from(TicketCache))

    async def recent_tasks(self, limit: int = 20) -> list[dict[str, Any]]:
        """Последние ``limit`` задач — данные для таблицы статусов."""
        stmt = (
            select(SearchTask)
            .order_by(SearchTask.created_at.desc())
            .limit(limit)
        )
        result = await self._session.execute(stmt)
        return [self._to_row(task) for task in result.scalars().all()]

    async def _scalar(self, stmt: Any) -> int:
        """Выполняет агрегатный запрос и возвращает целочисленный результат."""
        return int((await self._session.execute(stmt)).scalar_one() or 0)

    @staticmethod
    def _to_row(task: SearchTask) -> dict[str, Any]:
        """Преобразует ORM-задачу в плоскую строку для админ-таблицы."""
        inputs = task.user_inputs or {}
        filters = inputs.get("filters") or {}
        return {
            "id": str(task.id),
            "created_at": task.created_at.isoformat() if task.created_at else None,
            "status": task.status.value,
            "origin": inputs.get("origin_city") or inputs.get("start_city") or "—",
            "destination": (
                inputs.get("destination_city") or inputs.get("end_city") or "—"
            ),
            "metric": filters.get("optimization_metric", "money"),
            "error_message": task.error_message,
        }
