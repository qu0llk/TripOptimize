"""Алгоритм оптимизации маршрута по дереву переходов (Модуль 5 — Авиа).

Принимает все собранные билеты по каждой паре городов и строит оптимальный
маршрут с учётом минимального стоя в промежуточных городах и запаса дней.

Алгоритм — динамическое программирование:
* каждый билет является узлом графа переходов;
* ребро T_i → T_{i+1} существует, если T_{i+1} вылетает не раньше
  (прилёт T_i + min_стой) и не позже (прилёт T_i + min_стой
  + оставшийся_запас + INTRADAY_BUFFER_HOURS часов);
* ищется путь с минимальной суммарной стоимостью (метрика «money»)
  или суммарной длительностью перелётов (метрика «time»).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from itertools import permutations
from typing import Any

from src.scrapers.dto import TicketDTO

logger = logging.getLogger(__name__)

# Буфер внутри суток: даже при нулевом остатке запаса даём 12 ч гибкости.
_INTRADAY_BUFFER_HOURS = 12


def optimize_itinerary(
    city_pairs: list[tuple[str, str]],
    tickets_by_pair: dict[tuple[str, str], list[TicketDTO]],
    start_date_str: str,
    days_to_stay: list[int],
    surplus_days: int,
    metric: str,
) -> list[TicketDTO | None]:
    """Возвращает оптимальный список билетов — по одному на каждое плечо.

    Args:
        city_pairs: Упорядоченные пары городов [(A,B), (B,C), …].
        tickets_by_pair: Все собранные билеты, ключ — пара (отправление, прибытие).
        start_date_str: Дата начала поездки (ISO-8601, YYYY-MM-DD).
        days_to_stay: Минимальные дни пребывания после каждого промежуточного
            прилёта; len == N_legs − 1 (для финишного города стоя нет).
        surplus_days: Запас дней, который можно распределить между плечами.
        metric: ``"money"`` — минимизировать суммарную цену;
                ``"time"`` — минимизировать суммарную длительность перелётов.

    Returns:
        Список len(city_pairs) билетов. ``None`` на позиции плеча означает, что
        допустимый билет не найден (нет рейсов или путь обрывается).
    """
    if not city_pairs:
        return []

    start_dt = _parse_start_date(start_date_str)

    # Состояние DP: (накопленная_стоимость, остаток_запаса, время_прилёта, путь)
    # Начальное «виртуальное» состояние — до первого плеча.
    dp: list[tuple[int, int, datetime | None, list[TicketDTO]]] = [
        (0, surplus_days, None, [])
    ]

    for leg_idx, city_pair in enumerate(city_pairs):
        # Минимальный стой между предыдущим прилётом и текущим вылетом.
        min_stay_days = days_to_stay[leg_idx - 1] if leg_idx > 0 else 0
        dp_next: list[tuple[int, int, datetime | None, list[TicketDTO]]] = []

        for acc_cost, remaining, prev_arrival, path in dp:
            min_dep = (
                start_dt
                if prev_arrival is None
                else prev_arrival + timedelta(days=min_stay_days)
            )
            max_dep = min_dep + timedelta(
                days=remaining, hours=_INTRADAY_BUFFER_HOURS
            )

            for ticket in tickets_by_pair.get(city_pair, []):
                dep_dt = _parse_dt(ticket.departure_time)
                if dep_dt is None:
                    continue
                if dep_dt < min_dep or dep_dt > max_dep:
                    continue

                # Сколько дней запаса потрачено на этом плече.
                surplus_used = max(0, (dep_dt.date() - min_dep.date()).days)
                new_surplus = remaining - surplus_used
                if new_surplus < 0:
                    continue

                cost_delta = (
                    ticket.price if metric == "money" else ticket.duration_minutes
                )
                arr_dt = _parse_dt(ticket.arrival_time)
                dp_next.append(
                    (
                        acc_cost + cost_delta,
                        new_surplus,
                        arr_dt,
                        path + [ticket],
                    )
                )

        if not dp_next:
            logger.warning(
                "Нет допустимых билетов для плеча %s→%s (leg %d), запас=%d дн.",
                city_pair[0],
                city_pair[1],
                leg_idx,
                dp[0][1] if dp else 0,
            )
            return [None] * len(city_pairs)

        dp = _prune(dp_next)

    if not dp:
        return [None] * len(city_pairs)

    best = min(dp, key=lambda s: s[0])
    return best[3]


# ---------------------------------------------------------------------------
# TSP-оболочка: перебор перестановок промежуточных городов
# ---------------------------------------------------------------------------

def find_optimal_route(
    origin: str,
    destination: str,
    intermediates: list[str],
    days_by_city: dict[str, int],
    tickets_by_pair: dict[tuple[str, str], list[TicketDTO]],
    start_date_str: str,
    surplus_days: int,
    metric: str,
) -> tuple[list[TicketDTO | None], list[str]]:
    """Находит оптимальный порядок промежуточных городов перебором перестановок.

    Для каждой перестановки ``intermediates`` вызывает :func:`optimize_itinerary`
    и выбирает маршрут с минимальной суммарной стоимостью (или длительностью).
    Если ни одна перестановка не даёт полный маршрут, возвращает результат для
    исходного порядка (в нём могут быть ``None``-плечи).

    Сложность: O(N! × T²), где N — число промежуточных городов, T — среднее
    число билетов на пару. При N ≤ 7 (5040 перестановок) работает за секунды.

    Args:
        origin: Город отправления (фиксированный).
        destination: Город назначения (фиксированный; может совпадать с origin).
        intermediates: Промежуточные города в исходном порядке пользователя.
        days_by_city: Минимальные дни пребывания по названию города.
        tickets_by_pair: Все собранные билеты, ключ — (отправление, прибытие).
        start_date_str: Дата начала поездки (ISO-8601).
        surplus_days: Запас дней на весь маршрут.
        metric: ``"money"`` или ``"time"``.

    Returns:
        ``(path, city_sequence)`` — список билетов и упорядоченный список городов.
    """
    best_cost: float = float("inf")
    best_path: list[TicketDTO | None] = []
    best_sequence: list[str] = []

    candidates = intermediates if intermediates else []

    for perm in (permutations(candidates) if candidates else [()]):
        city_seq = [origin] + list(perm) + [destination]
        city_pairs = [
            (city_seq[i], city_seq[i + 1]) for i in range(len(city_seq) - 1)
        ]
        # Стой после каждого промежуточного города (в порядке текущей перестановки).
        stay_list = [days_by_city.get(c, 0) for c in perm]

        path = optimize_itinerary(
            city_pairs=city_pairs,
            tickets_by_pair=tickets_by_pair,
            start_date_str=start_date_str,
            days_to_stay=stay_list,
            surplus_days=surplus_days,
            metric=metric,
        )

        if any(t is None for t in path):
            continue  # неполный путь — пропускаем

        cost = sum(
            t.price if metric == "money" else t.duration_minutes
            for t in path
            if t is not None
        )
        if cost < best_cost:
            best_cost = cost
            best_path = path
            best_sequence = city_seq

    if best_sequence:
        logger.info(
            "TSP: лучший маршрут %s, стоимость=%s",
            " → ".join(best_sequence),
            best_cost,
        )
        return best_path, best_sequence

    # Фоллбэк: исходный порядок пользователя (путь может быть неполным).
    logger.warning("TSP: ни одна перестановка не дала полный маршрут — исходный порядок")
    fallback_seq = [origin] + intermediates + [destination]
    fallback_pairs = [
        (fallback_seq[i], fallback_seq[i + 1]) for i in range(len(fallback_seq) - 1)
    ]
    fallback_stay = [days_by_city.get(c, 0) for c in intermediates]
    fallback_path = optimize_itinerary(
        city_pairs=fallback_pairs,
        tickets_by_pair=tickets_by_pair,
        start_date_str=start_date_str,
        days_to_stay=fallback_stay,
        surplus_days=surplus_days,
        metric=metric,
    )
    return fallback_path, fallback_seq


# ---------------------------------------------------------------------------
# Вспомогательные функции
# ---------------------------------------------------------------------------

def _prune(
    states: list[tuple[int, int, datetime | None, list[TicketDTO]]],
) -> list[tuple[int, int, datetime | None, list[TicketDTO]]]:
    """Убирает доминируемые состояния с одинаковым последним билетом.

    Два состояния, заканчивающихся одним и тем же билетом (одинаковый ключ
    departure+arrival+price), не могут оба оказаться оптимальными: оставляем
    то, у которого меньше накопленная стоимость; при равной — больше остаток
    запаса (более гибкий для следующих плеч).
    """
    best: dict[tuple[Any, ...], tuple[int, int, datetime | None, list[TicketDTO]]] = {}
    for state in states:
        acc_cost, remaining, arr_dt, path = state
        last = path[-1]
        key = (last.departure_time, last.arrival_time, last.price)
        existing = best.get(key)
        if existing is None:
            best[key] = state
        else:
            ex_cost, ex_surplus = existing[0], existing[1]
            if acc_cost < ex_cost or (acc_cost == ex_cost and remaining > ex_surplus):
                best[key] = state
    return list(best.values())


def _parse_start_date(raw: str) -> datetime:
    """Парсит строку даты старта, возвращает datetime 00:00 UTC."""
    try:
        dt = datetime.fromisoformat(raw)
        dt = dt.replace(hour=0, minute=0, second=0, microsecond=0)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        logger.warning("Некорректная start_date %r — использую текущий момент", raw)
        return datetime.now(timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0
        )


def _parse_dt(iso_str: str) -> datetime | None:
    """Парсит ISO-8601 строку в datetime с UTC; возвращает None при ошибке."""
    try:
        dt = datetime.fromisoformat(iso_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None
