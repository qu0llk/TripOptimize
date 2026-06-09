"""Состояния конечного автомата диалога бота (Модуль 6, FSM).

Линейный сценарий сбора параметров маршрута, повторяющий контракт веб-формы
(:class:`src.schemas.SearchRequest`). Состояния сгруппированы по разделам:

* Маршрут: ``origin`` → ``destination`` → ``start_date`` → ``surplus``
* Промежуточные города: ``intermediate_count`` → ``intermediate_city`` × N
* Транспорт: ``transport`` (Любой / Самолёт / Поезд)
* Фильтры: ``filters_menu`` → ``baggage`` / ``budget`` / ``metric``
* Подтверждение: ``confirm`` → запуск задачи (``running`` отмечается вручную)

Линейная история :data:`STATE_ORDER` питает навигацию «Назад»; шаги ввода
промежуточных городов в историю не входят (откат возвращает к выбору
количества, а внутри подцикла работает своя навигация).
"""

from __future__ import annotations

from aiogram.fsm.state import State, StatesGroup


class StateMachine(StatesGroup):
    """Шаги диалога подбора маршрута (строго по порядку)."""

    # --- Маршрут ---------------------------------------------------------
    waiting_for_origin = State()       # город отправления
    waiting_for_destination = State()  # город назначения
    waiting_for_start_date = State()   # дата отправления
    waiting_for_surplus = State()      # запас (гибкость) дней

    # --- Промежуточные города --------------------------------------------
    waiting_for_intermediate_count = State()  # сколько (0..8) добавить
    waiting_for_intermediate_city = State()   # ввод очередного города

    # --- Транспорт -------------------------------------------------------
    waiting_for_transport = State()    # any / plane / train

    # --- Фильтры (зацикленное подменю) -----------------------------------
    waiting_for_filters_menu = State()  # экран-оглавление фильтров
    waiting_for_baggage = State()       # вкл/выкл
    waiting_for_budget = State()        # ввод бюджета (текст/кнопка)
    waiting_for_metric = State()        # money / time

    # --- Подтверждение и запуск ------------------------------------------
    waiting_for_confirm = State()       # карточка-сводка + кнопки
    running = State()                   # задача поставлена, ждём результат


#: Линейная «внешняя» история состояний — для навигации «Назад».
STATE_ORDER: list[State] = [
    StateMachine.waiting_for_origin,
    StateMachine.waiting_for_destination,
    StateMachine.waiting_for_start_date,
    StateMachine.waiting_for_surplus,
    StateMachine.waiting_for_intermediate_count,
    StateMachine.waiting_for_transport,
    StateMachine.waiting_for_filters_menu,
    StateMachine.waiting_for_baggage,
    StateMachine.waiting_for_budget,
    StateMachine.waiting_for_metric,
    StateMachine.waiting_for_confirm,
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


#: Максимальное число промежуточных городов — как в веб-форме (``MAX_CITIES``).
MAX_INTERMEDIATE_CITIES = 8
