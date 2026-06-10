"""Справочник городов мира и их IATA-кодов (Модуль 2, расширение).

Загружает `cities.json` (база городов Aviasales) и строит индекс
«название города → IATA-метакод». Позволяет скраперу авиабилетов и генератору
ссылок работать со всеми городами мира, а не с захардкоженным мини-списком.

Справочник кэшируется как ленивый синглтон: тяжёлый JSON (~5 МБ) читается
один раз при первом обращении.
"""

from __future__ import annotations

import json
import logging
from functools import lru_cache
from pathlib import Path

logger = logging.getLogger(__name__)

#: Путь к базе городов рядом с модулями конфигурации.
_CITIES_PATH = Path(__file__).resolve().parent / "cities.json"


def _normalize(name: str) -> str:
    """Приводит название города к ключу индекса (нижний регистр, без «ё»)."""
    return name.strip().lower().replace("ё", "е")


# Приоритетные коды для мировых мегаполисов-омонимов: в базе несколько городов
# с одинаковым названием (например, три «Лондона»), и без подсказки побеждает
# случайный по порядку. Здесь закреплены метакоды агломераций для крупнейших.
_OVERRIDES: dict[str, str] = {
    "лондон": "LON",
    "париж": "PAR",
    "москва": "MOW",
    "нью-йорк": "NYC",
    "рим": "ROM",
    "милан": "MIL",
    "берлин": "BER",
    "токио": "TYO",
    "пекин": "BJS",
    "шанхай": "SHA",
    "торонто": "YTO",
    "вашингтон": "WAS",
    "чикаго": "CHI",
    "осака": "OSA",
    "барселона": "BCN",
    "валенсия": "VLC",
}


class CityCatalog:
    """Индекс «город → IATA-код» по русским и английским названиям."""

    def __init__(self, path: Path = _CITIES_PATH) -> None:
        # Значение индекса: (код, есть_аэропорт). Флаг нужен для разрешения
        # коллизий одноимённых городов в пользу города с аэропортом.
        self._index: dict[str, tuple[str, bool]] = {}
        # normalized_key → original display name (для автодополнения).
        self._names: dict[str, str] = {}
        # normalized_key → (lat, lon) для отрисовки карты маршрута. Заполняется
        # лениво из того же JSON, что и индекс; координаты — публичное поле,
        # используемое визуализацией маршрута на карте мира.
        self._coords: dict[str, tuple[float, float]] = {}
        self._load(path)

    def _load(self, path: Path) -> None:
        try:
            entries = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logger.warning("Не удалось загрузить справочник городов %s: %s", path, exc)
            return

        for entry in entries:
            code = entry.get("code")
            if not code:
                continue
            flightable = bool(entry.get("has_flightable_airport"))
            names = [entry.get("name"), entry.get("name_translations", {}).get("en")]
            coords_raw = entry.get("coordinates") or {}
            lat = coords_raw.get("lat")
            lon = coords_raw.get("lon")
            coords: tuple[float, float] | None = (
                (float(lat), float(lon)) if lat is not None and lon is not None else None
            )
            for raw in names:
                if not raw:
                    continue
                key = _normalize(raw)
                existing = self._index.get(key)
                # Записываем, если ключа ещё нет, либо новый город с аэропортом
                # вытесняет ранее найденный без аэропорта.
                if existing is None or (flightable and not existing[1]):
                    self._index[key] = (code, flightable)
                    self._names[key] = raw
                    if coords is not None:
                        self._coords[key] = coords

        logger.info("Справочник городов загружен: %d ключей", len(self._index))

    def search(self, query: str, limit: int = 10) -> list[str]:
        """Возвращает названия городов, начинающихся с ``query`` (prefix-поиск).

        Города с аэропортом ставятся первыми, затем алфавитный порядок.
        """
        if not query:
            return []
        key = _normalize(query)
        matches: list[tuple[bool, str]] = []
        for norm_key, name in self._names.items():
            if norm_key.startswith(key):
                flightable = self._index[norm_key][1]
                matches.append((not flightable, name))  # False < True → с аэропортом первее
        matches.sort()
        return [name for _, name in matches[:limit]]

    def resolve_iata(self, city: str) -> str | None:
        """Возвращает IATA-код города или ``None``, если он не найден.

        Сначала проверяется список приоритетных метакодов мегаполисов
        (:data:`_OVERRIDES`), затем — основной индекс справочника.
        """
        key = _normalize(city)
        if key in _OVERRIDES:
            return _OVERRIDES[key]
        found = self._index.get(key)
        return found[0] if found else None

    def coordinates(self, city: str) -> tuple[float, float] | None:
        """Возвращает ``(lat, lon)`` для города или ``None``, если не найден.

        Используется фронтендом для отрисовки маршрута на карте мира.
        Тот же алгоритм разрешения омонимов, что и в :meth:`resolve_iata`.
        """
        key = _normalize(city)
        if key in _OVERRIDES:
            # Для омонимов мегаполисов координаты — у агломерации; читаем
            # напрямую из уже загруженного индекса (он обновлён при _load).
            return self._coords.get(key)
        return self._coords.get(key)


@lru_cache(maxsize=1)
def get_city_catalog() -> CityCatalog:
    """Возвращает кэшированный экземпляр справочника городов (ленивый синглтон)."""
    return CityCatalog()
