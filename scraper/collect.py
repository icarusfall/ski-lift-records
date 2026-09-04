"""
Main collection script. Run daily (or on demand).

Usage:
    python -m scraper.collect                   # midday run, all resorts
    python -m scraper.collect --slot morning    # morning run
    python -m scraper.collect --catch-up        # only resorts still missing today
    python -m scraper.collect --force           # run even if paused
    python -m scraper.collect cervinia          # single resort (ignores pause)
    python -m scraper.collect --init-db         # initialise database schema
"""

import json
import sys
import time
from pathlib import Path
from datetime import date, datetime, timezone

from .db import (init_db, upsert_resort, upsert_holiday, get_setting, set_setting,
                 get_disabled_resorts, get_collected_resorts)
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


SLOTS = ("morning", "midday")
RETRY_DELAY_S = 20


def scrape_with_retry(resort: dict, attempts: int = 2):
    """Scrape a resort, retrying once on exception or scrape error.

    A cron run is the only chance to capture a given resort-slot, so without
    a retry a transient network blip loses that reading permanently.
    """
    last_exc = None
    for attempt in range(1, attempts + 1):
        try:
            snap = run_scraper(resort)
            if not snap.error or attempt == attempts:
                return snap, None
            print(f"         attempt {attempt} failed ({snap.error}); retrying...")
        except Exception as e:
            last_exc = e
            if attempt == attempts:
                break
            print(f"         attempt {attempt} raised ({e}); retrying...")
        time.sleep(RETRY_DELAY_S)
    return None, last_exc


def collect_all(resort_filter: str | None = None, slot: str = "midday",
                catch_up: bool = False):
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

    if catch_up:
        done = get_collected_resorts(today, slot)
        resorts = [r for r in resorts if r["id"] not in done]
        if not resorts:
            print(f"Catch-up: all resorts already collected for {today} ({slot}).")
            return []
        print(f"  Catch-up: {len(resorts)} resort(s) still missing for {today} ({slot}).")

    print(f"\n{'='*60}")
    print(f"  Ski Lift Tracker — {today.isoformat()} UTC ({slot})")
    print(f"  Collecting {len(resorts)} resort(s)")
    print(f"{'='*60}\n")

    results = []
    for resort in resorts:
        print(f"  [{resort['id']}] {resort['name']} ({resort['scraper']})...")
        try:
            snap, exc = scrape_with_retry(resort)
            if snap is None:
                raise exc

            # Fetch weather from Open-Meteo
            if resort.get("latitude") and resort.get("longitude"):
                weather = fetch_weather(resort["latitude"], resort["longitude"],
                                        resort.get("top_altitude_m"))
                snap.wind_gust_max_kmh    = weather.get("wind_gust_max_kmh")
                snap.wind_speed_max_kmh   = weather.get("wind_speed_max_kmh")
                snap.temp_min_c           = weather.get("temp_min_c")
                snap.temp_max_c           = weather.get("temp_max_c")
                snap.fresh_snow_cm        = weather.get("fresh_snow_cm")
                snap.precipitation_mm     = weather.get("precipitation_mm")
                snap.weather_code         = weather.get("weather_code")
                snap.sunshine_hours       = weather.get("sunshine_hours")
                snap.freezing_level_max_m = weather.get("freezing_level_max_m")
                snap.freezing_level_min_m = weather.get("freezing_level_min_m")
                snap.wind_700hpa_max_kmh  = weather.get("wind_700hpa_max_kmh")
                snap.wind_dir_dominant_deg = weather.get("wind_dir_dominant_deg")
                snap.wind_700hpa_dir_deg   = weather.get("wind_700hpa_dir_deg")

            snapshot_id = save_snapshot(snap, today, slot)

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
    catch_up = "--catch-up" in args

    slot = "midday"
    if "--slot" in args:
        i = args.index("--slot")
        if i + 1 >= len(args) or args[i + 1] not in SLOTS:
            print(f"--slot must be followed by one of: {', '.join(SLOTS)}")
            return
        slot = args[i + 1]
        args = args[:i] + args[i + 2:]

    resort_filter = next((a for a in args if not a.startswith("--")), None)

    # The pause gate only applies to full (cron) runs; a named single-resort
    # run is always an explicit request, so it proceeds regardless.
    if resort_filter is None and not check_collection_gate(force):
        return

    collect_all(resort_filter, slot=slot, catch_up=catch_up)


if __name__ == "__main__":
    main()
