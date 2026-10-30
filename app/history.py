"""Bounded context, updated under a database row lock."""
import json

from sqlalchemy import text


class HistoryStore:
    # Repository контекста владельца. Храним ограниченную историю, а не архив.
    # JSONB — тип PostgreSQL для JSON; в Python это список словарей.
    def __init__(self, engine, owner_id):
        self.engine = engine
        self.owner_id = owner_id

    async def load(self):
        # :owner — SQL-параметр, значение передаётся отдельно от текста SQL.
        # scalar_one_or_none() возвращает поле строки либо None (аналог null).
        async with self.engine.connect() as connection:
            result = await connection.execute(text(
                "SELECT messages FROM conversations WHERE owner_id=:owner"),
                {"owner": self.owner_id})
            return result.scalar_one_or_none() or []

    async def append(self, messages, connection=None):
        # Можно передать существующую транзакцию, чтобы сохранить контекст
        # вместе со статусом действия. Иначе открываем собственную.
        if connection is None:
            async with self.engine.begin() as connection:
                await self.append(messages, connection)
            return
        await connection.execute(text("""INSERT INTO conversations VALUES (:owner, '[]'::jsonb)
            ON CONFLICT (owner_id) DO NOTHING"""), {"owner": self.owner_id})
        # FOR UPDATE блокирует строку до конца транзакции: параллельные
        # добавления не перезапишут изменения друг друга.
        result = await connection.execute(text("""SELECT messages FROM conversations
            WHERE owner_id=:owner FOR UPDATE"""), {"owner": self.owner_id})
        history = result.scalar_one() + messages
        # Добавляем парами user + assistant. [-8:] — последние 8 элементов.
        # del history[:2] удаляет старейшую пару при превышении размера JSON.
        history = history[-8:]
        while len(json.dumps(history, ensure_ascii=False)) > 16000:
            del history[:2]
        await connection.execute(text("""UPDATE conversations SET messages=CAST(:messages AS jsonb)
            WHERE owner_id=:owner"""),
            {"owner": self.owner_id, "messages": json.dumps(history, ensure_ascii=False)})

    async def clear(self):
        # /reset удаляет только контекст, сохраняя события и черновики.
        async with self.engine.begin() as connection:
            await connection.execute(text("DELETE FROM conversations WHERE owner_id=:owner"),
                                     {"owner": self.owner_id})


def action_receipt(draft, status):
    # Кнопку обрабатывает обычный код. Программно добавляем результат
    # в разговор, чтобы следующий запрос модели знал об этом действии.
    details = json.dumps({"title": draft["title"], "start": draft["starts_at"].isoformat(),
                          "end": draft["ends_at"].isoformat(),
                          "attendees": draft.get("attendees") or []}, ensure_ascii=False)
    operation = draft.get("operation", "create")
    labels = {"create": "Создать", "move": "Перенести", "delete": "Удалить встречу"}
    results = {"create": "Google Calendar подтвердил создание встречи",
               "move": "Google Calendar подтвердил перенос встречи",
               "delete": "Google Calendar подтвердил отсутствие встречи после запроса удаления"}
    action = labels[operation] if status == "created" else "Не выполнять"
    result = results[operation] if status == "created" else "Черновик отменён, действие не выполнялось"
    if status == "created" and operation == "create" and draft.get("attendees"):
        result += "; отправка приглашений запрошена у Google, доставка писем не проверена"
    return [
        {"role": "user", "content": f"[Нажата кнопка «{action}» для встречи {details}]"},
        {"role": "assistant", "content": f"{result}: {details}. Это результат действия на тот момент; актуальное расписание проверяется через list_events."},
    ]
