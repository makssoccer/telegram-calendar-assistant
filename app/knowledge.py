"""Загрузка знаний: черновик → подтверждение → доступность для retrieval."""
import hashlib
import io
import json
import re
from uuid import uuid4

from sqlalchemy import text
from app.tourism import load_catalog

MAX_BYTES = 100_000
MAX_CHARS = 20_000
PLACES = {p["id"]: p["name"] for p in load_catalog()["places"]}


def resolve_place(value):
    normalized = " ".join(value.lower().strip().split())
    aliases = {"старый замок": "old_castle", "новый замок": "new_castle",
               "университет": "university", "гргу": "university", "вокзал": "station",
               "драмтеатр": "drama_theatre"}
    return next((key for key, name in PLACES.items() if normalized in (key, name.lower())), aliases.get(normalized))


class KnowledgeError(Exception):
    pass


class BoundedBuffer(io.BytesIO):
    # Проверяем реальные загруженные байты, а не только метаданные Telegram.
    def write(self, data):
        if self.tell() + len(data) > MAX_BYTES:
            raise KnowledgeError("Файл слишком большой: максимум 100 КБ.")
        return super().write(data)


def prepare_text(value: str) -> tuple[str, list[str], str]:
    value = value.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not 40 <= len(value) <= MAX_CHARS:
        raise KnowledgeError("Материал должен содержать от 40 до 20 000 символов.")
    if any(ord(c) < 32 and c not in "\n\t" for c in value):
        raise KnowledgeError("В файле есть нетекстовые символы. Нужен обычный текст UTF-8.")
    # Простое разбиение с перекрытием: граница предложения по возможности,
    # иначе слова. Перекрытие сохраняет контекст возле границы фрагментов.
    chunks, start = [], 0
    while start < len(value):
        end = min(start + 1000, len(value))
        if end < len(value):
            split = max(value.rfind("\n", start + 500, end), value.rfind(". ", start + 500, end))
            if split < 0:
                split = value.rfind(" ", start + 500, end)
            if split >= 0:
                end = split + 1
        chunks.append(value[start:end].strip())
        if end == len(value):
            break
        start = end - 100
    digest = hashlib.sha256(re.sub(r"\s+", " ", value).encode()).hexdigest()
    return value, chunks, digest


class KnowledgeStore:
    # Один store привязан к владельцу: его документы не смешиваются с чужими.
    def __init__(self, engine, owner_id: int):
        self.engine, self.owner_id = engine, owner_id

    async def _lock(self, connection):
        # Транзакционная блокировка сериализует подтверждения и защищает лимит
        # и проверку дублей даже при двух параллельных нажатиях.
        await connection.execute(text("SELECT pg_advisory_xact_lock(:owner)"), {"owner": self.owner_id})

    async def begin(self, place_id: str, title: str, source: str) -> str:
        place_id = resolve_place(place_id)
        if place_id is None:
            raise KnowledgeError("Не узнал место. Напиши /learn и выбери место кнопкой, например «Старый замок».")
        if not 1 <= len(title.strip()) <= 150:
            raise KnowledgeError("Название должно содержать от 1 до 150 символов.")
        if not 1 <= len(source.strip()) <= 300:
            raise KnowledgeError("В поле «Источник» нужна только ссылка или краткое название источника (до 300 символов). Саму статью отправь следующим сообщением. Для пошагового ввода: /learn.")
        if any(ord(c) < 32 for c in title + source):
            raise KnowledgeError("Название и источник должны быть в одной строке.")
        identifier = uuid4().hex
        async with self.engine.begin() as c:
            await self._lock(c)
            # Неподтверждённые материалы можно заменить новой загрузкой.
            await c.execute(text("DELETE FROM knowledge_documents WHERE owner_id=:owner AND status!='active'"), {"owner": self.owner_id})
            await c.execute(text("""INSERT INTO knowledge_documents
                (id, owner_id, place_id, title, source, status, expires_at)
                VALUES (:id,:owner,:place,:title,:source,'awaiting',now()+interval '30 minutes')"""),
                {"id": identifier, "owner": self.owner_id, "place": place_id, "title": title.strip(), "source": source.strip()})
        return identifier

    async def start_wizard(self):
        identifier = uuid4().hex
        async with self.engine.begin() as c:
            await self._lock(c)
            await c.execute(text("DELETE FROM knowledge_documents WHERE owner_id=:owner AND status!='active'"), {"owner": self.owner_id})
            await c.execute(text("""INSERT INTO knowledge_documents
                (id,owner_id,place_id,title,source,status,expires_at)
                VALUES (:id,:owner,'','','','choose_place',now()+interval '30 minutes')"""),
                {"id": identifier, "owner": self.owner_id})
        return identifier

    async def advance(self, identifier, stage, value):
        if stage == 'choose_place':
            value = resolve_place(value)
            if value is None:
                raise KnowledgeError("Не узнал место. Выбери кнопку или напиши, например: Старый замок.")
            field, following = 'place_id', 'title'
        elif stage == 'title':
            field, following = 'title', 'source'
            if not 1 <= len(value.strip()) <= 150:
                raise KnowledgeError("Нужно короткое название материала, до 150 символов. Сам текст отправим позже.")
        elif stage == 'source':
            field, following = 'source', 'awaiting'
            if not 1 <= len(value.strip()) <= 300:
                raise KnowledgeError("Сейчас нужна только ссылка или краткое описание источника, до 300 символов. Текст статьи будет следующим шагом.")
        else:
            raise KnowledgeError("Этот шаг уже пройден. Начать заново: /learn.")
        if any(ord(c)<32 for c in value):
            raise KnowledgeError("Введи значение одной строкой. Текст статьи бот попросит отдельно.")
        # Имя поля выбирает код выше, а значение передаётся SQL-параметром.
        async with self.engine.begin() as c:
            result = await c.execute(text(f"""UPDATE knowledge_documents SET {field}=:value,status=:following
                WHERE id=:id AND owner_id=:owner AND status=:stage AND expires_at>now()"""),
                {"id":identifier,"owner":self.owner_id,"stage":stage,"value":value.strip(),"following":following})
        if not result.rowcount:
            raise KnowledgeError("Этот шаг уже пройден или загрузка истекла. Начать заново: /learn.")
        return following

    async def pending(self):
        async with self.engine.begin() as c:
            await c.execute(text("DELETE FROM knowledge_documents WHERE owner_id=:owner AND status!='active' AND expires_at<=now()"), {"owner": self.owner_id})
            return (await c.execute(text("SELECT * FROM knowledge_documents WHERE owner_id=:owner AND status NOT IN ('active','review') ORDER BY created_at DESC LIMIT 1"), {"owner": self.owner_id})).mappings().first()

    async def preview(self, identifier: str, value: str):
        body, chunks, digest = prepare_text(value)
        async with self.engine.begin() as c:
            row = (await c.execute(text("""UPDATE knowledge_documents SET body=:body,
                chunks=CAST(:chunks AS jsonb),digest=:digest,status='review'
                WHERE id=:id AND owner_id=:owner AND status='awaiting' AND expires_at>now() RETURNING *"""),
                {"id": identifier, "owner": self.owner_id, "body": body, "chunks": json.dumps(chunks, ensure_ascii=False), "digest": digest})).mappings().first()
        if row is None:
            raise KnowledgeError("Загрузка истекла или заменена. Начни заново: /learn.")
        return row

    async def confirm(self, identifier: str) -> str:
        async with self.engine.begin() as c:
            await self._lock(c)
            row = (await c.execute(text("SELECT * FROM knowledge_documents WHERE id=:id AND owner_id=:owner FOR UPDATE"), {"id": identifier, "owner": self.owner_id})).mappings().first()
            if not row:
                return "Материал не найден или отменён."
            if row["status"] == "active":
                return "Этот материал уже добавлен в базу знаний."
            valid = (await c.execute(text("SELECT expires_at>now() FROM knowledge_documents WHERE id=:id"), {"id": identifier})).scalar()
            if row["status"] != "review" or not valid:
                return "Черновик не готов или истёк. Начни заново: /learn."
            duplicate = (await c.execute(text("SELECT id FROM knowledge_documents WHERE owner_id=:owner AND place_id=:place AND digest=:digest AND status='active'"), {"owner": self.owner_id, "place": row["place_id"], "digest": row["digest"]})).scalar()
            if duplicate:
                await c.execute(text("DELETE FROM knowledge_documents WHERE id=:id"), {"id": identifier})
                return "Такой текст для этого места уже есть в базе. Дубликат не добавлен."
            count = (await c.execute(text("SELECT count(*) FROM knowledge_documents WHERE owner_id=:owner AND status='active'"), {"owner": self.owner_id})).scalar()
            if count >= 100:
                return "Достигнут лимит 100 материалов. Удали ненужный через /materials."
            await c.execute(text("UPDATE knowledge_documents SET status='active' WHERE id=:id"), {"id": identifier})
        return f"Добавлено: {row['title']}. Фрагментов: {len(row['chunks'])}. Теперь можно спросить об этом месте."

    async def cancel(self, identifier=None):
        async with self.engine.begin() as c:
            await c.execute(text("DELETE FROM knowledge_documents WHERE owner_id=:owner AND status!='active' AND (CAST(:id AS text) IS NULL OR id=:id)"), {"owner": self.owner_id, "id": identifier})

    async def list_documents(self):
        async with self.engine.connect() as c:
            return (await c.execute(text("SELECT id,title,place_id,source FROM knowledge_documents WHERE owner_id=:owner AND status='active' ORDER BY created_at DESC"), {"owner": self.owner_id})).mappings().all()

    async def delete(self, identifier):
        async with self.engine.begin() as c:
            result = await c.execute(text("DELETE FROM knowledge_documents WHERE id=:id AND owner_id=:owner AND status='active'"), {"id": identifier, "owner": self.owner_id})
        return result.rowcount > 0

    async def retrieve_documents(self, place_id: str) -> list[dict]:
        # Один загруженный материал хранится одной строкой с массивом chunks.
        # На выходе каждый фрагмент получает свой ID из ID материала и номера
        # в массиве. Благодаря этому рассказчик может сослаться на конкретную выдержку.
        async with self.engine.connect() as c:
            rows = (await c.execute(text("SELECT id,title,source,chunks,created_at FROM knowledge_documents WHERE owner_id=:owner AND place_id=:place AND status='active' ORDER BY created_at DESC"), {"owner": self.owner_id, "place": place_id})).mappings().all()
        return [{"id": f"kb_{r['id']}_{i}", "place_id": place_id,
                 "title": r["title"], "text": chunk,
                 "url": f"Материал пользователя {r['id'][:8]}, фрагмент {i+1}; источник: {r['source']}",
                 "uploaded_at": r["created_at"].isoformat()}
                for r in rows for i, chunk in enumerate(r["chunks"])]
