"""Инициализация схемы БД и быстрая проверка работоспособности (Модуль 3).

Скрипт выполняет три действия:

1. Подключается к PostgreSQL по ``DATABASE_URL`` и создаёт таблицы, если их
   ещё нет (``create_all`` — идемпотентно).
2. Прогоняет sanity-check кэша билетов: записывает несколько mock-:class:`\
   ~src.scrapers.dto.TicketDTO` через :class:`TicketRepository` и читает их
   обратно с учётом TTL.
3. Прогоняет sanity-check задач: создаёт :class:`SearchTask`, меняет статус и
   сохраняет результат через :class:`TaskRepository`.

Запуск::

    python init_db.py

Требуется доступный экземпляр PostgreSQL и корректный ``DATABASE_URL`` в
`.env`. Без них скрипт завершится с понятной диагностикой.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy.exc import SQLAlchemyError

from src.config import ConfigurationError, get_settings
from src.database import (
    DatabaseSessionManager,
    TaskRepository,
    TaskStatus,
    TicketRepository,
)
from src.scrapers.dto import TicketDTO


def _configure_logging() -> None:
    """Настраивает базовое логирование для наглядного вывода прогресса."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def _build_mock_tickets() -> list[TicketDTO]:
    """Готовит небольшой набор mock-билетов для проверки кэша."""
    base = datetime(2026, 6, 2, 8, 0, tzinfo=timezone.utc)
    return [
        TicketDTO(
            source="aviasales",
            departure_city="Москва",
            arrival_city="Санкт-Петербург",
            departure_time=base.isoformat(),
            arrival_time=(base + timedelta(hours=1, minutes=25)).isoformat(),
            duration_minutes=85,
            price=4500,
            has_baggage=False,
        ),
        TicketDTO(
            source="rzd",
            departure_city="Москва",
            arrival_city="Санкт-Петербург",
            departure_time=(base + timedelta(hours=2)).isoformat(),
            arrival_time=(base + timedelta(hours=5, minutes=40)).isoformat(),
            duration_minutes=220,
            price=2800,
            has_baggage=True,
        ),
    ]


async def _check_ticket_repository(manager: DatabaseSessionManager) -> None:
    """Записывает и читает mock-билеты, проверяя TicketRepository."""
    mock_tickets = _build_mock_tickets()

    async with manager.session() as session:
        repo = TicketRepository(session)
        saved = await repo.bulk_save_tickets(mock_tickets)
        print(f"  • Записано билетов в кэш: {len(saved)}")

    async with manager.session() as session:
        repo = TicketRepository(session)
        cached = await repo.get_cached_tickets(
            departure_city="Москва",
            arrival_city="Санкт-Петербург",
        )
        print(f"  • Прочитано из кэша (актуальных): {len(cached)}")
        for ticket in cached:
            print(
                f"      [{ticket.source:>9}] {ticket.price} ₽ | "
                f"{ticket.duration_minutes} мин | id={ticket.id}"
            )


async def _check_task_repository(manager: DatabaseSessionManager) -> None:
    """Создаёт задачу, меняет статус и сохраняет результат — проверка TaskRepository."""
    user_inputs = {
        "start_city": "Москва",
        "end_city": "Сочи",
        "intermediate": [{"city": "Казань", "days": 2}],
        "filters": {"transport": "any", "optimize_by": "price"},
    }

    async with manager.session() as session:
        repo = TaskRepository(session)
        task = await repo.create_task(user_inputs)
        task_id = task.id
        print(f"  • Создана задача: id={task_id} | статус={task.status.value}")

    async with manager.session() as session:
        repo = TaskRepository(session)
        await repo.update_task_status(task_id, TaskStatus.SCRAPING)
        await repo.update_task_status(task_id, TaskStatus.OPTIMIZING)

    async with manager.session() as session:
        repo = TaskRepository(session)
        result_route = {"order": ["Москва", "Казань", "Сочи"], "total_price": 15300}
        await repo.save_task_result(task_id, result_route)

    async with manager.session() as session:
        repo = TaskRepository(session)
        final = await repo.get_task(task_id)
        assert final is not None  # задача только что сохранена
        print(
            f"  • Финальный статус: {final.status.value} | "
            f"результат: {final.result_route}"
        )


async def _run() -> None:
    """Основная корутина: создание схемы и два sanity-check сценария."""
    settings = get_settings()
    manager = DatabaseSessionManager.from_settings(settings)

    try:
        print("Создание схемы БД…")
        await manager.create_all()
        print("Схема готова.\n")

        print("Проверка TicketRepository:")
        await _check_ticket_repository(manager)

        print("\nПроверка TaskRepository:")
        await _check_task_repository(manager)

        print("\nГотово: все проверки выполнены успешно.")
    finally:
        await manager.dispose()


def main() -> None:
    """Точка входа: обрабатывает ошибки конфигурации и БД с понятным выводом."""
    _configure_logging()
    try:
        asyncio.run(_run())
    except ConfigurationError as exc:
        print(f"\nОшибка конфигурации:\n{exc}")
        raise SystemExit(1) from exc
    except SQLAlchemyError as exc:
        print(
            "\nОшибка базы данных. Проверьте, что PostgreSQL доступен и "
            f"DATABASE_URL верен:\n{exc}"
        )
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()
