"""
Scraper for Les Menuires (and Saint-Martin-de-Belleville) via
https://api.lesmenuires.com/get/json/area.json

Structure:
  area.lifts.sectors[n].list -> lift objects
    {id, name, type, status, is_link, opening_hours, ...}

Status values:
  "open"      -> open
  "scheduled" -> open  (normal operating day, no live update yet)
  "delayed"   -> closed
  "closed"    -> closed

Sectors and which resort_id they map to:
  Masse, Mont de la Chambre, Trois Marches -> les-menuires
  Saint-Martin-de-Belleville               -> saint-martin
"""

import requests
from .base import LiftStatus, ResortSnapshot

URL = "https://api.lesmenuires.com/get/json/area.json"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/121.0.0.0 Safari/537.36"
    ),
    "Referer": "https://lesmenuires.com/",
}

# Sectors that belong to the Saint-Martin resort entry
SAINT_MARTIN_SECTORS = {"saint-martin-de-belleville"}


def _status(raw: str) -> str | None:
    if raw in ("open", "scheduled"):
        return "open"
    if raw in ("closed", "delayed"):
        return "closed"
    return None


def _build_lifts(raw: list[dict]) -> list[LiftStatus]:
    """Disambiguate duplicate names within a resort's lift list."""
    from collections import Counter

    # Stage 1: for names that appear more than once, append sector name
    name_counts = Counter(e["name"] for e in raw)
    stage1: list[tuple[str, dict]] = []
    for e in raw:
        name = e["name"]
        if name_counts[name] > 1:
            name = f"{name} ({e['sector']})"
        stage1.append((name, e))

    # Stage 2: if still duplicated (same sector, same base name), append type
    stage1_counts = Counter(n for n, _ in stage1)
    lifts: list[LiftStatus] = []
    for name, e in stage1:
        if stage1_counts[name] > 1:
            name = f"{name} / {e['lift_type']}"
        lifts.append(LiftStatus(
            name=name,
            status=e["status"],
            lift_type=e["lift_type"],
            is_link=e["is_link"],
        ))
    return lifts


def _scrape_both() -> tuple[list[LiftStatus], list[LiftStatus], str | None]:
    """Returns (menuires_lifts, saint_martin_lifts, error)."""
    try:
        data = requests.get(URL, headers=HEADERS, timeout=15).json()
    except Exception as e:
        return [], [], str(e)

    menuires_raw: list[dict] = []
    saint_martin_raw: list[dict] = []

    for sector in data.get("area", {}).get("lifts", {}).get("sectors", []):
        sector_name = sector.get("name", "")
        target_raw = saint_martin_raw if sector_name.lower() in SAINT_MARTIN_SECTORS else menuires_raw
        for lift in sector.get("list", []):
            status = _status(lift.get("status", ""))
            if status:
                target_raw.append({
                    "name": lift["name"],
                    "status": status,
                    "lift_type": lift.get("type", ""),
                    "is_link": lift.get("is_link", False),
                    "sector": sector_name,
                })

    return _build_lifts(menuires_raw), _build_lifts(saint_martin_raw), None


def scrape(resort_id: str) -> ResortSnapshot:
    menuires_lifts, saint_martin_lifts, error = _scrape_both()
    snapshot = ResortSnapshot(resort_id=resort_id, source="api.lesmenuires.com")

    if error:
        snapshot.error = error
        return snapshot

    if resort_id == "saint-martin":
        snapshot.lifts = saint_martin_lifts
    else:
        snapshot.lifts = menuires_lifts

    if not snapshot.lifts:
        snapshot.error = "No lift data returned from API"

    return snapshot
