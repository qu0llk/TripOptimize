"""Smoke-тесты Telegram-бота (Модуль 6).

Покрывают три контракта, которые легко сломать рефакторингом:

* Все состояния FSM имеют описание в :func:`src.bot.keyboards.view_for`.
* :meth:`TripOptimizerService.format_itinerary` корректно рендерит
  пустые плечи (с восстановлением названий городов) и карточки с
  кликабельными ссылками на покупку.
* Подцикл «название города → дни» не съедает ввод и не плодит лишние
  элементы в ``intermediate_cities`` (регресс на коллизию двух
  ``@router.message(waiting_for_intermediate_city)``).

Тесты намеренно дешёвые: только ассерты на текст, без aiogram/БД/Playwright.
"""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, MagicMock

from src.bot import handlers as bot_handlers
from src.bot import keyboards as kb
from src.bot.service import TripOptimizerService
from src.bot.states import STATE_ORDER, StateMachine


class ViewForCoverageTests(unittest.TestCase):
    """У каждого состояния должен быть осмысленный экран."""

    def test_all_states_have_screen(self) -> None:
        for state in STATE_ORDER:
            text, markup = kb.view_for(state, {})
            self.assertTrue(text, f"Пустой текст для {state.state}")
            self.assertIsNotNone(markup, f"Нет клавиатуры для {state.state}")
            # В каждом экране есть либо «Главное меню», либо он первый.
            self.assertIn("🏠 Главное меню", str(markup), state.state)

    def test_filters_menu_lists_all_sections(self) -> None:
        text, _ = kb.view_for(StateMachine.waiting_for_filters_menu, {})
        for section in ("Багаж", "Бюджет", "Оптимизация"):
            self.assertIn(section, text, section)

    def test_confirm_screen_lists_all_fields(self) -> None:
        data = {
            "origin_city": "Москва",
            "destination_city": "Сочи",
            "start_date": "2026-07-15",
            "surplus_days": 1,
            "transport_type": "any",
            "require_baggage": True,
            "max_budget": 30000,
            "optimization_metric": "time",
            "intermediate_cities": [{"city": "Воронеж", "days_to_stay": 1}],
        }
        text, _ = kb.view_for(StateMachine.waiting_for_confirm, data)
        for needle in ("Москва", "Сочи", "2026-07-15", "Воронеж", "30 000 ₽", "Любой"):
            self.assertIn(needle, text, needle)
        # Метрика показывается в короткой форме: «⏱ Время» или «💰 Деньги».
        self.assertIn("Время", text)


class FormatItineraryTests(unittest.TestCase):
    """Парс карточек маршрута в Telegram-Markdown."""

    def _full_result(self) -> dict:
        return {
            "order": ["Москва", "Воронеж", "Краснодар", "Сочи"],
            "optimization_metric": "time",
            "legs": [
                {
                    "source": "aviasales",
                    "departure_city": "Москва",
                    "arrival_city": "Воронеж",
                    "departure_time": "2026-07-15T08:00:00",
                    "arrival_time": "2026-07-15T09:30:00",
                    "duration_minutes": 90,
                    "price": 5500,
                    "has_baggage": True,
                    "booking_url": "https://www.aviasales.ru/search/MOW=VOG1507",
                },
                {
                    "__empty__": True,
                    "reason": "Нет прямых поездов Воронеж → Краснодар.",
                },
                {
                    "source": "rzd",
                    "departure_city": "Краснодар",
                    "arrival_city": "Сочи",
                    "departure_time": "2026-07-18T11:00:00",
                    "arrival_time": "2026-07-18T14:20:00",
                    "duration_minutes": 200,
                    "price": 1800,
                    "has_baggage": False,
                    "booking_url": "https://www.tutu.ru/poezda/view_d.php?x=1",
                },
            ],
            "total_price": 7300,
            "total_duration_minutes": 290,
        }

    def test_renders_route_summary(self) -> None:
        text = TripOptimizerService.format_itinerary(self._full_result())
        self.assertIn("Москва → Воронеж → Краснодар → Сочи", text)
        self.assertIn("7300 ₽", text)
        self.assertIn("4 ч 50 мин", text)
        self.assertIn("по времени", text)

    def test_renders_booking_link(self) -> None:
        text = TripOptimizerService.format_itinerary(self._full_result())
        self.assertIn("[Купить билет →](https://www.aviasales.ru", text)
        self.assertIn("[Купить билет →](https://www.tutu.ru", text)

    def test_empty_leg_uses_order_cities(self) -> None:
        """Пустое плечо должно подставить имена городов из ``order``."""
        text = TripOptimizerService.format_itinerary(self._full_result())
        self.assertIn("Воронеж → Краснодар", text)
        # НЕ должно остаться «— → —» — это значит, что мы сломали парсинг order.
        self.assertNotIn("— → —", text)
        self.assertIn("Нет прямых поездов Воронеж → Краснодар", text)

    def test_global_reason_appears(self) -> None:
        result = self._full_result()
        result["global_reason"] = "Тестовая глобальная причина."
        text = TripOptimizerService.format_itinerary(result)
        self.assertIn("Тестовая глобальная причина", text)

    def test_partial_fail_counter(self) -> None:
        text = TripOptimizerService.format_itinerary(self._full_result())
        self.assertIn("Найдены билеты на 2 из 3 плеч", text)


class CallbackDataUniquenessTests(unittest.TestCase):
    """Каждое значение CB_* должно быть уникальным (Telegram-роутер не различает)."""

    def test_unique_callbacks(self) -> None:
        seen: dict[str, str] = {}
        for name in dir(kb):
            if not name.startswith("CB_"):
                continue
            value = getattr(kb, name)
            if not isinstance(value, str):
                continue
            # CB_BUDGET_SET_PREFIX и CB_INTERMEDIATE_COUNT_PREFIX — префиксы,
            # намеренно общие для семейства кнопок. Игнорируем при проверке.
            if name.endswith("_PREFIX"):
                continue
            if value in seen:
                self.fail(f"Коллизия callback_data: {name}={value!r} ↔ {seen[value]}")
            seen[value] = name


class IntermediateCityFlowTests(unittest.IsolatedAsyncioTestCase):
    """Подцикл «название → дни → название → дни» не должен ломать список.

    Регресс: оба ``@router.message(waiting_for_intermediate_city)`` обработчика
    срабатывали на любой текст, и первый из них добавлял новый город, из-за
    чего ввод дней для первого города терялся, а счётчик «город N из M»
    вылезал за ``M``.
    """

    def _make_state(self, initial: dict | None = None) -> MagicMock:
        """FSM-заглушка: dict под капотом, async-методы наружу."""
        data: dict = dict(initial or {})
        state = MagicMock()
        state.get_state = AsyncMock(return_value=StateMachine.waiting_for_intermediate_city.state)
        state.get_data = AsyncMock(side_effect=lambda: data)
        state.update_data = AsyncMock(side_effect=lambda **kw: data.update(kw))
        state.set_state = AsyncMock(side_effect=lambda s: setattr(state, "_state", s))
        state._data = data  # для ассертов
        return state

    def _make_message(self, text: str) -> MagicMock:
        msg = MagicMock()
        msg.text = text
        msg.answer = AsyncMock()
        return msg

    async def test_two_cities_with_skip(self) -> None:
        """Классический сценарий: 2 города, для каждого «⏭ Пропустить»."""
        bot = MagicMock()
        bot.send_message = AsyncMock()
        state = self._make_state({"_intermediate_target": 2, "intermediate_cities": []})

        # 1) Имя первого города.
        await bot_handlers.on_intermediate_city(
            self._make_message("Казань"), state, bot
        )
        # Перешли в подцикл дней.
        state.set_state.assert_awaited_with(StateMachine.waiting_for_intermediate_days)
        # Промежуточный список содержит ровно один элемент.
        self.assertEqual(len(state._data["intermediate_cities"]), 1)
        self.assertEqual(state._data["intermediate_cities"][0]["city"], "Казань")
        self.assertTrue(state._data["intermediate_cities"][0]["_pending_days"])

        # 2) Текст «0» (ручной ввод дней) — НЕ должен превратиться во второй город.
        await bot_handlers.on_intermediate_days_text(
            self._make_message("0"), state, bot
        )
        self.assertEqual(len(state._data["intermediate_cities"]), 1)
        self.assertEqual(state._data["intermediate_cities"][0]["days_to_stay"], 0)
        self.assertNotIn("_pending_days", state._data["intermediate_cities"][0])

        # 3) Имя второго города.
        await bot_handlers.on_intermediate_city(
            self._make_message("Москва"), state, bot
        )
        self.assertEqual(len(state._data["intermediate_cities"]), 2)
        self.assertEqual(state._data["intermediate_cities"][1]["city"], "Москва")
        self.assertTrue(state._data["intermediate_cities"][1]["_pending_days"])

        # 4) Текст «2» — дни для второго города.
        await bot_handlers.on_intermediate_days_text(
            self._make_message("2"), state, bot
        )
        items = state._data["intermediate_cities"]
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]["city"], "Казань")
        self.assertEqual(items[0]["days_to_stay"], 0)
        self.assertEqual(items[1]["city"], "Москва")
        self.assertEqual(items[1]["days_to_stay"], 2)

    async def test_skip_callback_advances_to_next_city(self) -> None:
        """Колбэк «⏭ Пропустить» после первого города предлагает ввести второй."""
        bot = MagicMock()
        state = self._make_state(
            {
                "_intermediate_target": 2,
                "intermediate_cities": [{"city": "Казань", "days_to_stay": 0, "_pending_days": True}],
            }
        )
        state.get_state = AsyncMock(return_value=StateMachine.waiting_for_intermediate_days.state)

        callback = MagicMock()
        callback.data = f"{kb.CB_INTERMEDIATE_DONE}:0"
        callback.answer = AsyncMock()
        callback.message = MagicMock()
        callback.message.edit_text = AsyncMock()

        await bot_handlers.on_intermediate_done(callback, state)
        # Дни первого города зафиксированы.
        items = state._data["intermediate_cities"]
        self.assertEqual(items[0]["days_to_stay"], 0)
        self.assertNotIn("_pending_days", items[0])
        # Перешли в режим ввода следующего города.
        state.set_state.assert_awaited_with(StateMachine.waiting_for_intermediate_city)

    async def test_text_does_not_create_extra_city(self) -> None:
        """Текст в состоянии ожидания дней не плодит фантомные города."""
        bot = MagicMock()
        bot.send_message = AsyncMock()
        state = self._make_state(
            {
                "_intermediate_target": 1,
                "intermediate_cities": [{"city": "Казань", "days_to_stay": 0, "_pending_days": True}],
            }
        )
        state.get_state = AsyncMock(return_value=StateMachine.waiting_for_intermediate_days.state)

        # Текст «3» в подцикле дней: должен обновить дни, а не добавить город «3».
        await bot_handlers.on_intermediate_days_text(
            self._make_message("3"), state, bot
        )
        items = state._data["intermediate_cities"]
        self.assertEqual(len(items), 1, f"Фантомный город: {items}")
        self.assertEqual(items[0]["city"], "Казань")
        self.assertEqual(items[0]["days_to_stay"], 3)


if __name__ == "__main__":
    unittest.main()
