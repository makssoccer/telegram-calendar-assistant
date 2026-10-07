"""Persistent event drafts; Google event IDs remain stable across retries."""
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import text
from app.history import HistoryStore, action_receipt


class EventDraft(BaseModel):
    # DTO встречи: Field задаёт ограничения, extra="forbid" запрещает
    # неизвестные поля. Pydantic проверяет данные при создании объекта.
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=300)
    start: AwareDatetime
    end: AwareDatetime

    @model_validator(mode="after")
    def validate_interval(self):
        self.title = self.title.strip()
        if not self.title or not timedelta(0) < self.end - self.start <= timedelta(days=7):
            raise ValueError("Provide a title and a positive interval of at most seven days")
        return self


class DraftStore:
    # Repository черновиков. Черновик — намерение, а не выполненное действие.
    # PostgreSQL сохраняет параметры и состояние при перезапуске бота.
    def __init__(self, engine):
        self.engine = engine

    async def save(self, owner, calendar_id, draft, request_text=None):
        # Постоянный UUID также служит Google ID при создании новой встречи.
        # Для переноса/удаления target_event_id — ID существующего события.
        draft_id = uuid4().hex
        async with self.engine.begin() as c:
            # begin(): COMMIT при успешном выходе, ROLLBACK при исключении.
            # Черновик и его запись в историю фиксируются одной транзакцией.
            await c.execute(text("""INSERT INTO event_drafts
                (id, owner_id, calendar_id, title, starts_at, ends_at, expires_at, status,
                 operation, target_event_id, target_etag, original_start, original_end)
                VALUES (:id, :owner, :calendar, :title, :start, :end, :expires, 'pending',
                        :operation, :target, :etag, :original_start, :original_end)"""),
                dict(id=draft_id, owner=owner, calendar=calendar_id, title=draft.title,
                     start=draft.start, end=draft.end,
                     # getattr читает атрибут или значение по умолчанию:
                     # operation есть у EventChange, но нет у EventDraft.
                     operation=getattr(draft, "operation", "create"),
                     target=getattr(draft, "target_event_id", None), etag=getattr(draft, "target_etag", None),
                     original_start=getattr(draft, "original_start", None), original_end=getattr(draft, "original_end", None),
                     expires=datetime.now(timezone.utc) + timedelta(minutes=15)))
            if request_text is not None:
                await HistoryStore(self.engine, owner).append([
                    {"role": "user", "content": f"[{datetime.now(timezone.utc).isoformat()}] {request_text}"},
                    {"role": "assistant", "content": "Подготовлен только черновик: " + draft.model_dump_json() + ". Требуется кнопка подтверждения."},
                ], c)
        return draft_id

    async def get(self, draft_id, owner):
        async with self.engine.connect() as c:
            row = await c.execute(text("SELECT * FROM event_drafts WHERE id=:id AND owner_id=:owner"),
                                  dict(id=draft_id, owner=owner))
            return row.mappings().first()

    async def claim(self, draft_id, owner):
        # Одним UPDATE забираем действие в работу: два нажатия не должны
        # выполнить его одновременно. creating означает «выполняется»
        # и для переноса/удаления. Через 2 минуты разрешён повтор после сбоя;
        # CalendarService сначала проверит результат предыдущей попытки.
        async with self.engine.begin() as c:
            result = await c.execute(text("""UPDATE event_drafts SET status='creating', updated_at=now()
                WHERE id=:id AND owner_id=:owner AND
                ((status='pending' AND expires_at>now() AND (operation='delete' OR starts_at>now())) OR
                 (status='creating' AND updated_at<now()-interval '2 minutes')) RETURNING *"""),
                dict(id=draft_id, owner=owner))
            return result.mappings().first()

    async def invalidate(self, draft_id):
        # Запрещаем повторно применять устаревшее подтверждение.
        async with self.engine.begin() as c:
            await c.execute(text("UPDATE event_drafts SET status='invalid' WHERE id=:id AND status='creating'"),
                            dict(id=draft_id))

    async def finish(self, draft_id):
        # created — историческое имя статуса «завершено» для всех операций.
        # RETURNING возвращает изменённую строку. При повторном вызове строки
        # нет, поэтому результат не добавляется в историю повторно.
        async with self.engine.begin() as c:
            result = await c.execute(text("""UPDATE event_drafts SET status='created', updated_at=now()
                WHERE id=:id AND status='creating' RETURNING *"""), dict(id=draft_id))
            draft = result.mappings().first()
            if draft:
                await HistoryStore(self.engine, draft["owner_id"]).append(action_receipt(draft, "created"), c)

    async def cancel(self, draft_id, owner):
        # Отменяем ЧЕРНОВИК. Эта функция ничего не удаляет из Google.
        async with self.engine.begin() as c:
            result = await c.execute(text("""UPDATE event_drafts SET status='cancelled'
                WHERE id=:id AND owner_id=:owner AND status='pending' RETURNING *"""),
                dict(id=draft_id, owner=owner))
            draft = result.mappings().first()
            if draft:
                await HistoryStore(self.engine, owner).append(action_receipt(draft, "cancelled"), c)
            return draft is not None
