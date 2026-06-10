"""Хендлеры диалога Telegram-бота (Модуль 6).

Тонкий слой маршрутизации: разбор ввода, навигация по FSM и делегирование
бизнес-логики :class:`~src.bot.service.TripOptimizerService`. Презентация
(тексты/клавиатуры) вынесена в :mod:`src.bot.keyboards`.

Сценарий повторяет контракт веб-формы (см. ``buildPayload`` в ``static/main.js``):

    origin → destination → start_date → surplus
        → промежуточные города (count → name → days × N)
        → transport (any / plane / train)
        → фильтры (baggage → budget → metric)
        → confirm → submit
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
from src.bot.keyboards import view_for, view_intermediate_days
from src.bot.service import TripOptimizerService
from src.bot.states import MAX_INTERMEDIATE_CITIES, StateMachine, previous_state

logger = logging.getLogger(__name__)
router = Router(name="tripoptimizer-bot")


# ====================================================================== #
# Утилиты отрисовки и хранения промежуточных городов
# ====================================================================== #
async def _render(state: StateMachine, chat_id: int, bot: Bot, fsm: FSMContext) -> None:
    """Устанавливает состояние и отправляет соответствующий экран."""
    await fsm.set_state(state)
    data = await fsm.get_data()
    text, markup = view_for(state, data)
    await bot.send_message(chat_id, text, reply_markup=markup, parse_mode="Markdown")


async def _refresh_in_place(
    callback: CallbackQuery, state: StateMachine, fsm: FSMContext
) -> None:
    """Перерисовывает текущий экран «на месте» (после тумблера)."""
    data = await fsm.get_data()
    await fsm.set_state(state)
    text, markup = view_for(state, data)
    try:
        await callback.message.edit_text(
            text, reply_markup=markup, parse_mode="Markdown"
        )
    except Exception:  # noqa: BLE001 — «message is not modified» и т.п. безопасны
        pass


def _get_intermediate(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Безопасно достаёт список промежуточных городов из памяти диалога."""
    raw = data.get("intermediate_cities") or []
    return [dict(item) for item in raw]


# ====================================================================== #
# Старт и навигация
# ====================================================================== #
@router.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext, bot: Bot) -> None:
    """Точка входа: сбрасывает контекст и запускает диалог с первого шага."""
    await state.clear()
    await message.answer(
        "👋 Привет! Я *TripOptimizer* — подберу оптимальный маршрут путешествия.\n\n"
        "Помогу найти маршрут через несколько городов — дёшево или быстро, "
        "с выбором транспорта и фильтрами.\n\n"
        "В любой момент можно вернуться к предыдущему шагу кнопкой ⬅️ Назад "
        "или начать заново через 🏠 Главное меню.",
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
    value = (message.text or "").strip()
    if not value:
        await message.answer("⚠ Введите непустое название города.")
        return
    await state.update_data(origin_city=value)
    await _render(StateMachine.waiting_for_destination, message.chat.id, bot, state)


@router.message(StateMachine.waiting_for_destination)
async def on_destination(message: Message, state: FSMContext, bot: Bot) -> None:
    value = (message.text or "").strip()
    if not value:
        await message.answer("⚠ Введите непустое название города.")
        return
    await state.update_data(destination_city=value)
    await _render(StateMachine.waiting_for_start_date, message.chat.id, bot, state)


@router.message(StateMachine.waiting_for_start_date)
async def on_date(message: Message, state: FSMContext, bot: Bot) -> None:
    raw = (message.text or "").strip()
    try:
        date.fromisoformat(raw)
    except ValueError:
        await message.answer(
            "⚠ Неверный формат. Введите дату как `ГГГГ-ММ-ДД` "
            "(например, 2026-07-15).",
            parse_mode="Markdown",
        )
        return
    await state.update_data(start_date=raw)
    await _render(StateMachine.waiting_for_surplus, message.chat.id, bot, state)


@router.message(StateMachine.waiting_for_surplus)
async def on_surplus(message: Message, state: FSMContext, bot: Bot) -> None:
    raw = (message.text or "").strip()
    if not raw.isdigit():
        await message.answer("⚠ Введите целое число дней (≥ 0).")
        return
    await state.update_data(surplus_days=int(raw))
    await _render(
        StateMachine.waiting_for_intermediate_count, message.chat.id, bot, state
    )


# ====================================================================== #
# Промежуточные города
# ====================================================================== #
@router.callback_query(
    StateMachine.waiting_for_intermediate_count,
    F.data.in_({kb.CB_NO_INTERMEDIATE, *[f"{kb.CB_INTERMEDIATE_COUNT_PREFIX}{n}"
                                            for n in range(1, MAX_INTERMEDIATE_CITIES + 1)]}),
)
async def on_intermediate_count(
    callback: CallbackQuery, state: FSMContext, bot: Bot
) -> None:
    """Зафиксировали количество промежуточных городов (или «без них»)."""
    if callback.data == kb.CB_NO_INTERMEDIATE:
        target = 0
    else:
        target = int(callback.data.split(":")[-1])
    await state.update_data(
        _intermediate_target=target,
        intermediate_cities=[],
    )
    await callback.answer()
    if target == 0:
        await _render(
            StateMachine.waiting_for_transport, callback.message.chat.id, bot, state
        )
        return
    await _render(
        StateMachine.waiting_for_intermediate_city, callback.message.chat.id, bot, state
    )


@router.message(StateMachine.waiting_for_intermediate_city)
async def on_intermediate_city(message: Message, state: FSMContext, bot: Bot) -> None:
    """Название очередного промежуточного города. Переходим к вводу дней."""
    value = (message.text or "").strip()
    if not value:
        await message.answer("⚠ Введите непустое название города.")
        return
    data = await state.get_data()
    items = _get_intermediate(data)
    items.append({"city": value, "days_to_stay": 0, "_pending_days": True})
    await state.update_data(intermediate_cities=items)
    target = int(data.get("_intermediate_target", 0))
    idx = len(items)
    text, markup = view_intermediate_days(
        await state.get_data(), city=value, idx=idx, target=target
    )
    # Переходим в подцикл дней, иначе следующее текстовое сообщение
    # («3», «нет» и т.п.) будет воспринято как имя нового города.
    await state.set_state(StateMachine.waiting_for_intermediate_days)
    await message.answer(text, reply_markup=markup, parse_mode="Markdown")


@router.callback_query(
    StateMachine.waiting_for_intermediate_days,
    F.data.startswith(kb.CB_INTERMEDIATE_DONE),
)
async def on_intermediate_done(callback: CallbackQuery, state: FSMContext) -> None:
    """Зафиксировали дни для только что названного города."""
    parts = callback.data.split(":")
    try:
        days = int(parts[-1])
    except ValueError:
        days = 0
    data = await state.get_data()
    items = _get_intermediate(data)
    target = int(data.get("_intermediate_target", 0))
    if not items:
        await callback.answer("Нечего сохранять.")
        return
    items[-1]["days_to_stay"] = days
    items[-1].pop("_pending_days", None)
    await state.update_data(intermediate_cities=items)
    await callback.answer(f"Сохранено: {days} дн.")
    if len(items) >= target:
        # Все города введены — переходим к выбору транспорта.
        await state.set_state(StateMachine.waiting_for_transport)
        text, markup = view_for(StateMachine.waiting_for_transport, data)
        await callback.message.edit_text(text, reply_markup=markup, parse_mode="Markdown")
    else:
        # Продолжаем ввод следующего города.
        await state.set_state(StateMachine.waiting_for_intermediate_city)
        text, markup = view_for(
            StateMachine.waiting_for_intermediate_city,
            {**data, "intermediate_cities": items},
        )
        await callback.message.edit_text(text, reply_markup=markup, parse_mode="Markdown")


@router.message(StateMachine.waiting_for_intermediate_days)
async def on_intermediate_days_text(
    message: Message, state: FSMContext, bot: Bot
) -> None:
    """Текстовый ввод дней (на случай «своей» суммы) — для подцикла дней.

    Подцикл дней открыт только в :func:`on_intermediate_city`, где сразу
    после имени бот переключается в :class:`StateMachine.waiting_for_intermediate_days`
    и показывает клавиатуру с «⏭ Пропустить». На случай ручного ввода
    обрабатываем и текст здесь.
    """
    raw = (message.text or "").strip()
    if not raw.isdigit():
        await message.answer("⚠ Введите целое число дней (≥ 0) или нажмите кнопку.")
        return
    days = int(raw)
    data = await state.get_data()
    items = _get_intermediate(data)
    target = int(data.get("_intermediate_target", 0))
    if not items:
        return
    items[-1]["days_to_stay"] = days
    items[-1].pop("_pending_days", None)
    await state.update_data(intermediate_cities=items)
    if len(items) >= target:
        await _render(
            StateMachine.waiting_for_transport, message.chat.id, bot, state
        )
    else:
        await _render(
            StateMachine.waiting_for_intermediate_city, message.chat.id, bot, state
        )


# ====================================================================== #
# Выбор транспорта
# ====================================================================== #
@router.callback_query(
    StateMachine.waiting_for_transport,
    F.data.in_({kb.CB_TRANSPORT_ANY, kb.CB_TRANSPORT_PLANE, kb.CB_TRANSPORT_TRAIN}),
)
async def on_transport(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    mapping = {
        kb.CB_TRANSPORT_ANY: "any",
        kb.CB_TRANSPORT_PLANE: "plane",
        kb.CB_TRANSPORT_TRAIN: "train",
    }
    transport = mapping[callback.data]
    await state.update_data(transport_type=transport)
    await callback.answer()
    await _render(
        StateMachine.waiting_for_filters_menu, callback.message.chat.id, bot, state
    )


# ====================================================================== #
# Фильтры: багаж
# ====================================================================== #
@router.callback_query(
    StateMachine.waiting_for_filters_menu, F.data == kb.CB_FILTERS_OPEN_BAGGAGE
)
async def on_open_baggage(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await _refresh_in_place(callback, StateMachine.waiting_for_baggage, state)


@router.callback_query(
    StateMachine.waiting_for_baggage, F.data.in_({kb.CB_BAGGAGE_ON, kb.CB_BAGGAGE_OFF})
)
async def on_baggage(callback: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(require_baggage=(callback.data == kb.CB_BAGGAGE_ON))
    await callback.answer("Сохранено")
    await _refresh_in_place(callback, StateMachine.waiting_for_filters_menu, state)


# ====================================================================== #
# Фильтры: бюджет
# ====================================================================== #
@router.callback_query(
    StateMachine.waiting_for_filters_menu, F.data == kb.CB_FILTERS_OPEN_BUDGET
)
async def on_open_budget(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await _refresh_in_place(callback, StateMachine.waiting_for_budget, state)


@router.callback_query(
    StateMachine.waiting_for_budget,
    F.data.startswith(kb.CB_BUDGET_SET_PREFIX),
)
async def on_budget_preset(callback: CallbackQuery, state: FSMContext) -> None:
    value = int(callback.data.split(":")[-1])
    await state.update_data(max_budget=value)
    await callback.answer(f"Бюджет: {value} ₽")
    await _refresh_in_place(callback, StateMachine.waiting_for_filters_menu, state)


@router.callback_query(
    StateMachine.waiting_for_budget, F.data == kb.CB_BUDGET_SKIP
)
async def on_budget_skip(callback: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(max_budget=None)
    await callback.answer("Без лимита")
    await _refresh_in_place(callback, StateMachine.waiting_for_filters_menu, state)


@router.callback_query(
    StateMachine.waiting_for_budget, F.data == kb.CB_BUDGET_CUSTOM
)
async def on_budget_custom(callback: CallbackQuery, state: FSMContext) -> None:
    """«Своя сумма» — переключаем на текстовый ввод (остаёмся в том же состоянии)."""
    await callback.answer()
    await callback.message.answer(
        "Введите *максимальный бюджет* в рублях (целое число ≥ 0), "
        "либо отправьте «нет» для снятия лимита:",
        parse_mode="Markdown",
    )


@router.message(StateMachine.waiting_for_budget)
async def on_budget_text(message: Message, state: FSMContext, bot: Bot) -> None:
    raw = (message.text or "").strip().lower()
    if raw in {"нет", "no", "∞", "inf", "бесконечно", "-", "0"}:
        await state.update_data(max_budget=None)
        await _render(
            StateMachine.waiting_for_filters_menu, message.chat.id, bot, state
        )
        return
    if not raw.isdigit():
        await message.answer("⚠ Введите целое число или «нет» для снятия лимита.")
        return
    await state.update_data(max_budget=int(raw))
    await _render(
        StateMachine.waiting_for_filters_menu, message.chat.id, bot, state
    )


# ====================================================================== #
# Фильтры: метрика
# ====================================================================== #
@router.callback_query(
    StateMachine.waiting_for_filters_menu, F.data == kb.CB_FILTERS_OPEN_METRIC
)
async def on_open_metric(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await _refresh_in_place(callback, StateMachine.waiting_for_metric, state)


@router.callback_query(
    StateMachine.waiting_for_metric,
    F.data.in_({kb.CB_METRIC_MONEY, kb.CB_METRIC_TIME}),
)
async def on_metric(callback: CallbackQuery, state: FSMContext) -> None:
    metric = "time" if callback.data == kb.CB_METRIC_TIME else "money"
    await state.update_data(optimization_metric=metric)
    await callback.answer()
    await _refresh_in_place(callback, StateMachine.waiting_for_filters_menu, state)


# ====================================================================== #
# Переход к подтверждению
# ====================================================================== #
@router.callback_query(
    StateMachine.waiting_for_filters_menu, F.data == kb.CB_FILTERS_DONE
)
async def on_filters_done(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    await callback.answer()
    await _render(StateMachine.waiting_for_confirm, callback.message.chat.id, bot, state)


# ====================================================================== #
# Подтверждение и запуск
# ====================================================================== #
@router.callback_query(
    StateMachine.waiting_for_confirm, F.data == kb.CB_CONFIRM_EDIT
)
async def on_confirm_edit(callback: CallbackQuery, state: FSMContext) -> None:
    """«Изменить параметры» — откатываемся в меню фильтров."""
    await callback.answer()
    await _refresh_in_place(callback, StateMachine.waiting_for_filters_menu, state)


@router.callback_query(
    StateMachine.waiting_for_confirm, F.data == kb.CB_CONFIRM_RUN
)
async def on_confirm_run(
    callback: CallbackQuery,
    state: FSMContext,
    bot: Bot,
    service: TripOptimizerService,
) -> None:
    """Финальный запуск: собираем payload, отдаём оркестратору, следим за прогрессом."""
    data = await state.get_data()
    payload = _build_payload(data)
    await state.set_state(StateMachine.running)
    await callback.answer()
    status_message = await callback.message.answer(
        "⏳ Запускаю оптимизацию…", parse_mode="Markdown"
    )
    task_id = await service.submit(payload)
    asyncio.create_task(
        _report_when_ready(bot, callback.message.chat.id, status_message.message_id,
                            service, task_id)
    )
    await state.clear()


# ====================================================================== #
# Сбор полезной нагрузки (зеркало buildPayload из static/main.js)
# ====================================================================== #
def _build_payload(data: dict[str, Any]) -> dict[str, Any]:
    """Преобразует память диалога в полезную нагрузку под Pydantic-схему API."""
    transport = data.get("transport_type", "any")
    if transport == "any":
        transport = "both"
    return {
        "origin_city": data.get("origin_city", ""),
        "destination_city": data.get("destination_city", ""),
        "start_date": data.get("start_date", ""),
        "surplus_days": int(data.get("surplus_days", 0) or 0),
        "filters": {
            "transport_type": transport,
            "require_baggage": bool(data.get("require_baggage", False)),
            "max_budget": data.get("max_budget"),
            "optimization_metric": data.get("optimization_metric", "money"),
        },
        "intermediate_cities": [
            {"city": item["city"], "days_to_stay": int(item.get("days_to_stay", 0) or 0)}
            for item in (data.get("intermediate_cities") or [])
            if item.get("city")
        ],
    }


# ====================================================================== #
# Фоновое слежение за задачей
# ====================================================================== #
async def _report_when_ready(
    bot: Bot,
    chat_id: int,
    status_message_id: int,
    service: TripOptimizerService,
    task_id: uuid.UUID,
) -> None:
    """Следит за прогрессом задачи и шлёт обновления + финальный маршрут."""
    last_pct = -1
    while True:
        progress = service.get_progress(task_id)
        if progress is None:
            await asyncio.sleep(0.5)
            continue
        pct = progress.percentage
        if pct != last_pct:
            last_pct = pct
            try:
                await bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=status_message_id,
                    text=(
                        f"⏳ Парсинг билетов: *{pct}%*\n"
                        f"Собрано плеч: {progress.completed_legs} / {progress.total_legs}\n"
                        f"Статус: `{progress.status.value}`"
                    ),
                    parse_mode="Markdown",
                )
            except Exception:  # noqa: BLE001
                pass

        if progress.status.value in {"COMPLETED", "FAILED"}:
            break
        await asyncio.sleep(1.0)

    if progress.status.value == "COMPLETED" and progress.result:
        text = service.format_itinerary(progress.result)
        try:
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=status_message_id,
                text=text,
                parse_mode="Markdown",
                disable_web_page_preview=True,
            )
        except Exception:  # noqa: BLE001
            await bot.send_message(
                chat_id, text, parse_mode="Markdown", disable_web_page_preview=True
            )
    else:
        try:
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=status_message_id,
                text="😔 Не удалось оптимизировать маршрут. Попробуйте ещё раз — /start.",
            )
        except Exception:  # noqa: BLE001
            await bot.send_message(
                chat_id, "😔 Не удалось оптимизировать маршрут. Попробуйте ещё раз — /start."
            )
