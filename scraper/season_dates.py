"""Planned season opening against the first day lifts were actually seen open.

The planned dates in config/season_dates.json were written down on 4 Nov 2026,
before the season, so the comparison cannot be tuned after the fact. The
question it serves: did the strong El Nino autumn delay the start?

    python -m scraper.season_dates           # planned vs observed, every resort

"Observed open" is the first clean snapshot since 1 Sep with any lift open, and
"half open" the first with at least half of them. A resort already open when
collection resumed on 1 Nov shows that date with a note, since the true first
day is earlier and unseen.
"""
import json
import sys
from datetime import date
from pathlib import Path

from .db import cursor

DATES_FILE = Path(__file__).parent.parent / "config" / "season_dates.json"
SEASON_START = date(2026, 9, 1)
COLLECTION_RESUMED = date(2026, 11, 1)


def load_planned() -> dict[str, dict]:
    return json.loads(DATES_FILE.read_text(encoding="utf-8"))["resorts"]


def observed() -> dict[str, dict]:
    with cursor() as cur:
        cur.execute("""
            SELECT resort_id,
                   MIN(snapshot_date) FILTER (WHERE lifts_open > 0) AS first_open,
                   MIN(snapshot_date) FILTER (WHERE pct_lifts_open >= 50) AS half_open
            FROM snapshots
            WHERE snapshot_date >= %s AND scrape_error IS NULL
            GROUP BY resort_id
        """, (SEASON_START,))
        return {r["resort_id"]: r for r in cur.fetchall()}


def report():
    planned = load_planned()
    seen = observed()
    print(f"{'resort':18} {'early':>10} {'planned':>10} {'first open':>11} {'late by':>8} {'half open':>10}")
    for resort_id, p in planned.items():
        o = seen.get(resort_id, {})
        first, half = o.get("first_open"), o.get("half_open")
        target = date.fromisoformat(p["open"]) if p.get("open") else None
        late = f"{(first - target).days:+d}d" if first and target else ""
        note = " (open on resume)" if first == COLLECTION_RESUMED else ""
        print(f"{resort_id:18} {p.get('early', ''):>10} {p.get('open', ''):>10} "
              f"{str(first or '-'):>11} {late:>8} {str(half or '-'):>10}{note}")


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    report()
    return 0


if __name__ == "__main__":
    sys.exit(main())
