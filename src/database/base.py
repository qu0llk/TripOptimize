"""Декларативная база SQLAlchemy для всех ORM-моделей приложения.

Выделена в отдельный модуль, чтобы разорвать потенциальные циклические
импорты: модели зависят только от :class:`Base`, а менеджер сессий и
репозитории — от моделей. Используется современный стиль SQLAlchemy 2.0
(`DeclarativeBase` + `Mapped`/`mapped_column`).
"""

from __future__ import annotations

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Общий декларативный базовый класс для ORM-моделей.

    Все таблицы регистрируются в едином :attr:`Base.metadata`, что позволяет
    менеджеру сессий создавать и удалять схему целиком одним вызовом.
    """
