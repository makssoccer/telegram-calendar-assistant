"""Небольшой детерминированный планировщик поверх матрицы пеших переходов."""
import math
from dataclasses import dataclass

from app.walking import WalkingLeg, WalkingError


@dataclass(frozen=True)
class Visit:
    place_id: str
    matrix_index: int
    minutes: int
    priority: int = 1


@dataclass(frozen=True)
class WalkPlan:
    visits: tuple[Visit, ...]
    walking_minutes: int
    visiting_minutes: int
    reserve_minutes: int
    distance_meters: int

    @property
    def total_minutes(self):
        return self.walking_minutes + self.visiting_minutes + self.reserve_minutes


def plan_walk(matrix: list[list[WalkingLeg | None]], candidates: list[Visit],
              budget_minutes: int, return_to_start: bool = True,
              reserve_minutes: int = 10, preserve_order: bool = False) -> WalkPlan:
    # Нулевая точка матрицы — старт. Проверяем бюджет кодом, а не просьбой к LLM.
    # Это жадный алгоритм для малого каталога, не поиск глобального оптимума.
    if not 60 <= budget_minutes <= 180 or not 0 <= reserve_minutes < budget_minutes:
        raise WalkingError("Прогулка должна занимать от 60 до 180 минут.")
    size = len(matrix)
    if not size or any(len(row) != size for row in matrix):
        raise WalkingError("Некорректная матрица переходов.")
    if (len({c.place_id for c in candidates}) != len(candidates)
            or len({c.matrix_index for c in candidates}) != len(candidates)
            or any(not 0 < c.matrix_index < size or c.minutes <= 0 or c.priority <= 0 for c in candidates)):
        raise WalkingError("Некорректные точки прогулки.")
    remaining = list(candidates)
    selected = []
    current = 0
    walking_seconds = 0.0
    walking_meters = 0.0
    visiting_minutes = 0
    while remaining:
        feasible = []
        for visit in remaining:
            leg = matrix[current][visit.matrix_index]
            back = matrix[visit.matrix_index][0] if return_to_start else WalkingLeg(0, 0)
            if leg is None or back is None:
                continue
            total = walking_seconds / 60 + math.ceil(leg.seconds / 60) + math.ceil(back.seconds / 60)
            total += visiting_minutes + visit.minutes + reserve_minutes
            if total <= budget_minutes:
                # Приоритет интересов / стоимость остановки и перехода.
                score = visit.priority / (visit.minutes + leg.seconds / 60 + 1)
                feasible.append((score, visit, leg))
        if not feasible:
            break
        _, visit, leg = feasible[0] if preserve_order else max(feasible, key=lambda item: item[0])
        selected.append(visit)
        if preserve_order:
            # Пропущенные более ранние точки не посещаем задним ходом.
            remaining = remaining[remaining.index(visit) + 1:]
        else:
            remaining.remove(visit)
        current = visit.matrix_index
        walking_seconds += math.ceil(leg.seconds / 60) * 60
        walking_meters += leg.meters
        visiting_minutes += visit.minutes
    if not selected:
        raise WalkingError("Ни одна остановка не помещается в доступное время с учётом переходов и запаса.")
    if return_to_start:
        back = matrix[current][0]
        walking_seconds += math.ceil(back.seconds / 60) * 60
        walking_meters += back.meters
    result = WalkPlan(tuple(selected), math.ceil(walking_seconds / 60), visiting_minutes,
                      reserve_minutes, math.ceil(walking_meters))
    if result.total_minutes > budget_minutes:
        raise WalkingError("Маршрут не прошёл проверку времени.")
    return result
