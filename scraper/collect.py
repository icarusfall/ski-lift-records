"""
Main collection script. Run daily (or on demand).

Usage:
    python -m scraper.collect              # run all resorts (respects pause state)
    python -m scraper.collect --force      # run all resorts even if paused
    python -m scraper.collect cervinia     # run a single resort by id (ignores pause)
    python -m scraper.collect --init-db    # initialise database schema
"""

import json
import sys
import time
from pathlib import Path
from datetime import date, datetime, timezone

from .db import (init_db, upsert_resort, upsert_holiday, get_setting, set_setting,
                 get_disabled_resorts)
from .holidays import load_holidays
from .scrapers import run_scraper
from .store import save_snapshot
from .weather import fetch_weather

RESORTS_FILE = Path(__file__).parent.parent / "config" / "resorts.json"


def load_resorts() -> list[dict]:
    with open(RESORTS_FILE, encoding="utf-8") as f:
        return json.load(f)


def check_collection_gate(force: bool = False) -> bool:
    """Return True if a full collection run should proceed.

    Pause state lives in app_settings so it can be flipped from the web
    admin panel. Auto pause/resume dates let the season start and end
    without anyone remembering to press the button.
    """
    today = datetime.now(timezone.utc).date()
    paused = get_setting("collection_paused", "false") == "true"

    def _date_setting(key: str) -> date | None:
        raw = get_setting(key)
        if not raw:
            return None
        try:
            return date.fromisoformat(raw)
        except ValueError:
            print(f"  Ignoring invalid {key}: {raw!r}")
            return None

    resume_on = _date_setting("auto_resume_date")
    pause_on = _date_setting("auto_pause_date")

    if paused and resume_on and today >= resume_on:
        set_setting("collection_paused", "false")
        set_setting("auto_resume_date", "")
        print(f"Auto-resumed collection ({resume_on.isoformat()} reached).")
        paused = False

    if not paused and pause_on and today >= pause_on:
        set_setting("collection_paused", "true")
        set_setting("auto_pause_date", "")
        print(f"Auto-paused collection ({pause_on.isoformat()} reached).")
        paused = True

    if paused and not force:
        print("Collection is paused (toggle it on /admin, or use --force).")
        return False
    return True


def collect_all(resort_filter: str | None = None):
    resorts = load_resorts()
    if resort_filter:
        resorts = [r for r in resorts if r["id"] == resort_filter]
        if not resorts:
            print(f"No resort found with id '{resort_filter}'")
            return
    else:
        disabled = get_disabled_resorts()
        if disabled:
            resorts = [r for r in resorts if r["id"] not in disabled]
            print(f"  Skipping {len(disabled)} disabled resort(s): {', '.join(sorted(disabled))}")

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

            results.append({"resort": resort["id"], "snap": snap, "id": snapshot_id})
            print(f"         {status}")

        except Exception as e:
            print(f"         EXCEPTION: {e}")

        # Small delay between resorts using bergfex to avoid rate limits
        if resort.get("scraper") == "bergfex":
            time.sleep(3)

    print(f"\n  Done. {sum(1 for r in results if not r['snap'].error)}/{len(results)} succeeded.\n")
    return results


def main():
    # Windows consoles default to cp1252, which can't print the progress bars
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    args = sys.argv[1:]

    if "--init-db" in args:
        print("Initialising database schema...")
        init_db()
        print("Loading resort list into database...")
        for resort in load_resorts():
            upsert_resort(resort)
        print(f"Loaded {len(load_resorts())} resorts.")
        print("Loading school holiday periods...")
        holidays = load_holidays()
        for h in holidays:
            upsert_holiday(h)
        print(f"Loaded {len(holidays)} holiday periods.")
        return

    force = "--force" in args
    resort_filter = next((a for a in args if not a.startswith("--")), None)

    # The pause gate only applies to full (cron) runs; a named single-resort
    # run is always an explicit request, so it proceeds regardless.
    if resort_filter is None and not check_collection_gate(force):
        return

    collect_all(resort_filter)


if __name__ == "__main__":
    main()
