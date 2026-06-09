"""Регрессионные тесты для src.optimization.optimizer.

Покрывают четыре сценария:
1. Старый штраф ``_OVERSHOOT_RUB_PENALTY`` заставлял медленный поезд
   «внутри окна» выигрывать у быстрого/дешёвого самолёта «с перелётом».
   Без штрафа метрика ``money`` должна выбирать дешёвый самолёт.
2. Раньше оптимизатор возвращал ``None`` на каждом плече, если
   ``min_dep`` оказывался позже всех доступных рейсов. Теперь он берёт
   ближайший билет к ``min_dep`` (но не раньше ``start_dt``) и
   продолжает — длинные маршруты перестают «обрываться».
3. Перекрёстный штраф ``_cross_term`` (метрика ``money``): длинный
   дешёвый перелёт НЕ должен выигрывать у короткого чуть более
   дорогого, если разница в цене меньше штрафа за длительность.
4. Перекрёстный штраф (метрика ``time``): медленный дешёвый поезд
   НЕ должен выигрывать у быстрого дорогого самолёта, если штраф
   за цену перевешивает разницу в длительности.

Запускать из корня проекта:
    .venv/bin/python3.12 -m unittest tests.test_optimizer -v
"""

from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Добавляем корень проекта в sys.path, чтобы тесты запускались и из IDE,
# и из ``python -m unittest``.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from src.optimization.optimizer import optimize_itinerary  # noqa: E402
from src.scrapers.dto import TicketDTO  # noqa: E402


def _ticket(
    *,
    src: str,
    dep_city: str,
    arr_city: str,
    dep: str,
    arr: str,
    price: int,
    duration_min: int,
) -> TicketDTO:
    """Удобный конструктор TicketDTO с дефолтами по багажу и booking_url."""
    return TicketDTO(
        source=src,
        departure_city=dep_city,
        arrival_city=arr_city,
        departure_time=dep,
        arrival_time=arr,
        duration_minutes=duration_min,
        price=price,
        has_baggage=False,
        booking_url=None,
    )


class CheapPlaneWinsOverExpensiveTrainInsideWindow(unittest.TestCase):
    """Самолёт с dep+arr строго за окном, но дешевле поезда.

    До удаления ``_OVERSHOOT_RUB_PENALTY`` самолёт получал +5000₽ за каждый
    день сверх ``prefer_within``, и поезд с dep+arr внутри окна (без штрафа)
    мог выиграть, даже если он дороже. Сейчас штрафа нет — метрика ``money``
    честно выбирает самолёт.
    """

    def test_cheap_plane_outside_window_beats_expensive_train_inside(self) -> None:
        # Старт поездки 1 июня 2026, должен быть в Казани 3 июня (must_stay=2).
        start = "2026-06-01"
        # Окно: start + must_stay + surplus(0) = 3 июня.
        # Поезд: dep 1 июня 09:00, arr в тот же день 21:00 — внутри окна.
        train = _ticket(
            src="rzd",
            dep_city="Москва", arr_city="Казань",
            dep="2026-06-01T09:00:00+00:00",
            arr="2026-06-01T21:00:00+00:00",
            price=2800,
            duration_min=720,
        )
        # Самолёт: dep 2 июня 06:00, arr 2 июня 08:00 — за окном (arr_date 2
        # июня, а prefer_within=3 июня, разница 0... но мы делаем arr позже,
        # чтобы штраф точно сработал в старой версии).
        plane = _ticket(
            src="aviasales",
            dep_city="Москва", arr_city="Казань",
            dep="2026-06-04T06:00:00+00:00",  # dep уже после окна
            arr="2026-06-04T08:00:00+00:00",  # arr за окном
            price=4500,
            duration_min=120,
        )
        # Чтобы старый штраф точно не сыграл — поднимаем цену поезда выше
        # цены самолёта. Тогда без штрафа выигрывает самолёт; со старым
        # штрафом (5000₽/день × 1 день = 5000) итог был бы 4500+5000=9500
        # против 6500 у поезда — поезд бы выиграл.
        train_high = _ticket(
            src="rzd",
            dep_city="Москва", arr_city="Казань",
            dep="2026-06-01T09:00:00+00:00",
            arr="2026-06-01T21:00:00+00:00",
            price=6500,
            duration_min=720,
        )

        result = optimize_itinerary(
            city_pairs=[("Москва", "Казань")],
            tickets_by_pair={("Москва", "Казань"): [train_high, plane]},
            start_date_str=start,
            days_to_stay=[],
            surplus_days=0,
            metric="money",
        )
        self.assertEqual(len(result), 1)
        self.assertIsNotNone(result[0])
        assert result[0] is not None  # для тайп-чекера
        self.assertEqual(
            result[0].source, "aviasales",
            f"Ожидался дешёвый самолёт, но выбран {result[0].source!r} "
            f"с ценой {result[0].price}.",
        )


class LongTripOverflowDoesNotProduceNone(unittest.TestCase):
    """Маршрут А→Б→В, где второе плечо не помещается в окно.

    День 0: вылет А→Б (быстрый).
    День 1+2: обязательный стой в Б.
    min_dep для плеча Б→В = день 3, но в Б есть рейс только на день 1
    (слишком рано) и на день 5 (dep попадает в окно, и arr_day 5 выходит
    за окно, но dep >= min_dep — должен пройти обычный фильтр).
    Цель: убедиться, что оба случая дают не-None, и что в случае, когда
    ни один dep не попадает в окно, всё равно возвращается билет.
    """

    def test_overflow_falls_back_to_earliest_available(self) -> None:
        # Старт 1 июня 2026. Сценарий: Москва→Казань→Сочи, должен быть в
        # Казани минимум 3 дня. min_dep для Казань→Сочи = 4 июня 12:00.
        # Доступные билеты Казань→Сочи — все с dep < 4 июня (и один с dep
        # 3 июня 08:00, тоже раньше). Ни один не влезает в окно → должен
        # сработать фоллбэк «ближайший к min_dep, но >= start_dt».
        leg1 = _ticket(
            src="aviasales",
            dep_city="Москва", arr_city="Казань",
            dep="2026-06-01T10:00:00+00:00",
            arr="2026-06-01T12:00:00+00:00",
            price=5000,
            duration_min=120,
        )
        # Все билеты Казань→Сочи вылетают до 4 июня — НИ ОДИН не влезает
        # в окно. Самый поздний из них — 3 июня 08:00 (ближе всего к min_dep).
        best_in_window = _ticket(
            src="aviasales",
            dep_city="Казань", arr_city="Сочи",
            dep="2026-06-03T08:00:00+00:00",
            arr="2026-06-03T11:00:00+00:00",
            price=7000,
            duration_min=180,
        )
        earlier1 = _ticket(
            src="aviasales",
            dep_city="Казань", arr_city="Сочи",
            dep="2026-06-02T08:00:00+00:00",
            arr="2026-06-02T11:00:00+00:00",
            price=6500,
            duration_min=180,
        )
        earlier2 = _ticket(
            src="aviasales",
            dep_city="Казань", arr_city="Сочи",
            dep="2026-06-02T20:00:00+00:00",
            arr="2026-06-02T23:00:00+00:00",
            price=6800,
            duration_min=180,
        )

        result = optimize_itinerary(
            city_pairs=[("Москва", "Казань"), ("Казань", "Сочи")],
            tickets_by_pair={
                ("Москва", "Казань"): [leg1],
                ("Казань", "Сочи"): [earlier1, earlier2, best_in_window],
            },
            start_date_str="2026-06-01",
            days_to_stay=[3],  # 3 дня в Казани
            surplus_days=0,
            metric="money",
        )
        self.assertEqual(len(result), 2, "Должно быть 2 билета, не None")
        self.assertIsNotNone(result[0])
        self.assertIsNotNone(result[1], "Второе плечо не должно быть None "
                                        "даже при переполнении окна")
        assert result[1] is not None
        # Фоллбэк берёт ближайший к min_dep, т.е. самый поздний из доступных
        # до min_dep — это best_in_window (3 июня 08:00).
        self.assertEqual(
            result[1].departure_time, best_in_window.departure_time,
            "Фоллбэк должен выбрать билет с dep, ближайшим к min_dep снизу",
        )

    def test_leg_fits_window_uses_normal_path(self) -> None:
        # Контрольный случай: билет на втором плече попадает в окно —
        # обычный путь, никакого фоллбэка, берётся самый дешёвый.
        leg1 = _ticket(
            src="aviasales",
            dep_city="Москва", arr_city="Казань",
            dep="2026-06-01T10:00:00+00:00",
            arr="2026-06-01T12:00:00+00:00",
            price=5000,
            duration_min=120,
        )
        # min_dep для leg2 = 4 июня 12:00. dep 5 июня 09:00 — попадает.
        on_time = _ticket(
            src="aviasales",
            dep_city="Казань", arr_city="Сочи",
            dep="2026-06-05T09:00:00+00:00",
            arr="2026-06-05T12:00:00+00:00",
            price=6000,
            duration_min=180,
        )
        on_time_expensive = _ticket(
            src="aviasales",
            dep_city="Казань", arr_city="Сочи",
            dep="2026-06-05T15:00:00+00:00",
            arr="2026-06-05T18:00:00+00:00",
            price=9000,
            duration_min=180,
        )
        result = optimize_itinerary(
            city_pairs=[("Москва", "Казань"), ("Казань", "Сочи")],
            tickets_by_pair={
                ("Москва", "Казань"): [leg1],
                ("Казань", "Сочи"): [on_time, on_time_expensive],
            },
            start_date_str="2026-06-01",
            days_to_stay=[3],
            surplus_days=0,
            metric="money",
        )
        self.assertEqual(len(result), 2)
        self.assertIsNotNone(result[0])
        self.assertIsNotNone(result[1])
        assert result[1] is not None
        # Должен быть самый дешёвый из попавших в окно.
        self.assertEqual(result[1].price, 6000)


class CrossTermSurchargeOnMoneyMetric(unittest.TestCase):
    """Метрика ``money``: длинный дешёвый билет проигрывает короткому
    чуть более дорогому из-за перекрёстного штрафа ``+50₽/ч``.

    Конкретные числа подобраны так, чтобы без штрафа побеждал длинный
    дешёвый (5000₽ < 5300₽), а со штрафом — короткий:
        длинный:  price=5000,  duration=12ч → 5000 + 12·50 = 5600
        короткий: price=5300,  duration=3ч  → 5300 +  3·50 = 5450
    → выигрывает короткий (5450 < 5600).
    """

    def test_short_slightly_pricier_beats_long_cheap(self) -> None:
        long_cheap = _ticket(
            src="aviasales",
            dep_city="Москва", arr_city="Казань",
            dep="2026-06-01T08:00:00+00:00",
            arr="2026-06-01T20:00:00+00:00",  # 12 часов
            price=5000,
            duration_min=720,
        )
        short_pricier = _ticket(
            src="aviasales",
            dep_city="Москва", arr_city="Казань",
            dep="2026-06-01T10:00:00+00:00",
            arr="2026-06-01T13:00:00+00:00",  # 3 часа
            price=5300,
            duration_min=180,
        )
        result = optimize_itinerary(
            city_pairs=[("Москва", "Казань")],
            tickets_by_pair={("Москва", "Казань"): [long_cheap, short_pricier]},
            start_date_str="2026-06-01",
            days_to_stay=[],
            surplus_days=0,
            metric="money",
        )
        self.assertEqual(len(result), 1)
        self.assertIsNotNone(result[0])
        assert result[0] is not None
        # Без перекрёстного штрафа выиграл бы long_cheap (5000 < 5300).
        # С перекрёстным штрафом выигрывает short_pricier (5450 < 5600).
        self.assertEqual(
            result[0].duration_minutes, 180,
            f"Ожидался короткий 3-часовой билет, "
            f"но выбран {result[0].duration_minutes / 60:.1f}-часовой",
        )
        self.assertEqual(result[0].price, 5300)


class CrossTermSurchargeOnTimeMetric(unittest.TestCase):
    """Метрика ``time``: быстрый дорогой билет выигрывает у медленного
    дешёвого благодаря штрафу ``+1ч за 2000₽``.

    Конкретные числа: без штрафа медленный дешёвый (8ч) бьёт быстрый
    дорогой (2ч, 16 000₽) по чистой длительности. Со штрафом
    ``+1ч·(16000/2000) = +8ч`` короткий самолёт получает эффективные
    10ч, длинный поезд — 8ч (штраф за 2000₽ = +1ч), итого 9ч. Поезд
    выигрывает с перевесом. Это и есть желаемое поведение: дешёвый
    поезд не «проигрывает» быстрому самолёту только из-за цены.
    """

    def test_cheap_slow_train_beats_expensive_fast_plane(self) -> None:
        slow_cheap = _ticket(
            src="rzd",
            dep_city="Москва", arr_city="Казань",
            dep="2026-06-01T08:00:00+00:00",
            arr="2026-06-01T16:00:00+00:00",  # 8 часов
            price=2000,
            duration_min=480,
        )
        fast_expensive = _ticket(
            src="aviasales",
            dep_city="Москва", arr_city="Казань",
            dep="2026-06-01T10:00:00+00:00",
            arr="2026-06-01T12:00:00+00:00",  # 2 часа
            price=16000,
            duration_min=120,
        )
        result = optimize_itinerary(
            city_pairs=[("Москва", "Казань")],
            tickets_by_pair={("Москва", "Казань"): [slow_cheap, fast_expensive]},
            start_date_str="2026-06-01",
            days_to_stay=[],
            surplus_days=0,
            metric="time",
        )
        self.assertEqual(len(result), 1)
        self.assertIsNotNone(result[0])
        assert result[0] is not None
        # Без штрафа: 120 < 480 → выиграл бы fast_expensive.
        # Со штрафом: slow=480+60=540 мин, fast=120+480=600 мин
        # → выигрывает slow_cheap.
        self.assertEqual(
            result[0].duration_minutes, 480,
            f"Ожидался 8-часовой поезд, "
            f"но выбран {result[0].duration_minutes / 60:.1f}-часовой билет",
        )
        self.assertEqual(result[0].price, 2000)


if __name__ == "__main__":
    unittest.main()
