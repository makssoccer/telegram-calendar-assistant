"""Каталог и сценарий прогулки. Числа маршрута формирует Python, а не LLM."""
import json
import math
from pathlib import Path
from typing import Literal
from urllib.parse import urlencode

from pydantic import BaseModel, ConfigDict, Field

from app.walking import WalkingClient, WalkingError
from app.walk_planner import Visit, plan_walk

PlaceId = Literal["station", "university", "gilibert_park", "lenin_square",
                  "soviet_square", "drama_theatre", "old_castle", "new_castle"]


class WalkRequest(BaseModel):
    # DTO ограничивает аргументы инструмента; модель не задаёт координаты и URL.
    model_config = ConfigDict(extra="forbid", strict=True)
    budget_minutes: int = Field(ge=60, le=180)
    start_id: PlaceId = "station"
    return_to_start: bool = False


class StoryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    place_id: PlaceId
    question: str = Field(default="Расскажи историю места", min_length=1, max_length=500)


def load_catalog():
    # Файл внутри app попадает в Docker вместе с кодом. Секретов в нём нет.
    return json.loads((Path(__file__).parent / "tour_data" / "grodno.json").read_text(encoding="utf-8"))


def walking_map_url(origin: dict, destination: dict) -> str:
    # Google Maps принимает широту, долготу (порядок отличается от ORS).
    # Ссылка строится из каталога, а не из ответа модели. API-ключ не нужен.
    return "https://www.google.com/maps/dir/?" + urlencode({
        "api": 1,
        "origin": f"{origin['lat']},{origin['lon']}",
        "destination": f"{destination['lat']},{destination['lon']}",
        "travelmode": "walking",
    })


class TourismService:
    def __init__(self, walking: WalkingClient):
        self.walking = walking
        self.catalog = load_catalog()
        self.places = {p["id"]: p for p in self.catalog["places"]}

    def list_places(self) -> dict:
        return {"city": self.catalog["city"], "places": list(self.places.values()),
                "scope": "Осмотр снаружи; часы работы, билеты, кафе и доступ внутрь не проверяются."}

    async def plan(self, request: WalkRequest) -> str:
        # Сохраняем пользовательский порядок. Старт из середины каталога
        # означает прогулку по оставшейся части, без возврата к ранним точкам.
        places = list(self.places.values())
        start_index = next(i for i, p in enumerate(places) if p["id"] == request.start_id)
        points = places[start_index:]
        if len(points) < 2:
            raise WalkingError("Новый замок — последняя точка. Выбери более ранний старт: /places.")
        matrix = await self.walking.matrix([(p["lon"], p["lat"]) for p in points])
        visits = [Visit(p["id"], i, p["visit_minutes"]) for i, p in enumerate(points) if i]
        plan = plan_walk(matrix, visits, request.budget_minutes, request.return_to_start,
                         preserve_order=True)
        lines = [f"Гродно • пешая прогулка • бюджет {request.budget_minutes} мин",
                 f"Старт: {points[0]['name']}"]
        current = 0
        elapsed = 0
        for number, visit in enumerate(plan.visits, 1):
            leg = matrix[current][visit.matrix_index]
            minutes = math.ceil(leg.seconds / 60)
            elapsed += minutes
            place = self.places[visit.place_id]
            lines.append(f"{number}. {place['name']}\n"
                         f"Переход {minutes} мин ({math.ceil(leg.meters)} м); "
                         f"осмотр {visit.minutes} мин. На месте через {elapsed} мин от старта.\n"
                         f"Пешком: {points[current]['name']} → {place['name']}\n"
                         f"Google Maps: {walking_map_url(points[current], place)}")
            elapsed += visit.minutes
            current = visit.matrix_index
        if request.return_to_start:
            back = matrix[current][0]
            lines.append(f"Возврат к старту: {math.ceil(back.seconds / 60)} мин ({math.ceil(back.meters)} м).\n"
                         f"Пешком: {points[current]['name']} → {points[0]['name']}\n"
                         f"Google Maps: {walking_map_url(points[current], points[0])}")
        else:
            lines.append(f"Финиш: {self.places[plan.visits[-1].place_id]['name']}. Возврат не включён.")
        lines.extend([f"Итого: {plan.total_minutes} мин из {request.budget_minutes}; "
                      f"пешком {plan.walking_minutes}, осмотр {plan.visiting_minutes}, запас {plan.reserve_minutes} мин.",
                      f"Пешая дистанция: {plan.distance_meters / 1000:.2f} км."])
        selected = {v.place_id for v in plan.visits}
        omitted = [p["name"] for p in points[1:] if p["id"] not in selected]
        if omitted:
            lines.append("Сокращённый маршрут. Не включены по времени или доступности переходов: " + ", ".join(omitted))
        lines.extend(["Переходы рассчитаны openrouteservice по данным OpenStreetMap. Время оценочное; "
                      "остановки — плановый осмотр снаружи, без музеев, очередей и кафе. "
                      "Фактические перекрытия и часы доступа не проверяются. "
                      "Google Maps строит переходы самостоятельно: путь и время могут отличаться от расчёта бота.",
                      "Рассказ о месте: «Расскажи про Старый замок».\n© openrouteservice • © OpenStreetMap contributors"])
        return "\n\n".join(lines)
