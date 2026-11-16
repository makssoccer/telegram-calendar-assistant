"""Общий бюджет одного действия и долговечные ограничения для всех воркеров."""
import json
from contextlib import asynccontextmanager
from contextvars import ContextVar
from functools import wraps

from aiogram.enums import ChatType
from sqlalchemy import text

MAX_CALLS = 4
MAX_PAYLOAD_BYTES = 64_000
MAX_TOOL_BYTES = 8_000
USER_CALLS_PER_MINUTE = 8
GLOBAL_CALLS_PER_DAY = 100
GLOBAL_UNITS_PER_DAY = 1_000_000
# Отдельный ключ advisory lock в общей БД приложения.
MODEL_LOCK = 7338420193501
_remaining = ContextVar("remaining_model_calls", default=None)


class AgentError(Exception):
    pass


def bounded_action(fn):
    @wraps(fn)
    async def wrapped(*args, **kwargs):
        if _remaining.get() is not None:
            return await fn(*args, **kwargs)
        token = _remaining.set(MAX_CALLS)
        try:
            return await fn(*args, **kwargs)
        finally:
            _remaining.reset(token)
    return wrapped


def consume_call():
    remaining = _remaining.get()
    if not remaining:
        raise AgentError("Исчерпан общий бюджет обращений к модели.")
    _remaining.set(remaining - 1)


def tool_content(result):
    def encode():
        return json.dumps(result, ensure_ascii=False, separators=(",", ":"))
    value = encode()
    if len(value.encode()) <= MAX_TOOL_BYTES:
        return value
    if isinstance(result.get("events"), list):
        result = dict(result, events=result["events"][:], truncated=True)
        while result["events"]:
            result["events"].pop()
            value = encode()
            if len(value.encode()) <= MAX_TOOL_BYTES:
                return value
    return '{"error":"Tool output too large. Request a narrower range."}'


class UsageGuard:
    def __init__(self, engine, owner):
        self.engine = engine
        self.owner = owner

    async def on_update(self, handler, update, data):
        event = update.callback_query or update.message
        message = update.callback_query.message if update.callback_query else update.message
        if (not event or not event.from_user or event.from_user.id != self.owner
                or not message or message.chat.type != ChatType.PRIVATE):
            return
        async with self.engine.begin() as c:
            await c.execute(text("DELETE FROM telegram_receipts WHERE seen < now() - interval '7 days'"))
            fresh = await c.scalar(text("""INSERT INTO telegram_receipts (bot, update_id)
                VALUES (:bot,:uid) ON CONFLICT DO NOTHING RETURNING 1"""),
                {"bot": data["bot"].id, "uid": update.update_id})
        # Сначала фиксируем получение. После аварии повторная доставка не запустит действие снова.
        if fresh:
            return await handler(update, data)

    @asynccontextmanager
    async def admission(self, units):
        # Блокировка на время HTTP-вызова: один запрос на все процессы этой БД.
        async with self.engine.begin() as lock:
            if not await lock.scalar(text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": MODEL_LOCK}):
                raise AgentError("Модель уже обрабатывает другой запрос. Попробуй позже.")
            # Резервируем отдельно, чтобы таймаут/падение процесса не вернули бюджет.
            async with self.engine.begin() as c:
                await c.execute(text("DELETE FROM model_reservations WHERE sent < now() - interval '25 hours'"))
                row = (await c.execute(text("""SELECT
                    count(*) FILTER (WHERE owner=:owner AND sent > now()-interval '1 minute'),
                    count(*), coalesce(sum(units),0)
                    FROM model_reservations WHERE sent > now()-interval '24 hours'"""),
                    {"owner": self.owner})).one()
                if (row[0] >= USER_CALLS_PER_MINUTE or row[1] >= GLOBAL_CALLS_PER_DAY
                        or row[2] + units > GLOBAL_UNITS_PER_DAY):
                    raise AgentError("Достигнут лимит запросов или суточный бюджет модели.")
                await c.execute(text("INSERT INTO model_reservations(owner,units) VALUES (:owner,:units)"),
                                {"owner": self.owner, "units": units})
            yield
