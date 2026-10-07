"""Напоминания без участия модели: Google Calendar → PostgreSQL → Telegram."""
import asyncio
import logging
import math
from datetime import datetime, timedelta, timezone

from aiogram.exceptions import TelegramRetryAfter
from sqlalchemy import text

logger = logging.getLogger(__name__)


class ReminderStore:
    # Repository настроек и попыток доставки. Уникальный ключ включает время
    # начала: перенос на другое время позволяет отправить новое напоминание.
    def __init__(self, engine, owner_id, calendar_id):
        self.engine = engine
        self.owner_id = owner_id
        self.calendar_id = calendar_id

    async def initialize(self):
        async with self.engine.begin() as c:
            await c.execute(text("""INSERT INTO reminder_settings VALUES (:owner, true)
                ON CONFLICT (owner_id) DO NOTHING"""), {"owner": self.owner_id})

    async def enabled(self):
        async with self.engine.connect() as c:
            result = await c.execute(text("SELECT enabled FROM reminder_settings WHERE owner_id=:owner"),
                                     {"owner": self.owner_id})
            return result.scalar_one_or_none() is True

    async def set_enabled(self, enabled):
        async with self.engine.begin() as c:
            await c.execute(text("""INSERT INTO reminder_settings VALUES (:owner, :enabled)
                ON CONFLICT (owner_id) DO UPDATE SET enabled=EXCLUDED.enabled"""),
                {"owner": self.owner_id, "enabled": enabled})

    def key(self, event_id, start):
        return dict(owner=self.owner_id, calendar=self.calendar_id, event=event_id, start=start)

    async def claim(self, event_id, start):
        # Резервируем ДО сетевой отправки. Даже после аварии второй процесс
        # не отправит то же напоминание повторно. Обратная сторона: при потере
        # ответа Telegram доставка остаётся неопределённой, без автоповтора.
        async with self.engine.begin() as c:
            result = await c.execute(text("""INSERT INTO reminder_deliveries
                (owner_id, calendar_id, event_id, starts_at, status)
                SELECT :owner, :calendar, :event, :start, 'attempted'
                WHERE EXISTS (SELECT 1 FROM reminder_settings WHERE owner_id=:owner AND enabled)
                ON CONFLICT DO NOTHING RETURNING event_id"""), self.key(event_id, start))
            return result.first() is not None

    async def mark(self, event_id, start, status):
        async with self.engine.begin() as c:
            await c.execute(text("""UPDATE reminder_deliveries SET status=:status
                WHERE owner_id=:owner AND calendar_id=:calendar AND event_id=:event AND starts_at=:start"""),
                {**self.key(event_id, start), "status": status})

    async def release(self, event_id, start):
        # Только для явного отказа 429: Telegram сообщил, что запрос не принят.
        async with self.engine.begin() as c:
            await c.execute(text("""DELETE FROM reminder_deliveries
                WHERE owner_id=:owner AND calendar_id=:calendar AND event_id=:event AND starts_at=:start
                AND status='attempted'"""), self.key(event_id, start))


def due_event(event, now):
    # События «весь день» не имеют конкретного времени для уведомления.
    # Отменённые события и приглашения, отклонённые владельцем, пропускаем.
    if event.get("status") == "cancelled" or not event.get("id"):
        return None
    if any(a.get("self") and a.get("responseStatus") == "declined" for a in event.get("attendees", [])):
        return None
    try:
        start = datetime.fromisoformat(event["start"]["dateTime"])
        end = datetime.fromisoformat(event["end"]["dateTime"])
        if start.utcoffset() is None or end.utcoffset() is None or end <= start:
            return None
        if timedelta(0) < start - now <= timedelta(minutes=15):
            return start, end
    except (KeyError, TypeError, ValueError):
        pass
    return None


class ReminderService:
    # Фоновый сервис, аналог scheduled job в Java. Один цикл раз в минуту.
    # Lock не даёт команде отключения пересечься с отправкой этого экземпляра.
    def __init__(self, calendar, bot, store):
        self.calendar = calendar
        self.bot = bot
        self.store = store
        self._lock = asyncio.Lock()
        self._retry_at = datetime.min.replace(tzinfo=timezone.utc)

    async def set_enabled(self, enabled):
        async with self._lock:
            await self.store.set_enabled(enabled)

    async def tick(self):
        async with self._lock:
            now = datetime.now(timezone.utc)
            if now < self._retry_at or not await self.store.enabled():
                return
            # Каждый цикл берёт актуальные данные: нет очереди старых событий,
            # которая продолжала бы напоминать об удалённых встречах.
            events = await self.calendar.list_events(now, now + timedelta(minutes=15))
            for event in events:
                now = datetime.now(timezone.utc)
                interval = due_event(event, now)
                if interval is None:
                    continue
                start, end = interval
                if not await self.store.claim(event["id"], start):
                    continue
                local_start = start.astimezone(self.calendar.timezone)
                local_end = end.astimezone(self.calendar.timezone)
                minutes = max(1, math.ceil((start - now).total_seconds() / 60))
                title = " ".join(str(event.get("summary") or "Без названия").split())[:300]
                message = (f"Скоро встреча — через {minutes} мин.\n{title}\n"
                           f"{local_start:%d.%m.%Y %H:%M} — {local_end:%d.%m.%Y %H:%M}\n"
                           f"{self.calendar.timezone}")
                try:
                    await self.bot.send_message(self.store.owner_id, message)
                except TelegramRetryAfter as exc:
                    await self.store.release(event["id"], start)
                    self._retry_at = datetime.now(timezone.utc) + timedelta(seconds=exc.retry_after)
                    logger.warning("Reminder delivery rate limited")
                    break
                except Exception as exc:
                    # Не логируем текст встречи, токены и содержимое HTTP-ошибки.
                    await self.store.mark(event["id"], start, "uncertain")
                    logger.warning("Reminder delivery uncertain: %s", type(exc).__name__)
                else:
                    await self.store.mark(event["id"], start, "sent")
                    logger.info("Reminder delivered")

    async def run(self):
        while True:
            try:
                await self.tick()
            except Exception as exc:
                logger.warning("Reminder cycle failed: %s", type(exc).__name__)
            # asyncio.sleep освобождает event loop: команды бота работают
            # независимо. CancelledError при остановке не скрываем.
            await asyncio.sleep(60)
