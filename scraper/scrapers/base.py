"""Base scraper interface."""
from dataclasses import dataclass, field
from datetime import date
from typing import Optional


@dataclass
class LiftStatus:
    name: str
    status: str          # 'open' | 'closed' | 'hold' | 'unknown'
    lift_type: str = ""
    is_link: bool = False  # True for cross-resort summit links (e.g. Cervinia→Zermatt)


@dataclass
class PisteStatus:
    name: str
    status: str           # 'open' | 'closed' | 'unknown'
    colour: str = ""      # 'green' | 'blue' | 'red' | 'black'


@dataclass
class SourceReading:
    """One source's aggregate view of a resort on one day.

    Every snapshot records a reading per source consulted (primary scraper
    and bergfex), so sources can be cross-validated even when one of them
    is silently wrong rather than erroring.
    """
    source: str
    lifts_open: Optional[int] = None
    lifts_total: Optional[int] = None
    pistes_open_km: Optional[float] = None
    pistes_total_km: Optional[float] = None
    error: Optional[str] = None


@dataclass
class ResortSnapshot:
    resort_id: str
    source: str
    lifts: list[LiftStatus] = field(default_factory=list)
    pistes: list[PisteStatus] = field(default_factory=list)
    pistes_open_km: Optional[float] = None
    pistes_total_km: Optional[float] = None
    snow_depth_mountain_cm: Optional[int] = None
    snow_depth_valley_cm: Optional[int] = None
    snow_condition: Optional[str] = None
    last_snowfall_date: Optional[date] = None
    piste_conditions: Optional[str] = None
    avalanche_danger: Optional[int] = None   # 1–5 European danger scale
    wind_gust_max_kmh: Optional[float] = None
    wind_speed_max_kmh: Optional[float] = None
    temp_min_c: Optional[float] = None
    temp_max_c: Optional[float] = None
    fresh_snow_cm: Optional[float] = None
    precipitation_mm: Optional[float] = None
    weather_code: Optional[int] = None
    sunshine_hours: Optional[float] = None
    freezing_level_max_m: Optional[int] = None
    freezing_level_min_m: Optional[int] = None
    wind_700hpa_max_kmh: Optional[float] = None
    error: Optional[str] = None
    source_readings: list["SourceReading"] = field(default_factory=list)

    @property
    def lifts_open(self) -> int:
        return sum(1 for l in self.lifts if l.status == "open")

    @property
    def lifts_total(self) -> int:
        return len(self.lifts)

    @property
    def pct_open(self) -> Optional[float]:
        if self.lifts_total == 0:
            return None
        return round(100 * self.lifts_open / self.lifts_total, 1)
