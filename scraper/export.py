"""Dump the whole database to local files.

Two seasons of daily observations cannot be re-collected if the Railway
Postgres is lost, so keep periodic copies:

    python -m scraper.export                 # CSVs into ./exports/<date>/
    python -m scraper.export --dir /some/dir
    python -m scraper.export --format json

Each table becomes one file; snapshots are also written joined to resort
names as `snapshots_wide.csv`, which is the form most useful for analysis.
"""
import csv
import io
import json
import sys
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from .db import cursor

TABLES = ["resorts", "lifts", "pistes", "snapshots", "lift_readings",
          "piste_readings", "source_readings", "holiday_periods", "app_settings"]

WIDE_QUERY = """
    SELECT r.id AS resort_id, r.name AS resort, r.country, r.area, r.top_altitude_m,
           s.snapshot_date, s.slot, s.snapshot_time,
           s.lifts_open, s.lifts_total, s.pct_lifts_open,
           s.pistes_open_km, s.pistes_total_km,
           s.snow_depth_mountain_cm, s.snow_depth_valley_cm, s.snow_condition,
           s.last_snowfall_date, s.piste_conditions, s.avalanche_danger,
           s.wind_gust_max_kmh, s.wind_speed_max_kmh, s.wind_700hpa_max_kmh,
           s.temp_min_c, s.temp_max_c, s.fresh_snow_cm, s.precipitation_mm,
           s.weather_code, s.sunshine_hours,
           s.freezing_level_max_m, s.freezing_level_min_m,
           s.is_uk_school_holiday, s.holiday_name, s.source, s.scrape_error
    FROM snapshots s
    JOIN resorts r ON r.id = s.resort_id
    ORDER BY s.snapshot_date, r.country, r.area, r.name, s.slot
"""


def _plain(v):
    if isinstance(v, (date, datetime)):
        return v.isoformat()
    if isinstance(v, Decimal):
        return float(v)
    return v


def _fetch(sql: str) -> list[dict]:
    with cursor() as cur:
        cur.execute(sql)
        return [{k: _plain(v) for k, v in row.items()} for row in cur.fetchall()]


def _write_csv(path: Path, rows: list[dict]):
    buf = io.StringIO()
    if rows:
        writer = csv.DictWriter(buf, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    path.write_text(buf.getvalue(), encoding="utf-8")


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    args = sys.argv[1:]
    fmt = "json" if "--format" in args and args[args.index("--format") + 1:][:1] == ["json"] else "csv"

    if "--dir" in args:
        out = Path(args[args.index("--dir") + 1])
    else:
        out = Path("exports") / date.today().isoformat()
    out.mkdir(parents=True, exist_ok=True)

    total = 0
    for table in TABLES:
        try:
            rows = _fetch(f"SELECT * FROM {table}")
        except Exception as e:
            print(f"  {table}: skipped ({e})")
            continue
        path = out / f"{table}.{fmt}"
        if fmt == "json":
            path.write_text(json.dumps(rows, indent=1), encoding="utf-8")
        else:
            _write_csv(path, rows)
        total += len(rows)
        print(f"  {table:<18} {len(rows):>7} rows -> {path.name}")

    wide = _fetch(WIDE_QUERY)
    wide_path = out / f"snapshots_wide.{fmt}"
    if fmt == "json":
        wide_path.write_text(json.dumps(wide, indent=1), encoding="utf-8")
    else:
        _write_csv(wide_path, wide)
    print(f"  {'snapshots_wide':<18} {len(wide):>7} rows -> {wide_path.name}")

    size = sum(f.stat().st_size for f in out.iterdir()) / 1024
    print(f"\n  {total} rows exported to {out.resolve()} ({size:.0f} KB)\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
