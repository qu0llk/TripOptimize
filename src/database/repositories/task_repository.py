"""Репозиторий доступа к задачам подбора маршрута (:class:`SearchTask`).

Инкапсулирует CRUD-операции над таблицей задач: создание, обновление статуса
и сохранение результата. Как и :class:`~src.database.repositories.\
ticket_repository.TicketRepository`, работает в рамках переданной сессии и не
управляет транзакцией самостоятельно.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from src.database.models import SearchTask, TaskStatus

logger = logging.getLogger(__name__)


def _coerce_uuid(task_id: uuid.UUID | str) -> uuid.UUID:
    """Приводит идентификатор задачи к :class:`uuid.UUID`.

    Позволяет вызывающему коду (бот, web-слой) передавать id как строку,
    не заботясь о преобразовании.
    """
    return task_id if isinstance(task_id, uuid.UUID) else uuid.UUID(str(task_id))


class TaskRepository:
    """Доступ к данным задач подбора маршрута.

    Args:
        session: Активная асинхронная сессия SQLAlchemy.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create_task(self, user_inputs: dict[str, Any]) -> SearchTask:
        """Создаёт новую задачу в статусе ``PENDING``.

        Args:
            user_inputs: Пользовательский ввод (города, дни, фильтры) —
                сохраняется как JSONB без изменения структуры.

        Returns:
            Созданный объект :class:`SearchTask` с заполненными ``id`` и
            серверными полями (``created_at``/``updated_at``).
        """
        task = SearchTask(status=TaskStatus.PENDING, user_inputs=user_inputs)
        self._session.add(task)
        await self._session.flush()
        # refresh подтягивает значения server_default (временные метки),
        # которые БД проставила при INSERT.
        await self._session.refresh(task)
        logger.info("Создана задача %s", task.id)
        return task

    async def get_task(self, task_id: uuid.UUID | str) -> SearchTask | None:
        """Возвращает задачу по идентификатору или ``None``, если её нет."""
        return await self._session.get(SearchTask, _coerce_uuid(task_id))

    async def update_task_status(
        self,
        task_id: uuid.UUID | str,
        status: TaskStatus,
        *,
        error_message: str | None = None,
    ) -> SearchTask | None:
        """Обновляет статус задачи.

        Для статуса ``FAILED`` рекомендуется передавать ``error_message`` с
        описанием причины. ``updated_at`` обновляется автоматически (onupdate).

        Args:
            task_id: Идентификатор задачи.
            status: Новый статус.
            error_message: Текст ошибки (обычно для ``FAILED``).

        Returns:
            Обновлённый объект задачи либо ``None``, если задача не найдена.
        """
        task = await self.get_task(task_id)
        if task is None:
            logger.warning("Задача %s не найдена — статус не обновлён", task_id)
            return None

        task.status = status
        if error_message is not None:
            task.error_message = error_message
        await self._session.flush()
        logger.info("Задача %s → статус %s", task.id, status.value)
        return task

    async def save_task_result(
        self,
        task_id: uuid.UUID | str,
        result_route: dict[str, Any],
        *,
        status: TaskStatus = TaskStatus.COMPLETED,
    ) -> SearchTask | None:
        """Сохраняет итоговый маршрут и переводит задачу в финальный статус.

        Args:
            task_id: Идентификатор задачи.
            result_route: Итоговый оптимизированный маршрут (JSONB-payload).
            status: Финальный статус (по умолчанию ``COMPLETED``).

        Returns:
            Обновлённый объект задачи либо ``None``, если задача не найдена.
        """
        task = await self.get_task(task_id)
        if task is None:
            logger.warning("Задача %s не найдена — результат не сохранён", task_id)
            return None

        task.result_route = result_route
        task.status = status
        await self._session.flush()
        logger.info("Задача %s завершена со статусом %s", task.id, status.value)
        return task
