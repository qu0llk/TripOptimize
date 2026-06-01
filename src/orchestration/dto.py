"""DTO слоя оркестрации (Модуль 4).

Содержит :class:`RouteLeg` — атомарное «плечо» маршрута (пара городов на
конкретную дату), которым оперируют планировщик плеч и скрапинг-координатор.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True, slots=True)
class RouteLeg:
    """Одно «плечо» маршрута: переезд из города в город на заданную дату.

    Поля:
        departure_city: Город отправления.
        arrival_city: Город прибытия.
        travel_date: Дата поездки.
    """

    departure_city: str
    arrival_city: str
    travel_date: date

    def as_tuple(self) -> tuple[str, str, date]:
        """Возвращает плечо в формате, ожидаемом ``ScraperManager.collect_many``."""
        return (self.departure_city, self.arrival_city, self.travel_date)
