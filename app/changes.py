"""Resolve an existing event before preparing a confirmed change."""
from datetime import datetime, timedelta
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from app.calendar import CalendarError, CalendarService, check_changeable
from app.drafts import EventDraft


class ChangeRequest(BaseModel):
    # DTO аргументов модели для поиска. Literal ограничивает допустимые
    # значения (похоже на небольшой enum); T | None разрешает отсутствие поля.
    # Исходный диапазон поиска и новое время встречи — разные поля.
    model_config = ConfigDict(extra="forbid")
    operation: Literal["move", "delete"]
    search_start: AwareDatetime
    search_end: AwareDatetime
    title: str = Field(max_length=300, description="Title or fragment given by the user; empty if unspecified")
    original_start: AwareDatetime | None = Field(default=None, description="Only if user specified the original time to disambiguate")
    new_start: AwareDatetime | None = None
    new_end: AwareDatetime | None = Field(default=None, description="Omit to preserve existing duration")

    @model_validator(mode="after")
    def validate_request(self):
        if not timedelta(0) < self.search_end - self.search_start <= timedelta(days=31):
            raise ValueError("Search at most 31 days")
        if self.operation == "move" and self.new_start is None:
            raise ValueError("New start is required")
        if self.operation == "delete" and (self.new_start or self.new_end):
            raise ValueError("Deletion has no new time")
        return self


class EventChange(EventDraft):
    # Наследует название и время из EventDraft (аналог extends).
    # Добавляет ID и версию события, полученные из Google, а не придуманные
    # моделью. start/end — итоговое время; original_* — время до переноса.
    operation: Literal["move", "delete"]
    target_event_id: str
    target_etag: str
    original_start: AwareDatetime
    original_end: AwareDatetime


async def prepare_change(calendar: CalendarService, request: ChangeRequest) -> EventChange | str:
    # Возвращает либо готовый DTO, либо текст уточнения. Записи в Google нет.
    events = await calendar.list_events(request.search_start, request.search_end)
    title = " ".join(request.title.casefold().split())
    # [e for e in ... if ...] — list comprehension: цикл с фильтром,
    # похожий на stream().filter(...).toList() в Java.
    matches = [e for e in events if title in " ".join(str(e.get("summary", "")).casefold().split())]
    if request.original_start:
        matches = [e for e in matches if e.get("start", {}).get("dateTime")
                   and datetime.fromisoformat(e["start"]["dateTime"]) == request.original_start]
    if not matches:
        return "Не нашёл встречу в указанном периоде. Уточни её название и исходную дату."
    if len(matches) != 1:
        # При неоднозначности код возвращает уточнение вместо выбора наугад.
        lines = ["Найдено несколько встреч. Уточни название и исходное время:"]
        for event in matches[:10]:
            start = event.get("start", {})
            when = datetime.fromisoformat(start["dateTime"]).astimezone(calendar.timezone).strftime("%d.%m.%Y %H:%M") if "dateTime" in start else start.get("date", "весь день")
            lines.append(f"• {when} — {str(event.get('summary') or 'Без названия')[:100]}")
        if len(matches) > 10:
            lines.append("Показаны первые 10; уточни период поиска.")
        return "\n".join(lines)
    event = matches[0]
    # Обращаемся по индексу только после проверки, что совпадение одно.
    check_changeable(event)
    start = datetime.fromisoformat(event["start"]["dateTime"])
    end = datetime.fromisoformat(event["end"]["dateTime"])
    new_start = request.new_start if request.operation == "move" else start
    new_end = (request.new_end or new_start + (end - start)) if request.operation == "move" else end
    # Если новый конец не задан, сохраняем длительность исходной встречи.
    if request.operation == "move" and new_start <= datetime.now(calendar.timezone):
        raise CalendarError("Новое время должно быть в будущем.")
    return EventChange(title=str(event.get("summary") or "Без названия")[:300],
                       start=new_start, end=new_end, operation=request.operation,
                       target_event_id=event["id"], target_etag=event["etag"],
                       original_start=start, original_end=end)
