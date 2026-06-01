"""Репозиторий доступа к кэшу билетов (:class:`TicketCache`).

Инкапсулирует все запросы к таблице кэша, скрывая детали SQLAlchemy от
вышестоящих слоёв (паттерн Repository, принцип единственной ответственности).
Реализует TTL-логику: записи старше :attr:`TicketRepository.DEFAULT_TTL`
считаются протухшими — их можно игнорировать при чтении и физически удалять.

Репозиторий не управляет транзакцией: он работает в рамках переданной сессии,
а коммит/откат остаётся за вызывающим кодом (как правило — контекст-менеджер
:meth:`~src.database.manager.DatabaseSessionManager.session`).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Iterable, Sequence

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.database.models import TicketCache
from src.scrapers.dto import TicketDTO

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    """Возвращает текущий момент в UTC (timezone-aware).

    Вынесено в отдельную функцию, чтобы единообразно работать с
    ``timestamptz``-колонками и упростить подмену времени в тестах.
    """
    return datetime.now(timezone.utc)


class TicketRepository:
    """Доступ к данным кэша билетов.

    Args:
        session: Активная асинхронная сессия SQLAlchemy.
    """

    #: TTL кэша по умолчанию. Билеты старше суток считаются неактуальными:
    #: цены меняются часто, а хранить устаревшие предложения смысла нет.
    DEFAULT_TTL: timedelta = timedelta(hours=24)

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_cached_tickets(
        self,
        departure_city: str,
        arrival_city: str,
        *,
        source: str | None = None,
        departure_from: datetime | None = None,
        departure_to: datetime | None = None,
        ttl: timedelta | None = None,
        now: datetime | None = None,
    ) -> list[TicketCache]:
        """Возвращает актуальные (не протухшие) билеты из кэша.

        Учитываются только записи, добавленные не ранее чем ``now - ttl`` —
        протухшие по TTL строки молча игнорируются.

        Args:
            departure_city: Город отправления (точное совпадение).
            arrival_city: Город прибытия (точное совпадение).
            source: Необязательный фильтр по источнику (`aviasales`/`rzd`).
            departure_from: Нижняя граница времени отправления (включительно).
            departure_to: Верхняя граница времени отправления (включительно).
            ttl: Порог свежести; по умолчанию :attr:`DEFAULT_TTL`.
            now: Опорный момент «сейчас» (для тестов); по умолчанию UTC-now.

        Returns:
            Список :class:`TicketCache`, отсортированный по возрастанию цены.
        """
        ttl = ttl if ttl is not None else self.DEFAULT_TTL
        cutoff = (now or _utcnow()) - ttl

        stmt = select(TicketCache).where(
            TicketCache.departure_city == departure_city,
            TicketCache.arrival_city == arrival_city,
            TicketCache.created_at >= cutoff,
        )
        if source is not None:
            stmt = stmt.where(TicketCache.source == source)
        if departure_from is not None:
            stmt = stmt.where(TicketCache.departure_time >= departure_from)
        if departure_to is not None:
            stmt = stmt.where(TicketCache.departure_time <= departure_to)

        stmt = stmt.order_by(TicketCache.price.asc())

        result = await self._session.execute(stmt)
        tickets = list(result.scalars().all())
        logger.debug(
            "Кэш: найдено %d актуальных билетов %s → %s (TTL=%s)",
            len(tickets),
            departure_city,
            arrival_city,
            ttl,
        )
        return tickets

    async def bulk_save_tickets(
        self, tickets: Iterable[TicketDTO]
    ) -> list[TicketCache]:
        """Массово сохраняет билеты из DTO скраперов в кэш.

        DTO хранит времена в виде ISO-8601 строк — здесь они приводятся к
        ``datetime``. Поле ``created_at`` заполняется на стороне БД (``now()``).
        Метод выполняет ``flush`` (но не ``commit``): фиксация транзакции —
        ответственность вызывающего кода.

        Args:
            tickets: Итерируемое DTO-билетов от скраперов.

        Returns:
            Список созданных ORM-объектов :class:`TicketCache` с заполненными
            идентификаторами.
        """
        models: list[TicketCache] = [
            TicketCache(
                source=dto.source,
                departure_city=dto.departure_city,
                arrival_city=dto.arrival_city,
                departure_time=datetime.fromisoformat(dto.departure_time),
                arrival_time=datetime.fromisoformat(dto.arrival_time),
                duration_minutes=dto.duration_minutes,
                price=dto.price,
                has_baggage=dto.has_baggage,
            )
            for dto in tickets
        ]
        if not models:
            return []

        self._session.add_all(models)
        # flush присваивает первичные ключи и отправляет INSERT в БД, не
        # закрывая транзакцию — это позволяет вернуть наверх готовые объекты.
        await self._session.flush()
        logger.info("В кэш записано билетов: %d", len(models))
        return models

    async def purge_expired(
        self,
        *,
        ttl: timedelta | None = None,
        now: datetime | None = None,
    ) -> int:
        """Физически удаляет протухшие по TTL записи кэша.

        Полезно вызывать периодически (по расписанию) для контроля размера
        таблицы. В отличие от :meth:`get_cached_tickets`, которая лишь
        игнорирует старые строки при чтении, этот метод освобождает место.

        Args:
            ttl: Порог свежести; по умолчанию :attr:`DEFAULT_TTL`.
            now: Опорный момент «сейчас» (для тестов); по умолчанию UTC-now.

        Returns:
            Количество удалённых записей.
        """
        ttl = ttl if ttl is not None else self.DEFAULT_TTL
        cutoff = (now or _utcnow()) - ttl

        stmt = delete(TicketCache).where(TicketCache.created_at < cutoff)
        result = await self._session.execute(stmt)
        deleted = result.rowcount or 0
        logger.info("Удалено протухших записей кэша: %d", deleted)
        return deleted
