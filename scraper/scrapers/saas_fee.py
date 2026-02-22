"""
Scraper for Saas-Fee using the saas-fee.ch open lifts page.

The page at /en/.../open-lifts is SSR and shows individual lift rows
with open/closed status. Falls back gracefully if structure is unrecognised.
"""

import requests
from bs4 import BeautifulSoup
from .base import LiftStatus, ResortSnapshot

URL = (
    "https://www.saas-fee.ch/en/services-informationen/"
    "prices-timetables-cable-cars/timetables-cable-cars/open-lifts"
)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/121.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-GB,en;q=0.9",
}

_OPEN_WORDS = {"open", "geöffnet", "offen", "ouvert"}
_CLOSED_WORDS = {"closed", "geschlossen", "fermé", "ferme", "geschl"}


def _status_from_text(text: str) -> str | None:
    t = text.lower().strip()
    if any(w in t for w in _OPEN_WORDS):
        return "open"
    if any(w in t for w in _CLOSED_WORDS):
        return "closed"
    return None


def _status_from_classes(classes: list[str]) -> str | None:
    joined = " ".join(classes).lower()
    if "is-open" in joined or "status-open" in joined or "open" in joined:
        return "open"
    if "is-closed" in joined or "status-closed" in joined or "closed" in joined:
        return "closed"
    return None


def scrape(resort_id: str = "saas-fee") -> ResortSnapshot:
    snapshot = ResortSnapshot(resort_id=resort_id, source="saas-fee.ch")
    try:
        resp = requests.get(URL, headers=HEADERS, timeout=20)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "lxml")
    except Exception as e:
        snapshot.error = str(e)
        return snapshot

    lifts: list[LiftStatus] = []

    # Pattern 1: table rows (name in first cell, status in subsequent cells)
    for table in soup.find_all("table"):
        for row in table.find_all("tr"):
            cells = row.find_all(["td", "th"])
            if len(cells) < 2:
                continue
            name = cells[0].get_text(separator=" ", strip=True)
            if not name or len(name) < 2:
                continue
            # Skip header-like cells
            if name.lower() in {"name", "lift", "anlage", "status", ""}:
                continue

            status: str | None = None
            for cell in cells[1:]:
                # Check CSS classes on child elements first
                for elem in cell.find_all(True):
                    status = _status_from_classes(elem.get("class") or [])
                    if status:
                        break
                if not status:
                    status = _status_from_text(cell.get_text(strip=True))
                if status:
                    break

            if status:
                lifts.append(LiftStatus(name=name, status=status))

    # Pattern 2: list/div items with paired name + status elements
    if not lifts:
        candidates = soup.find_all(
            ["li", "article", "div"],
            class_=lambda c: c and any(
                w in " ".join(c).lower()
                for w in ["lift", "facility", "installation", "anlage", "gondola"]
            ),
        )
        for item in candidates:
            # Name: first text-bearing child
            name_elem = item.find(["span", "div", "p", "h3", "h4", "strong"])
            if not name_elem:
                continue
            name = name_elem.get_text(strip=True)
            if not name or len(name) < 2:
                continue

            # Status: look for a status-bearing sibling/child element
            status = None
            for elem in item.find_all(True):
                status = _status_from_classes(elem.get("class") or [])
                if status:
                    break
            if not status:
                status = _status_from_text(item.get_text(strip=True))

            if name and status:
                lifts.append(LiftStatus(name=name, status=status))

    if lifts:
        snapshot.lifts = lifts
    else:
        snapshot.error = "No individual lift data found — page structure unrecognised"

    return snapshot
