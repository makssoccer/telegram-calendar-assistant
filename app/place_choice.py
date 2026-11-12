"""Проверяем название из сообщения до передачи координат или источников инструменту."""
import re
from dataclasses import dataclass
from difflib import SequenceMatcher

from app.knowledge import PLACES, resolve_place
from app.tourism import WalkRequest


def normalize(text):
    return " ".join(re.findall(r"[\w]+", text.casefold().replace("ё", "е")))


ALIASES = {
    "station": ("вокзал", "вокзала", "железнодорожного вокзала"),
    "old_castle": ("старого замка", "старом замке", "старому замку"),
    "new_castle": ("нового замка", "новом замке", "новому замку"),
    "university": ("гргу", "университет", "университета", "университете"),
    "drama_theatre": ("драмтеатр", "драмтеатра", "драмтеатре", "драматического театра"),
    "gilibert_park": ("парка жилибера", "парке жилибера"),
    "lenin_square": ("площади ленина",),
    "soviet_square": ("советской площади",),
}


@dataclass(frozen=True)
class PlaceChoice:
    action: str
    question: str
    mention: str
    options: tuple
    walk: WalkRequest | None = None
    return_to_start: bool = False
    start_id: str = "station"

    def prompt(self):
        if self.action == "route":
            return f"Маршрута «{self.mention}» в каталоге нет. Что выбрать?"
        if not self.options:
            return f"«{self.mention}» не удалось найти в каталоге Гродно. Выбери доступное место."
        return f"Какое место ты имеешь в виду под «{self.mention}»?"


def match_place(mention):
    value = normalize(mention)
    exact = resolve_place(value)
    if exact:
        return exact, False, ()
    candidates = []
    for identifier, name in PLACES.items():
        forms = [normalize(name), *ALIASES[identifier]]
        if value in forms:
            return identifier, False, ()
        # Сравниваем каждое слово: одно общее слово «замок» не делает
        # прилагательные «белый» и «старый» опечатками друг друга.
        scores = [min(SequenceMatcher(None, a, b).ratio() for a, b in zip(value.split(), form.split()))
                  for form in forms if value and len(value.split()) == len(form.split())]
        candidates.append((max(scores, default=0), identifier))
    candidates.sort(reverse=True)
    score, best = candidates[0]
    if score >= 0.80 and score - candidates[1][0] >= 0.15:
        return best, True, ()
    if set(value.split()) & {"замок", "замка", "замке", "замку"}:
        return None, False, ("old_castle", "new_castle")
    return None, False, tuple(key for score, key in candidates[:3] if score >= 0.65)


def mention_in(text, action):
    # Для частых конструкций извлекаем буквальное название до вызова LLM.
    if action == "walk":
        pattern = r"(?:\bот|\bиз|\bсо стартом (?:из|у)|\bначиная с)\s+(.+?)(?=\s+(?:на \d|на час|с возвратом|и верн|и обрат)|[?!.,;]|$)"
    else:
        pattern = r"(?:\bпро|\bоб?|\bв|\bистори[юя])\s+([^?!.,;]+?)(?=[?!.,;]|$)"
    matches = list(re.finditer(pattern, text, re.I))
    return matches[-1][1].strip(' «»"') if matches else ""


def local_request(text):
    route = re.search(r'\bмаршрут\s+[«"](.*?)[»"]', text, re.I)
    if not route:
        route = re.search(r"\bмаршрут(?:\s+под названием|\s+по имени)?\s+([А-ЯЁ][а-яё-]+(?:\s+[А-Яа-яЁё-]+)*?)(?=\s+на\s|[.!?]|$)", text)
    duration = re.search(r"\bна\s+(?:(\d+)\s*)?(час\w*|мин\w*)\b", text, re.I)
    wants_return = bool(re.search(r"с возвратом|верн\w* (?:к старту|обратно)|и обратно", text, re.I))
    walk = None
    if duration:
        minutes = int(duration[1] or 1) * (60 if duration[2].lower().startswith("час") else 1)
        if 60 <= minutes <= 180:
            walk = WalkRequest(budget_minutes=minutes, return_to_start=wants_return)
    if route:
        return PlaceChoice("route", text, route[1], ("walk", "find"), walk, wants_return)
    # Составные условия маршрута обрабатывает обычный harness.
    if re.search(r"\b(?:через|зайти|посетить|обязательно|заехать|кафе)\b", text, re.I):
        return None
    if (walk or not re.search(r"\b(?:час\w*|минут\w*|полчаса|полтора)\b", text, re.I)) and re.search(r"\b(?:прогулк\w*|маршрут\w*)\b", text, re.I):
        mention = mention_in(text, "walk")
        if mention:
            return PlaceChoice("walk", text, mention, (), walk, wants_return)
    if re.match(r"\s*(?:расскажи|кто жил|какие правители)", text, re.I):
        mention = mention_in(text, "story")
        looks_like_place = mention and (
            match_place(mention)[0] or match_place(mention)[2]
            or re.search(r"\b(?:зам\w*|парк\w*|музе\w*|улиц\w*|зоопарк\w*|площад\w*|теат\w*|кост[её]л\w*|двор\w*|башн\w*|вокзал\w*)\b", mention, re.I)
        )
        if looks_like_place and not re.search(r"\b(?:встреч\w*|календар\w*|расписани\w*)\b", text, re.I):
            return PlaceChoice("story", text, mention, ())
    return None
