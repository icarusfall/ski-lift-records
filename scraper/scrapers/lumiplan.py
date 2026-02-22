"""
Generic scraper for resorts using Lumiplan-powered see*.com pages.

These sites are SSR and share identical HTML structure:
  div.lift-status-datum         one facility entry
    .lift-status-datum__icon    img src contains /type/{CODE}.svg
    .lift-status-datum__name    facility name
    .lift-status-datum__value   img src contains /etats/O.svg (open) or /etats/F.svg (closed)

Mechanical lift type codes (everything else is pistes, terrain parks, nordic routes):
  FUNI, TC, TPH, TK, TS, TSD, TR, LIAISON

Known working URLs:
  Val d'Isere  https://www.seevaldisere.com/lifts/status
  Tignes       https://www.seetignes.com/lifts/status
  Val Thorens  https://www.seevalthorens.com/lifts/status
  Meribel      https://www.seemeribel.com/lifts/status
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

LIFT_TYPES = {"FUNI", "TC", "TPH", "TK", "TS", "TSD", "TR", "LIAISON"}


def _type_code(datum) -> str:
    icon = datum.find(class_="lift-status-datum__icon")
    if icon:
        img = icon.find("img")
        if img and "/type/" in img.get("src", ""):
            return img["src"].split("/type/")[-1].replace(".svg", "")
    return ""


def scrape(resort_id: str, url: str) -> ResortSnapshot:
    snapshot = ResortSnapshot(resort_id=resort_id, source=url.split("/")[2])
    try:
        resp = requests.get(url, headers=HEADERS, timeout=20)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "lxml")
    except Exception as e:
        snapshot.error = str(e)
        return snapshot

    lifts: list[LiftStatus] = []

    for datum in soup.find_all(class_="lift-status-datum"):
        if _type_code(datum) not in LIFT_TYPES:
            continue

        name_elem = datum.find(class_="lift-status-datum__name")
        value_elem = datum.find(class_="lift-status-datum__value")
        if not name_elem or not value_elem:
            continue

        name = name_elem.get_text(strip=True).rstrip(".")
        if not name:
            continue

        img = value_elem.find("img")
        if not img:
            continue
        src = img.get("src", "")
        if "/etats/O." in src:
            status = "open"
        elif "/etats/F." in src:
            status = "closed"
        else:
            continue

        lifts.append(LiftStatus(name=name, status=status))

    if lifts:
        snapshot.lifts = lifts
    else:
        snapshot.error = "No lift data found — page structure may have changed"

    return snapshot
