"""ORM-модели приложения (Модуль 3).

Содержит две ключевые таблицы:

* :class:`TicketCache` — кэш собранных скраперами билетов с TTL-логикой,
  снижающий нагрузку на резидентный прокси (повторный сбор того же маршрута
  в течение суток берётся из БД).
* :class:`SearchTask` — запись об асинхронной задаче подбора маршрута:
  пользовательский ввод, статус выполнения и итоговый результат.

Стиль — SQLAlchemy 2.0 (`Mapped` / `mapped_column`). Типы PostgreSQL-специфичны
(`JSONB`, нативный `UUID`), поскольку проект жёстко привязан к PostgreSQL.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Enum as SAEnum,
    Index,
    Integer,
    String,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.database.base import Base


class TaskStatus(str, enum.Enum):
    """Жизненный цикл задачи подбора маршрута.

    Наследование от :class:`str` делает значения JSON-сериализуемыми и
    удобными для сравнения, а также упрощает отдачу статуса наружу (бот,
    web-интерфейс) без дополнительного преобразования.

    Стадии:
        PENDING: Задача создана, ожидает обработки воркером.
        SCRAPING: Идёт сбор билетов скраперами.
        OPTIMIZING: Билеты собраны, выполняется решение TSP.
        COMPLETED: Маршрут успешно построен, результат сохранён.
        FAILED: Задача завершилась ошибкой (см. ``error_message``).
    """

    PENDING = "PENDING"
    SCRAPING = "SCRAPING"
    OPTIMIZING = "OPTIMIZING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class TicketCache(Base):
    """Кэш одного билета, собранного скрапером.

    Записи живут ограниченное время (TTL контролируется на уровне
    репозитория, по умолчанию 24 часа). Композитный индекс
    ``ix_ticket_cache_lookup`` обеспечивает быстрый поиск по типичному
    запросу «город отправления → город прибытия на дату из источника».
    """

    __tablename__ = "ticket_cache"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    #: Источник данных (`aviasales` либо `rzd`) — соответствует
    #: :data:`src.scrapers.dto.ALLOWED_SOURCES`.
    source: Mapped[str] = mapped_column(String(32), nullable=False)

    departure_city: Mapped[str] = mapped_column(String(128), nullable=False)
    arrival_city: Mapped[str] = mapped_column(String(128), nullable=False)

    #: Время отправления/прибытия. ``timezone=True`` хранит значения как
    #: ``timestamptz`` — это исключает неоднозначности часовых поясов.
    departure_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    arrival_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    #: Длительность поездки в минутах (денормализовано ради быстрых сортировок).
    duration_minutes: Mapped[int] = mapped_column(Integer, nullable=False)

    #: Стоимость билета в рублях (целое; копейки округляются ещё в DTO).
    price: Mapped[int] = mapped_column(Integer, nullable=False)

    has_baggage: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    #: Момент попадания записи в кэш. Заполняется на стороне БД (``now()``)
    #: и служит опорной точкой для проверки TTL.
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    __table_args__ = (
        # Композитный индекс под основной сценарий поиска в кэше. Порядок
        # колонок повторяет частоту использования в фильтрах: город вылета и
        # прилёта задаются всегда, время — обычно диапазоном, источник —
        # опционально, поэтому он стоит последним.
        Index(
            "ix_ticket_cache_lookup",
            "departure_city",
            "arrival_city",
            "departure_time",
            "source",
        ),
    )

    def __repr__(self) -> str:  # pragma: no cover - вспомогательное представление
        return (
            f"<TicketCache id={self.id} {self.source} "
            f"{self.departure_city}→{self.arrival_city} "
            f"{self.price}₽>"
        )


class SearchTask(Base):
    """Запись об асинхронной задаче подбора оптимального маршрута.

    Хранит исходный пользовательский ввод и итоговый результат в виде JSONB,
    что позволяет менять структуру payload без миграций схемы на ранних
    стадиях проекта.
    """

    __tablename__ = "search_tasks"

    #: Первичный ключ — UUID. Генерируется на стороне приложения, чтобы id
    #: задачи был известен сразу после создания (его можно вернуть клиенту
    #: до коммита/обработки).
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )

    #: Текущий статус задачи. Хранится как нативный PostgreSQL-enum
    #: ``task_status`` для целостности данных на уровне БД.
    status: Mapped[TaskStatus] = mapped_column(
        SAEnum(TaskStatus, name="task_status"),
        nullable=False,
        default=TaskStatus.PENDING,
        server_default=TaskStatus.PENDING.value,
    )

    #: Пользовательский ввод: города старта/финиша, промежуточные города с
    #: числом дней и фильтры-ограничения (бюджет, транспорт, багаж и т.д.).
    user_inputs: Mapped[dict] = mapped_column(JSONB, nullable=False)

    #: Итоговый оптимизированный маршрут. NULL, пока задача не завершена.
    result_route: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    #: Текст ошибки для статуса FAILED (иначе NULL).
    error_message: Mapped[str | None] = mapped_column(String, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    #: Обновляется при каждом изменении строки (смена статуса, запись
    #: результата) — удобно для отслеживания «зависших» задач.
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    def __repr__(self) -> str:  # pragma: no cover - вспомогательное представление
        return f"<SearchTask id={self.id} status={self.status.value}>"
