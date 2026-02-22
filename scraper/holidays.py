"""UK school holiday date ranges for ski season context."""
from datetime import date

# England & Wales school holiday periods (approximate)
UK_SCHOOL_HOLIDAYS = [
    # 2025/26 season
    {"name": "Christmas 2025",      "start": date(2025, 12, 20), "end": date(2026, 1,  4)},
    {"name": "Feb Half-Term 2026",  "start": date(2026, 2,  14), "end": date(2026, 2,  22)},
    {"name": "Easter 2026",         "start": date(2026, 4,   1), "end": date(2026, 4,  17)},
    # 2026/27 season (placeholders — adjust when confirmed)
    {"name": "Christmas 2026",      "start": date(2026, 12, 19), "end": date(2027, 1,   3)},
    {"name": "Feb Half-Term 2027",  "start": date(2027, 2,  13), "end": date(2027, 2,  21)},
    {"name": "Easter 2027",         "start": date(2027, 3,  27), "end": date(2027, 4,  11)},
]


def get_holiday(d: date) -> dict | None:
    """Return the holiday dict if d falls within a UK school holiday, else None."""
    for h in UK_SCHOOL_HOLIDAYS:
        if h["start"] <= d <= h["end"]:
            return h
    return None


def is_uk_school_holiday(d: date) -> tuple[bool, str | None]:
    h = get_holiday(d)
    return (True, h["name"]) if h else (False, None)
