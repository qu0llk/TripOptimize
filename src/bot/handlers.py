"""Хендлеры диалога Telegram-бота (Модуль 6).

Тонкий слой маршрутизации: разбор ввода, навигация по FSM и делегирование
бизнес-логики :class:`~src.bot.service.TripOptimizerService`. Презентация
(тексты/клавиатуры) вынесена в :mod:`src.bot.keyboards`.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import date
from typing import Any

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from src.bot import keyboards as kb
from src.bot.service import TripOptimizerService
from src.bot.states import StateMachine, previous_state
from src.bot.keyboards import view_for

logger = logging.getLogger(__name__)
router = Router(name="tripoptimizer-bot")


# ====================================================================== #
# Вспомогательное: отрисовка экрана состояния
# ====================================================================== #
async def _render(state: StateMachine, chat_id: int, bot: Bot, fsm: FSMContext) -> None:
    """Устанавливает состояние и отправляет соответствующий экран."""
    await fsm.set_state(state)
    data = await fsm.get_data()
    text, markup = view_for(state, data)
    await bot.send_message(chat_id, text, reply_markup=markup, parse_mode="Markdown")


async def _refresh_filters(callback: CallbackQuery, fsm: FSMContext) -> None:
    """Перерисовывает экран фильтров «на месте» после переключения тумблера."""
    data = await fsm.get_data()
    text, markup = view_for(StateMachine.waiting_for_filters, data)
    try:
        await callback.message.edit_text(
            text, reply_markup=markup, parse_mode="Markdown"
        )
    except Exception:  # noqa: BLE001 — «message is not modified» и т.п. безопасны
        pass


# ====================================================================== #
# Старт и навигация
# ====================================================================== #
@router.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext, bot: Bot) -> None:
    """Точка входа: сбрасывает контекст и запускает диалог с первого шага."""
    await state.clear()
    await message.answer(
        "👋 Привет! Я *TripOptimizer* — подберу оптимальный маршрут путешествия.",
        parse_mode="Markdown",
    )
    await _render(StateMachine.waiting_for_origin, message.chat.id, bot, state)


@router.callback_query(F.data == kb.CB_MENU)
async def on_menu(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    """«Главное меню»: полный сброс FSM и возврат к первому шагу."""
    await state.clear()
    await callback.answer("Сброшено")
    await _render(StateMachine.waiting_for_origin, callback.message.chat.id, bot, state)


@router.callback_query(F.data == kb.CB_BACK)
async def on_back(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    """«Назад»: откат на один шаг по линейной истории состояний."""
    current = await state.get_state()
    target = previous_state(current)
    await callback.answer()
    if target is not None:
        await _render(target, callback.message.chat.id, bot, state)


# ====================================================================== #
# Текстовые шаги: origin → destination → date → surplus
# ====================================================================== #
@router.message(StateMachine.waiting_for_origin)
async def on_origin(message: Message, state: FSMContext, bot: Bot) -> None:
    await state.update_data(origin_city=message.text.strip())
    await _render(StateMachine.waiting_for_destination, message.chat.id, bot, state)


@router.message(StateMachine.waiting_for_destination)
async def on_destination(message: Message, state: FSMContext, bot: Bot) -> None:
    await state.update_data(destination_city=message.text.strip())
    await _render(StateMachine.waiting_for_start_date, message.chat.id, bot, state)


@router.message(StateMachine.waiting_for_start_date)
async def on_date(message: Message, state: FSMContext, bot: Bot) -> None:
    raw = message.text.strip()
    try:
        date.fromisoformat(raw)
    except ValueError:
        await message.answer("⚠ Неверный формат. Введите дату как `ГГГГ-ММ-ДД`.",
                             parse_mode="Markdown")
        return
    await state.update_data(start_date=raw)
    await _render(StateMachine.waiting_for_surplus, message.chat.id, bot, state)


@router.message(StateMachine.waiting_for_surplus)
async def on_surplus(message: Message, state: FSMContext, bot: Bot) -> None:
    raw = message.text.strip()
    if not raw.isdigit():
        await message.answer("⚠ Введите целое число дней (≥ 0).")
        return
    await state.update_data(surplus_days=int(raw))
    await _render(StateMachine.waiting_for_transport, message.chat.id, bot, state)


# ====================================================================== #
# Выбор транспорта
# ====================================================================== #
@router.callback_query(
    StateMachine.waiting_for_transport,
    F.data.in_({kb.CB_TRANSPORT_PLANE, kb.CB_TRANSPORT_TRAIN}),
)
async def on_transport(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    transport = "plane" if callback.data == kb.CB_TRANSPORT_PLANE else "train"
    await state.update_data(transport_type=transport)
    await callback.answer()
    await _render(StateMachine.waiting_for_filters, callback.message.chat.id, bot, state)


# ====================================================================== #
# Экран фильтров (зацикленный): тумблеры и запуск оптимизации
# ====================================================================== #
@router.callback_query(StateMachine.waiting_for_filters, F.data == kb.CB_TOGGLE_BAGGAGE)
async def on_toggle_baggage(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    await state.update_data(require_baggage=not data.get("require_baggage", False))
    await callback.answer()
    await _refresh_filters(callback, state)


@router.callback_query(StateMachine.waiting_for_filters, F.data == kb.CB_TOGGLE_METRIC)
async def on_toggle_metric(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    new_metric = "time" if data.get("optimization_metric", "money") == "money" else "money"
    await state.update_data(optimization_metric=new_metric)
    await callback.answer()
    await _refresh_filters(callback, state)


@router.callback_query(StateMachine.waiting_for_filters, F.data == kb.CB_OPTIMIZE)
async def on_optimize(
    callback: CallbackQuery,
    state: FSMContext,
    bot: Bot,
    service: TripOptimizerService,
) -> None:
    """Собирает данные, ставит задачу и запускает фоновое слежение."""
    data = await state.get_data()
    payload = _build_payload(data)
    await callback.answer()

    task_id = await service.submit(payload)
    await callback.message.answer(
        "✅ Мы сообщим, когда оптимизируем ваш маршрут… Это может занять минуту."
    )
    # Фоновый цикл слежения за задачей — не блокирует обработку других апдейтов.
    asyncio.create_task(
        _report_when_ready(bot, callback.message.chat.id, service, task_id)
    )
    await state.clear()


def _build_payload(data: dict[str, Any]) -> dict[str, Any]:
    """Преобразует память диалога в полезную нагрузку под Pydantic-схему API."""
    return {
        "origin_city": data.get("origin_city", ""),
        "destination_city": data.get("destination_city", ""),
        "start_date": data.get("start_date", ""),
        "surplus_days": data.get("surplus_days", 0),
        "filters": {
            "transport_type": data.get("transport_type", "both"),
            "require_baggage": data.get("require_baggage", False),
            "max_budget": None,
            "optimization_metric": data.get("optimization_metric", "money"),
        },
        "intermediate_cities": [],
    }


async def _report_when_ready(
    bot: Bot, chat_id: int, service: TripOptimizerService, task_id: uuid.UUID
) -> None:
    """Дожидается завершения задачи и присылает итоговый маршрут или ошибку."""
    status, result = await service.wait_for_result(task_id)
    if status == "COMPLETED" and result:
        await bot.send_message(
            chat_id,
            service.format_itinerary(result),
            parse_mode="Markdown",
            disable_web_page_preview=True,
        )
    else:
        await bot.send_message(
            chat_id,
            "😔 Не удалось оптимизировать маршрут. Попробуйте ещё раз — /start.",
        )
