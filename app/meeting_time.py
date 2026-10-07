"""Проверяем простые явно заданные время и длительность независимо от модели."""
import re
from datetime import datetime, time, timedelta

HOURS = {
    "час": 1, "один": 1, "два": 2, "три": 3, "четыре": 4, "пять": 5,
    "шесть": 6, "семь": 7, "восемь": 8, "девять": 9, "десять": 10,
    "одиннадцать": 11, "двенадцать": 12,
}
DURATIONS = {
    "пять": 5, "пяти": 5, "десять": 10, "десяти": 10,
    "пятнадцать": 15, "пятнадцати": 15, "двадцать": 20, "двадцати": 20,
    "тридцать": 30, "тридцати": 30, "сорок": 40, "сорока": 40,
    "шестьдесят": 60, "шестидесяти": 60,
}


def explicit_meeting_time(draft, text, now, timezone):
    # Это узкая проверка однозначных фраз, не универсальный парсер русского.
    # Сложные диапазоны, несколько времён и «без десяти» не исправляем наугад.
    normalized = text.casefold().replace("ё", "е")
    hours = "|".join(HOURS)
    times = list(re.finditer(
        rf"\bв\s+(?P<hour>\d{{1,2}}|{hours})(?::(?P<minute>\d{{2}}))?"
        r"(?:\s+(?P<part>утра|вечера|дня|ночи))?(?![\w:])", normalized))
    duration_words = "|".join(DURATIONS)
    durations = list(re.finditer(
        rf"\b(?P<value>\d+|{duration_words})[\s-]*минут(?:ный|ная|ное|ную|ные|ного|ы|у)?\b",
        normalized))
    start, end = draft.start, draft.end
    if len(times) == 1:
        match = times[0]
        hour_text = match["hour"]
        hour = int(hour_text) if hour_text.isdigit() else HOURS[hour_text]
        minute = int(match["minute"] or 0)
        part = match["part"]
        # Не принимаем «в десять тридцать» за 10:00 или дату «в 10 октября».
        tail = normalized[match.end():].lstrip()
        ambiguous_tail = re.match(
            r"(?:\d|час|минут|тридцать|пятнадцать|сорок|двадцать|пять|"
            r"январ|феврал|март|апрел|ма[яй]|июн|июл|август|сентябр|октябр|ноябр|декабр)", tail)
        # Без части суток «в три» неоднозначно; не меняем решение модели.
        explicit_clock = part is not None or match["minute"] is not None or hour >= 13
        if explicit_clock and not ambiguous_tail and hour <= 23 and minute <= 59:
            if part in ("вечера", "дня") and hour < 12:
                hour += 12
            elif part in ("утра", "ночи") and hour == 12:
                hour = 0
            local = start.astimezone(timezone)
            relative = re.findall(r"\b(?:сегодня|завтра|послезавтра)\b", normalized)
            date = local.date()
            if len(relative) == 1:
                date = now.astimezone(timezone).date() + timedelta(
                    days={"сегодня": 0, "завтра": 1, "послезавтра": 2}[relative[0]])
            duration = end - start
            start = datetime.combine(date, time(hour, minute), timezone)
            end = start + duration
    # Составную длительность «на час и десять минут» здесь не разбираем.
    has_hours = re.search(r"\b(?:на|длительностью)\s+(?:\d+\s+|один\s+|два\s+|три\s+)?час", normalized)
    if len(durations) == 1 and not has_hours:
        value = durations[0]["value"]
        minutes = int(value) if value.isdigit() else DURATIONS[value]
        # «Двадцать пять минут» не должно превратиться в пять минут.
        prefix = normalized[:durations[0].start()].rstrip()
        if not re.search(r"\b(?:двадцать|тридцать|сорок|пятьдесят|сто)\s*$", prefix):
            if 0 < minutes <= 7 * 24 * 60:
                end = start + timedelta(minutes=minutes)
    # model_copy не валидирует изменения; явно повторяем проверку Pydantic.
    return type(draft).model_validate({**draft.model_dump(), "start": start, "end": end})
