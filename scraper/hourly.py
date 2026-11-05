"""Hour-by-hour model weather per resort, from Open-Meteo's historical
forecast archive (free, no key, hourly from 2022 including the 700 hPa winds
the ERA5 archive lacks).

The daily snapshot keeps only peaks. Timing is what separates a lift held at
opening and released by lunch from one shut all day, so this keeps every hour.
It is model output for a grid cell at the resort's top altitude, not a
measurement.

    python -m scraper.hourly --update                         # the collector runs this
    python -m scraper.hourly --backfill 2025-11-01 2026-05-31
    python -m scraper.hourly --status
"""
import sys
import time
from datetime import date, datetime, timedelta, timezone

import psycopg2.extras
import requests

from .collect import load_resorts
from .db import cursor

API_URL = "https://historical-forecast-api.open-meteo.com/v1/forecast"

# (API field, column, cast)
FIELDS = [
    ("wind_gusts_10m",        "wind_gust_kmh",       float),
    ("wind_speed_10m",        "wind_speed_kmh",      float),
    ("wind_direction_10m",    "wind_dir_deg",        lambda v: round(v) % 360),
    ("wind_speed_700hPa",     "wind_700hpa_kmh",     float),
    ("wind_direction_700hPa", "wind_700hpa_dir_deg", lambda v: round(v) % 360),
    ("temperature_2m",        "temp_c",              float),
    ("snowfall",              "snowfall_cm",         float),
    ("precipitation",         "precipitation_mm",    float),
    ("cloud_cover",           "cloud_cover_pct",     round),
    ("cloud_cover_low",       "cloud_cover_low_pct", round),
    ("visibility",            "visibility_m",        round),
    ("freezing_level_height", "freezing_level_m",    round),
    ("weather_code",          "weather_code",        round),
]

# Same restraint as the ERA5 backfill: one resort at a time, and a long
# request is split into month-sized pieces so no single call is heavy.
REQUEST_PAUSE_S = 3.0
CHUNK_DAYS = 31


def _fetch(resort: dict, start: date, end: date) -> list[dict]:
    params = {
        "latitude": resort["latitude"],
        "longitude": resort["longitude"],
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "hourly": ",".join(f for f, _, _ in FIELDS),
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
        row = {"resort_id": resort["id"], "time": datetime.fromisoformat(t)}
        for field, col, cast in FIELDS:
            v = (hourly.get(field) or [None] * (i + 1))[i]
            row[col] = cast(v) if v is not None else None
        # The archive returns empty hours at the far end of its range.
        if row["wind_gust_kmh"] is not None or row["temp_c"] is not None:
            rows.append(row)
    return rows


def _store(rows: list[dict]):
    if not rows:
        return
    cols = ["resort_id", "time"] + [c for _, c, _ in FIELDS]
    updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in cols[2:])
    with cursor() as cur:
        psycopg2.extras.execute_batch(cur, f"""
            INSERT INTO weather_hourly ({", ".join(cols)})
            VALUES ({", ".join(f"%({c})s" for c in cols)})
            ON CONFLICT (resort_id, time) DO UPDATE SET {updates}
        """, rows, page_size=500)


def backfill(start: date, end: date, resort_filter: str | None = None):
    """Fill a date range for every resort, skipping days already complete."""
    resorts = [r for r in load_resorts()
               if r.get("latitude") and (resort_filter is None or r["id"] == resort_filter)]
    with cursor() as cur:
        cur.execute("""
            SELECT resort_id, time::date AS day FROM weather_hourly
            WHERE time >= %s AND time < %s
            GROUP BY 1, 2 HAVING COUNT(*) >= 24
        """, (start, end + timedelta(days=1)))
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
            try:
                rows = _fetch(resort, chunk, chunk_end)
            except RuntimeError as e:
                print(f"  Stopping: {e}. Re-run later; finished days are skipped.")
                return
            except Exception as e:
                print(f"  [{resort['id']}] {chunk}..{chunk_end}: {e}")
                rows = []
            _store(rows)
            written += len(rows)
            chunk = chunk_end + timedelta(days=1)
        print(f"  [{resort['id']}] {lo}..{hi}: {written} hours")


def update_recent():
    """The last three days: the archive fills in a day or so behind real time,
    and a missed run should not leave a hole."""
    today = datetime.now(timezone.utc).date()
    backfill(today - timedelta(days=3), today - timedelta(days=1))


def status():
    with cursor() as cur:
        cur.execute("""
            SELECT resort_id, MIN(time)::date AS first, MAX(time)::date AS last,
                   COUNT(*) AS hours
            FROM weather_hourly GROUP BY 1 ORDER BY 1
        """)
        rows = cur.fetchall()
    if not rows:
        print("No hourly weather yet.")
    for r in rows:
        print(f"  {r['resort_id']:18} {r['first']} to {r['last']}  {r['hours']:>7} hours")


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
