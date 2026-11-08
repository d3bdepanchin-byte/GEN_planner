"""Производственный календарь: рабочие дни по файлам config/calendar/<год>.toml."""
import tomllib
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path


class UnknownYear(Exception):
    """Для года нет файла календаря."""


@dataclass(frozen=True)
class _Year:
    holidays: frozenset[date]
    working_weekends: frozenset[date]


class Calendar:
    def __init__(self, years: dict[int, _Year]):
        self._years = years

    @classmethod
    def load(cls, dir: Path) -> 'Calendar':
        years = {}
        for path in Path(dir).glob('*.toml'):
            with path.open('rb') as f:
                data = tomllib.load(f)
            years[int(path.stem)] = _Year(
                frozenset(data.get('holidays', [])),
                frozenset(data.get('working_weekends', [])),
            )
        return cls(years)

    def is_workday(self, d: date) -> bool:
        year = self._years.get(d.year)
        if year is None:
            raise UnknownYear(d.year)
        if d in year.working_weekends:
            return True
        if d in year.holidays:
            return False
        return d.weekday() < 5

    def add_workdays(self, start: datetime, n: int) -> datetime:
        """Дата через n рабочих дней после start; время суток сохраняется."""
        current = start
        left = n
        while left > 0:
            current += timedelta(days=1)
            if self.is_workday(current.date()):
                left -= 1
        return current
