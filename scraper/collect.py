"""
Main collection script. Run daily (or on demand).

Usage:
    python -m scraper.collect              # run all resorts
    python -m scraper.collect cervinia     # run a single resort by id
    python -m scraper.collect --init-db    # initialise database schema
"""

import json
import sys
import time
from pathlib import Path
from datetime import datetime, timezone

from .db import init_db, upsert_resort
from .scrapers import run_scraper
from .store import save_snapshot
from .weather import fetch_weather

RESORTS_FILE = Path(__file__).parent.parent / "config" / "resorts.json"


def load_resorts() -> list[dict]:
    with open(RESORTS_FILE) as f:
        return json.load(f)


def collect_all(resort_filter: str | None = None):
    resorts = load_resorts()
    if resort_filter:
        resorts = [r for r in resorts if r["id"] == resort_filter]
        if not resorts:
            print(f"No resort found with id '{resort_filter}'")
            return

    today = datetime.now(timezone.utc).date()
    print(f"\n{'='*60}")
    print(f"  Ski Lift Tracker — {today.isoformat()} UTC")
    print(f"  Collecting {len(resorts)} resort(s)")
    print(f"{'='*60}\n")

    results = []
    for resort in resorts:
        print(f"  [{resort['id']}] {resort['name']} ({resort['scraper']})...")
        try:
            snap = run_scraper(resort)

            # Fetch weather from Open-Meteo
            if resort.get("latitude") and resort.get("longitude"):
                weather = fetch_weather(resort["latitude"], resort["longitude"],
                                        resort.get("top_altitude_m"))
                snap.wind_gust_max_kmh  = weather.get("wind_gust_max_kmh")
                snap.wind_speed_max_kmh = weather.get("wind_speed_max_kmh")
                snap.temp_min_c         = weather.get("temp_min_c")
                snap.temp_max_c         = weather.get("temp_max_c")
                snap.fresh_snow_cm      = weather.get("fresh_snow_cm")
                snap.precipitation_mm   = weather.get("precipitation_mm")
                snap.weather_code       = weather.get("weather_code")

            snapshot_id = save_snapshot(snap, today)

            if snap.error:
                status = f"ERROR: {snap.error}"
            else:
                pct = snap.pct_open or 0
                bar_len = 20
                filled = int(bar_len * pct / 100)
                bar = "█" * filled + "░" * (bar_len - filled)
                status = f"{snap.lifts_open:3d}/{snap.lifts_total:<3d} [{bar}] {pct:5.1f}%"
                if snap.pistes_open_km:
                    status += f"   {snap.pistes_open_km}/{snap.pistes_total_km} km"

            print(f"         {status}")
            results.append({"resort": resort["id"], "snap": snap, "id": snapshot_id})

        except Exception as e:
            print(f"         EXCEPTION: {e}")

        # Small delay between resorts using bergfex to avoid rate limits
        if resort.get("scraper") == "bergfex":
            time.sleep(3)

    print(f"\n  Done. {sum(1 for r in results if not r['snap'].error)}/{len(results)} succeeded.\n")
    return results


def main():
    args = sys.argv[1:]

    if "--init-db" in args:
        print("Initialising database schema...")
        init_db()
        print("Loading resort list into database...")
        for resort in load_resorts():
            upsert_resort(resort)
        print(f"Loaded {len(load_resorts())} resorts.")
        return

    resort_filter = args[0] if args and not args[0].startswith("--") else None
    collect_all(resort_filter)


if __name__ == "__main__":
    main()
