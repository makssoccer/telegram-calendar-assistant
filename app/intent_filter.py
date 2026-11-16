"""Локальный допуск свободного сообщения к агенту. Команды обслуживает Telegram router."""
import asyncio
import logging
import re

REFUSE = "Я помогаю с прогулками по Гродно, историями мест и календарём. С этим вопросом помочь не смогу."
CLARIFY = "Уточни, пожалуйста: посмотреть расписание, запланировать встречу или подобрать место для прогулки?"
EXAMPLES = {
    "calendar": ("Какие планы у меня на следующую неделю?", "Перенеси созвон на пятницу",
                 "Запиши меня к стоматологу завтра в десять", "Подбери свободное время для встречи"),
    "tourism": ("Куда сходить в Гродно на пару часов?", "Где приятно побродить среди деревьев?",
                "Покажи старинные здания рядом с центром", "Кто жил в этом дворце?",
                "Как добавить исторический материал в базу бота?"),
    "other": ("Как отремонтировать автомобильный двигатель?", "Напиши программу сортировки на Java",
              "Приготовь рецепт пирога", "Реши математическое уравнение", "Напиши стихотворение о любви"),
}
CALENDAR = re.compile(r"\b(?:добав\w*|запланир\w*|созда\w*|запиш\w*|постав\w*|перенес\w*|удал\w*|отмен\w*|назнач\w*|покаж\w*)\b.*?\b(?:встреч\w*|событи\w*|календар\w*|мит\w*|созвон\w*|расписани\w*)\b|\b(?:свободн\w* (?:окн\w*|время|слот\w*)|что у меня|мои планы)\b")
OTHER = re.compile(r"\b(?:напиши|сгенерируй) (?:мне )?(?:код|программу|стих\w*)\b|\b(?:как |объясни |расскажи )[^.!?]{0,45}(?:ремень грм|двигател\w*)\b|\b(?:дай рецепт|реши уравнение)\b")
TOUR = re.compile(r"\b(?:прогулк\w*|маршрут\w*|погулять|достопримечательност\w*|стар\w* зам\w*|нов\w* зам\w*|парк\w* жилибера|советск\w* площад\w*)\b")
FOLLOWUP = re.compile(r"(?:а |тогда )?(?:завтра|сегодня|послезавтра|от вокзала|с возвратом|на \d+ (?:час\w*|минут\w*)|в \d{1,2}(?::\d{2})?|подробнее|да|нет)")
STRIP_STAMP = re.compile(r"^\[[^\]]+\]\s*")


def explicit_refusal(text):
    value = text.casefold().replace("ё", "е")
    if OTHER.search(value) and not CALENDAR.search(value):
        return REFUSE
    return None


async def check_intent(text, history, search):
    value = " ".join(text.casefold().replace("ё", "е").split()).strip(" ?!.,")
    if CALENDAR.search(value):
        return None
    if FOLLOWUP.fullmatch(value):
        # Последняя пара позволяет отличить продолжение от нового разговора.
        recent = " ".join(STRIP_STAMP.sub("", str(m.get("content", ""))).lower() for m in history[-2:])
        return None if (CALENDAR.search(recent) or TOUR.search(recent)
                        or re.search(r"встреч|расписани|календар|пешком|вокзал|событи", recent)) else CLARIFY
    if TOUR.search(value):
        return None
    try:
        scores = await asyncio.to_thread(search.intent_scores, value, EXAMPLES)
    except Exception as exc:
        logging.getLogger(__name__).warning("Local intent model unavailable (%s)", type(exc).__name__)
        return CLARIFY
    supported = max(scores["calendar"], scores["tourism"])
    # Для отказа нужен и хороший результат, и отрыв от поддерживаемых тем.
    if scores["other"] >= 0.82 and scores["other"] - supported >= 0.025:
        return REFUSE
    if supported >= 0.80 and supported - scores["other"] >= 0.012:
        return None
    return CLARIFY
