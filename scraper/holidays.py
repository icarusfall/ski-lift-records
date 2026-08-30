"""School holiday periods for ski-season crowding context.

Dates live in `config/holidays.json` (version controlled, re-verified each
summer) and are loaded into the `holiday_periods` table by `--init-db`,
mirroring how resorts.json seeds the resorts table. This module reads the
JSON directly so collection has no extra DB round-trip; the web app joins
the table at query time.
"""
import json
from datetime import date
from pathlib import Path

HOLIDAYS_FILE = Path(__file__).parent.parent / "config" / "holidays.json"

_cache: list[dict] | None = None


def load_holidays() -> list[dict]:
    """All holiday periods as dicts with parsed date objects."""
    global _cache
    if _cache is None:
        with open(HOLIDAYS_FILE, encoding="utf-8") as f:
            raw = json.load(f)["holidays"]
        _cache = [
            {
                "country": h["country"],
                "region": h.get("region") or "",
                "name": h["name"],
                "start": date.fromisoformat(h["start"]),
                "end": date.fromisoformat(h["end"]),
            }
            for h in raw
        ]
    return _cache


def get_holidays(d: date, country: str | None = None) -> list[dict]:
    """Every holiday period covering date d, optionally filtered by country."""
    return [
        h for h in load_holidays()
        if h["start"] <= d <= h["end"] and (country is None or h["country"] == country)
    ]


def label(holidays: list[dict]) -> str | None:
    """Compact label for a set of concurrent periods, e.g. 'FR Zone A + UK'."""
    if not holidays:
        return None
    seen, parts = set(), []
    for h in holidays:
        key = f"{h['country']} {h['region']}".strip()
        if key not in seen:
            seen.add(key)
            parts.append(key)
    return " + ".join(parts)


# --- backwards-compatible UK helpers (snapshots.is_uk_school_holiday) -------

def get_holiday(d: date) -> dict | None:
    """First UK holiday period covering d, else None."""
    uk = get_holidays(d, country="UK")
    return uk[0] if uk else None


def is_uk_school_holiday(d: date) -> tuple[bool, str | None]:
    h = get_holiday(d)
    return (True, h["name"]) if h else (False, None)
