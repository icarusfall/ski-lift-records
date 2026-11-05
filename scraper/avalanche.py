"""Official avalanche danger per resort, from the EAWS bulletin archive.

Every European warning service (SLF, Météo-France, the Austrian and Italian
regional services, Andorra, Aran) publishes its daily bulletin in the common
CAAMLv6 format, and avalanche.report keeps one file per region per day:

    https://static.avalanche.report/eaws_bulletins/{date}/{date}-{file}.json

The archive starts in winter 2021/22, so history comes for free alongside the
daily capture. Each resort's micro-region lives in config/avalanche_regions.json.

    python -m scraper.avalanche --update                 # today + yesterday (the collector runs this)
    python -m scraper.avalanche --backfill 2026-02-14 2026-02-22
    python -m scraper.avalanche --status                 # coverage by season

A day with no bulletin (most of November, and all summer) simply has no row.
"""
import json
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import psycopg2.extras
import requests

from .db import cursor

ARCHIVE_URL = "https://static.avalanche.report/eaws_bulletins/{d}/{d}-{file}.json"
REGIONS_FILE = Path(__file__).parent.parent / "config" / "avalanche_regions.json"
HEADERS = {"User-Agent": "ski-lift-records/1.0 (hobby research project)"}

# A static file host run by a small public service: one file at a time.
REQUEST_PAUSE_S = 1.5

DANGER = {"low": 1, "moderate": 2, "considerable": 3, "high": 4, "very_high": 5}


def load_regions() -> dict[str, dict]:
    return json.loads(REGIONS_FILE.read_text(encoding="utf-8"))["resorts"]


def _fetch(day: date, file: str) -> list[dict] | None:
    """The day's bulletins for one region file; None when none was published."""
    d = day.isoformat()
    resp = requests.get(ARCHIVE_URL.format(d=d, file=file), headers=HEADERS, timeout=30)
    time.sleep(REQUEST_PAUSE_S)
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return resp.json().get("bulletins", [])


def _pick(bulletins: list[dict], region_ids: list[str], day: date) -> tuple[str, dict] | None:
    """The bulletin covering this resort at midday, else the latest issued.

    The Swiss service issues an evening bulletin and a morning update for the
    same day; midday is when the collector looks at the lifts, so that is the
    one to match.
    """
    noon = datetime(day.year, day.month, day.day, 11, tzinfo=timezone.utc)
    candidates = []
    for b in bulletins:
        ids = {r["regionID"] for r in b.get("regions", [])}
        region = next((r for r in region_ids if r in ids), None)
        if not region:
            continue
        vt = b.get("validTime", {})
        try:
            covers = (datetime.fromisoformat(vt["startTime"]) <= noon
                      < datetime.fromisoformat(vt["endTime"]))
        except (KeyError, ValueError):
            covers = False
        candidates.append((covers, b.get("publicationTime", ""), region, b))
    if not candidates:
        return None
    covers, _, region, b = max(candidates, key=lambda c: (c[0], c[1]))
    return region, b


def _parse(b: dict) -> dict:
    """Danger levels from one bulletin, split by elevation where it says so."""
    low = high = top = None
    split = None
    for r in b.get("dangerRatings", []):
        level = DANGER.get(r.get("mainValue"))
        if level is None:
            continue
        top = max(top or 0, level)
        elev = r.get("elevation") or {}
        if "lowerBound" in elev:
            high = max(high or 0, level)
            split = elev["lowerBound"]
        elif "upperBound" in elev:
            low = max(low or 0, level)
            split = elev["upperBound"]
    if split is None:
        low = high = top
    problems = sorted({p["problemType"] for p in b.get("avalancheProblems", [])
                       if p.get("problemType")})
    return {
        "danger_max": top,
        "danger_low": low,
        "danger_high": high,
        "elevation_split": str(split) if split is not None else None,
        "problems": ",".join(problems) or None,
        "bulletin_id": b.get("bulletinID"),
        "published_at": b.get("publicationTime"),
    }


def collect_day(day: date, regions: dict[str, dict] | None = None) -> int:
    """Store one day's danger for every resort. Returns rows written."""
    regions = regions or load_regions()
    by_file: dict[str, list[str]] = {}
    for resort_id, cfg in regions.items():
        by_file.setdefault(cfg["file"], []).append(resort_id)

    rows = []
    for file, resort_ids in sorted(by_file.items()):
        try:
            bulletins = _fetch(day, file)
        except Exception as e:
            print(f"  {day} {file}: {e}")
            continue
        if not bulletins:
            continue
        for resort_id in resort_ids:
            picked = _pick(bulletins, regions[resort_id]["regions"], day)
            if picked:
                region, b = picked
                rows.append({"resort_id": resort_id, "date": day,
                             "region_id": region, **_parse(b)})

    if not rows:
        return 0
    with cursor() as cur:
        psycopg2.extras.execute_batch(cur, """
            INSERT INTO avalanche_daily
                (resort_id, date, region_id, danger_max, danger_low, danger_high,
                 elevation_split, problems, bulletin_id, published_at)
            VALUES (%(resort_id)s, %(date)s, %(region_id)s, %(danger_max)s,
                    %(danger_low)s, %(danger_high)s, %(elevation_split)s,
                    %(problems)s, %(bulletin_id)s, %(published_at)s)
            ON CONFLICT (resort_id, date) DO UPDATE SET
                region_id = EXCLUDED.region_id, danger_max = EXCLUDED.danger_max,
                danger_low = EXCLUDED.danger_low, danger_high = EXCLUDED.danger_high,
                elevation_split = EXCLUDED.elevation_split, problems = EXCLUDED.problems,
                bulletin_id = EXCLUDED.bulletin_id, published_at = EXCLUDED.published_at,
                fetched_at = NOW()
        """, rows)
        # snapshots.avalanche_danger was meant to come from bergfex and never
        # did; fill it from the official figure so exports carry it too.
        cur.execute("""
            UPDATE snapshots s SET avalanche_danger = a.danger_max
            FROM avalanche_daily a
            WHERE a.resort_id = s.resort_id AND a.date = s.snapshot_date
              AND a.date = %s
              AND s.avalanche_danger IS DISTINCT FROM a.danger_max
        """, (day,))
    return len(rows)


def update_recent():
    """Today and yesterday: bulletins are revised in the morning, and a missed
    run should not leave a hole."""
    today = datetime.now(timezone.utc).date()
    regions = load_regions()
    for day in (today - timedelta(days=1), today):
        n = collect_day(day, regions)
        print(f"Avalanche danger {day}: {n} resorts.")


def backfill(start: date, end: date):
    """Fill a date range, skipping days already held for every resort."""
    regions = load_regions()
    with cursor() as cur:
        cur.execute("""
            SELECT date FROM avalanche_daily WHERE date BETWEEN %s AND %s
            GROUP BY date HAVING COUNT(*) >= %s
        """, (start, end, len(regions)))
        done = {r["date"] for r in cur.fetchall()}
    day = start
    while day <= end:
        if day not in done:
            n = collect_day(day, regions)
            print(f"{day}: {n} resorts")
        day += timedelta(days=1)


def status():
    with cursor() as cur:
        cur.execute("""
            SELECT CASE WHEN EXTRACT(MONTH FROM date) >= 7
                        THEN EXTRACT(YEAR FROM date)::int
                        ELSE EXTRACT(YEAR FROM date)::int - 1 END AS season,
                   COUNT(*) AS rows, COUNT(DISTINCT date) AS days,
                   COUNT(DISTINCT resort_id) AS resorts,
                   MIN(date) AS first, MAX(date) AS last
            FROM avalanche_daily GROUP BY 1 ORDER BY 1
        """)
        rows = cur.fetchall()
    if not rows:
        print("No avalanche data yet.")
    for r in rows:
        print(f"  {r['season']}/{(r['season'] + 1) % 100:02d}: {r['days']} days, "
              f"{r['resorts']} resorts, {r['rows']} rows ({r['first']} to {r['last']})")


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
        end = date.fromisoformat(args[i + 2]) if len(args) > i + 2 else start
        backfill(start, end)
    else:
        print(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(main())
