import logging
from datetime import datetime
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware, Dispatcher, Router
from aiogram.enums import ChatType
from aiogram.filters import Command
from aiogram.utils.chat_action import ChatActionSender
from aiogram.types import Message, TelegramObject, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from app.drafts import DraftStore, EventDraft
from app.changes import EventChange
from app.voice import VoiceService, VoiceError

logger = logging.getLogger(__name__)
from app.calendar import CalendarService, CalendarError, CalendarConflict, day_bounds, format_events
from app.agent import Agent, AgentError


class OwnerOnlyMiddleware(BaseMiddleware):
    # Middleware — перехватчик перед обработчиком (похож на servlet filter).
    # Пропускает только личные сообщения владельца.
    def __init__(self, owner_id: int) -> None:
        self.owner_id = owner_id

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        # __call__ позволяет вызывать экземпляр как функцию: middleware(...).
        # handler — следующий обработчик, data — его зависимости от aiogram.
        if not isinstance(event, Message):
            return None
        if (
            event.chat.type != ChatType.PRIVATE
            or event.from_user is None
            or event.from_user.id != self.owner_id
        ):
            return None
        return await handler(event, data)


def create_dispatcher(owner_id: int, calendar: CalendarService | None = None, agent: Agent | None = None, drafts: DraftStore | None = None, reminders=None) -> Dispatcher:
    # Собираем маршруты входящих событий. Dispatcher — диспетчер,
    # Router — набор обработчиков. Вложенные функции используют зависимости
    # этой функции через замыкание, поэтому глобальные сервисы не нужны.
    dispatcher = Dispatcher()
    router = Router(name="personal-assistant")
    router.message.outer_middleware(OwnerOnlyMiddleware(owner_id))
    voice_service = VoiceService()

    @router.message(Command("reminders_on", "reminders_off"))
    async def toggle_reminders(message: Message) -> None:
        if reminders is None:
            await message.answer("Напоминания ещё не настроены.")
            return
        enabled = message.text.split()[0].split("@")[0] == "/reminders_on"
        await reminders.set_enabled(enabled)
        await message.answer("Напоминания включены: примерно за 15 минут до встречи, пока бот запущен."
                             if enabled else "Напоминания выключены.")

    @router.callback_query()
    async def confirm_event(query: CallbackQuery) -> None:
        # callback_query приходит при нажатии inline-кнопки.
        # Middleware сообщений сюда не применяется: владельца проверяем отдельно.
        if (query.from_user.id != owner_id or not isinstance(query.message, Message)
                or query.message.chat.type != ChatType.PRIVATE):
            await query.answer()
            return
        if drafts is None or calendar is None or not query.data:
            await query.answer("Действия с календарём не настроены.")
            return
        action, _, draft_id = query.data.partition(":")
        # partition делит строку на три части; "_" — ненужный разделитель.
        # Кнопка содержит лишь действие и ID, параметры встречи читаем из БД.
        if action not in ("create", "confirm", "cancel"):
            await query.answer()
            return
        await query.answer()
        # Убираем индикатор ожидания на кнопке до длительного запроса к Google.
        if action == "cancel":
            if await drafts.cancel(draft_id, owner_id):
                await query.message.edit_text("Черновик отменён. Действие с календарём не выполнялось.")
            else:
                await query.message.answer("Этот черновик уже обработан или действие началось.")
            return
        draft = await drafts.claim(draft_id, owner_id)
        if draft is None:
            previous = await drafts.get(draft_id, owner_id)
            if previous and previous["status"] == "created":
                await query.message.edit_text("Это действие уже выполнено. Актуальное состояние можно проверить запросом расписания.")
            else:
                await query.message.answer("Черновик истёк, отменён или обрабатывается. Для повтора после ошибки подожди две минуты; для истёкшего черновика отправь новый запрос.")
            return
        try:
            await calendar.apply_draft(draft)
            # Фиксируем успех только после ответа Google. При сетевом сбое
            # оставляем возможность повторно проверить результат тем же ID.
            await drafts.finish(draft_id)
        except CalendarConflict as exc:
            await drafts.invalidate(draft_id)
            await query.message.edit_text(str(exc))
            return
        except CalendarError as exc:
            await query.message.answer(str(exc))
            return
        start = draft["starts_at"].astimezone(calendar.timezone)
        end = draft["ends_at"].astimezone(calendar.timezone)
        label = {"create": "Создано в Google Calendar", "move": "Встреча перенесена",
                 "delete": "Встреча удалена или уже отсутствовала в календаре"}[draft["operation"]]
        await query.message.edit_text(f"{label}: {draft['title']}\n{start:%d.%m.%Y %H:%M} — {end:%d.%m.%Y %H:%M}\n{calendar.timezone}")

    @router.message(Command("start", "help"))
    async def help_message(message: Message) -> None:
        # @router.message(...) регистрирует функцию для подходящих сообщений:
        # по смыслу похоже на mapping-аннотации контроллера Spring.
        await message.answer(
            "Личный помощник запущен.\n\n"
            "/ping — проверить связь\n"
            "/help — показать команды\n"
            "/today — события Google Calendar за сегодня\n"
            "/reset — забыть контекст диалога\n\n"
            "/reminders_on — включить напоминания за 15 минут\n"
            "/reminders_off — выключить напоминания\n\n"
            "Можно спросить: «Что у меня завтра?» или «Покажи встречи на неделю».\n"
            "Можно отправить голосовое на русском до 60 секунд: распознавание локальное, затем текст получает облачная модель.\n"
            "Для создания: «Добавь завтра в 15:00 встречу с командой на час». Затем подтверди кнопкой.\n"
            "Для переноса: «Перенеси завтрашнюю встречу с командой на 16:00».\n"
            "Для удаления: «Отмени завтра встречу Проверка бота». Действия требуют подтверждения.\n"
            "Перенос и удаление пока доступны для обычных встреч без участников и повторений.\n"
            "Обычные сообщения обрабатываются облачной моделью; для ответа ей передаются названия и время встреч."
        )

    @router.message(Command("ping"))
    async def ping(message: Message) -> None:
        await message.answer("pong")

    @router.message(Command("today"))
    async def today(message: Message) -> None:
        if calendar is None:
            await message.answer("Google Calendar ещё не настроен.")
            return
        start, end = day_bounds(datetime.now(calendar.timezone), calendar.timezone)
        try:
            events = await calendar.list_events(start, end)
        except CalendarError as exc:
            await message.answer(str(exc))
            return
        for chunk in format_events(events, start, calendar.timezone):
            await message.answer(chunk)

    @router.message(Command("reset"))
    async def reset(message: Message) -> None:
        if agent:
            await agent.reset()
        await message.answer("Сохранённая история диалога очищена. Встречи и черновики не удалены.")

    @router.message()
    async def unknown_message(message: Message) -> None:
        # Последний обработчик получает сообщения, не перехваченные командами.
        # Он связывает Telegram, Agent и сохранение черновика.
        request_text = message.text
        if message.voice:
            await message.answer("Распознаю голосовое на компьютере…")
            try:
                async with ChatActionSender.typing(bot=message.bot, chat_id=message.chat.id):
                    request_text = await voice_service.transcribe(message.bot, message.voice)
            except VoiceError as exc:
                await message.answer(str(exc))
                return
            for i in range(0, len(request_text), 1800):
                await message.answer("Распознал: " + request_text[i:i + 1800])
        if not request_text or (not message.voice and request_text.startswith("/")):
            await message.answer("Отправь текстовый вопрос или используй /help.")
            return
        if agent is None:
            await message.answer("Модель пока не настроена. Используй /today.")
            return
        await message.answer("Обрабатываю запрос…")
        try:
            async with ChatActionSender.typing(bot=message.bot, chat_id=message.chat.id):
                answer = await agent.reply(request_text)
        except AgentError as exc:
            answer = str(exc)
        if isinstance(answer, EventDraft):
            # isinstance похож на instanceof. EventChange тоже пройдёт проверку,
            # потому что наследует EventDraft.
            if drafts is None or calendar is None:
                await message.answer("Действия с календарём ещё не настроены.")
                return
            draft_id = await drafts.save(owner_id, calendar.calendar_id, answer, request_text)
            start = answer.start.astimezone(calendar.timezone)
            end = answer.end.astimezone(calendar.timezone)
            operation = getattr(answer, "operation", "create")
            button = {"create": "Создать", "move": "Перенести", "delete": "Удалить встречу"}[operation]
            prompt = f"{button}?\n{answer.title}\n"
            if isinstance(answer, EventChange) and operation == "move":
                old_start = answer.original_start.astimezone(calendar.timezone)
                old_end = answer.original_end.astimezone(calendar.timezone)
                prompt += f"Было: {old_start:%d.%m.%Y %H:%M} — {old_end:%d.%m.%Y %H:%M}\nБудет: "
            prompt += f"{start:%d.%m.%Y %H:%M} — {end:%d.%m.%Y %H:%M}\n"
            await message.answer(
                prompt +
                f"Часовой пояс: {calendar.timezone}\nЧерновик действует 15 минут.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(text=button, callback_data=f"confirm:{draft_id}"),
                    InlineKeyboardButton(text="Не выполнять", callback_data=f"cancel:{draft_id}")]]))
            return
        for i in range(0, len(answer), 1800):
            await message.answer(answer[i:i + 1800])

    @dispatcher.errors()
    async def on_error(event: Any) -> bool:
        # Последний рубеж обработки исключений. True означает «обработано».
        # Do not log message contents, API request URLs or exception payloads.
        logger.error("Update failed: %s", type(event.exception).__name__)
        return True

    dispatcher.include_router(router)
    return dispatcher
