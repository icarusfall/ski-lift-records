"""
Generic scraper for resorts using Lumiplan-powered see*.com pages.

These sites are SSR and share identical HTML structure:
  div.lift-status-datum         one facility entry
    .lift-status-datum__icon    img src contains /type/{CODE}.svg
    .lift-status-datum__name    facility name
    .lift-status-datum__value   img src contains /etats/O.svg (open) or /etats/F.svg (closed)

Mechanical lift type codes (everything else is pistes, terrain parks, nordic routes):
  FUNI, TC, TPH, TK, TS, TSD, TR, LIAISON

Stale boards: out of season these pages keep serving the last live board
with every lift still "O". The page header's "Updated <time>" is only the
render time; the board's own date is a <p>Updated 19 April 2026</p> just
inside div.lift-and-trail-statuses. A board older than STALE_AFTER_DAYS is
reported as an error so bergfex takes over and nothing frozen is stored.

Known working URLs:
  Val d'Isere  https://www.seevaldisere.com/lifts/status
  Tignes       https://www.seetignes.com/lifts/status
  Val Thorens  https://www.seevalthorens.com/lifts/status
  Meribel      https://www.seemeribel.com/lifts/status
"""

import re
from datetime import date, datetime, timezone

import requests
from bs4 import BeautifulSoup
from .base import LiftStatus, ResortSnapshot, normalise_status

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/121.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-GB,en;q=0.9",
}

LIFT_TYPES = {"FUNI", "TC", "TPH", "TK", "TS", "TSD", "TR", "LIAISON"}

STALE_AFTER_DAYS = 2


def _board_date(soup) -> date | None:
    """The date the lift board itself was last updated, if the page says."""
    panel = soup.find(class_="lift-and-trail-statuses")
    if not panel:
        return None
    for p in panel.find_all("p", limit=5):
        m = re.match(r"Updated\s+(\d{1,2}\s+\w+\s+\d{4})", p.get_text(strip=True))
        if m:
            try:
                return datetime.strptime(m.group(1), "%d %B %Y").date()
            except ValueError:
                return None
    return None


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

    board_date = _board_date(soup)
    today = datetime.now(timezone.utc).date()
    if board_date and (today - board_date).days > STALE_AFTER_DAYS:
        snapshot.error = f"Stale lift board (updated {board_date.isoformat()})"
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
        code_match = re.search(r"/etats/([A-Za-z0-9]+)\.", src)
        if not code_match:
            continue
        raw = code_match.group(1)
        # Codes beyond O/F are recorded as-is rather than dropping the lift.
        status = {"O": "open", "F": "closed", "P": "hold"}.get(raw, "unknown")

        lifts.append(LiftStatus(name=name, status=status, raw_status=raw))

    if lifts:
        snapshot.lifts = lifts
    else:
        snapshot.error = "No lift data found — page structure may have changed"

    return snapshot
