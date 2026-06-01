"""DTO билета — единый формат вывода для всех скраперов.

Любой скрапер обязан нормализовать сырые данные источника к этому объекту,
чтобы вышестоящие модули (оптимизатор маршрутов, БД, бот) работали с
унифицированной структурой и не зависели от исходного API.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any, Final

# Допустимые источники данных. Используется при валидации DTO и для
# сегментации записей в БД на следующих этапах проекта.
ALLOWED_SOURCES: Final[frozenset[str]] = frozenset({"aviasales", "rzd"})


@dataclass(frozen=True, slots=True)
class TicketDTO:
    """Иммутабельное представление одного билета.

    Поля:
        source: Идентификатор источника (`aviasales` либо `rzd`).
        departure_city: Название города отправления (как у источника).
        arrival_city: Название города прибытия.
        departure_time: Время отправления в ISO-8601.
        arrival_time: Время прибытия в ISO-8601.
        duration_minutes: Длительность поездки в минутах.
        price: Стоимость билета в рублях (целое число — копейки округляются).
        has_baggage: Признак наличия багажа в тарифе.
        booking_url: Прямая deep-link ссылка на покупку билета (заполняется на
            этапе сборки итогового маршрута; до этого — ``None``).
    """

    source: str
    departure_city: str
    arrival_city: str
    departure_time: str
    arrival_time: str
    duration_minutes: int
    price: int
    has_baggage: bool
    #: Необязательная deep-link ссылка на бронирование. Поле опционально, чтобы
    #: не ломать существующее создание DTO в скраперах: ссылка проставляется
    #: позже сервисом :class:`~src.orchestration.booking.BookingLinkBuilder`.
    booking_url: str | None = None

    def __post_init__(self) -> None:
        """Базовая валидация полей — отлавливает мусорные значения на входе."""
        if self.source not in ALLOWED_SOURCES:
            raise ValueError(
                f"Неизвестный источник {self.source!r}; ожидался один из "
                f"{sorted(ALLOWED_SOURCES)}."
            )
        if self.duration_minutes < 0:
            raise ValueError(
                f"duration_minutes должен быть неотрицательным, получено "
                f"{self.duration_minutes}."
            )
        if self.price < 0:
            raise ValueError(
                f"price должен быть неотрицательным, получено {self.price}."
            )
        # Проверяем ISO-формат, чтобы дальше не разгребать кривые строки.
        for field_name in ("departure_time", "arrival_time"):
            value = getattr(self, field_name)
            try:
                datetime.fromisoformat(value)
            except ValueError as exc:
                raise ValueError(
                    f"Поле {field_name} должно быть в формате ISO-8601, "
                    f"получено {value!r}."
                ) from exc

    def to_dict(self) -> dict[str, Any]:
        """Возвращает словарное представление DTO (удобно для логов и БД)."""
        return asdict(self)