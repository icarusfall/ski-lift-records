"""Measured weather from official stations near the lifts.

The modelled hourly wind (scraper/hourly.py) is a grid cell. These are real
anemometers: MeteoSwiss SwissMetNet (hourly CSVs on data.geo.admin.ch) and
SLF IMIS (half-hourly; a live API for the last 24 hours, plus one CSV per
station with the full history, refreshed weekly). Both are free, keyless and
CC BY. Stations are listed in config/stations.json.

    python -m scraper.stations --update      # recent hours (the collector runs this)
    python -m scraper.stations --backfill    # full history, every station
    python -m scraper.stations --backfill slf-ZER1
    python -m scraper.stations --status

Every row is the hour ENDING at `time` (UTC), matching weather_hourly.
"""
import csv
import io
import json
import math
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg2.extras
import requests

from .db import cursor

STATIONS_FILE = Path(__file__).parent.parent / "config" / "stations.json"
HEADERS = {"User-Agent": "ski-lift-records/1.0 (hobby research project)"}
REQUEST_PAUSE_S = 2.0

MS_BASE = "https://data.geo.admin.ch/ch.meteoschweiz.ogd-smn/{c}/ogd-smn_{c}_h_{part}.csv"
MS_DECADES = ["historical_1990-1999", "historical_2000-2009",
              "historical_2010-2019", "historical_2020-2029"]
# MeteoSwiss column -> our column. Wind already in km/h.
MS_COLUMNS = {
    "tre200h0": "temp_c",
    "ure200h0": "rh_pct",
    "fu3010h0": "wind_speed_kmh",
    "fu3010h1": "wind_gust_kmh",
    "dkl010h0": "wind_dir_deg",
    "rre150h0": "precip_mm",
    "sre000h0": "sunshine_min",
    "prestah0": "pressure_hpa",
    "htoauths": "snow_depth_cm",
}

SLF_HISTORY = "https://measurement-data.slf.ch/imis/data/by_station/{c}.csv"
SLF_LIVE = "https://measurement-api.slf.ch/public/api/imis/station/{c}/measurements"

COLUMNS = ["temp_c", "rh_pct", "wind_speed_kmh", "wind_gust_kmh", "wind_dir_deg",
           "precip_mm", "sunshine_min", "pressure_hpa", "snow_depth_cm", "n_samples"]
INTEGER_COLUMNS = {"rh_pct", "wind_dir_deg", "sunshine_min", "snow_depth_cm"}


def load_stations() -> list[dict]:
    return json.loads(STATIONS_FILE.read_text(encoding="utf-8"))["stations"]


def _get(url: str, **kw) -> requests.Response:
    resp = requests.get(url, headers=HEADERS, timeout=kw.pop("timeout", 120), **kw)
    time.sleep(REQUEST_PAUSE_S)
    return resp


def _num(v):
    if v is None or v == "":
        return None
    try:
        return float(v)
    except ValueError:
        return None


# ---------------------------------------------------------------- MeteoSwiss

def _meteoswiss_rows(station: dict, part: str, since: datetime | None = None) -> list[dict]:
    resp = _get(MS_BASE.format(c=station["code"].lower(), part=part))
    if resp.status_code == 404:
        return []
    resp.raise_for_status()
    # The files are latin-1 and semicolon-separated.
    reader = csv.DictReader(io.StringIO(resp.content.decode("latin-1")), delimiter=";")
    rows = []
    for r in reader:
        t = datetime.strptime(r["reference_timestamp"], "%d.%m.%Y %H:%M")
        if since and t <= since:
            continue
        row = {"station_id": station["id"], "time": t, "n_samples": 1}
        for src, col in MS_COLUMNS.items():
            v = _num(r.get(src))
            row[col] = round(v) if v is not None and col in INTEGER_COLUMNS else v
        if row["wind_dir_deg"] is not None:
            row["wind_dir_deg"] %= 360
        rows.append(row)
    return rows


# ---------------------------------------------------------------------- SLF

def _hour_ending(t: datetime) -> datetime:
    """A reading at 08:30 or 09:00 belongs to the hour ending 09:00."""
    floor = t.replace(minute=0, second=0, microsecond=0)
    return floor if t == floor else floor + timedelta(hours=1)


def _slf_hourly(station: dict, samples, since: datetime | None = None) -> list[dict]:
    """Roll half-hourly IMIS samples (dicts with SLF field names) into hours."""
    hours: dict[datetime, list[dict]] = {}
    for s in samples:
        raw = s["measure_date"].replace("Z", "+00:00")
        t = datetime.fromisoformat(raw).astimezone(timezone.utc).replace(tzinfo=None)
        h = _hour_ending(t)
        if since and h <= since:
            continue
        hours.setdefault(h, []).append(s)

    rows = []
    for h, group in sorted(hours.items()):
        def vals(field):
            return [v for v in (_num(g.get(field)) for g in group) if v is not None]
        temp, rh = vals("TA_30MIN_MEAN"), vals("RH_30MIN_MEAN")
        speed, gust = vals("VW_30MIN_MEAN"), vals("VW_30MIN_MAX")
        # Direction: speed-weighted vector mean, so a calm half hour does not
        # drag the bearing about.
        x = y = 0.0
        for g in group:
            d, v = _num(g.get("DW_30MIN_MEAN")), _num(g.get("VW_30MIN_MEAN"))
            if d is not None and v:
                x += v * math.sin(math.radians(d))
                y += v * math.cos(math.radians(d))
        direction = round(math.degrees(math.atan2(x, y))) % 360 if (x or y) else None
        rows.append({
            "station_id": station["id"], "time": h,
            "temp_c": round(sum(temp) / len(temp), 1) if temp else None,
            "rh_pct": round(sum(rh) / len(rh)) if rh else None,
            # m/s -> km/h
            "wind_speed_kmh": round(sum(speed) / len(speed) * 3.6, 1) if speed else None,
            "wind_gust_kmh": round(max(gust) * 3.6, 1) if gust else None,
            "wind_dir_deg": direction,
            "precip_mm": None, "sunshine_min": None, "pressure_hpa": None,
            "snow_depth_cm": (round(vals("HS")[-1]) if vals("HS") else None),
            "n_samples": len(group),
        })
    return rows


def _slf_history(station: dict) -> list[dict]:
    """The full-history CSV (tens of MB), streamed rather than held whole."""
    resp = requests.get(SLF_HISTORY.format(c=station["code"]), headers=HEADERS,
                        timeout=300, stream=True)
    resp.raise_for_status()
    lines = (l.decode("utf-8") for l in resp.iter_lines() if l)
    rows = _slf_hourly(station, csv.DictReader(lines))
    time.sleep(REQUEST_PAUSE_S)
    return rows


def _slf_live(station: dict, since: datetime | None = None) -> list[dict]:
    resp = _get(SLF_LIVE.format(c=station["code"]), timeout=30)
    resp.raise_for_status()
    rows = _slf_hourly(station, resp.json(), since)
    # The first hour of a 24-hour window may hold only its second half.
    return [r for r in rows if r["n_samples"] >= 2] if rows else rows


# -------------------------------------------------------------------- store

def _register(stations: list[dict]):
    with cursor() as cur:
        for s in stations:
            cur.execute("""
                INSERT INTO stations (id, source, code, name, latitude, longitude,
                                      elevation_m, resort_ids, note)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (id) DO UPDATE SET
                    source = EXCLUDED.source, code = EXCLUDED.code, name = EXCLUDED.name,
                    latitude = EXCLUDED.latitude, longitude = EXCLUDED.longitude,
                    elevation_m = EXCLUDED.elevation_m, resort_ids = EXCLUDED.resort_ids,
                    note = EXCLUDED.note
            """, (s["id"], s["source"], s["code"], s["name"], s["latitude"],
                  s["longitude"], s["elevation_m"], ",".join(s["resort_ids"]), s.get("note")))


def _store(rows: list[dict]) -> int:
    if not rows:
        return 0
    cols = ["station_id", "time"] + COLUMNS
    updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in COLUMNS)
    with cursor() as cur:
        psycopg2.extras.execute_values(cur, f"""
            INSERT INTO station_hourly ({", ".join(cols)}) VALUES %s
            ON CONFLICT (station_id, time) DO UPDATE SET {updates}
        """, [tuple(r[c] for c in cols) for r in rows], page_size=5000)
    return len(rows)


def _latest(station_id: str) -> datetime | None:
    with cursor() as cur:
        cur.execute("SELECT MAX(time) AS t FROM station_hourly WHERE station_id = %s",
                    (station_id,))
        return cur.fetchone()["t"]


# ------------------------------------------------------------------ actions

def backfill(station_filter: str | None = None):
    stations = [s for s in load_stations()
                if station_filter is None or s["id"] == station_filter]
    _register(stations)
    for s in stations:
        total = 0
        if s["source"] == "meteoswiss":
            for part in MS_DECADES + ["recent", "now"]:
                n = _store(_meteoswiss_rows(s, part))
                print(f"  [{s['id']}] {part}: {n} hours")
                total += n
        elif s["source"] == "slf":
            total += _store(_slf_history(s))
            total += _store(_slf_live(s))
        print(f"  [{s['id']}] {total} hours written")


def _first_gap(station_id: str, days: int = 21) -> datetime | None:
    """The last stored hour before the earliest hole in the past few weeks."""
    with cursor() as cur:
        cur.execute("""
            SELECT prev FROM (
                SELECT time, LAG(time) OVER (ORDER BY time) AS prev
                FROM station_hourly
                WHERE station_id = %s AND time > NOW() - make_interval(days => %s)
            ) x WHERE time - prev > INTERVAL '1 hour' ORDER BY prev LIMIT 1
        """, (station_id, days))
        row = cur.fetchone()
    return row["prev"] if row else None


def _slf_refill(s: dict):
    """SLF's live API keeps 24 hours, so a missed day leaves a hole that only
    the full-history file can fill. That file is refreshed about weekly; pull
    it (tens of MB) only when it is newer than the hole, so at most once a
    week and never while the hole is still beyond its end."""
    gap = _first_gap(s["id"])
    if not gap:
        return
    head = requests.head(SLF_HISTORY.format(c=s["code"]), headers=HEADERS, timeout=30)
    modified = head.headers.get("Last-Modified")
    if not modified:
        return
    file_time = datetime.strptime(modified, "%a, %d %b %Y %H:%M:%S GMT")
    if file_time <= gap + timedelta(hours=6):
        print(f"  [{s['id']}] hole after {gap:%Y-%m-%d %H:%M}; "
              f"history file ({file_time:%Y-%m-%d}) does not reach it yet")
        return
    n = _store(_slf_history(s))
    print(f"  [{s['id']}] refilled from history file ({file_time:%Y-%m-%d}): {n} hours")


def update_recent():
    """New hours since the last stored one. MeteoSwiss 'recent' runs from 1
    January to yesterday and 'now' covers today, so together they close any
    gap this year. SLF's live API holds only 24 hours; holes it leaves are
    filled from the weekly history file once that catches up."""
    stations = load_stations()
    _register(stations)
    for s in stations:
        try:
            latest = _latest(s["id"])
            # Step back two hours so a part-filled last hour is redone whole.
            since = latest - timedelta(hours=2) if latest else None
            if s["source"] == "meteoswiss":
                n = sum(_store(_meteoswiss_rows(s, p, since)) for p in ("recent", "now"))
            else:
                n = _store(_slf_live(s, since))
                _slf_refill(s)
            print(f"Station {s['id']}: {n} new hours.")
        except Exception as e:
            print(f"Station {s['id']} skipped: {e}")


def status():
    with cursor() as cur:
        cur.execute("""
            SELECT station_id, MIN(time) AS first, MAX(time) AS last, COUNT(*) AS hours,
                   COUNT(wind_gust_kmh) AS gust_hours
            FROM station_hourly GROUP BY 1 ORDER BY 1
        """)
        rows = cur.fetchall()
    if not rows:
        print("No station data yet.")
    for r in rows:
        print(f"  {r['station_id']:16} {r['first']:%Y-%m-%d} to {r['last']:%Y-%m-%d %H:%M}  "
              f"{r['hours']:>7} hours, {r['gust_hours']:>7} with gusts")


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    args = sys.argv[1:]
    if "--update" in args:
        update_recent()
    elif "--status" in args:
        status()
    elif "--backfill" in args:
        named = next((a for a in args if not a.startswith("--")), None)
        backfill(named)
    else:
        print(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(main())
