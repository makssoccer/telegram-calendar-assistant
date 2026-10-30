"""Calendar integration; each worker owns its HTTP client."""
import asyncio
import json
import os
import tempfile
import time
from datetime import datetime, time as day_time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httplib2
from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_httplib2 import AuthorizedHttp
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

SCOPES = ["https://www.googleapis.com/auth/calendar.events"]
READ_SCOPES = ["https://www.googleapis.com/auth/calendar.events.readonly"]


class CalendarError(Exception):
    # Сообщение можно показывать пользователю: без токенов и сырых ответов API.
    """Safe, user-facing error."""


class CalendarConflict(CalendarError):
    # Частный вид CalendarError: старое подтверждение нужно заблокировать.
    """The reviewed draft can no longer be applied safely."""


def check_changeable(event: dict) -> None:
    # Проверка в коде, а не только в инструкции модели. Пока изменяем
    # обычные события своего календаря без повторений и участников.
    if (event.get("recurringEventId") or event.get("recurrence") or event.get("attendees")
            or event.get("attendeesOmitted") or event.get("eventType", "default") != "default"
            or not event.get("organizer", {}).get("self")
            or "dateTime" not in event.get("start", {}) or "dateTime" not in event.get("end", {})):
        raise CalendarConflict("Пока можно переносить и удалять только обычные встречи своего календаря: без повторений, участников и режима «весь день».")
    if not event.get("etag") or not event.get("id"):
        raise CalendarConflict("Не удалось определить версию встречи. Повтори запрос.")


def save_token(path: Path, credentials: Credentials) -> None:
    # Пишем сначала временный файл, затем заменяем основной целиком.
    # Это снижает риск оставить недописанный JSON при сбое записи.
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".token-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(credentials.to_json())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def day_bounds(now: datetime, timezone: ZoneInfo) -> tuple[datetime, datetime]:
    # Возвращаем пару (начало дня, начало следующего дня) в часовом поясе
    # пользователя. Конец диапазона не включается — [start, end).
    today = now.astimezone(timezone).date()
    return (
        datetime.combine(today, day_time.min, timezone),
        datetime.combine(today + timedelta(days=1), day_time.min, timezone),
    )


class CalendarService:
    # Клиент внешнего Google API (аналог integration service в Java).
    # Здесь авторизация, HTTP-запросы и защита от повторного изменения.
    def __init__(self, calendar_id: str, token_path: Path, timezone: str):
        self.calendar_id = calendar_id
        self.token_path = token_path
        self.timezone = ZoneInfo(timezone)
        self._lock = asyncio.Lock()
        # Lock — асинхронный замок: запросы этого экземпляра выполняются
        # по одному, в частности чтобы не обновлять файл токена одновременно.

    async def list_events(self, start: datetime, end: datetime) -> list[dict]:
        if start.utcoffset() is None or end.utcoffset() is None or end <= start:
            raise ValueError("An ordered timezone-aware interval is required")
        async with self._lock:
            # Google SDK синхронный. to_thread выполняет его в рабочем потоке,
            # чтобы сетевое ожидание не блокировало event loop Telegram.
            return await asyncio.to_thread(self._list_events, start, end)

    def _list_events(self, start: datetime, end: datetime) -> list[dict]:
        if not self.token_path.exists():
            raise CalendarError("Google Calendar ещё не подключён. Выполни настройку из docs/GOOGLE_CALENDAR.md.")
        try:
            credentials = Credentials.from_authorized_user_file(str(self.token_path))
            if not (credentials.has_scopes(SCOPES) or credentials.has_scopes(READ_SCOPES)):
                raise CalendarError("Нужно повторно подключить Google Calendar с доступом к событиям.")
            if not credentials.valid:
                # Короткоживущий access token обновляем через refresh token.
                # Секреты остаются в файле, модель их не получает.
                if not credentials.refresh_token:
                    raise CalendarError("Нужна повторная авторизация Google Calendar.")
                credentials.refresh(Request())
                save_token(self.token_path, credentials)
            http = AuthorizedHttp(credentials, http=httplib2.Http(timeout=15))
            try:
                service = build("calendar", "v3", http=http, cache_discovery=False)
                events, page = [], None
                deadline = time.monotonic() + 45
                while True:
                    # Google возвращает события страницами. pageToken позволяет
                    # получить следующую, пока не прочитаем весь диапазон.
                    if time.monotonic() > deadline:
                        raise CalendarError("Календарь отвечает слишком долго. Попробуй ещё раз.")
                    response = service.events().list(
                        calendarId=self.calendar_id,
                        timeMin=start.isoformat(), timeMax=end.isoformat(),
                        timeZone=str(self.timezone), singleEvents=True,
                        orderBy="startTime", showDeleted=False,
                        maxResults=250, pageToken=page,
                    ).execute(num_retries=0)
                    events.extend(e for e in response.get("items", []) if e.get("status") != "cancelled")
                    page = response.get("nextPageToken")
                    if not page:
                        break
                if credentials.token:
                    save_token(self.token_path, credentials)
                return events
            finally:
                http.http.close()
        except CalendarError:
            raise
        except RefreshError:
            raise CalendarError("Доступ Google истёк или отозван. Повтори авторизацию на компьютере.") from None
        except HttpError as exc:
            if exc.resp.status in (401, 403):
                raise CalendarError("Google отказал в доступе. Проверь Calendar API, разрешения и повтори авторизацию при необходимости.") from None
            if exc.resp.status == 404:
                raise CalendarError("Календарь не найден. Проверь GOOGLE_CALENDAR_ID.") from None
            raise CalendarError("Google Calendar временно недоступен. Попробуй позже.") from None
        except (ValueError, json.JSONDecodeError):
            raise CalendarError("Не удалось прочитать авторизацию Google. Повтори подключение.") from None
        except Exception:
            raise CalendarError("Не удалось прочитать календарь. Проверь соединение и доступ к файлу авторизации.") from None


    async def apply_draft(self, draft: dict) -> None:
        # Вызывается обработчиком подтверждения. Выбираем действие
        # по сохранённому черновику, а не по новому ответу модели.
        if draft.get("operation", "create") == "create":
            await self.create_event(draft)
        else:
            async with self._lock:
                await asyncio.to_thread(self._change_event, draft)

    def _change_event(self, draft: dict) -> None:
        try:
            credentials = Credentials.from_authorized_user_file(str(self.token_path))
            if not credentials.has_scopes(SCOPES):
                raise CalendarError("Повторно подключи Google Calendar с правом записи.")
            if not credentials.valid:
                credentials.refresh(Request())
                save_token(self.token_path, credentials)
            http = AuthorizedHttp(credentials, http=httplib2.Http(timeout=15))
            try:
                events = build("calendar", "v3", http=http, cache_discovery=False).events()
                self._apply_change(events, draft)
                save_token(self.token_path, credentials)
            finally:
                http.http.close()
        except CalendarError:
            raise
        except RefreshError:
            raise CalendarError("Повтори авторизацию Google Calendar на компьютере.") from None
        except HttpError as exc:
            if exc.resp.status == 412:
                raise CalendarConflict("Встреча изменилась после подготовки. Отправь запрос заново, чтобы проверить новые данные.") from None
            if exc.resp.status in (401, 403):
                raise CalendarError("Google отказал в изменении встречи. Проверь права доступа.") from None
            raise CalendarError("Не удалось подтвердить результат. Через две минуты повтори нажатие той же кнопки.") from None
        except Exception:
            raise CalendarError("Не удалось подтвердить результат. Через две минуты повтори нажатие той же кнопки.") from None

    def _apply_change(self, events, draft: dict) -> None:
        # Сначала перечитываем событие: после показа кнопки его могли
        # изменить вручную. Проверка версии защищает от потери этих изменений.
        args = dict(calendarId=draft["calendar_id"], eventId=draft["target_event_id"])
        try:
            current = events.get(**args).execute(num_retries=0)
        except HttpError as exc:
            # 404 can also mean lost access; do not claim successful deletion.
            if exc.resp.status == 410 and draft["operation"] == "delete":
                return
            if exc.resp.status in (404, 410):
                raise CalendarConflict("Встреча недоступна или удалена. Проверь календарь и отправь запрос заново.") from None
            raise
        if current.get("status") == "cancelled":
            if draft["operation"] == "delete":
                return
            raise CalendarConflict("Эта встреча уже удалена; перенести её нельзя.")
        check_changeable(current)
        private = current.get("extendedProperties", {}).get("private", {})
        # Маркер позволяет узнать, что перенос уже прошёл, даже если
        # предыдущий HTTP-ответ потерялся. Повторно переносить не нужно.
        if draft["operation"] == "move" and private.get("assistant_operation") == draft["id"]:
            if (datetime.fromisoformat(current["start"]["dateTime"]) == draft["starts_at"]
                    and datetime.fromisoformat(current["end"]["dateTime"]) == draft["ends_at"]):
                return
            raise CalendarConflict("Перенос уже выполнялся, но время встречи снова изменено. Проверь календарь.")
        if current["etag"] != draft["target_etag"]:
            raise CalendarConflict("Встреча изменилась после подготовки. Отправь запрос заново.")
        if draft["expires_at"] <= datetime.now(self.timezone):
            raise CalendarConflict("Подтверждение истекло. Отправь запрос заново.")
        if draft["operation"] == "move":
            if draft["starts_at"] <= datetime.now(self.timezone):
                raise CalendarConflict("Новое время уже наступило. Отправь запрос с будущим временем.")
            request = events.patch(**args, sendUpdates="none", body={
                "start": {"dateTime": draft["starts_at"].isoformat(), "timeZone": str(self.timezone)},
                "end": {"dateTime": draft["ends_at"].isoformat(), "timeZone": str(self.timezone)},
                "extendedProperties": {"private": {**private, "assistant_operation": draft["id"]}},
            })
        elif draft["operation"] == "delete":
            request = events.delete(**args, sendUpdates="none")
        else:
            raise CalendarConflict("Неизвестное действие.")
        request.headers["If-Match"] = draft["target_etag"]
        # Google тоже проверит etag при записи: между чтением и записью
        # событие мог изменить другой клиент. Несовпадение даст HTTP 412.
        request.execute(num_retries=0)

    async def create_event(self, draft: dict) -> None:
        async with self._lock:
            await asyncio.to_thread(self._create_event, draft)

    def _create_event(self, draft: dict) -> None:
        # ID берём из черновика, а не генерируем на каждую попытку:
        # повторное нажатие обращается к тому же событию.
        try:
            credentials = Credentials.from_authorized_user_file(str(self.token_path))
            if not credentials.has_scopes(SCOPES):
                raise CalendarError("Для создания встреч повторно подключи Google Calendar с правом записи на компьютере.")
            if not credentials.valid:
                credentials.refresh(Request())
                save_token(self.token_path, credentials)
            http = AuthorizedHttp(credentials, http=httplib2.Http(timeout=15))
            try:
                service = build("calendar", "v3", http=http, cache_discovery=False)
                events = service.events()
                args = dict(calendarId=draft["calendar_id"], eventId=draft["id"])
                try:
                    existing = events.get(**args).execute(num_retries=0)
                except HttpError as exc:
                    if exc.resp.status != 404:
                        raise
                    existing = None
                if existing is None:
                    body = {"id": draft["id"], "summary": draft["title"],
                            "start": {"dateTime": draft["starts_at"].isoformat()},
                            "end": {"dateTime": draft["ends_at"].isoformat()}}
                    attendees = draft.get("attendees") or []
                    if attendees:
                        body["attendees"] = [{"email": email} for email in attendees]
                    try:
                        existing = events.insert(calendarId=draft["calendar_id"], body=body,
                                                 sendUpdates="all" if attendees else "none").execute(num_retries=0)
                    except HttpError as exc:
                        if exc.resp.status != 409:
                            raise
                        existing = events.get(**args).execute(num_retries=0)
                if existing.get("status") == "cancelled":
                    raise CalendarError("Эта встреча уже была удалена в Google Calendar. Подготовь новый черновик.")
                save_token(self.token_path, credentials)
            finally:
                http.http.close()
        except CalendarError:
            raise
        except RefreshError:
            raise CalendarError("Повтори авторизацию Google Calendar на компьютере.") from None
        except Exception:
            raise CalendarError("Не удалось подтвердить запись в Google Calendar. Через две минуты нажми «Создать» ещё раз: повторная попытка использует тот же ID встречи.") from None


def format_events(events: list[dict], start: datetime, timezone: ZoneInfo) -> list[str]:
    # Отдельное форматирование для /today: оно не использует языковую модель.
    # Возвращаем список строк-сообщений, учитывая ограничение длины Telegram.
    lines = [f"Расписание на {start:%d.%m.%Y} ({timezone}):"]
    if not events:
        lines.append("Событий нет.")
    for event in events:
        title = " ".join(str(event.get("summary") or "Без названия").split())[:500]
        begin, finish = event["start"], event["end"]
        if "date" in begin:
            label = "Весь день"
        else:
            a = datetime.fromisoformat(begin["dateTime"]).astimezone(timezone)
            b = datetime.fromisoformat(finish["dateTime"]).astimezone(timezone)
            label = f"{a:%H:%M}–{b:%H:%M}" if a.date() == b.date() == start.date() else f"{a:%d.%m %H:%M}–{b:%d.%m %H:%M}"
        lines.append(f"• {label} — {title}")
    chunks, chunk = [], ""
    for line in lines:
        # Conservative UTF-16 length limit also covers emoji-heavy titles.
        if len((chunk + line).encode("utf-16-le")) // 2 > 3500:
            chunks.append(chunk.rstrip())
            chunk = ""
        chunk += line + "\n"
    if chunk:
        chunks.append(chunk.rstrip())
    return chunks
