"""Pydantic-схемы запросов/ответов API (Модуль 7A).

Описывают контракт между фронтендом и бэкендом: тело запроса на подбор
маршрута и нормализацию пользовательского ввода в формат, понятный
оркестратору (Модуль 4/7A).
"""

from __future__ import annotations

from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

TransportType = Literal["both", "plane", "train"]
OptimizationMetric = Literal["money", "time"]


class SearchFilters(BaseModel):
    """Фильтры подбора: транспорт, багаж, бюджет и метрика оптимизации."""

    transport_type: TransportType = "both"
    require_baggage: bool = False
    #: Максимальный бюджет в рублях; ``None`` — без ограничения.
    max_budget: int | None = Field(default=None, ge=0)
    optimization_metric: OptimizationMetric = "money"


class IntermediateCity(BaseModel):
    """Промежуточный город маршрута с числом дней пребывания."""

    city: str = Field(min_length=1)
    days_to_stay: int = Field(ge=0)


class SearchRequest(BaseModel):
    """Тело запроса на создание задачи подбора маршрута.

    Повторяет табличный ввод пользователя на фронтенде. Метод
    :meth:`to_user_inputs` приводит запрос к плоскому словарю, который
    сохраняется в БД (JSONB) и читается планировщиком плеч.
    """

    origin_city: str = Field(min_length=1)
    destination_city: str = Field(min_length=1)
    #: Дата старта в формате YYYY-MM-DD.
    start_date: str
    #: «Запас» свободных дней, которые оптимизатор может распределить по маршруту.
    surplus_days: int = Field(default=0, ge=0)
    filters: SearchFilters = Field(default_factory=SearchFilters)
    intermediate_cities: list[IntermediateCity] = Field(default_factory=list)

    @field_validator("start_date")
    @classmethod
    def _validate_start_date(cls, value: str) -> str:
        """Проверяет, что дата задана в ISO-формате YYYY-MM-DD."""
        try:
            date.fromisoformat(value)
        except ValueError as exc:
            raise ValueError("start_date должна быть в формате YYYY-MM-DD") from exc
        return value

    def to_user_inputs(self) -> dict[str, Any]:
        """Приводит запрос к словарю user_inputs для оркестратора и БД."""
        return self.model_dump()
