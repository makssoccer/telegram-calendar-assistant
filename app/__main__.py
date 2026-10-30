import asyncio
from contextlib import suppress
import logging
import sys
from pathlib import Path

from aiogram import Bot
from aiogram.client.session.aiohttp import AiohttpSession
from alembic import command
from alembic.config import Config
from pydantic import ValidationError

from app.config import Settings
from app.calendar import CalendarService
from app.agent import Agent
from app.drafts import DraftStore
from app.history import HistoryStore
from app.reminders import ReminderStore, ReminderService
from app.storage import check_database, create_engine
from app.telegram_bot import create_dispatcher
from app.tourism import TourismService
from app.walking import WalkingClient

logger = logging.getLogger(__name__)


def migrate() -> None:
    # Alembic применяет ещё не выполненные миграции до последней версии (head).
    # Аналог Flyway/Liquibase: структура БД обновляется перед запуском polling.
    root = Path(__file__).resolve().parent.parent
    config = Config(str(root / "alembic.ini"))
    command.upgrade(config, "head")


async def run(settings: Settings) -> None:
    # Точка сборки приложения: создаём сервисы и передаём зависимости вручную.
    engine = create_engine(settings)
    try:
        await check_database(engine)
        logger.info("PostgreSQL connected; starting Telegram polling")
        async with Bot(
            token=settings.telegram_bot_token.get_secret_value(),
            session=AiohttpSession(timeout=40),
        ) as bot:
            calendar = CalendarService(settings.google_calendar_id, settings.google_token_path, settings.user_timezone)
            tourism = TourismService(WalkingClient(settings.ors_api_key.get_secret_value()))
            agent = Agent(calendar, settings.openrouter_api_key.get_secret_value(), settings.openrouter_model,
                          HistoryStore(engine, settings.owner_telegram_id), tourism)
            reminder_store = ReminderStore(engine, settings.owner_telegram_id, settings.google_calendar_id)
            await reminder_store.initialize()
            reminders = ReminderService(calendar, bot, reminder_store)
            dispatcher = create_dispatcher(settings.owner_telegram_id, calendar, agent, DraftStore(engine), reminders)
            # create_task запускает фоновую корутину в том же event loop.
            worker = asyncio.create_task(reminders.run(), name="calendar-reminders")
            try:
                await dispatcher.start_polling(
                    bot,
                    allowed_updates=dispatcher.resolve_used_update_types(),
                    close_bot_session=False,
                    handle_as_tasks=False,
                    # Сообщения последовательны, но фоновая задача независима.
                )
            finally:
                # Сначала останавливаем worker, затем закрываются Bot и БД.
                worker.cancel()
                with suppress(asyncio.CancelledError):
                    await worker
    finally:
        # finally выполняется и при ошибке; освобождаем пул соединений.
        await engine.dispose()


def main() -> int:
    # Синхронная точка входа. asyncio.run запускает event loop и корутину run().
    # Возвращаем код завершения: 0 — успех, 1 — ошибка запуска.
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        settings = Settings()
        migrate()
        asyncio.run(run(settings))
    except ValidationError as exc:
        fields = ", ".join(".".join(map(str, e["loc"])) for e in exc.errors())
        logger.error("Invalid configuration fields: %s. Check .env", fields)
        return 1
    except KeyboardInterrupt:
        logger.info("Stopped")
    except Exception as exc:
        logger.error("Startup failed (%s). Check configuration and service availability.", type(exc).__name__)
        return 1
    return 0


if __name__ == "__main__":
    # Блок выполняется при запуске python -m app, но не при импорте модуля.
    sys.exit(main())
