"""Пакет управления конфигурацией приложения (Модуль 2)."""

from src.config.cities import CityCatalog, get_city_catalog
from src.config.settings import (
    ConfigurationError,
    MissingDatabaseConfigurationError,
    MissingProxyConfigurationError,
    MissingTelegramConfigurationError,
    ProxySettings,
    Settings,
    get_settings,
    require_telegram_token,
)

__all__ = [
    "CityCatalog",
    "get_city_catalog",
    "ConfigurationError",
    "MissingDatabaseConfigurationError",
    "MissingProxyConfigurationError",
    "MissingTelegramConfigurationError",
    "ProxySettings",
    "Settings",
    "get_settings",
    "require_telegram_token",
]
