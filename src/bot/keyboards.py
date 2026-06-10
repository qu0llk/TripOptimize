"""Сборка инлайн-клавиатур и текстов состояний бота (Модуль 6).

Здесь сосредоточена презентационная логика: какой текст и какая клавиатура
соответствуют каждому шагу диалога. Хендлеры остаются тонкими и лишь вызывают
:func:`view_for` (принцип единственной ответственности).
"""

from __future__ import annotations

from typing import Any

from aiogram.fsm.state import State
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from src.bot.states import MAX_INTERMEDIATE_CITIES, StateMachine

# --- Callback-данные (компактные префиксы) --------------------------------
CB_BACK = "nav:back"
CB_MENU = "nav:menu"

# Промежуточные города
CB_NO_INTERMEDIATE = "ic:none"
CB_INTERMEDIATE_COUNT_PREFIX = "ic:cnt:"  # +N
CB_INTERMEDIATE_REMOVE_LAST = "ic:undo"
CB_INTERMEDIATE_DONE = "ic:done"

# Транспорт
CB_TRANSPORT_ANY = "tr:any"
CB_TRANSPORT_PLANE = "tr:plane"
CB_TRANSPORT_TRAIN = "tr:train"

# Фильтры (вход/навигация)
CB_FILTERS_OPEN_BAGGAGE = "flt:bag"
CB_FILTERS_OPEN_BUDGET = "flt:bgt"
CB_FILTERS_OPEN_METRIC = "flt:met"
CB_FILTERS_DONE = "flt:done"  # «Готово — перейти к запуску»

# Фильтры (выбор значений)
CB_BAGGAGE_ON = "bag:on"
CB_BAGGAGE_OFF = "bag:off"

CB_BUDGET_SKIP = "bgt:skip"
CB_BUDGET_SET_PREFIX = "bgt:set:"  # +значение
CB_BUDGET_CUSTOM = "bgt:custom"

CB_METRIC_MONEY = "met:money"
CB_METRIC_TIME = "met:time"

# Подтверждение
CB_CONFIRM_RUN = "cfm:run"
CB_CONFIRM_EDIT = "cfm:edit"

#: Пресеты бюджета — крупные «круглые» суммы плюс «без лимита» / «своя».
BUDGET_PRESETS: list[int | None] = [None, 5_000, 15_000, 30_000, 50_000, 100_000]


# ====================================================================== #
# Вспомогательное
# ====================================================================== #
def _nav_row(include_back: bool) -> list[InlineKeyboardButton]:
    """Ряд навигации: «Назад» (опционально) + «Главное меню»."""
    row: list[InlineKeyboardButton] = []
    if include_back:
        row.append(InlineKeyboardButton(text="⬅️ Назад", callback_data=CB_BACK))
    row.append(InlineKeyboardButton(text="🏠 Главное меню", callback_data=CB_MENU))
    return row


def _format_transport(value: str | None) -> str:
    """Человекочитаемая подпись выбранного транспорта."""
    return {
        "any": "Любой 🚆✈",
        "plane": "Самолёт ✈",
        "train": "Поезд 🚆",
    }.get(value or "", "—")


def _format_metric(value: str | None) -> str:
    return "⏱ Время" if value == "time" else "💰 Деньги"


def _format_baggage(value: bool | None) -> str:
    return "🧳 Только с багажом" if value else "🎒 Багаж не обязателен"


def _format_budget(value: int | None) -> str:
    return "∞ Без лимита" if value is None else f"{value:,} ₽".replace(",", " ")


def _intermediate_summary(items: list[dict[str, Any]]) -> str:
    """Список промежуточных городов: «Город — N дн.» по строкам."""
    if not items:
        return "_не добавлены_"
    return "\n".join(
        f"• {item['city']} — {item['days_to_stay']} дн."
        for item in items
    )


def _summary_text(data: dict[str, Any]) -> str:
    """Сводка по всем собранным параметрам (используется в ``confirm``)."""
    transport = _format_transport(data.get("transport_type"))
    metric = _format_metric(data.get("optimization_metric"))
    baggage = _format_baggage(bool(data.get("require_baggage")))
    budget = _format_budget(data.get("max_budget"))
    items = data.get("intermediate_cities", []) or []
    return (
        "*Проверьте параметры маршрута:*\n\n"
        f"• Откуда: *{data.get('origin_city') or '—'}*\n"
        f"• Куда: *{data.get('destination_city') or '—'}*\n"
        f"• Дата отправления: *{data.get('start_date') or '—'}*\n"
        f"• Запас дней: *{data.get('surplus_days', 0)}*\n"
        f"• Транспорт: *{transport}*\n"
        f"• Багаж: *{baggage}*\n"
        f"• Бюджет (макс.): *{budget}*\n"
        f"• Оптимизация: *{metric}*\n\n"
        f"*Промежуточные города:*\n{_intermediate_summary(items)}\n\n"
        "Запустить оптимизацию или изменить параметры?"
    )


# ====================================================================== #
# Главная точка входа — текст и клавиатура для каждого состояния
# ====================================================================== #
def view_for(state: State, data: dict[str, Any]) -> tuple[str, InlineKeyboardMarkup]:
    """Возвращает текст и клавиатуру для указанного шага диалога.

    Args:
        state: Состояние FSM, для которого строится экран.
        data: Текущая память диалога (используется в экранах сводки).
    """
    # --- Маршрут ---------------------------------------------------------
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
            "🗓️ Введите *запас дней* — насколько вы готовы сдвинуть даты "
            "ради лучшей цены (целое число ≥ 0):",
            InlineKeyboardMarkup(inline_keyboard=[_nav_row(include_back=True)]),
        )

    # --- Промежуточные города --------------------------------------------
    if state == StateMachine.waiting_for_intermediate_count:
        rows: list[list[InlineKeyboardButton]] = []
        # Сетка 4 × 2: «0» (без) и «1»..«7», «8» — последняя отдельно.
        first = [InlineKeyboardButton(text="🚫 Без промежуточных", callback_data=CB_NO_INTERMEDIATE)]
        rows.append(first)
        # 4 кнопки в строке для 1..8
        row: list[InlineKeyboardButton] = []
        for n in range(1, MAX_INTERMEDIATE_CITIES + 1):
            row.append(
                InlineKeyboardButton(text=str(n), callback_data=f"{CB_INTERMEDIATE_COUNT_PREFIX}{n}")
            )
            if len(row) == 4:
                rows.append(row)
                row = []
        if row:
            rows.append(row)
        rows.append(_nav_row(include_back=True))
        return (
            "🧭 *Промежуточные города*\n\n"
            "Сколько городов хотите посетить по пути? "
            f"Максимум — {MAX_INTERMEDIATE_CITIES}.",
            InlineKeyboardMarkup(inline_keyboard=rows),
        )

    if state == StateMachine.waiting_for_intermediate_city:
        # Внутри подцикла: показываем прогресс и предлагаем «Готово / Отменить».
        items = data.get("intermediate_cities", []) or []
        idx = len(items) + 1
        target = int(data.get("_intermediate_target", 0))
        return (
            f"🏙 *Промежуточный город №{idx} из {target}*\n\n"
            "Введите *название* города:",
            InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="↩ Изменить количество",
                            callback_data=CB_BACK,
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            text="🏠 Главное меню", callback_data=CB_MENU
                        )
                    ],
                ]
            ),
        )

    if state == StateMachine.waiting_for_intermediate_days:
        # Подцикл дней: показываем карточку с клавиатурой «Пропустить».
        # В обычном сценарии этот экран рисуется из ``view_intermediate_days``
        # сразу после ввода названия, но сюда можно попасть через «Назад».
        items = data.get("intermediate_cities", []) or []
        target = int(data.get("_intermediate_target", 0))
        last_city = items[-1]["city"] if items else "—"
        return view_intermediate_days(
            data, city=last_city, idx=len(items), target=target
        )

    # --- Транспорт -------------------------------------------------------
    if state == StateMachine.waiting_for_transport:
        return (
            "🚉 Выберите *тип транспорта*:",
            InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="🌐 Любой", callback_data=CB_TRANSPORT_ANY
                        ),
                        InlineKeyboardButton(
                            text="✈ Только самолёт", callback_data=CB_TRANSPORT_PLANE
                        ),
                        InlineKeyboardButton(
                            text="🚆 Только поезд", callback_data=CB_TRANSPORT_TRAIN
                        ),
                    ],
                    _nav_row(include_back=True),
                ]
            ),
        )

    # --- Фильтры ---------------------------------------------------------
    if state == StateMachine.waiting_for_filters_menu:
        # Оглавление фильтров: три «кнопки-раздела» показывают текущее
        # значение и открывают соответствующий подэкран.
        current = {
            "baggage": _format_baggage(bool(data.get("require_baggage"))),
            "budget": _format_budget(data.get("max_budget")),
            "metric": _format_metric(data.get("optimization_metric")),
        }
        return (
            "🎛 *Фильтры*\n\n"
            f"• 🧳 Багаж: {current['baggage']}\n"
            f"• 💰 Бюджет (макс.): {current['budget']}\n"
            f"• ⚙️ Оптимизация: {current['metric']}\n\n"
            "Нажмите на раздел, чтобы изменить значение.",
            InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text=f"🧳 Багаж: {current['baggage']}",
                            callback_data=CB_FILTERS_OPEN_BAGGAGE,
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            text=f"💰 Бюджет: {current['budget']}",
                            callback_data=CB_FILTERS_OPEN_BUDGET,
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            text=f"⚙️ Оптимизация: {current['metric']}",
                            callback_data=CB_FILTERS_OPEN_METRIC,
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            text="✅ Готово — перейти к запуску",
                            callback_data=CB_FILTERS_DONE,
                        )
                    ],
                    _nav_row(include_back=True),
                ]
            ),
        )

    if state == StateMachine.waiting_for_baggage:
        return (
            "🧳 *Только с багажом?*\n\n"
            "Если включить — оптимизатор будет учитывать билеты с включённым "
            "багажом (для самолётов это платная опция).",
            InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="🧳 Да, только с багажом",
                            callback_data=CB_BAGGAGE_ON,
                        ),
                        InlineKeyboardButton(
                            text="🎒 Нет, багаж не нужен",
                            callback_data=CB_BAGGAGE_OFF,
                        ),
                    ],
                    _nav_row(include_back=True),
                ]
            ),
        )

    if state == StateMachine.waiting_for_budget:
        # Пресеты + «своя сумма» (текстом).
        rows: list[list[InlineKeyboardButton]] = []
        row: list[InlineKeyboardButton] = []
        for value in BUDGET_PRESETS:
            label = "∞ Без лимита" if value is None else f"{value // 1000}K ₽"
            callback = CB_BUDGET_SKIP if value is None else f"{CB_BUDGET_SET_PREFIX}{value}"
            row.append(InlineKeyboardButton(text=label, callback_data=callback))
            if len(row) == 2:
                rows.append(row)
                row = []
        if row:
            rows.append(row)
        rows.append(
            [InlineKeyboardButton(text="✍ Своя сумма…", callback_data=CB_BUDGET_CUSTOM)]
        )
        rows.append(_nav_row(include_back=True))
        return (
            "💰 *Максимальный бюджет на поездку*\n\n"
            "Билеты дороже указанной суммы будут отсеяны. "
            "Можно ввести свою сумму текстом.",
            InlineKeyboardMarkup(inline_keyboard=rows),
        )

    if state == StateMachine.waiting_for_metric:
        return (
            "⚙️ *Критерий оптимизации*\n\n"
            "💰 *Деньги* — минимальная итоговая цена маршрута.\n"
            "⏱ *Время* — минимальное суммарное время в пути.",
            InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="💰 Деньги", callback_data=CB_METRIC_MONEY
                        ),
                        InlineKeyboardButton(
                            text="⏱ Время", callback_data=CB_METRIC_TIME
                        ),
                    ],
                    _nav_row(include_back=True),
                ]
            ),
        )

    # --- Подтверждение ---------------------------------------------------
    if state == StateMachine.waiting_for_confirm:
        return (
            _summary_text(data),
            InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="🚀 Запустить оптимизацию!",
                            callback_data=CB_CONFIRM_RUN,
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            text="✏ Изменить параметры",
                            callback_data=CB_CONFIRM_EDIT,
                        )
                    ],
                    _nav_row(include_back=True),
                ]
            ),
        )

    # Фоллбэк — состояние, для которого экран не описан.
    return (
        "🤖 Не знаю, как сюда попасть. Нажмите 🏠 Главное меню.",
        InlineKeyboardMarkup(inline_keyboard=[_nav_row(include_back=False)]),
    )


# ====================================================================== #
# Промежуточные состояния: ввод дней для уже названного города
# ====================================================================== #
def view_intermediate_days(
    data: dict[str, Any], *, city: str, idx: int, target: int
) -> tuple[str, InlineKeyboardMarkup]:
    """Экран ввода числа дней для только что названного промежуточного города."""
    return (
        f"📍 *{city}* — сколько дней хотите там провести? "
        f"(город {idx} из {target})\n\n"
        "Введите целое число ≥ 0, либо нажмите «Готово», если передумали:",
        InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="⏭ Пропустить (0 дней)",
                        callback_data=f"{CB_INTERMEDIATE_DONE}:0",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="↩ Назад к выбору количества",
                        callback_data=CB_BACK,
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="🏠 Главное меню", callback_data=CB_MENU
                    )
                ],
            ]
        ),
    )
