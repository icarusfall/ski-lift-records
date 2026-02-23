"""Base scraper interface."""
from dataclasses import dataclass, field
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
class ResortSnapshot:
    resort_id: str
    source: str
    lifts: list[LiftStatus] = field(default_factory=list)
    pistes: list[PisteStatus] = field(default_factory=list)
    pistes_open_km: Optional[float] = None
    pistes_total_km: Optional[float] = None
    snow_depth_mountain_cm: Optional[int] = None
    snow_depth_valley_cm: Optional[int] = None
    error: Optional[str] = None

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
