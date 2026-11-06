"""What the forecast said a day or two ahead, hour by hour per resort.

weather_hourly holds what the model settled on for each hour. This holds the
forecast as it stood about 24 and 48 hours earlier, from Open-Meteo's
previous-runs archive (free, no key, back to late January 2024). Set side by
side with the closures it answers "could you have seen this coming the night
before?", which is the question a skier planning tomorrow actually has.

Visibility, low cloud and freezing level are not kept in the archive, and
nor is upper-air wind, so this is a narrower set than weather_hourly.

    python -m scraper.forecast_archive --update                       # the collector runs this
    python -m scraper.forecast_archive --backfill 2025-11-01 2026-05-31
    python -m scraper.forecast_archive --status

Same requested point and elevation as scraper/hourly.py, so rows join on
(resort_id, time). time is UTC, hour ending.
"""
import sys
import time
from datetime import date, datetime, timedelta, timezone

import psycopg2.extras
import requests

from .collect import load_resorts
from .db import cursor

API_URL = "https://previous-runs-api.open-meteo.com/v1/forecast"
LEADS = (1, 2)

# (API field, column, cast)
FIELDS = [
    ("wind_gusts_10m",     "wind_gust_kmh",    float),
    ("wind_speed_10m",     "wind_speed_kmh",   float),
    ("wind_direction_10m", "wind_dir_deg",     lambda v: round(v) % 360),
    ("temperature_2m",     "temp_c",           float),
    ("snowfall",           "snowfall_cm",      float),
    ("precipitation",      "precipitation_mm", float),
    ("cloud_cover",        "cloud_cover_pct",  round),
    ("weather_code",       "weather_code",     round),
]

# Sixteen variables a request, so keep the pieces to a month and unhurried.
REQUEST_PAUSE_S = 3.0
CHUNK_DAYS = 31
RETRY_PAUSE_S = 30.0


def _fetch(resort: dict, start: date, end: date) -> list[dict]:
    params = {
        "latitude": resort["latitude"],
        "longitude": resort["longitude"],
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "hourly": ",".join(f"{f}_previous_day{d}" for d in LEADS for f, _, _ in FIELDS),
        "timezone": "UTC",
    }
    if resort.get("top_altitude_m"):
        params["elevation"] = resort["top_altitude_m"]
    resp = requests.get(API_URL, params=params, timeout=60)
    time.sleep(REQUEST_PAUSE_S)
    if resp.status_code == 429:
        raise RuntimeError(f"Open-Meteo quota reached: {resp.text[:200]}")
    resp.raise_for_status()
    hourly = resp.json().get("hourly", {})

    rows = []
    for i, t in enumerate(hourly.get("time", [])):
        for d in LEADS:
            row = {"resort_id": resort["id"], "time": datetime.fromisoformat(t),
                   "lead_days": d}
            for field, col, cast in FIELDS:
                v = (hourly.get(f"{field}_previous_day{d}") or [None] * (i + 1))[i]
                row[col] = cast(v) if v is not None else None
            # Before the archive starts, and at its live end, hours are empty.
            if row["wind_gust_kmh"] is not None or row["temp_c"] is not None:
                rows.append(row)
    return rows


def _store(rows: list[dict]):
    if not rows:
        return
    cols = ["resort_id", "time", "lead_days"] + [c for _, c, _ in FIELDS]
    updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in cols[3:])
    with cursor() as cur:
        psycopg2.extras.execute_values(cur, f"""
            INSERT INTO forecast_hourly ({", ".join(cols)}) VALUES %s
            ON CONFLICT (resort_id, time, lead_days) DO UPDATE SET {updates}
        """, [tuple(r[c] for c in cols) for r in rows], page_size=2000)


def backfill(start: date, end: date, resort_filter: str | None = None):
    """Fill a date range for every resort, skipping days already complete
    (24 hours at both leads)."""
    resorts = [r for r in load_resorts()
               if r.get("latitude") and (resort_filter is None or r["id"] == resort_filter)]
    with cursor() as cur:
        cur.execute("""
            SELECT resort_id, time::date AS day FROM forecast_hourly
            WHERE time >= %s AND time < %s
            GROUP BY 1, 2 HAVING COUNT(*) >= %s
        """, (start, end + timedelta(days=1), 24 * len(LEADS)))
        have = {(r["resort_id"], r["day"]) for r in cur.fetchall()}

    for resort in resorts:
        missing = [start + timedelta(days=i) for i in range((end - start).days + 1)
                   if (resort["id"], start + timedelta(days=i)) not in have]
        if not missing:
            continue
        lo, hi, written = missing[0], missing[-1], 0
        chunk = lo
        while chunk <= hi:
            chunk_end = min(chunk + timedelta(days=CHUNK_DAYS - 1), hi)
            rows = []
            for attempt in (1, 2):
                try:
                    rows = _fetch(resort, chunk, chunk_end)
                    break
                except RuntimeError as e:
                    print(f"  Stopping: {e}. Re-run later; finished days are skipped.")
                    return
                except Exception as e:
                    print(f"  [{resort['id']}] {chunk}..{chunk_end}: {e}")
                    if attempt == 1:
                        time.sleep(RETRY_PAUSE_S)
            _store(rows)
            written += len(rows)
            chunk = chunk_end + timedelta(days=1)
        print(f"  [{resort['id']}] {lo}..{hi}: {written} rows")


def update_recent():
    """Today and the three days before: the day-ahead forecast for today
    already exists, and a missed run should not leave a hole."""
    today = datetime.now(timezone.utc).date()
    backfill(today - timedelta(days=3), today)


def status():
    with cursor() as cur:
        cur.execute("""
            SELECT lead_days, COUNT(DISTINCT resort_id) AS resorts,
                   MIN(time)::date AS first, MAX(time)::date AS last, COUNT(*) AS hours
            FROM forecast_hourly GROUP BY 1 ORDER BY 1
        """)
        rows = cur.fetchall()
    if not rows:
        print("No archived forecasts yet.")
    for r in rows:
        print(f"  {r['lead_days']} day ahead: {r['resorts']} resorts, "
              f"{r['first']} to {r['last']}, {r['hours']} hours")


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    args = sys.argv[1:]
    if "--update" in args:
        update_recent()
    elif "--status" in args:
        status()
    elif "--backfill" in args:
        i = args.index("--backfill")
        start = date.fromisoformat(args[i + 1])
        end = date.fromisoformat(args[i + 2])
        named = next((a for a in args[i + 3:] if not a.startswith("--")), None)
        backfill(start, end, named)
    else:
        print(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(main())
