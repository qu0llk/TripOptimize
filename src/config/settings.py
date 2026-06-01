"""Загрузка и строгая валидация переменных окружения.

Модуль отвечает за чтение `.env`, нормализацию значений и фатальную проверку
учётных данных резидентного прокси. Без корректного прокси скрапинг
запрещён: целевые сайты быстро блокируют дата-центровые IP-адреса.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from typing import Final

from dotenv import load_dotenv


class ConfigurationError(RuntimeError):
    """Базовое исключение слоя конфигурации.

    Общий предок для всех фатальных ошибок инициализации настроек. Позволяет
    вышестоящим слоям перехватывать любые проблемы конфигурации одним
    `except`, не перечисляя конкретные подклассы.
    """


class MissingProxyConfigurationError(ConfigurationError):
    """Исключение фатального уровня для отсутствующих настроек прокси.

    Поднимается, когда хотя бы одна обязательная переменная окружения,
    описывающая резидентный прокси, не задана или пуста. Запуск скрапера
    без прокси заведомо приведёт к блокировке IP-адреса и потере данных,
    поэтому ошибка выбрасывается на самой ранней стадии инициализации.
    """


class MissingDatabaseConfigurationError(ConfigurationError):
    """Исключение фатального уровня для отсутствующей конфигурации БД.

    Поднимается, когда переменная окружения ``DATABASE_URL`` не задана либо
    указывает на драйвер, несовместимый с асинхронным стеком приложения
    (требуется диалект ``postgresql+asyncpg``).
    """


class MissingTelegramConfigurationError(ConfigurationError):
    """Исключение фатального уровня для отсутствующего токена Telegram-бота.

    Поднимается только при запуске Модуля 6 (бота), когда переменная
    ``TELEGRAM_BOT_TOKEN`` не задана. Остальные модули (API, скрапер) работают
    без токена, поэтому проверка вынесена в отдельный helper
    :func:`require_telegram_token`, а не в общий :func:`get_settings`.
    """


# Перечень переменных, без которых сетевая часть приложения работать не может.
_REQUIRED_PROXY_ENV_VARS: Final[tuple[str, ...]] = (
    "PROXY_HOST",
    "PROXY_PORT",
    "PROXY_USER",
    "PROXY_PASS",
)

# Обязательный префикс строки подключения: приложение работает только с
# асинхронным драйвером asyncpg, поэтому синхронные URL отклоняются сразу.
_REQUIRED_DB_URL_PREFIX: Final[str] = "postgresql+asyncpg://"


@dataclass(frozen=True, slots=True)
class ProxySettings:
    """Иммутабельный набор учётных данных резидентного прокси.

    Используется как DTO между конфигурационным слоем и Playwright.
    Метод :meth:`as_playwright_proxy` возвращает словарь в нативном
    формате Playwright (`browser.new_context(proxy=...)`).
    """

    host: str
    port: int
    username: str
    password: str

    @property
    def server(self) -> str:
        """Возвращает URL прокси-сервера (без учётных данных)."""
        return f"http://{self.host}:{self.port}"

    def as_playwright_proxy(self) -> dict[str, str]:
        """Преобразует объект в словарь для Playwright.

        Playwright принимает учётные данные отдельными полями `username`
        и `password`, что позволяет избежать утечки секретов в URL.
        """
        return {
            "server": self.server,
            "username": self.username,
            "password": self.password,
        }


@dataclass(frozen=True, slots=True)
class Settings:
    """Сводный объект конфигурации приложения.

    Содержит обязательные настройки прокси и набор опциональных параметров,
    влияющих на поведение Playwright.
    """

    proxy: ProxySettings
    #: Строка подключения SQLAlchemy в формате ``postgresql+asyncpg://...``.
    #: Используется менеджером сессий БД (Модуль 3) для создания async-движка.
    database_url: str
    headless: bool
    nav_timeout_ms: int
    #: Длина «окна добора» (мс) после первой партии результатов, по истечении
    #: которого страница принудительно закрывается ради экономии трафика.
    results_grace_ms: int
    #: Токен Telegram-бота (Модуль 6). Опционален: нужен только боту, поэтому
    #: ``None`` для прочих модулей. Валидируется через :func:`require_telegram_token`.
    telegram_bot_token: str | None
    #: HTTP-прокси для Telegram-бота (VPS-прокси). Формат: ``http://host:port``
    #: или ``http://user:pass@host:port``. ``None`` — прямое подключение.
    #: Скрапер использует отдельный резидентный прокси (``proxy``).
    telegram_proxy_url: str | None


def _load_env_once() -> None:
    """Загружает переменные из `.env`, если файл присутствует.

    `python-dotenv` сам отслеживает повторные вызовы, но для явной семантики
    функция обёрнута в отдельный шаг.
    """
    load_dotenv(override=False)


def _read_str(name: str) -> str:
    """Возвращает значение переменной окружения, очищая пробельные символы."""
    return (os.getenv(name) or "").strip()


def _validate_proxy_env() -> None:
    """Проверяет наличие всех обязательных прокси-переменных.

    При отсутствии или пустом значении хотя бы одной переменной выбрасывает
    :class:`MissingProxyConfigurationError` со списком недостающих ключей.
    """
    missing = [name for name in _REQUIRED_PROXY_ENV_VARS if not _read_str(name)]
    if missing:
        formatted = ", ".join(missing)
        raise MissingProxyConfigurationError(
            "Отсутствуют обязательные переменные окружения для резидентного "
            f"прокси: {formatted}. Скрапер не будет запущен — без валидного "
            "прокси запросы к aviasales.ru и ticket.rzd.ru гарантированно "
            "приведут к блокировке IP-адреса. Заполните `.env` по образцу "
            "`.env.example`."
        )


def _build_proxy_settings() -> ProxySettings:
    """Собирает иммутабельный объект :class:`ProxySettings` из окружения."""
    raw_port = _read_str("PROXY_PORT")
    try:
        port = int(raw_port)
    except ValueError as exc:
        raise MissingProxyConfigurationError(
            f"PROXY_PORT должен быть целым числом, получено: {raw_port!r}."
        ) from exc

    if not 1 <= port <= 65535:
        raise MissingProxyConfigurationError(
            f"PROXY_PORT должен находиться в диапазоне 1–65535, получено: {port}."
        )

    return ProxySettings(
        host=_read_str("PROXY_HOST"),
        port=port,
        username=_read_str("PROXY_USER"),
        password=_read_str("PROXY_PASS"),
    )


def _build_database_url() -> str:
    """Читает и валидирует строку подключения к PostgreSQL.

    Требования:
        * переменная ``DATABASE_URL`` обязана быть задана и непуста;
        * URL обязан использовать драйвер ``postgresql+asyncpg`` — иного
          асинхронный движок SQLAlchemy в этом проекте не поддерживает.

    Синхронные URL (``postgresql://``/``postgres://``) отклоняются с понятной
    подсказкой, чтобы исключить трудноуловимую ошибку «движок не асинхронный».
    """
    raw = _read_str("DATABASE_URL")
    if not raw:
        raise MissingDatabaseConfigurationError(
            "Отсутствует обязательная переменная окружения DATABASE_URL. "
            "Задайте строку подключения вида "
            "postgresql+asyncpg://user:pass@host:5432/dbname в файле `.env` "
            "(см. образец `.env.example`)."
        )

    if not raw.startswith(_REQUIRED_DB_URL_PREFIX):
        raise MissingDatabaseConfigurationError(
            "DATABASE_URL должен использовать асинхронный драйвер asyncpg и "
            f"начинаться с {_REQUIRED_DB_URL_PREFIX!r}. Получено: {raw!r}. "
            "Например: postgresql+asyncpg://user:pass@localhost:5432/dbname."
        )

    return raw


def _read_bool(name: str, default: bool) -> bool:
    """Парсит булево значение переменной окружения с разумными дефолтами."""
    raw = _read_str(name).lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def _read_int(name: str, default: int) -> int:
    """Парсит целочисленное значение переменной окружения."""
    raw = _read_str(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise MissingProxyConfigurationError(
            f"Переменная {name} должна быть целым числом, получено: {raw!r}."
        ) from exc


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Возвращает единственный экземпляр :class:`Settings`.

    Используется паттерн "ленивый синглтон" через :func:`functools.lru_cache`.
    Любое нарушение валидации прерывает инициализацию приложения.
    """
    _load_env_once()
    _validate_proxy_env()

    return Settings(
        proxy=_build_proxy_settings(),
        database_url=_build_database_url(),
        headless=_read_bool("HEADLESS", default=True),
        nav_timeout_ms=_read_int("NAV_TIMEOUT_MS", default=45_000),
        results_grace_ms=_read_int("RESULTS_GRACE_MS", default=3_000),
        # Токен бота читается опционально — отсутствие не ломает прочие модули.
        telegram_bot_token=_read_str("TELEGRAM_BOT_TOKEN") or None,
        telegram_proxy_url=_read_str("TELEGRAM_PROXY_URL") or None,
    )


def require_telegram_token(settings: Settings | None = None) -> str:
    """Возвращает токен Telegram-бота, валидируя его наличие.

    Вызывается точкой входа бота (Модуль 6). Если токен не задан — выбрасывает
    :class:`MissingTelegramConfigurationError`, чтобы бот не стартовал «вхолостую».
    """
    settings = settings or get_settings()
    token = settings.telegram_bot_token
    if not token:
        raise MissingTelegramConfigurationError(
            "Отсутствует переменная окружения TELEGRAM_BOT_TOKEN. Получите токен "
            "у @BotFather и добавьте его в `.env` (см. образец `.env.example`)."
        )
    return token
