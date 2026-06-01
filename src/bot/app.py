"""Сборка и запуск Telegram-бота (Модуль 6).

Точка композиции зависимостей: конфигурация → менеджер БД → оркестратор →
сервис → бот/диспетчер. Бот работает как самостоятельный процесс и использует
ту же PostgreSQL, что и API; собственный фоновый воркер оркестратора
запускается на старте и гасится при остановке.
"""

from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.fsm.storage.memory import MemoryStorage

from src.bot.handlers import router
from src.bot.service import TripOptimizerService
from src.config import get_settings, require_telegram_token
from src.database import DatabaseSessionManager
from src.orchestration import TaskOrchestrator

logger = logging.getLogger(__name__)


async def main() -> None:
    """Поднимает все зависимости и запускает long-polling бота."""
    logging.basicConfig(level=logging.INFO)

    settings = get_settings()                  # валидирует прокси и DATABASE_URL
    token = require_telegram_token(settings)   # валидирует TELEGRAM_BOT_TOKEN

    db_manager = DatabaseSessionManager.from_settings(settings)
    orchestrator = TaskOrchestrator(db_manager, settings=settings)
    service = TripOptimizerService(orchestrator)

    tg_session = AiohttpSession(proxy=settings.telegram_proxy_url) if settings.telegram_proxy_url else None
    bot = Bot(token, session=tg_session) if tg_session else Bot(token)
    dispatcher = Dispatcher(storage=MemoryStorage())
    dispatcher["service"] = service  # DI: сервис прокидывается в хендлеры
    dispatcher.include_router(router)

    await orchestrator.start()
    logger.info("Бот запущен, начинаем long-polling…")
    try:
        await dispatcher.start_polling(bot)
    finally:
        await orchestrator.stop()
        await db_manager.dispose()
        await bot.session.close()
        logger.info("Бот остановлен, ресурсы освобождены")


def run() -> None:
    """Синхронная обёртка для запуска из точки входа ``run_bot.py``."""
    asyncio.run(main())
