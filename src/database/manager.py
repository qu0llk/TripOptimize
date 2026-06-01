"""Менеджер асинхронных подключений и сессий к PostgreSQL.

:class:`DatabaseSessionManager` инкапсулирует жизненный цикл async-движка
SQLAlchemy и фабрики сессий, предоставляя:

* контекст-менеджер :meth:`session` для единицы работы (unit of work) с
  автоматическим коммитом/откатом;
* контекст-менеджер :meth:`connect` для DDL-операций (создание схемы);
* удобные методы :meth:`create_all` / :meth:`drop_all` и генератор
  :meth:`get_session` для DI-фреймворков (FastAPI и т.п.).

Класс декуплирован от слоя конфигурации: он принимает готовую строку
подключения. Метод-фабрика :meth:`from_settings` остаётся тонким мостом к
:class:`~src.config.Settings` (принцип инверсии зависимостей).
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from src.config import Settings
from src.database.base import Base

logger = logging.getLogger(__name__)


class DatabaseSessionManager:
    """Управляет async-движком и фабрикой сессий SQLAlchemy.

    Один экземпляр рассчитан на всё время жизни приложения: движок держит
    пул соединений, поэтому пересоздавать менеджер на каждый запрос не нужно.
    По завершении работы обязателен вызов :meth:`dispose` для корректного
    закрытия пула.
    """

    def __init__(
        self,
        database_url: str,
        *,
        echo: bool = False,
        pool_size: int = 5,
        max_overflow: int = 10,
        pool_pre_ping: bool = True,
    ) -> None:
        """Создаёт движок и фабрику сессий.

        Args:
            database_url: Строка подключения ``postgresql+asyncpg://...``.
            echo: Логировать ли SQL (полезно для отладки).
            pool_size: Базовый размер пула соединений.
            max_overflow: Сколько соединений сверх ``pool_size`` допускается.
            pool_pre_ping: Проверять «живость» соединения перед выдачей из
                пула — защищает от обрывов со стороны сервера.
        """
        self._engine: AsyncEngine = create_async_engine(
            database_url,
            echo=echo,
            pool_size=pool_size,
            max_overflow=max_overflow,
            pool_pre_ping=pool_pre_ping,
        )
        # expire_on_commit=False — объекты остаются доступными после коммита
        # (иначе обращение к атрибутам вне сессии вызвало бы ленивую загрузку
        # и ошибку DetachedInstanceError).
        self._sessionmaker: async_sessionmaker[AsyncSession] = async_sessionmaker(
            bind=self._engine,
            expire_on_commit=False,
            autoflush=False,
        )

    @classmethod
    def from_settings(
        cls, settings: Settings, **kwargs: object
    ) -> "DatabaseSessionManager":
        """Собирает менеджер из сводного объекта конфигурации.

        Тонкий мост между слоем конфигурации и слоем БД: остальной код
        зависит от :class:`DatabaseSessionManager`, а не от того, откуда
        взялась строка подключения.
        """
        return cls(settings.database_url, **kwargs)  # type: ignore[arg-type]

    @property
    def engine(self) -> AsyncEngine:
        """Возвращает низкоуровневый async-движок (для миграций/диагностики)."""
        return self._engine

    @asynccontextmanager
    async def connect(self) -> AsyncIterator[AsyncConnection]:
        """Контекст-менеджер транзакционного соединения.

        Используется преимущественно для DDL (создание/удаление схемы).
        Транзакция фиксируется автоматически при выходе без ошибок.
        """
        async with self._engine.begin() as connection:
            yield connection

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        """Контекст-менеджер единицы работы с автоматическим коммитом.

        Семантика:
            * успешный выход из блока → ``commit``;
            * любое исключение внутри блока → ``rollback`` и проброс ошибки;
            * в любом случае сессия закрывается.

        Это избавляет вызывающий код от ручного управления транзакцией:
        репозитории лишь добавляют/изменяют объекты, а фиксация происходит
        на границе контекста.
        """
        session = self._sessionmaker()
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()

    async def get_session(self) -> AsyncIterator[AsyncSession]:
        """Async-генератор сессии для DI-фреймворков (например, FastAPI).

        Использование::

            async def endpoint(session: AsyncSession = Depends(db.get_session)):
                ...

        Делегирует всю логику транзакции контекст-менеджеру :meth:`session`.
        """
        async with self.session() as session:
            yield session

    async def create_all(self) -> None:
        """Создаёт все таблицы из метаданных, если их ещё нет.

        Импорт моделей выполняется внутри метода (а не на уровне модуля),
        чтобы гарантированно зарегистрировать их в ``Base.metadata`` и при
        этом избежать циклических импортов на этапе загрузки пакета.
        """
        from src.database import models  # noqa: F401  — регистрация таблиц

        logger.info("Создание схемы БД (create_all)…")
        async with self._engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        logger.info("Схема БД готова.")

    async def drop_all(self) -> None:
        """Удаляет все таблицы метаданных (используется в тестах/переинициализации)."""
        from src.database import models  # noqa: F401  — регистрация таблиц

        logger.warning("Удаление схемы БД (drop_all)…")
        async with self._engine.begin() as connection:
            await connection.run_sync(Base.metadata.drop_all)

    async def dispose(self) -> None:
        """Закрывает пул соединений движка. Вызывать при остановке приложения."""
        await self._engine.dispose()
        logger.info("Пул соединений БД закрыт.")
