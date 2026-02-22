"""
Scraper for St. Anton and Lech/Zürs via skiarlberg.at.

Both lift and piste data are available via a POST JSON endpoint that
returns an HTML fragment. Two tables:
  /en/live-info/table/lifts  - cable cars & lifts
  /en/live-info/table/slopes - ski runs

Parameters:
  regionId   - identifies the resort (hardcoded below)
  loadMore=1 - returns full list (loadMore=0 gives only first ~10 rows)
  orderState=DESC

Response: {"success": true, "html": "<table>...</table>"}

HTML rows:
  td[headers="liveInfoTable-name"]        - facility/run name
  td[headers="liveInfoTable-state"]       - "open" | "closed"
  td[headers="liveInfoTable-type"]        - lift type text (lifts only)
  td[headers="liveInfoTable-difficulty"]  - difficulty text (slopes only)

Difficulty text values:
  "easy" | "medium difficulty" | "challenging" | "extreme ski route" | "ski route"
"""

import requests
from bs4 import BeautifulSoup
from .base import LiftStatus, PisteStatus, ResortSnapshot

BASE = "https://www.skiarlberg.at"
LIFTS_PATH = "/en/live-info/table/lifts"
SLOPES_PATH = "/en/live-info/table/slopes"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/121.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-GB,en;q=0.9",
    "X-Requested-With": "XMLHttpRequest",
}

# regionId values from skiarlberg.at
REGION_IDS = {
    "st-anton": 164,
    "lech-zuers": 4,
}

# Map difficulty text -> colour field value
DIFFICULTY_MAP = {
    "easy": "blue",
    "medium difficulty": "red",
    "challenging": "black",
    "extreme ski route": "black",
    "ski route": "black",
}


def _post_table(path: str, region_id: int, referer: str) -> BeautifulSoup | None:
    url = f"{BASE}{path}?regionId={region_id}&loadMore=1&orderState=DESC"
    try:
        r = requests.post(
            url,
            headers={**HEADERS, "Referer": BASE + referer},
            timeout=20,
        )
        r.raise_for_status()
        html = r.json().get("html", "")
        return BeautifulSoup(html, "lxml")
    except Exception:
        return None


def _parse_lifts(soup: BeautifulSoup) -> list[LiftStatus]:
    lifts: list[LiftStatus] = []
    for row in soup.find_all("tr"):
        name_td = row.find("td", headers="liveInfoTable-name")
        state_td = row.find("td", headers="liveInfoTable-state")
        type_td = row.find("td", headers="liveInfoTable-type")
        if not (name_td and state_td):
            continue
        name = name_td.get_text(strip=True)
        status = state_td.get_text(strip=True).lower()
        lift_type = type_td.get_text(strip=True) if type_td else ""
        if status in ("open", "closed"):
            lifts.append(LiftStatus(name=name, status=status, lift_type=lift_type))
    return lifts


def _parse_pistes(soup: BeautifulSoup) -> list[PisteStatus]:
    pistes: list[PisteStatus] = []
    for row in soup.find_all("tr"):
        name_td = row.find("td", headers="liveInfoTable-name")
        state_td = row.find("td", headers="liveInfoTable-state")
        diff_td = row.find("td", headers="liveInfoTable-difficulty")
        if not (name_td and state_td):
            continue
        name = name_td.get_text(strip=True)
        status = state_td.get_text(strip=True).lower()
        difficulty_text = diff_td.get_text(strip=True) if diff_td else ""
        difficulty = DIFFICULTY_MAP.get(difficulty_text, difficulty_text)
        if status in ("open", "closed"):
            pistes.append(PisteStatus(name=name, status=status, colour=difficulty))
    return pistes


def scrape(resort_id: str) -> ResortSnapshot:
    region_id = REGION_IDS.get(resort_id)
    if region_id is None:
        snap = ResortSnapshot(resort_id=resort_id, source="skiarlberg.at")
        snap.error = f"No region ID for resort_id '{resort_id}'"
        return snap

    snapshot = ResortSnapshot(resort_id=resort_id, source="skiarlberg.at")
    referer = f"/en/{resort_id.replace('-', '-')}/live-info/cable-cars-lifts"

    lifts_soup = _post_table(LIFTS_PATH, region_id, referer)
    slopes_soup = _post_table(SLOPES_PATH, region_id, referer)

    if lifts_soup:
        snapshot.lifts = _parse_lifts(lifts_soup)
    if slopes_soup:
        snapshot.pistes = _parse_pistes(slopes_soup)

    if not snapshot.lifts and not snapshot.pistes:
        snapshot.error = "No data returned from skiarlberg.at"

    return snapshot
