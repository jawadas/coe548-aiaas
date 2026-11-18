"""Usage numbers shaped for the pages: totals, a per-key breakdown and chart geometry."""

from __future__ import annotations

import datetime as dt
import math
from collections.abc import Sequence
from dataclasses import dataclass

from app.gateway import DayUsage, KeyInfo
from app.templating import compact

RANGES = (7, 14, 30)
DEFAULT_RANGE = 14
GRID_STEPS = 5


def date_range(days: int, today: dt.date | None = None) -> tuple[dt.date, dt.date]:
    """The last ``days`` UTC days, today included."""
    end = today or dt.datetime.now(dt.UTC).date()
    return end - dt.timedelta(days=days - 1), end


@dataclass(frozen=True, slots=True)
class Totals:
    requests: int
    successful: int
    failed: int
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    avg_latency_ms: float | None

    @property
    def success_rate(self) -> float | None:
        """Percentage of requests that succeeded, or ``None`` when there were none."""
        return None if self.requests == 0 else 100 * self.successful / self.requests


def totals(days: Sequence[DayUsage]) -> Totals:
    timed = [(day.avg_latency_ms, day.successful) for day in days if day.avg_latency_ms]
    weight = sum(count for _, count in timed)
    latency = sum(ms * count for ms, count in timed) / weight if weight else None
    return Totals(
        requests=sum(day.requests for day in days),
        successful=sum(day.successful for day in days),
        failed=sum(day.failed for day in days),
        prompt_tokens=sum(day.prompt_tokens for day in days),
        completion_tokens=sum(day.completion_tokens for day in days),
        total_tokens=sum(day.total_tokens for day in days),
        avg_latency_ms=latency,
    )


def change_percent(today: int, yesterday: int) -> int | None:
    """Today against yesterday as a whole percentage; ``None`` when yesterday had nothing."""
    return None if yesterday == 0 else round(100 * (today - yesterday) / yesterday)


@dataclass(frozen=True, slots=True)
class KeyRow:
    name: str
    requests: int
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    share: float  # of all tokens in the range, 0 to 100


def by_key(days: Sequence[DayUsage], keys: Sequence[KeyInfo]) -> list[KeyRow]:
    """Usage per key over the range, busiest first. Keys that no longer exist keep their usage."""
    names = {key.token: key.name for key in keys}
    sums: dict[str, list[int]] = {}
    for day in days:
        for entry in day.by_key:
            row = sums.setdefault(entry.token, [0, 0, 0, 0])
            row[0] += entry.requests
            row[1] += entry.prompt_tokens
            row[2] += entry.completion_tokens
            row[3] += entry.total_tokens
    everything = sum(row[3] for row in sums.values())
    rows = [
        KeyRow(
            name=names.get(token, "Revoked key"),
            requests=row[0],
            prompt_tokens=row[1],
            completion_tokens=row[2],
            total_tokens=row[3],
            share=100 * row[3] / everything if everything else 0.0,
        )
        for token, row in sums.items()
    ]
    return sorted(rows, key=lambda row: (row.total_tokens, row.requests), reverse=True)


# -- chart ---------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Bar:
    """One day of the stacked chart. Positions are percentages of the plot area."""

    label: str  # under the bar: "5 Nov", "6", "Today"
    day: str  # for the tooltip and screen readers: "5 Nov"
    show_label: bool
    is_today: bool
    slot_x: float
    slot_width: float
    center: float
    prompt_y: float
    prompt_height: float
    completion_y: float
    completion_height: float
    prompt: str
    completion: str
    total: str
    has_data: bool


@dataclass(frozen=True, slots=True)
class Chart:
    bars: list[Bar]
    ticks: list[tuple[float, str]]  # (y percentage from the top, label)
    dense: bool


def nice_ceiling(value: float) -> float:
    """The next round axis maximum at or above ``value`` (1, 2, 2.5 or 5 times a power of ten)."""
    if value <= 0:
        return 1000.0
    magnitude = 10 ** math.floor(math.log10(value))
    for factor in (1, 2, 2.5, 5, 10):
        if value <= factor * magnitude:
            return factor * magnitude
    return 10 * magnitude


def _short_date(day: dt.date) -> str:
    return f"{day.day} {day.strftime('%b')}"


def build_chart(days: Sequence[DayUsage]) -> Chart:
    """Geometry for the stacked prompt/completion bars, drawn as SVG by the template."""
    top = nice_ceiling(max((day.total_tokens for day in days), default=0))
    count = max(len(days), 1)
    slot = 100 / count
    label_every = 1 if count <= 14 else 3
    bars = []
    for index, day in enumerate(days):
        is_today = index == len(days) - 1
        prompt_height = 100 * day.prompt_tokens / top
        completion_height = 100 * day.completion_tokens / top
        first_of_month = index == 0 or day.date.day == 1
        bars.append(
            Bar(
                label="Today"
                if is_today
                else _short_date(day.date)
                if first_of_month
                else str(day.date.day),
                day=_short_date(day.date),
                show_label=is_today or index % label_every == 0,
                is_today=is_today,
                slot_x=round(index * slot, 3),
                slot_width=round(slot, 3),
                center=round((index + 0.5) * slot, 3),
                prompt_y=round(100 - prompt_height, 2),
                prompt_height=round(prompt_height, 2),
                completion_y=round(100 - prompt_height - completion_height, 2),
                completion_height=round(completion_height, 2),
                prompt=compact(day.prompt_tokens),
                completion=compact(day.completion_tokens),
                total=compact(day.total_tokens),
                has_data=day.total_tokens > 0,
            )
        )
    ticks = [
        (100 * step / GRID_STEPS, compact(top * (GRID_STEPS - step) / GRID_STEPS))
        for step in range(GRID_STEPS + 1)
    ]
    return Chart(bars=bars, ticks=ticks, dense=count > 14)
