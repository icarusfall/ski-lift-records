"""
Generic scraper for resorts using the TYPO3 kg_pistes extension.

HTML structure:
  div.kg-pistes_ligne               one facility row
    span.kg-pistes-picto            contains type class, e.g. iconpistes-TS
    span.kg-pistes-name             facility name
    [last div col] span/img[title]  status: "Ouvert" | "Fermé" | "Prévision"

Mechanical lift picto suffixes (everything else is piste/luge/ludique):
  FUNI2, TB, TC, TK, TLC, TPH, TS, TSD, TSDB, remontee

Status mapping:
  title contains "Ouvert"   -> open
  title contains "Ferm"     -> closed  (handles Fermé encoding variants)
  title contains "Prévision"-> closed  (forecast, not yet open)

Known working URLs:
  Les Arcs   https://en.lesarcs.com/lifts-slopes-status
"""

import requests
from bs4 import BeautifulSoup
from .base import LiftStatus, ResortSnapshot

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/121.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-GB,en;q=0.9",
}

LIFT_PICTOS = {
    "iconpistes-FUNI2", "iconpistes-TB", "iconpistes-TC", "iconpistes-TK",
    "iconpistes-TLC", "iconpistes-TPH", "iconpistes-TS", "iconpistes-TSD",
    "iconpistes-TSDB", "iconpistes-remontee",
}


def _picto_type(row) -> str:
    picto = row.find("span", class_=lambda c: c and "kg-pistes-picto" in c)
    if picto:
        for cls in picto.get("class", []):
            if cls.startswith("iconpistes-"):
                return cls
    return ""


def _status_from_title(title: str) -> str | None:
    t = title.lower()
    if "ferm" in t:           # covers Fermé and encoding variants
        return "closed"
    if "ouvert" in t or "pr" in t:  # Ouvert = open, Prévision = open as scheduled
        return "open"
    return None


def scrape(resort_id: str, url: str) -> ResortSnapshot:
    hostname = url.split("/")[2]
    snapshot = ResortSnapshot(resort_id=resort_id, source=hostname)
    try:
        resp = requests.get(url, headers=HEADERS, timeout=20)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "lxml")
    except Exception as e:
        snapshot.error = str(e)
        return snapshot

    lifts: list[LiftStatus] = []

    for row in soup.find_all("div", class_="kg-pistes_ligne"):
        if _picto_type(row) not in LIFT_PICTOS:
            continue

        name_elem = row.find("span", class_="kg-pistes-name")
        if not name_elem:
            continue
        name = name_elem.get_text(strip=True).rstrip(".")
        if not name:
            continue

        # Status is in the title attribute of the last-column span or img
        status: str | None = None
        cols = row.find_all("div", recursive=False)
        if cols:
            last_col = cols[-1]
            for elem in last_col.find_all(["span", "img"]):
                title = elem.get("title", "")
                if title:
                    status = _status_from_title(title)
                    if status:
                        break

        if name and status:
            lifts.append(LiftStatus(name=name, status=status))

    if lifts:
        snapshot.lifts = lifts
    else:
        snapshot.error = "No lift data found — page structure may have changed"

    return snapshot
