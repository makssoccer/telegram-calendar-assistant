"""Telegram-сценарий загрузки знаний, независимый от вызовов LLM."""
from pathlib import PurePath

from aiogram import F
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton

from app.knowledge import BoundedBuffer, KnowledgeError, MAX_BYTES, PLACES

PROMPTS = {
    'title': 'Шаг 2/4. Напиши короткое название материала. Например: История Старого замка.',
    'source': 'Шаг 3/4. Отправь только ссылку на источник или его краткое описание (до 300 символов). Например: название книги, автор, страница. Саму статью пока не отправляй.',
    'awaiting': 'Шаг 4/4. Теперь отправь сам текст статьи отдельным сообщением или файлом TXT/MD в UTF-8 (до 100 КБ и 20 000 символов). Затем покажу подтверждение.',
}


def place_buttons(identifier):
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=name, callback_data=f'kbp:{identifier}:{key}')]
        for key, name in PLACES.items()])

HELP = ("Добавление материала о месте:\n"
        "/learn old_castle | История замка | Название книги или URL источника\n\n"
        "Затем отправь текст одним сообщением либо файл .txt/.md в UTF-8 (до 100 КБ и 20 000 символов). "
        "После просмотра подтверди добавление кнопкой. Места: /places.\n"
        "Материал хранится локально в PostgreSQL. При вопросе по нему найденные фрагменты "
        "могут передаваться облачной ИИ-модели.\n"
        "/cancel_material — отменить загрузку; /materials — сохранённые материалы.")


def buttons(items):
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=label, callback_data=data) for label, data in items]])


def register_knowledge_handlers(router, store, owner_id):
    # Регистрируем до общего обработчика текста и календарных callback.
    # Сообщения уже защищены OwnerOnlyMiddleware родительского router.
    @router.message(Command("learn"))
    async def start(message: Message):
        fields = (message.text or message.caption or "").split(maxsplit=1)
        if len(fields) != 2:
            identifier = await store.start_wizard()
            await message.answer("Шаг 1/4. О каком месте материал? Выбери кнопку или напиши название следующим сообщением, например: Старый замок.\n\n"
                                 "Далее спрошу название, источник и текст. /cancel_material — отмена. "
                                 "Загрузка действует 30 минут. После подтверждения найденные фрагменты могут передаваться облачной модели.",
                                 reply_markup=place_buttons(identifier))
            return
        parts = [p.strip() for p in fields[1].split("|", 2)]
        if len(parts) == 1:
            identifier = await store.start_wizard()
            try:
                stage = await store.advance(identifier, 'choose_place', parts[0])
                await message.answer(PROMPTS[stage])
            except KnowledgeError as exc:
                await message.answer(str(exc), reply_markup=place_buttons(identifier))
            return
        if len(parts) != 3:
            await message.answer(HELP)
            return
        try:
            identifier = await store.begin(*parts)
            if message.document:
                await ingest(message, {"id": identifier})
            else:
                await message.answer("Жду материал следующим сообщением. После загрузки покажу подтверждение. "
                                     "Черновик действует 30 минут. /cancel_material — отменить.\n"
                                     "После подтверждения фрагменты могут использоваться облачной моделью для ответов.")
        except KnowledgeError as exc:
            await message.answer(str(exc))

    @router.message(Command("place", "places"))
    async def places(message: Message):
        fields = (message.text or '').split(maxsplit=1)
        pending = await store.pending()
        if pending and pending['status'] == 'choose_place':
            if len(fields) == 2:
                try:
                    stage = await store.advance(pending['id'], 'choose_place', fields[1])
                    await message.answer(PROMPTS[stage])
                except KnowledgeError as exc:
                    await message.answer(str(exc), reply_markup=place_buttons(pending['id']))
            else:
                await message.answer("Выбери место кнопкой или напиши его название следующим сообщением.", reply_markup=place_buttons(pending['id']))
            return
        await message.answer("Места Гродно (название — короткий ID):\n\n" + '\n'.join(
            f"{name} — {key}" for key,name in PLACES.items()) +
            "\n\nДля загрузки напиши /learn: дальше место можно выбрать кнопкой или написать по-русски. "
            "Например: /learn Старый замок. /places сама по себе показывает список.\n"
            "Для прогулки: /walk 120.")

    @router.callback_query(F.data.startswith('kbp:'))
    async def choose_place(query: CallbackQuery):
        if (query.from_user.id != owner_id or not isinstance(query.message, Message)
                or query.message.chat.type != ChatType.PRIVATE):
            await query.answer()
            return
        await query.answer()
        parts = query.data.split(':')
        if len(parts) != 3:
            return
        try:
            stage = await store.advance(parts[1], 'choose_place', parts[2])
            await query.message.edit_text(f"Место: {PLACES[parts[2]]}\n\n{PROMPTS[stage]}")
        except KnowledgeError as exc:
            await query.message.answer(str(exc))

    @router.message(Command("cancel_material"))
    async def cancel(message: Message):
        await store.cancel()
        await message.answer("Загрузка отменена. Подтверждённые материалы сохранены.")

    @router.message(Command("materials"))
    async def listing(message: Message):
        parts = message.text.split()
        try:
            page = int(parts[1]) if len(parts) == 2 else 1
            if page < 1 or len(parts) > 2:
                raise ValueError()
        except ValueError:
            await message.answer("Пример: /materials или /materials 2.")
            return
        rows = await store.list_documents()
        selected = rows[(page-1)*10:page*10]
        if not selected:
            await message.answer("На этой странице нет материалов. Добавить: /learn.")
            return
        await message.answer(f"Твоих материалов: {len(rows)}. Страница {page}. "
                             "Встроенные карточки города здесь не показаны.")
        for row in selected:
            await message.answer(f"{row['title']}\nМесто: {PLACES[row['place_id']]}\n"
                                 f"Источник: {row['source']}\nID: {row['id'][:8]}",
                                 disable_web_page_preview=True,
                                 reply_markup=buttons([("Удалить…", f"kb:remove:{row['id']}")]))
        if len(rows) > page*10:
            await message.answer(f"Следующая страница: /materials {page+1}")

    @router.callback_query(F.data.startswith("kb:"))
    async def action(query: CallbackQuery):
        if (query.from_user.id != owner_id or not isinstance(query.message, Message)
                or query.message.chat.type != ChatType.PRIVATE):
            await query.answer()
            return
        await query.answer()
        parts = query.data.split(":")
        if len(parts) != 3:
            return
        _, operation, identifier = parts
        if operation == "add":
            result = await store.confirm(identifier)
        elif operation == "cancel":
            await store.cancel(identifier)
            result = "Черновик отменён. Если материал уже подтверждён, удалить его можно через /materials."
        elif operation == "remove":
            # Сначала отдельное подтверждение удаления. ID всегда проверяет store.
            await query.message.answer("Удалить этот материал из базы знаний? Он перестанет участвовать в поиске. "
                                       "Уже отправленные сообщения и история диалога останутся.",
                reply_markup=buttons([("Удалить", f"kb:delete:{identifier}"), ("Оставить", f"kb:keep:{identifier}")]))
            return
        elif operation == "delete":
            result = "Материал удалён." if await store.delete(identifier) else "Материал уже удалён или не найден."
        elif operation == "keep":
            result = "Материал оставлен."
        else:
            return
        await query.message.edit_text(result)

    async def material_filter(message: Message):
        value = message.text or message.caption or ""
        if value.startswith("/"):
            return False
        pending = await store.pending()
        if pending is not None or message.document:
            return {"knowledge_pending": pending}
        return False

    @router.message(material_filter)
    async def ingest(message: Message, knowledge_pending):
        if knowledge_pending is None:
            await message.answer("Чтобы добавить файл в базу знаний, сначала укажи место, название и источник: /learn.")
            return
        try:
            if knowledge_pending.get('status') in PROMPTS or knowledge_pending.get('status') == 'choose_place':
                if knowledge_pending['status'] != 'awaiting':
                    if not message.text:
                        raise KnowledgeError("Сначала закончим описание материала. На этом шаге нужен текст, не файл. /cancel_material — отмена.")
                    stage = await store.advance(knowledge_pending['id'], knowledge_pending['status'], message.text)
                    await message.answer(PROMPTS[stage])
                    return
            if message.document:
                document = message.document
                if PurePath(document.file_name or "").suffix.lower() not in (".txt", ".md"):
                    raise KnowledgeError("Пока поддерживаются .txt и .md в UTF-8. PDF, Word и изображения добавим отдельно.")
                if (document.file_size or 0) > MAX_BYTES:
                    raise KnowledgeError("Файл слишком большой: максимум 100 КБ.")
                with BoundedBuffer() as stream:
                    await message.bot.download(document, destination=stream, timeout=30)
                    value = stream.getvalue().decode("utf-8-sig")
            elif message.text:
                value = message.text
            else:
                raise KnowledgeError("Жду текст или файл .txt/.md. Чтобы вернуться к обычному диалогу: /cancel_material.")
            row = await store.preview(knowledge_pending["id"], value)
            await message.answer(f"Добавить в базу знаний?\n{row['title']}\n"
                f"Место: {PLACES[row['place_id']]}\nИсточник: {row['source']}\n"
                f"Символов: {len(row['body'])}; фрагментов: {len(row['chunks'])}.\n\n"
                f"Начало текста:\n{row['body'][:650]}\n\n"
                "Это материал пользователя; достоверность источника автоматически не проверяется. "
                "После подтверждения найденные фрагменты могут передаваться облачной модели.",
                disable_web_page_preview=True,
                reply_markup=buttons([("Добавить", f"kb:add:{row['id']}"), ("Отмена", f"kb:cancel:{row['id']}")]))
        except UnicodeDecodeError:
            await message.answer("Не удалось прочитать UTF-8. Сохрани файл в этой кодировке и отправь снова.")
        except KnowledgeError as exc:
            await message.answer(str(exc))
        except (TelegramAPIError, TimeoutError, OSError):
            await message.answer("Не удалось скачать файл. Отправь его повторно или используй текст.")
