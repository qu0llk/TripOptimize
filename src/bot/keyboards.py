"""Сборка инлайн-клавиатур и текстов состояний бота (Модуль 6).

Здесь сосредоточена презентационная логика: какой текст и какая клавиатура
соответствуют каждому шагу диалога. Хендлеры остаются тонкими и лишь вызывают
:func:`view_for` (принцип единственной ответственности).
"""

from __future__ import annotations

from typing import Any

from aiogram.fsm.state import State
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from src.bot.states import StateMachine

# --- Callback-данные (компактные префиксы) --------------------------------
CB_BACK = "nav:back"
CB_MENU = "nav:menu"
CB_TRANSPORT_PLANE = "tr:plane"
CB_TRANSPORT_TRAIN = "tr:train"
CB_TOGGLE_BAGGAGE = "flt:baggage"
CB_TOGGLE_METRIC = "flt:metric"
CB_OPTIMIZE = "flt:go"


def _nav_row(include_back: bool) -> list[InlineKeyboardButton]:
    """Ряд навигации: «Назад» (опционально) + «Главное меню»."""
    row: list[InlineKeyboardButton] = []
    if include_back:
        row.append(InlineKeyboardButton(text="⬅️ Назад", callback_data=CB_BACK))
    row.append(InlineKeyboardButton(text="🏠 Главное меню", callback_data=CB_MENU))
    return row


def _baggage_label(data: dict[str, Any]) -> str:
    """Текст переключателя багажа по текущему состоянию памяти."""
    return "🧳 Багаж включен" if data.get("require_baggage") else "🎒 Без багажа"


def _metric_label(data: dict[str, Any]) -> str:
    """Текст переключателя метрики оптимизации (по умолчанию — деньги)."""
    metric = data.get("optimization_metric", "money")
    return (
        "⏱️ Оптимизация по времени"
        if metric == "time"
        else "💰 Оптимизация по деньгам"
    )


def _filters_summary(data: dict[str, Any]) -> str:
    """Сводка собранных параметров для экрана фильтров."""
    transport = {"plane": "Самолёт ✈", "train": "Поезд 🚆"}.get(
        data.get("transport_type", ""), "—"
    )
    return (
        "*Проверьте параметры маршрута:*\n"
        f"• Откуда: {data.get('origin_city', '—')}\n"
        f"• Куда: {data.get('destination_city', '—')}\n"
        f"• Дата: {data.get('start_date', '—')}\n"
        f"• Запас дней: {data.get('surplus_days', 0)}\n"
        f"• Транспорт: {transport}\n\n"
        "Настройте фильтры кнопками ниже и запустите оптимизацию."
    )


def view_for(state: State, data: dict[str, Any]) -> tuple[str, InlineKeyboardMarkup]:
    """Возвращает текст и клавиатуру для указанного шага диалога.

    Args:
        state: Состояние FSM, для которого строится экран.
        data: Текущая память диалога (используется на экране фильтров).
    """
    if state == StateMachine.waiting_for_origin:
        return (
            "🌍 Введите *город отправления* (Откуда):",
            InlineKeyboardMarkup(inline_keyboard=[_nav_row(include_back=False)]),
        )

    if state == StateMachine.waiting_for_destination:
        return (
            "🎯 Введите *город назначения* (Куда):",
            InlineKeyboardMarkup(inline_keyboard=[_nav_row(include_back=True)]),
        )

    if state == StateMachine.waiting_for_start_date:
        return (
            "📅 Введите *дату отправления* в формате `ГГГГ-ММ-ДД`:",
            InlineKeyboardMarkup(inline_keyboard=[_nav_row(include_back=True)]),
        )

    if state == StateMachine.waiting_for_surplus:
        return (
            "🗓️ Введите *запас дней* (целое число ≥ 0):",
            InlineKeyboardMarkup(inline_keyboard=[_nav_row(include_back=True)]),
        )

    if state == StateMachine.waiting_for_transport:
        return (
            "🚉 Выберите *тип транспорта*:",
            InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="✈ Самолёт", callback_data=CB_TRANSPORT_PLANE
                        ),
                        InlineKeyboardButton(
                            text="🚆 Поезд", callback_data=CB_TRANSPORT_TRAIN
                        ),
                    ],
                    _nav_row(include_back=True),
                ]
            ),
        )

    # waiting_for_filters — зацикленный экран фильтров с тремя переключателями.
    return (
        _filters_summary(data),
        InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=_baggage_label(data), callback_data=CB_TOGGLE_BAGGAGE
                    )
                ],
                [
                    InlineKeyboardButton(
                        text=_metric_label(data), callback_data=CB_TOGGLE_METRIC
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="🚀 Оптимизировать маршрут!", callback_data=CB_OPTIMIZE
                    )
                ],
                _nav_row(include_back=True),
            ]
        ),
    )
