"""
Scraper for La Plagne using the ouvertures.la-plagne.com JSON API.

Two endpoints:
  datas_statics.json  - lift names, types, sectors (changes rarely)
  datas_dynamics.json - live openingStatus per lift ID (updated ~1 min)

Structure:
  statics['poi'][n]['lifts'] -> list of {id, name, type, liftType, ...}
  dynamics['poi'][n]['lifts'] -> list of {id, openingStatus: OPEN|CLOSED, ...}

Only entries with type == "SKI_LIFT" are included (excludes trails/pistes).
"""

import requests
from .base import LiftStatus, ResortSnapshot

BASE = "https://ouvertures.la-plagne.com/"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/121.0.0.0 Safari/537.36"
    ),
    "Referer": "https://en.la-plagne.com/",
}


def scrape(resort_id: str = "la-plagne") -> ResortSnapshot:
    snapshot = ResortSnapshot(resort_id=resort_id, source="ouvertures.la-plagne.com")
    try:
        statics = requests.get(BASE + "datas_statics.json", headers=HEADERS, timeout=15).json()
        dynamics = requests.get(BASE + "datas_dynamics.json", headers=HEADERS, timeout=15).json()
    except Exception as e:
        snapshot.error = str(e)
        return snapshot

    # Build lift name/type map from statics
    static_map: dict[str, dict] = {}
    for poi in statics.get("poi", []):
        for lift in poi.get("lifts", []):
            if lift.get("type") == "SKI_LIFT":
                static_map[lift["id"]] = lift

    # Build dynamic status map
    dyn_map: dict[str, str] = {}
    for poi in dynamics.get("poi", []):
        for lift in poi.get("lifts", []):
            dyn_map[lift["id"]] = lift.get("openingStatus", "")

    lifts: list[LiftStatus] = []
    for lift_id, lift in static_map.items():
        raw_status = dyn_map.get(lift_id, "")
        if raw_status == "OPEN":
            status = "open"
        elif raw_status == "CLOSED":
            status = "closed"
        else:
            continue  # unknown status, skip

        lifts.append(LiftStatus(
            name=lift["name"].rstrip("."),
            status=status,
            lift_type=lift.get("liftType", ""),
        ))

    if lifts:
        snapshot.lifts = lifts
    else:
        snapshot.error = "No lift data returned from API"

    return snapshot
