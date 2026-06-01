"""Демонстрационный запуск движка скрапинга (Модули 1 и 2).

Скрипт инициализирует :class:`ScraperManager`, параллельно запускает все
подключённые скраперы для маршрута `Москва → Санкт-Петербург` на завтрашнюю
дату и печатает нормализованные :class:`TicketDTO`-объекты.

Запуск:
    python main.py

Без валидной конфигурации резидентного прокси скрипт завершится с понятной
ошибкой ещё до запуска браузера — это намеренное поведение.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, timedelta

from src.config import ConfigurationError
from src.scrapers import ScraperError, ScraperManager, TicketDTO

# Маршрут и параметры демонстрации.
_DEPARTURE_CITY = "Москва"
_ARRIVAL_CITY = "Санкт-Петербург"


def _configure_logging() -> None:
    """Настраивает базовое логирование для наглядной демонстрации."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def _print_tickets(tickets: list[TicketDTO]) -> None:
    """Печатает собранные билеты в читаемом виде."""
    if not tickets:
        print("\nБилеты не найдены (или источники недоступны).")
        return

    print(f"\nСобрано билетов: {len(tickets)}\n" + "-" * 60)
    # Сортируем по цене — самое полезное представление для глаза.
    for ticket in sorted(tickets, key=lambda t: t.price):
        baggage = "с багажом" if ticket.has_baggage else "без багажа"
        print(
            f"[{ticket.source:>9}] "
            f"{ticket.departure_city} → {ticket.arrival_city} | "
            f"{ticket.departure_time} → {ticket.arrival_time} | "
            f"{ticket.duration_minutes} мин | "
            f"{ticket.price} ₽ | {baggage}"
        )


async def _run_demo() -> None:
    """Основная корутина демонстрации."""
    travel_date = date.today() + timedelta(days=1)

    # Инициализация менеджера попутно валидирует настройки прокси.
    manager = ScraperManager()
    tickets = await manager.collect(_DEPARTURE_CITY, _ARRIVAL_CITY, travel_date)
    _print_tickets(tickets)


def main() -> None:
    """Точка входа: обрабатывает фатальные ошибки конфигурации/скрапинга."""
    _configure_logging()
    try:
        asyncio.run(_run_demo())
    except ConfigurationError as exc:
        # Ошибка конфигурации (прокси или БД) — частый сценарий первого запуска.
        print(f"\nОшибка конфигурации:\n{exc}")
        raise SystemExit(1) from exc
    except ScraperError as exc:
        print(f"\nОшибка скрапинга:\n{exc}")
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()
