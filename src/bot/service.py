"""Сервисный слой бота — мост к оркестратору задач (Модуль 6).

:class:`TripOptimizerService` инкапсулирует работу с :class:`TaskOrchestrator`
(Модуль 4/7A): постановку задачи, ожидание результата и форматирование
итогового маршрута для Telegram. Хендлеры зависят от этого сервиса, а не от
внутренностей оркестратора (инверсия зависимостей).
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime
from typing import Any

from src.database import TaskStatus
from src.orchestration import TaskOrchestrator

logger = logging.getLogger(__name__)

_SOURCE_LABEL = {"aviasales": "✈ Aviasales", "rzd": "🚆 РЖД"}


class TripOptimizerService:
    """Прикладной фасад над оркестратором для Telegram-бота."""

    def __init__(
        self,
        orchestrator: TaskOrchestrator,
        *,
        poll_interval: float = 1.5,
        poll_timeout: float = 900.0,
    ) -> None:
        self._orch = orchestrator
        self._poll_interval = poll_interval
        self._poll_timeout = poll_timeout

    async def submit(self, user_inputs: dict[str, Any]) -> uuid.UUID:
        """Создаёт фоновую задачу подбора маршрута и возвращает её id."""
        return await self._orch.submit(user_inputs)

    async def wait_for_result(
        self, task_id: uuid.UUID
    ) -> tuple[str, dict[str, Any] | None]:
        """Активный цикл слежения за задачей до финального статуса.

        Returns:
            Кортеж ``(статус, результат)``. ``результат`` непуст только при
            статусе ``COMPLETED``; при ``FAILED``/``TIMEOUT`` — ``None``.
        """
        waited = 0.0
        while waited < self._poll_timeout:
            state = self._orch.get_progress(task_id)
            if state is not None:
                if state.status == TaskStatus.COMPLETED:
                    return "COMPLETED", state.result
                if state.status == TaskStatus.FAILED:
                    return "FAILED", None
            await asyncio.sleep(self._poll_interval)
            waited += self._poll_interval
        logger.warning("Задача %s не завершилась за %s c", task_id, self._poll_timeout)
        return "TIMEOUT", None

    # ------------------------------------------------------------------ #
    # Форматирование ответа
    # ------------------------------------------------------------------ #
    @staticmethod
    def format_itinerary(result: dict[str, Any]) -> str:
        """Формирует Markdown-сообщение с маршрутом и карточками билетов."""
        order = " → ".join(result.get("order", []))
        total_price = result.get("total_price", 0)
        total_minutes = result.get("total_duration_minutes", 0)
        metric = (
            "по времени"
            if result.get("optimization_metric") == "time"
            else "по деньгам"
        )

        lines = [
            f"🧭 *Ваш маршрут:* {order}",
            f"💰 *Итого:* {total_price} ₽ · ⏱ {TripOptimizerService._fmt_dur(total_minutes)}",
            f"⚙️ Оптимизация: {metric}",
            "",
        ]
        for index, leg in enumerate(result.get("legs", []), start=1):
            lines.append(TripOptimizerService._format_leg(index, leg))
        return "\n".join(lines)

    @staticmethod
    def _format_leg(index: int, leg: dict[str, Any] | None) -> str:
        """Карточка одного плеча: даты, станции, транспорт и ссылка на покупку."""
        if not leg:
            return f"*{index}.* Билеты не найдены для этого участка."

        source = _SOURCE_LABEL.get(leg["source"], leg["source"])
        baggage = "🧳 Багаж включён" if leg.get("has_baggage") else "🎒 Без багажа"
        url = leg.get("booking_url") or ""
        link = f"[Купить билет]({url})" if url else "ссылка недоступна"
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
        except ValueError:
            return iso

    @staticmethod
    def _fmt_dur(minutes: int) -> str:
        """Длительность в минутах → строка `Xч Yм`."""
        return f"{minutes // 60} ч {minutes % 60} мин"
