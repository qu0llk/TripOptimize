"""Сервисный слой бота — мост к оркестратору задач (Модуль 6).

:class:`TripOptimizerService` инкапсулирует работу с :class:`TaskOrchestrator`
(Модуль 4/7A): постановку задачи, ожидание результата и форматирование
итогового маршрута для Telegram. Хендлеры зависят от этого сервиса, а не от
внутренностей оркестратора (инверсия зависимостей).
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Any

from src.orchestration import TaskOrchestrator
from src.orchestration.orchestrator import ProgressState

logger = logging.getLogger(__name__)

_SOURCE_LABEL = {"aviasales": "✈ Aviasales", "rzd": "🚆 РЖД"}
_TRANSPORT_LABEL = {"plane": "Самолёт ✈", "train": "Поезд 🚆", "both": "Любой 🚆✈"}


class TripOptimizerService:
    """Прикладной фасад над оркестратором для Telegram-бота."""

    def __init__(self, orchestrator: TaskOrchestrator) -> None:
        self._orch = orchestrator

    # ------------------------------------------------------------------ #
    # Делегирование оркестратору
    # ------------------------------------------------------------------ #
    async def submit(self, user_inputs: dict[str, Any]) -> uuid.UUID:
        """Создаёт фоновую задачу подбора маршрута и возвращает её id."""
        return await self._orch.submit(user_inputs)

    def get_progress(self, task_id: uuid.UUID) -> ProgressState | None:
        """Возвращает текущий снимок прогресса (или ``None``)."""
        return self._orch.get_progress(task_id)

    # ------------------------------------------------------------------ #
    # Форматирование ответа
    # ------------------------------------------------------------------ #
    @staticmethod
    def format_itinerary(result: dict[str, Any]) -> str:
        """Формирует Markdown-сообщение с маршрутом и карточками билетов.

        Структура повторяет карточки из ``static/main.js``:

        * Сводка: порядок городов, итог, метрика, активные фильтры.
        * Карточка на каждое плечо: транспорт, время, цена, багаж, ссылка.
        * Пустые плечи (``__empty__``) — отдельным блоком с причиной.
        * ``global_reason`` — общая причина провала (если есть).
        """
        order_list: list[str] = list(result.get("order", []) or [])
        order_str = " → ".join(order_list) if order_list else "—"
        total_price = result.get("total_price", 0)
        total_minutes = result.get("total_duration_minutes", 0)
        metric = "по времени" if result.get("optimization_metric") == "time" else "по деньгам"

        legs = result.get("legs", []) or []
        empty_legs = [
            (idx, leg) for idx, leg in enumerate(legs, start=1)
            if isinstance(leg, dict) and leg.get("__empty__")
        ]
        has_partial_fail = bool(empty_legs) and len(empty_legs) < len(legs)

        lines: list[str] = []
        lines.append(f"🧭 *Ваш маршрут:* {order_str}")
        lines.append(
            f"💰 *Итого:* {total_price} ₽ · ⏱ {TripOptimizerService._fmt_dur(total_minutes)}"
        )
        lines.append(f"⚙️ Оптимизация: {metric}")

        # Глобальная причина провала — отдельным абзацем, чтобы пользователь
        # сразу видел, что дело не в одной паре городов.
        global_reason = result.get("global_reason")
        if global_reason:
            lines.append("")
            lines.append(f"⚠️ _{global_reason}_")

        if has_partial_fail:
            lines.append("")
            lines.append(
                f"ℹ️ Найдены билеты на {len(legs) - len(empty_legs)} из {len(legs)} плеч. "
                "Пустые участки см. ниже."
            )

        lines.append("")
        for index, leg in enumerate(legs, start=1):
            lines.append(TripOptimizerService._format_leg(index, leg, order_list))

        return "\n".join(lines)

    # ------------------------------------------------------------------ #
    # Внутренние хелперы форматирования
    # ------------------------------------------------------------------ #
    @staticmethod
    def _format_leg(index: int, leg: dict[str, Any] | None, order: list[str] | str) -> str:
        """Карточка одного плеча: пустое/нормальное."""
        if not leg:
            return f"*{index}.* Билеты не найдены для этого участка."

        if leg.get("__empty__"):
            # Восстановим человекочитаемые концы плеча из ``order``.
            cities = order if isinstance(order, list) else []
            from_city = cities[index - 1] if index - 1 < len(cities) else "—"
            to_city = cities[index] if index < len(cities) else "—"
            reason = leg.get("reason") or "Билеты не найдены."
            return (
                f"*{index}. {from_city} → {to_city}*\n"
                f"⚠️ _{reason}_\n"
            )

        source = _SOURCE_LABEL.get(leg.get("source", ""), leg.get("source", "—"))
        baggage = "🧳 Багаж включён" if leg.get("has_baggage") else "🎒 Без багажа"
        url = leg.get("booking_url") or ""
        if url:
            # Telegram-ссылки в Markdown обязаны быть в одном «куске» текста —
            # иначе парсер ломается на переносах строк внутри скобок.
            link = f"[Купить билет →]({url})"
        else:
            link = "ссылка недоступна"
        return (
            f"*{index}. {leg['departure_city']} → {leg['arrival_city']}*\n"
            f"{source}\n"
            f"🕓 {TripOptimizerService._fmt_time(leg['departure_time'])} → "
            f"{TripOptimizerService._fmt_time(leg['arrival_time'])} "
            f"({TripOptimizerService._fmt_dur(leg['duration_minutes'])})\n"
            f"💰 *{leg['price']} ₽* · {baggage}\n"
            f"➡️ {link}\n"
        )

    @staticmethod
    def _fmt_time(iso: str) -> str:
        """Время из ISO-строки в формат `ДД.ММ ЧЧ:ММ`."""
        try:
            return datetime.fromisoformat(iso).strftime("%d.%m %H:%M")
        except (ValueError, TypeError):
            return str(iso)

    @staticmethod
    def _fmt_dur(minutes: int) -> str:
        """Длительность в минутах → строка `Xч Yм`."""
        minutes = int(minutes or 0)
        return f"{minutes // 60} ч {minutes % 60} мин"


#: Обратная совместимость: старый API ``wait_for_result`` оставлен на случай,
#: если кто-то вызывает бота вне :mod:`src.bot.handlers`.
async def wait_for_result_compat(  # pragma: no cover
    service: TripOptimizerService,
    task_id: uuid.UUID,
    *,
    poll_interval: float = 1.5,
    poll_timeout: float = 900.0,
) -> tuple[str, dict[str, Any] | None]:
    import asyncio

    from src.database import TaskStatus

    waited = 0.0
    while waited < poll_timeout:
        state = service.get_progress(task_id)
        if state is not None:
            if state.status == TaskStatus.COMPLETED:
                return "COMPLETED", state.result
            if state.status == TaskStatus.FAILED:
                return "FAILED", None
        await asyncio.sleep(poll_interval)
        waited += poll_interval
    return "TIMEOUT", None
