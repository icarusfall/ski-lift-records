"""
Scraper for Saas-Fee using the saas-fee.ch/en/open-lifts/saas-fee page.

Page structure (SSR):
  Each lift is a div.row containing:
    div.name-icon-wrap > h6.header  (lift name)
    div.status-text                 (text: "open" | "closed")
    div.status-icon.circle-open/circle-closed  (icon backup)
"""

import requests
from bs4 import BeautifulSoup
from .base import LiftStatus, ResortSnapshot

URL = "https://www.saas-fee.ch/en/open-lifts/saas-fee"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/121.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-GB,en;q=0.9",
}


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
    seen_names: set[str] = set()

    # Each lift row: div.row containing div.name-icon-wrap + div.status-wrap
    for row in soup.find_all("div", class_="row"):
        name_wrap = row.find("div", class_="name-icon-wrap")
        if not name_wrap:
            continue

        h6 = name_wrap.find("h6", class_="header")
        if not h6:
            continue
        name = h6.get_text(strip=True)
        if not name:
            continue

        # Deduplicate
        if name in seen_names:
            continue
        seen_names.add(name)

        # Skip cross-country ski routes (not alpine lifts)
        if name.endswith(("(Klassisch)", "(Skating)")):
            continue

        # Status from div.status-text ("open" / "closed")
        status_div = row.find("div", class_="status-text")
        if status_div:
            text = status_div.get_text(strip=True).lower()
            if text in ("open", "closed"):
                lifts.append(LiftStatus(name=name, status=text))
                continue

        # Fallback: status-icon circle-open / circle-closed
        icon_div = row.find("div", class_="status-icon")
        if icon_div:
            icon_classes = icon_div.get("class", [])
            if "circle-open" in icon_classes:
                lifts.append(LiftStatus(name=name, status="open"))
            elif "circle-closed" in icon_classes:
                lifts.append(LiftStatus(name=name, status="closed"))

    if lifts:
        snapshot.lifts = lifts
    else:
        snapshot.error = "No individual lift data found — page structure unrecognised"

    return snapshot
