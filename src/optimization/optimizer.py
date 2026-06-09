"""Алгоритм оптимизации маршрута по дереву переходов (Модуль 5 — Авиа).

Принимает все собранные билеты по каждой паре городов и строит оптимальный
маршрут с учётом минимального стоя в промежуточных городах и запаса дней.

Алгоритм — динамическое программирование:
* каждый билет является узлом графа переходов;
* ребро T_i → T_{i+1} существует, если T_{i+1} вылетает не раньше
  ``прилёт T_i + min_стой`` (для первого плеча — ``start_date``);
* ищется путь с минимальной суммарной стоимостью (метрика «money»)
  или суммарной длительностью перелётов (метрика «time»).

Запас (``surplus_days``) — это **глобальный** бюджет насколько позже от
``start_date`` мы можем закончить маршрут. Раньше он уменьшался per-leg по
эвристике ``dep.date() - min_dep.date()``, что съедало весь запас на первом
плече и оставляло нулевой slack для следующих — отсюда «не находит билеты»
для длинных маршрутов с небольшим запасом. Сейчас запас считается по
фактической дате **прилёта последнего плеча** относительно ``start_date``.

Перекрёстный штраф (см. ``_cross_term``) применяется независимо от метрики,
чтобы длинная поездка ощущалась длинной даже при оптимизации по деньгам,
а дорогой билет — дорогим даже при оптимизации по времени.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from itertools import permutations
from typing import Any

from src.scrapers.dto import TicketDTO

logger = logging.getLogger(__name__)

# Минимальный «дышащий» зазор между прилётом предыдущего плеча и вылетом
# следующего, когда ``min_stay_days == 0``: один час, чтобы не ловить
# ситуации «прилетел в 14:00, улетел в 14:00».
_MIN_TRANSFER = timedelta(hours=1)

# Перекрёсный штраф «цена↔время», применяется НЕЗАВИСИМО от выбранной
# метрики. Идея: длинная поездка должна ощущаться как длинная даже при
# оптимизации по деньгам, а дорогой билет — как дорогой даже при
# оптимизации по времени. Без этого пользователь, выбравший метрику
# ``money``, регулярно получает 30-часовой поезд за 1500₽ как «лучший»,
# а пользователь с метрикой ``time`` — переплачивает за лишние сутки.
#
# Правила (по обратной связи пользователя):
#   * метрика ``money``: к цене билета прибавляется ``+50₽`` за каждый час
#     его ``duration_minutes`` (12-часовой перелёт → +600₽);
#   * метрика ``time``:  к длительности прибавляется ``+1 час (60 мин)`` за
#     каждые полные 2000₽ его ``price`` (билет за 20 000₽ → +10ч = +600 мин).
# Эти коэффициенты — компромисс: достаточно большие, чтобы «длинный
# дешёвый» и «быстрый дорогой» били друг друга на равных, но не настолько,
# чтобы полностью инвертировать выбор метрики.
_RUB_PER_HOUR_CROSS: int = 50
_HOURS_PER_2000_RUB_CROSS: int = 1
_RUB_STEP_CROSS: int = 2000
_MIN_PER_HOUR_CROSS: int = 60


def _cross_term(ticket: "TicketDTO", metric: str) -> int:
    """Возвращает перекрёстный штраф для одного билета в единицах метрики.

    Args:
        ticket: Билет, для которого считаем штраф.
        metric: ``"money"`` → штраф в рублях; ``"time"`` → штраф в минутах.

    Returns:
        Целое число, которое нужно прибавить к ``cost_delta`` билета.
    """
    if metric == "money":
        # +50₽ за каждый полный час. Дробные часы округляем вверх —
        # билет на 1ч20м получает штраф за 2ч, чтобы короткие «дроби»
        # не были бесплатным бонусом.
        hours = (ticket.duration_minutes + 59) // 60
        return hours * _RUB_PER_HOUR_CROSS
    # metric == "time": +60 мин за каждые полные 2000₽ цены.
    rub_steps = ticket.price // _RUB_STEP_CROSS
    return rub_steps * _HOURS_PER_2000_RUB_CROSS * _MIN_PER_HOUR_CROSS


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
            Билеты приходят со всех дат, которые собрал скрапер/кэш (для каждой
            пары в окне ``[start_date, start_date + Σstays + surplus_days]``).
        start_date_str: Дата начала поездки (ISO-8601, YYYY-MM-DD).
        days_to_stay: Минимальные дни пребывания после каждого промежуточного
            прилёта; len == N_legs − 1 (для финишного города стоя нет).
        surplus_days: Глобальный запас: на сколько дней позже ``start_date``
            допустимо завершить последний перелёт. Может быть ``0`` — тогда
            оптимизатор стремится уложить весь маршрут ровно в «сумма стоёв»
            дней от старта.
        metric: ``"money"`` — минимизировать суммарную цену;
                ``"time"`` — минимизировать суммарную длительность перелётов.

    Returns:
        Список len(city_pairs) билетов. ``None`` на позиции плеча означает, что
        допустимый билет не найден (нет рейсов или путь обрывается).
    """
    if not city_pairs:
        return []

    start_dt = _parse_start_date(start_date_str)
    # ``surplus_days`` намеренно НЕ используется для отсечения или штрафа
    # билетов. Это окно задаёт только планировщик (planner.py) при сборе
    # билетов — за пределами окна скрапер/кэш ничего не вернёт. Внутри
    # оптимизатора выбор между кандидатами делает ТОЛЬКО выбранная метрика
    # (``money`` или ``time``). Раньше здесь был штраф ``_OVERSHOOT_RUB_PENALTY``
    # за выход за ``start + Σstays + surplus``: он заставлял медленный поезд
    # «внутри окна» выигрывать у быстрого самолёта «с перелётом», даже если
    # самолёт дешевле — потому что штраф прибавлялся к цене самолёта. Сейчас
    # этого штрафа нет: метрика решает честно.
    logger.debug(
        "Оптимизация: start=%s, surplus=%d дн., metric=%s",
        start_dt.isoformat(),
        surplus_days,
        metric,
    )

    # Состояние DP: (накопленная_стоимость, время_прилёта_последнего_плеча, путь)
    # Запас НЕ хранится в состоянии — он проверяется один раз в конце, по
    # дате прилёта последнего плеча. Это устраняет «съедание» запаса на
    # промежуточных лега́х.
    dp: list[tuple[int, datetime | None, list[TicketDTO]]] = [
        (0, None, [])
    ]

    for leg_idx, city_pair in enumerate(city_pairs):
        # Минимальный стой после предыдущего прилёта (если он был).
        min_stay_days = days_to_stay[leg_idx - 1] if leg_idx > 0 else 0
        dp_next: list[tuple[int, datetime, list[TicketDTO]]] = []
        # Пары (dep_dt, ticket) на этом плече, отсортированные по дате вылета
        # — нужны для фоллбэка «не влезает в окно, берём ближайший билет».
        sorted_leg_tickets: list[tuple[datetime, TicketDTO]] = []
        for ticket in tickets_by_pair.get(city_pair, []):
            dep_dt = _parse_dt(ticket.departure_time)
            if dep_dt is None:
                continue
            sorted_leg_tickets.append((dep_dt, ticket))
        sorted_leg_tickets.sort(key=lambda x: x[0])

        for acc_cost, prev_arrival, path in dp:
            min_dep = (
                start_dt
                if prev_arrival is None
                else prev_arrival + timedelta(days=min_stay_days) + _MIN_TRANSFER
            )

            leg_added = False
            for dep_dt, ticket in sorted_leg_tickets:
                arr_dt = _parse_dt(ticket.arrival_time)
                if arr_dt is None:
                    continue
                # Билет должен стартовать не раньше, чем нам реально можно
                # (с учётом предыдущего плеча и минимального стоя), и не
                # раньше даты старта поездки. Дальше поезд может ехать
                # сколько угодно — выбор между кандидатами делает метрика.
                if dep_dt < min_dep:
                    continue
                if dep_dt < start_dt:
                    continue

                cost_delta = (
                    ticket.price if metric == "money" else ticket.duration_minutes
                ) + _cross_term(ticket, metric)
                dp_next.append(
                    (acc_cost + cost_delta, arr_dt, path + [ticket])
                )
                leg_added = True

            if not leg_added and prev_arrival is not None:
                # Ни один билет не влезает в окно «не раньше min_dep» —
                # значит, предыдущее плечо прилетело слишком поздно
                # относительно доступных рейсов. Это типичный случай
                # «сумма дней поездки больше окна»: планировщик отдал
                # билеты, но следующего рейса в нужную дату просто нет.
                # Раньше мы возвращали None и обрывали маршрут. Теперь —
                # выбираем билет, чей dep ближе всего к min_dep (но не
                # раньше start_dt), и продолжаем. Стоимость дополняется
                # штрафом за «сдвиг», чтобы из двух одинаково «неидеальных»
                # путей выигрывал тот, где сдвиг меньше.
                best_pick: tuple[timedelta, datetime, TicketDTO] | None = None
                for dep_dt, ticket in sorted_leg_tickets:
                    arr_dt = _parse_dt(ticket.arrival_time)
                    if arr_dt is None or dep_dt < start_dt:
                        continue
                    # Сдвиг относительно min_dep. Может быть отрицательным
                    # (билет уходит раньше — ок, едем без полного стоя),
                    # положительным (билет позже нужного).
                    shift = dep_dt - min_dep
                    abs_shift = abs(shift)
                    if best_pick is None:
                        best_pick = (abs_shift, dep_dt, ticket)
                        continue
                    best_abs, best_dep, _ = best_pick
                    if abs_shift < best_abs or (
                        abs_shift == best_abs and dep_dt < best_dep
                    ):
                        best_pick = (abs_shift, dep_dt, ticket)

                if best_pick is not None:
                    abs_shift, dep_dt, ticket = best_pick
                    arr_dt = _parse_dt(ticket.arrival_time)
                    assert arr_dt is not None
                    cost_delta = (
                        ticket.price if metric == "money"
                        else ticket.duration_minutes
                    ) + _cross_term(ticket, metric)
                    # Штраф за сдвиг из окна: 3000₽/день (метрика money)
                    # или 60 мин/день (метрика time). Меньше основного
                    # исторического штрафа (5000), чтобы не «задушить»
                    # длинные маршруты, но и не позволить им быть
                    # равноценными идеально уложенным.
                    shift_penalty_days = (
                        abs_shift.days + (1 if abs_shift.seconds else 0)
                    )
                    if metric == "money":
                        cost_delta += shift_penalty_days * 3000
                    else:
                        cost_delta += shift_penalty_days * 60
                    dp_next.append(
                        (acc_cost + cost_delta, arr_dt, path + [ticket])
                    )
                    logger.info(
                        "Плечо %s→%s (leg %d): нет билета в окне — "
                        "взят ближайший к min_dep=%s (dep=%s, shift=%s).",
                        city_pair[0], city_pair[1], leg_idx,
                        min_dep.isoformat(), dep_dt.isoformat(), abs_shift,
                    )
                # Если best_pick остался None (например, все билеты уехали
                # до start_dt) — это состояние просто не получит
                # продолжения, что эквивалентно «нет вариантов для этого
                # плеча вообще».
                # Если и такого нет (например, все билеты уехали до start_dt)
                # — это состояние просто не получит продолжения, что
                # эквивалентно «нет вариантов для этого плеча вообще».

        if not dp_next:
            # Плечо не удалось «приклеить» ни из одного DP-состояния —
            # на этом плече нет ни одного билета, подходящего хотя бы
            # по нижней границе ``start_dt``. Возвращаем максимальный
            # префикс, который удалось собрать, а хвост заполняем None'ами.
            logger.warning(
                "Нет билетов для плеча %s→%s (leg %d из %d) — "
                "возвращаем префикс из %d плеч.",
                city_pair[0],
                city_pair[1],
                leg_idx,
                len(city_pairs),
                leg_idx,
            )
            best_prefix = min(
                dp, key=lambda s: (s[0] if s[1] is not None else float("inf"), s[1] or start_dt)
            )
            return list(best_prefix[2]) + [None] * (len(city_pairs) - leg_idx)

        dp = _prune(dp_next)

    if not dp:
        return [None] * len(city_pairs)

    # Финальный отбор: лучший по стоимости/длительности, среди равных —
    # самый ранний по дате прилёта (минимизируем растягивание поездки).
    best = min(dp, key=lambda s: (s[0], s[1]))
    return best[2]


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
    *,
    collect_all: bool = False,
) -> tuple[list[TicketDTO | None], list[str]] | tuple[
    list[TicketDTO | None], list[str], list[dict[str, Any]]
]:
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
        collect_all: Если ``True`` — дополнительно возвращает список всех
            проверенных перестановок (полное «дерево решений»). Каждый узел
            содержит ``sequence``, ``total_price``, ``total_duration_minutes``,
            ``is_complete`` (True, если все плечи найдены) и ``is_chosen``
            (True для лучшего маршрута). Используется для визуализации.

    Returns:
        ``(path, city_sequence)`` — список билетов и упорядоченный список городов.
        При ``collect_all=True`` возвращает кортеж из трёх элементов:
        ``(path, city_sequence, all_permutations)``.
    """
    best_cost: float = float("inf")
    best_path: list[TicketDTO | None] = []
    best_sequence: list[str] = []
    all_permutations: list[dict[str, Any]] = []

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

        is_complete = not any(t is None for t in path)
        total_price = sum(
            t.price for t in path if t is not None
        )
        total_duration = sum(
            t.duration_minutes for t in path if t is not None
        )

        if collect_all:
            all_permutations.append(
                {
                    "sequence": list(city_seq),
                    "total_price": total_price,
                    "total_duration_minutes": total_duration,
                    "is_complete": is_complete,
                    "is_chosen": False,  # проставляется ниже
                }
            )

        if not is_complete:
            continue  # неполный путь — пропускаем при выборе лучшего

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
        # Помечаем выбранный узел в дереве (по полному совпадению sequence).
        if collect_all:
            for node in all_permutations:
                if node["sequence"] == best_sequence:
                    node["is_chosen"] = True
        if collect_all:
            return best_path, best_sequence, all_permutations
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

    if collect_all:
        # Помечаем фоллбэк как выбранный, чтобы фронтенд не рендерил «нет ответа».
        fb_complete = not any(t is None for t in fallback_path)
        fb_price = sum(t.price for t in fallback_path if t is not None)
        fb_duration = sum(t.duration_minutes for t in fallback_path if t is not None)
        all_permutations.append(
            {
                "sequence": list(fallback_seq),
                "total_price": fb_price,
                "total_duration_minutes": fb_duration,
                "is_complete": fb_complete,
                "is_chosen": True,
            }
        )
        return fallback_path, fallback_seq, all_permutations
    return fallback_path, fallback_seq


# ---------------------------------------------------------------------------
# Вспомогательные функции
# ---------------------------------------------------------------------------

def _prune(
    states: list[tuple[int, datetime, list[TicketDTO]]],
) -> list[tuple[int, datetime, list[TicketDTO]]]:
    """Убирает доминируемые состояния с одинаковым последним билетом.

    Два состояния, заканчивающихся одним и тем же билетом (одинаковый ключ
    departure+arrival+price), не могут оба оказаться оптимальными: оставляем
    то, у которого меньше накопленная стоимость; при равной — более ранний
    прилёт (минимизируем растягивание поездки — это даёт следующим плечам
    максимальный запас до конца окна).
    """
    best: dict[tuple[Any, ...], tuple[int, datetime, list[TicketDTO]]] = {}
    for state in states:
        acc_cost, arr_dt, path = state
        last = path[-1]
        key = (last.departure_time, last.arrival_time, last.price)
        existing = best.get(key)
        if existing is None:
            best[key] = state
            continue
        ex_cost, ex_arr = existing[0], existing[1]
        if acc_cost < ex_cost or (acc_cost == ex_cost and arr_dt < ex_arr):
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
