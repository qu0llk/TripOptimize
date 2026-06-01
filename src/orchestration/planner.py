"""Планировщик плеч маршрута (Модули 4 и 7A).

Преобразует «сырой» пользовательский ввод (города + дни) в список
:class:`~src.orchestration.dto.RouteLeg`, которые предстоит собрать скраперам.

Декомпозиция вынесена за абстракцию :class:`LegPlanner` (принцип инверсии
зависимостей): оркестратор зависит от интерфейса, а не от конкретной
стратегии. Это позволяет Модулю 5 (оптимизатор) подменить стратегию, не
трогая код оркестратора.

Поддерживаются два формата ключей пользовательского ввода:
* канонический (Модуль 7A): ``origin_city`` / ``destination_city`` /
  ``intermediate_cities`` (``[{city, days_to_stay}]``);
* legacy (Модуль 4): ``start_city`` / ``end_city`` / ``intermediate``
  (``[{city, days}]``).
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from datetime import date, datetime, timedelta
from typing import Any

from src.orchestration.dto import RouteLeg

logger = logging.getLogger(__name__)


def _extract_cities(user_inputs: dict[str, Any]) -> list[str]:
    """Собирает уникальный список городов в порядке: старт → … → финиш.

    Поддерживает оба формата ключей (канонический 7A и legacy 4).
    """
    ordered: list[str] = []
    start = (user_inputs.get("origin_city") or user_inputs.get("start_city") or "").strip()
    end = (
        user_inputs.get("destination_city") or user_inputs.get("end_city") or ""
    ).strip()
    intermediate = (
        user_inputs.get("intermediate_cities")
        or user_inputs.get("intermediate")
        or []
    )

    if start:
        ordered.append(start)
    for item in intermediate:
        city = (item.get("city") or "").strip()
        if city:
            ordered.append(city)
    if end:
        ordered.append(end)

    # Схлопываем только смежные дубликаты: кольцевой маршрут (старт == финиш)
    # должен остаться, а вот "Москва, Москва, СПб" → "Москва, СПб".
    unique: list[str] = []
    for city in ordered:
        if not unique or unique[-1] != city:
            unique.append(city)
    return unique


def _extract_days_to_stay(user_inputs: dict[str, Any]) -> list[int]:
    """Возвращает дни пребывания для каждого промежуточного города (по порядку).

    Длина списка равна числу промежуточных городов; i-й элемент — минимальный
    стой в i-м промежуточном городе перед следующим вылетом.
    """
    intermediate = (
        user_inputs.get("intermediate_cities")
        or user_inputs.get("intermediate")
        or []
    )
    result: list[int] = []
    for item in intermediate:
        city = (item.get("city") or "").strip()
        if city:
            days = int(item.get("days_to_stay") or item.get("days") or 0)
            result.append(days)
    return result


def _extract_start_date(user_inputs: dict[str, Any]) -> date:
    """Извлекает дату старта из ввода либо берёт «завтра» по умолчанию."""
    raw = (user_inputs.get("start_date") or "").strip()
    if raw:
        try:
            return date.fromisoformat(raw)
        except ValueError:
            logger.warning(
                "Некорректная start_date %r — используется дата по умолчанию", raw
            )
    # По умолчанию ищем на завтра: сегодняшние рейсы, как правило, уже не купить.
    return (datetime.now() + timedelta(days=1)).date()


class LegPlanner(ABC):
    """Абстрактная стратегия построения плеч маршрута из ввода пользователя."""

    @abstractmethod
    def plan(self, user_inputs: dict[str, Any]) -> list[RouteLeg]:
        """Возвращает список плеч, которые нужно оценить (собрать билеты)."""
        raise NotImplementedError


class SequentialLegPlanner(LegPlanner):
    """Последовательный маршрут: старт → промежуточные (по порядку) → финиш.

    Даёт ровно ``N_промежуточных + 1`` плеч — это и есть число «перелётов» в
    итоговом маршруте, по которому Модуль 7A считает прогресс. Самая
    экономная по трафику стратегия (линейное число плеч против квадратичного
    у :class:`AllPairsLegPlanner`), поэтому выбрана оркестратором по умолчанию.

    Даты плеч пока берутся равными дате старта (точная раскладка по дням
    появится в Модуле 5 — оптимизаторе). Город-дубликат подряд схлопывается.
    """

    def plan(self, user_inputs: dict[str, Any]) -> list[RouteLeg]:
        cities = _extract_cities(user_inputs)
        if len(cities) < 2:
            raise ValueError(
                "Для построения маршрута нужно минимум два города "
                f"(старт и финиш); получено: {cities}."
            )

        start_date = _extract_start_date(user_inputs)
        surplus_days = int(user_inputs.get("surplus_days") or 0)
        days_to_stay = _extract_days_to_stay(user_inputs)

        legs: list[RouteLeg] = []
        # Накопленный минимальный стой до текущего плеча (в днях от start_date).
        cumulative_stay = 0

        for i in range(len(cities) - 1):
            earliest = start_date + timedelta(days=cumulative_stay)
            latest = earliest + timedelta(days=surplus_days)
            d = earliest
            while d <= latest:
                legs.append(RouteLeg(cities[i], cities[i + 1], d))
                d += timedelta(days=1)
            # Стой в cities[i+1]: есть только для промежуточных городов.
            cumulative_stay += days_to_stay[i] if i < len(days_to_stay) else 0

        logger.info(
            "Последовательный план: %d дат-плеч по %d уникальным парам городов "
            "(запас=%d дн.)",
            len(legs),
            len(cities) - 1,
            surplus_days,
        )
        return legs


class AllPairsLegPlanner(LegPlanner):
    """Все нужные для TSP-оптимизации направленные пары городов × диапазон дат.

    Для каждой перестановки промежуточных городов нужны билеты по другим парам,
    чем задал пользователь: этот планировщик заранее собирает все направления,
    которые могут понадобиться оптимизатору.

    Генерируемые пары (экономия против наивного N²):
    * старт → каждый промежуточный
    * каждый промежуточный → финиш
    * каждый промежуточный → каждый другой промежуточный (оба направления)
    * если нет промежуточных — просто старт → финиш

    По каждой паре создаётся плечо на каждый день в окне
    ``[start_date, start_date + Σdays_to_stay + surplus_days]``.
    """

    def plan(self, user_inputs: dict[str, Any]) -> list[RouteLeg]:
        cities = _extract_cities(user_inputs)
        if len(cities) < 2:
            raise ValueError(
                "Для построения маршрута нужно минимум два города "
                f"(старт и финиш); получено: {cities}."
            )

        origin = cities[0]
        destination = cities[-1]
        intermediates = list(dict.fromkeys(cities[1:-1]))  # уникальные, порядок сохранён

        start_date = _extract_start_date(user_inputs)
        surplus_days = int(user_inputs.get("surplus_days") or 0)
        days_to_stay = _extract_days_to_stay(user_inputs)
        end_date = start_date + timedelta(days=sum(days_to_stay) + surplus_days)

        needed: set[tuple[str, str]] = set()
        if intermediates:
            for inter in intermediates:
                needed.add((origin, inter))
                needed.add((inter, destination))
            for i, a in enumerate(intermediates):
                for b in intermediates[i + 1:]:
                    needed.add((a, b))
                    needed.add((b, a))
        else:
            needed.add((origin, destination))

        legs: list[RouteLeg] = []
        for orig, dest in needed:
            d = start_date
            while d <= end_date:
                legs.append(RouteLeg(orig, dest, d))
                d += timedelta(days=1)

        logger.info(
            "TSP-план: %d направленных пар × %d дней = %d дат-плеч",
            len(needed),
            (end_date - start_date).days + 1,
            len(legs),
        )
        return legs
