"""Пешие переходы: реальные длительности API, без оценок языковой модели."""
import math
from dataclasses import dataclass

import aiohttp


class WalkingError(Exception):
    """Безопасное сообщение пользователю без API-ключей."""


@dataclass(frozen=True)
class WalkingLeg:
    seconds: float
    meters: float


class WalkingClient:
    # Модель выбирает точки, а этот клиент рассчитывает время по дорожной сети.
    # Координаты ORS идут в порядке longitude, latitude.
    def __init__(self, api_key: str):
        self.api_key = api_key

    async def matrix(self, coordinates: list[tuple[float, float]]) -> list[list[WalkingLeg | None]]:
        if not self.api_key:
            raise WalkingError("Для реального расчёта маршрута добавь ORS_API_KEY в .env и пересоздай контейнер.")
        if not 2 <= len(coordinates) <= 22:
            raise WalkingError("Для расчёта требуется от 2 до 22 точек.")
        if any(not (-180 <= lon <= 180 and -90 <= lat <= 90) for lon, lat in coordinates):
            raise WalkingError("Некорректные координаты маршрута.")
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=35)) as session:
                async with session.post(
                    "https://api.openrouteservice.org/v2/matrix/foot-walking",
                    headers={"Authorization": self.api_key},
                    json={"locations": coordinates, "metrics": ["duration", "distance"]},
                ) as response:
                    if response.status in (401, 403):
                        raise WalkingError("Сервис маршрутов отклонил ключ или запретил доступ. Проверь ORS_API_KEY.")
                    if response.status == 429:
                        raise WalkingError("Лимит сервиса маршрутов исчерпан. Попробуй позже.")
                    if response.status != 200:
                        raise WalkingError("Сервис не смог рассчитать пешие переходы. Попробуй другую точку старта.")
                    data = await response.json()
            return self.parse_matrix(data, len(coordinates))
        except WalkingError:
            raise
        except (aiohttp.ClientError, TimeoutError):
            raise WalkingError("Сервис маршрутов не отвечает. Расчётное время не подменяется приблизительным.") from None
        except (ValueError, TypeError):
            raise WalkingError("Сервис вернул некорректный ответ маршрута.") from None

    @staticmethod
    def parse_matrix(data, size):
        try:
            durations, distances = data["durations"], data["distances"]
            if len(durations) != size or len(distances) != size:
                raise ValueError()
            result = []
            for times, meters in zip(durations, distances):
                if len(times) != size or len(meters) != size:
                    raise ValueError()
                row = []
                for seconds, distance in zip(times, meters):
                    if seconds is None or distance is None:
                        row.append(None)  # Нельзя дойти — не считаем это нулевым временем.
                    elif all(isinstance(v, (int, float)) and not isinstance(v, bool)
                             and math.isfinite(v) and v >= 0 for v in (seconds, distance)):
                        row.append(WalkingLeg(seconds, distance))
                    else:
                        raise ValueError()
                result.append(row)
            return result
        except (KeyError, TypeError, ValueError):
            raise WalkingError("Сервис вернул некорректные данные маршрута.") from None
