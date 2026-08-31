"""Long-run climate context from the ERA5 reanalysis, via Open-Meteo's free
archive API (no key required, data back to 1940).

The point of this table is leverage. A single season of lift observations
measures how a resort's lifts *respond* to weather; thirty-five years of
reanalysis measures how often that weather actually happens. Together they
answer planning questions after one season instead of after ten.

    python -m scraper.climate --backfill              # all resorts, 1991 onward
    python -m scraper.climate --backfill cervinia
    python -m scraper.climate --backfill --from 2010-01-01
    python -m scraper.climate --update                # top up recent days only
    python -m scraper.climate --indices               # NAO + ONI monthly series
    python -m scraper.climate --status                # coverage report

The full backfill is 36 requests total — one per resort, each covering 35
years — and never needs repeating. Use --update thereafter, which asks only
for days missing since each resort's latest stored date.

Not available historically: freezing level and pressure-level winds come back
empty from the archive endpoint, so those exist only from season 2 onward.
"""
import sys
import time
from datetime import date, datetime, timedelta, timezone

import psycopg2.extras
import requests

from .collect import load_resorts
from .db import cursor

ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
DEFAULT_START = "1991-01-01"          # start of the 1991-2020 WMO normal period

DAILY_VARS = [
    "wind_gusts_10m_max",
    "wind_speed_10m_max",
    "temperature_2m_min",
    "temperature_2m_max",
    "temperature_2m_mean",
    "snowfall_sum",
    "precipitation_sum",
    "precipitation_hours",
    "sunshine_duration",
    "snow_depth_max",
    "weather_code",
]

NAO_URL = ("https://www.cpc.ncep.noaa.gov/products/precip/CWlink/pna/"
           "norm.nao.monthly.b5001.current.ascii")
ONI_URL = "https://www.cpc.ncep.noaa.gov/data/indices/oni.ascii.txt"

REQUEST_PAUSE_S = 2.0
MAX_ATTEMPTS = 4


def _fetch(resort: dict, start: str, end: str) -> dict:
    """One archive request covering a resort's whole date range."""
    params = {
        "latitude": resort["latitude"],
        "longitude": resort["longitude"],
        "start_date": start,
        "end_date": end,
        "daily": ",".join(DAILY_VARS),
        "timezone": "UTC",
    }
    if resort.get("top_altitude_m"):
        params["elevation"] = resort["top_altitude_m"]

    delay = 5.0
    for attempt in range(1, MAX_ATTEMPTS + 1):
        resp = requests.get(ARCHIVE_URL, params=params, timeout=180)
        if resp.status_code == 200:
            return resp.json().get("daily", {})
        # The free tier throttles; back off rather than losing the resort.
        if resp.status_code in (429, 500, 502, 503) and attempt < MAX_ATTEMPTS:
            print(f"         rate-limited ({resp.status_code}); waiting {delay:.0f}s")
            time.sleep(delay)
            delay *= 2
            continue
        raise RuntimeError(f"{resp.status_code}: {resp.text[:120]}")
    raise RuntimeError("exhausted retries")


def _rows(resort_id: str, daily: dict) -> list[tuple]:
    times = daily.get("time", [])
    cols = {v: daily.get(v, []) for v in DAILY_VARS}

    def val(name, i):
        seq = cols.get(name) or []
        return seq[i] if i < len(seq) else None

    out = []
    for i, t in enumerate(times):
        sun = val("sunshine_duration", i)
        out.append((
            resort_id, t,
            val("wind_gusts_10m_max", i), val("wind_speed_10m_max", i),
            val("temperature_2m_min", i), val("temperature_2m_max", i),
            val("temperature_2m_mean", i),
            val("snowfall_sum", i), val("precipitation_sum", i),
            val("precipitation_hours", i),
            round(sun / 3600, 1) if sun is not None else None,
            val("snow_depth_max", i), val("weather_code", i),
        ))
    return out


def _store(rows: list[tuple]) -> int:
    if not rows:
        return 0
    with cursor() as cur:
        psycopg2.extras.execute_values(cur, """
            INSERT INTO climate_daily
                (resort_id, date, wind_gust_max_kmh, wind_speed_max_kmh,
                 temp_min_c, temp_max_c, temp_mean_c, snowfall_cm,
                 precipitation_mm, precip_hours, sunshine_hours,
                 snow_depth_model_m, weather_code)
            VALUES %s
            ON CONFLICT (resort_id, date) DO UPDATE SET
                wind_gust_max_kmh  = EXCLUDED.wind_gust_max_kmh,
                wind_speed_max_kmh = EXCLUDED.wind_speed_max_kmh,
                temp_min_c         = EXCLUDED.temp_min_c,
                temp_max_c         = EXCLUDED.temp_max_c,
                temp_mean_c        = EXCLUDED.temp_mean_c,
                snowfall_cm        = EXCLUDED.snowfall_cm,
                precipitation_mm   = EXCLUDED.precipitation_mm,
                precip_hours       = EXCLUDED.precip_hours,
                sunshine_hours     = EXCLUDED.sunshine_hours,
                snow_depth_model_m = EXCLUDED.snow_depth_model_m,
                weather_code       = EXCLUDED.weather_code
        """, rows, page_size=2000)
    return len(rows)


def backfill(resort_filter: str | None = None, start: str = DEFAULT_START,
             end: str | None = None):
    if end is None:
        end = (datetime.now(timezone.utc).date() - timedelta(days=6)).isoformat()

    resorts = [r for r in load_resorts()
               if r.get("latitude") and r.get("longitude")
               and (resort_filter is None or r["id"] == resort_filter)]
    if not resorts:
        print(f"No resort found with id '{resort_filter}'")
        return

    print(f"\nERA5 backfill {start} -> {end} for {len(resorts)} resort(s)\n")
    total = 0
    for resort in resorts:
        print(f"  [{resort['id']}] {resort['name']}...")
        try:
            daily = _fetch(resort, start, end)
            n = _store(_rows(resort["id"], daily))
            total += n
            print(f"         {n:,} days stored")
        except Exception as e:
            print(f"         FAILED: {e}")
        time.sleep(REQUEST_PAUSE_S)
    print(f"\n  {total:,} resort-days stored.\n")


def update_recent():
    """Top up each resort from its latest stored day, and no further back.

    ERA5 lags real time by about five days, so this is worth running monthly
    at most — it exists so the archive stays current without ever re-requesting
    the 35 years already held.
    """
    end = (datetime.now(timezone.utc).date() - timedelta(days=6))
    with cursor() as cur:
        cur.execute("SELECT resort_id, MAX(date) AS last FROM climate_daily GROUP BY resort_id")
        latest = {r["resort_id"]: r["last"] for r in cur.fetchall()}

    resorts = [r for r in load_resorts() if r.get("latitude")]
    todo = []
    for r in resorts:
        have = latest.get(r["id"])
        start = (have + timedelta(days=1)) if have else date.fromisoformat(DEFAULT_START)
        if start <= end:
            todo.append((r, start.isoformat()))

    if not todo:
        print("\n  Climate archive already current.\n")
        return
    print(f"\n  Topping up {len(todo)} resort(s) to {end}\n")
    total = 0
    for resort, start in todo:
        try:
            n = _store(_rows(resort["id"], _fetch(resort, start, end.isoformat())))
            total += n
            print(f"  {resort['id']:<16} +{n} days")
        except Exception as e:
            print(f"  {resort['id']:<16} FAILED: {e}")
        time.sleep(REQUEST_PAUSE_S)
    print(f"\n  {total:,} days added.\n")


def load_indices():
    """Monthly NAO and ONI series from NOAA CPC."""
    stored = 0

    nao = requests.get(NAO_URL, timeout=60).text
    rows = []
    for line in nao.splitlines():
        parts = line.split()
        if len(parts) == 3:
            try:
                rows.append(("nao", int(parts[0]), int(parts[1]), float(parts[2])))
            except ValueError:
                continue
    stored += _store_indices(rows)
    print(f"  nao: {len(rows)} monthly values")

    # ONI is published as overlapping 3-month seasons; the centre month is the
    # conventional label, so DJF 1950 is recorded as January 1950.
    oni = requests.get(ONI_URL, timeout=60).text
    seasons = ["DJF", "JFM", "FMA", "MAM", "AMJ", "MJJ",
               "JJA", "JAS", "ASO", "SON", "OND", "NDJ"]
    rows = []
    for line in oni.splitlines():
        parts = line.split()
        if len(parts) == 4 and parts[0] in seasons:
            try:
                year, anom = int(parts[1]), float(parts[3])
            except ValueError:
                continue
            month = seasons.index(parts[0]) + 1
            rows.append(("oni", year, month, anom))
    stored += _store_indices(rows)
    print(f"  oni: {len(rows)} monthly values")
    return stored


def _store_indices(rows: list[tuple]) -> int:
    if not rows:
        return 0
    with cursor() as cur:
        psycopg2.extras.execute_values(cur, """
            INSERT INTO climate_indices (index_name, year, month, value)
            VALUES %s
            ON CONFLICT (index_name, year, month) DO UPDATE
                SET value = EXCLUDED.value
        """, rows, page_size=1000)
    return len(rows)


def status():
    with cursor() as cur:
        cur.execute("""
            SELECT r.id, r.name, COUNT(c.date) AS days,
                   MIN(c.date) AS first, MAX(c.date) AS last
            FROM resorts r LEFT JOIN climate_daily c ON c.resort_id = r.id
            GROUP BY r.id, r.name ORDER BY days DESC, r.name
        """)
        rows = cur.fetchall()
        cur.execute("SELECT index_name, COUNT(*) n, MAX(year) y FROM climate_indices GROUP BY index_name")
        idx = cur.fetchall()
    covered = sum(1 for r in rows if r["days"])
    print(f"\n  climate_daily: {covered}/{len(rows)} resorts covered")
    for r in rows[:4]:
        print(f"    {r['name'][:26]:<26} {r['days']:>7,} days  {r['first']} -> {r['last']}")
    missing = [r["id"] for r in rows if not r["days"]]
    if missing:
        print(f"    missing: {', '.join(missing)}")
    for i in idx:
        print(f"  {i['index_name']}: {i['n']} months, latest year {i['y']}")
    print()


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    args = sys.argv[1:]

    if "--update" in args:
        update_recent()
        return 0
    if "--indices" in args:
        print("\nLoading NAO and ONI monthly indices...")
        load_indices()
        print()
        return 0
    if "--status" in args:
        status()
        return 0
    if "--backfill" in args:
        start = DEFAULT_START
        if "--from" in args:
            i = args.index("--from")
            start = args[i + 1]
            args = args[:i] + args[i + 2:]
        named = next((a for a in args if not a.startswith("--")), None)
        backfill(named, start)
        return 0

    print(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(main())
