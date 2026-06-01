"""Состояния конечного автомата диалога бота (Модуль 6, FSM).

Линейный сценарий сбора параметров маршрута. Порядок состояний в
:data:`STATE_ORDER` используется навигацией «Назад» для отката на один шаг.
"""

from __future__ import annotations

from aiogram.fsm.state import State, StatesGroup


class StateMachine(StatesGroup):
    """Шаги диалога подбора маршрута (строго по порядку)."""

    waiting_for_origin = State()       # город отправления
    waiting_for_destination = State()  # город назначения
    waiting_for_start_date = State()   # дата отправления
    waiting_for_surplus = State()      # запас (гибкость) дней
    waiting_for_transport = State()    # выбор транспорта
    waiting_for_filters = State()      # фильтры (зацикленное состояние)


#: Порядок шагов — основа навигации «Назад».
STATE_ORDER: list[State] = [
    StateMachine.waiting_for_origin,
    StateMachine.waiting_for_destination,
    StateMachine.waiting_for_start_date,
    StateMachine.waiting_for_surplus,
    StateMachine.waiting_for_transport,
    StateMachine.waiting_for_filters,
]


def previous_state(current: str | None) -> State | None:
    """Возвращает предыдущее состояние в линейном сценарии или ``None``.

    Args:
        current: Строковый идентификатор текущего состояния
            (``StateMachine:waiting_for_...``), как его отдаёт FSM.
    """
    for index, state in enumerate(STATE_ORDER):
        if state.state == current:
            return STATE_ORDER[index - 1] if index > 0 else None
    return None
