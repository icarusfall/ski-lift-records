"""Base scraper interface."""
from dataclasses import dataclass, field
from datetime import date
from typing import Optional


import re
import unicodedata

# Normalised statuses. 'seasonal' means the lift is not operating in this part
# of the season at all, which is a different thing from being shut today —
# keeping them apart is what lets "why was it closed?" be answered later.
OPEN, CLOSED, HOLD, SEASONAL, UNKNOWN = "open", "closed", "hold", "seasonal", "unknown"

_STATUS_WORDS = {
    OPEN: {"open", "opened", "o", "ouvert", "ouverte", "offen", "geoffnet", "aperto",
           "abierto", "obert", "1", "true", "yes"},
    CLOSED: {"closed", "close", "f", "ferme", "fermee", "geschlossen", "chiuso",
             "cerrado", "tancat", "0", "false", "no"},
    HOLD: {"hold", "on hold", "wind hold", "wind", "standby", "stand by", "paused",
           "interrupted", "temporarily closed", "attente", "en attente", "sospeso",
           "unterbrochen", "delayed", "on_hold"},
    SEASONAL: {"out of period", "outofperiod", "seasonal", "closed for season",
               "hors periode", "hors saison", "prevision", "forecast", "planned",
               "scheduled", "not open yet", "saison beendet", "ausser betrieb"},
}

_LOOKUP = {word: status for status, words in _STATUS_WORDS.items() for word in words}


def normalise_status(raw: str | None) -> str:
    """Map a site's own status wording onto our fixed vocabulary.

    Unrecognised wording becomes 'unknown' rather than being discarded, so a
    site that invents a new status shows up as a gap instead of silently
    shrinking the lift count (which is how La Plagne came to report 3/77).
    """
    if not raw:
        return UNKNOWN
    text = unicodedata.normalize("NFKD", str(raw)).encode("ascii", "ignore").decode()
    text = re.sub(r"[_\-/]+", " ", text).strip().lower()
    text = re.sub(r"\s+", " ", text)
    if text in _LOOKUP:
        return _LOOKUP[text]
    # Fall back to substring matching for wordier phrasings.
    for status in (HOLD, SEASONAL, OPEN, CLOSED):
        for word in _STATUS_WORDS[status]:
            if len(word) > 3 and word in text:
                return status
    return UNKNOWN


@dataclass
class LiftStatus:
    name: str
    status: str          # 'open' | 'closed' | 'hold' | 'seasonal' | 'unknown'
    lift_type: str = ""
    is_link: bool = False  # True for cross-resort summit links (e.g. Cervinia→Zermatt)
    raw_status: str = ""   # the site's own wording, kept verbatim


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
        # Lifts we failed to read a status for are excluded rather than
        # counted as closed, so a parsing failure can't masquerade as a
        # closure-heavy day.
        return sum(1 for l in self.lifts if l.status != UNKNOWN)

    @property
    def pct_open(self) -> Optional[float]:
        if self.lifts_total == 0:
            return None
        return round(100 * self.lifts_open / self.lifts_total, 1)
