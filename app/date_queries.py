"""Точные границы дня для простых вопросов о расписании."""
import re
from datetime import datetime, timedelta

from app.calendar import day_bounds


def schedule_day(text, now, timezone):
    # Только полный вопрос о расписании. Не перехватываем создание, перенос,
    # «завтра утром», несколько дат или уточнения, которым нужен контекст.
    normalized = " ".join(text.casefold().strip(" \t\n?!.,").split())
    match = re.fullmatch(
        r"(?:что у меня(?: запланировано)?(?: на)?|"
        r"какие у меня (?:встречи|события)(?: на)?|"
        r"(?:покажи )?(?:расписание|встречи|события)(?: на)?) "
        r"(сегодня|завтра|послезавтра)", normalized)
    if not match:
        return None
    offset = {"сегодня": 0, "завтра": 1, "послезавтра": 2}[match[1]]
    return day_bounds(now.astimezone(timezone) + timedelta(days=offset), timezone)
