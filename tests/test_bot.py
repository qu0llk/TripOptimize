"""Smoke-тесты Telegram-бота (Модуль 6).

Покрывают два контракта, которые легко сломать рефакторингом:

* Все состояния FSM имеют описание в :func:`src.bot.keyboards.view_for`.
* :meth:`TripOptimizerService.format_itinerary` корректно рендерит
  пустые плечи (с восстановлением названий городов) и карточки с
  кликабельными ссылками на покупку.

Тесты намеренно дешёвые: только ассерты на текст, без aiogram/БД/Playwright.
"""

from __future__ import annotations

import unittest

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


if __name__ == "__main__":
    unittest.main()
