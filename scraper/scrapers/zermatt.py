"""Primary scraper for Zermatt (matterhornparadise.ch).

The lifts-and-pistes page is client-rendered, but each sector's panel is
served as a JSON fragment by a plain POST that needs no cookies:

    POST /en/facility-tab?tab=<sector id>&date=

The JSON has one key, `html`, holding the rendered accordion. Each lift is
a `.facilities-collapse-item` with:
    .facilities-collapse-item__name   lift name
    .facilities-collapse-item__text   status wording ("Open" / "Closed")
    .facilities-collapse-item__dot    carries `is-closed` when shut

Note the button's title/aria-label is always "Open" — that labels the
accordion toggle, not the lift — so it must not be read as a status.

Only the Swiss sectors are collected here. The site also publishes the
Breuil-Cervinia (3000414) and Valtournenche (3000413) sectors, which are a
ready-made independent cross-check on the Italian side of the link.
"""

import re
import requests
from bs4 import BeautifulSoup

from .base import LiftStatus, ResortSnapshot, normalise_status

URL = "https://www.matterhornparadise.ch/en/facility-tab"

# Swiss sectors only; the two Italian sectors are deliberately excluded so
# Zermatt's own open/total is not inflated by Cervinia's lifts.
SECTORS = {
    "3000416": "Matterhorn Glacier Paradise / Schwarzsee",
    "3000415": "Sunnegga / Rothorn",
    "3000412": "Gornergrat",
}

# Lifts forming (or feeding) the crossing to the Italian side.
LINK_KEYWORDS = (
    "testa grigia", "plateau rosa", "klein matterhorn",
    "matterhorn glacier ride", "furggsattel", "cervinia",
)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/121.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-GB,en;q=0.9",
    "X-Requested-With": "XMLHttpRequest",
}


def _is_link(name: str) -> bool:
    low = name.lower()
    return any(k in low for k in LINK_KEYWORDS)


def _parse_sector(html: str) -> list[tuple[str, str]]:
    """Return (name, raw_status) for each lift in a sector fragment."""
    soup = BeautifulSoup(html, "lxml")
    out: list[tuple[str, str]] = []
    for item in soup.select(".facilities-collapse-item"):
        name_el = item.select_one(".facilities-collapse-item__name")
        if not name_el:
            continue
        name = re.sub(r"\s+", " ", name_el.get_text(strip=True)).strip()
        if not name:
            continue

        text_el = item.select_one(".facilities-collapse-item__text")
        raw = text_el.get_text(strip=True) if text_el else ""
        if not raw:
            # Fall back to the status dot when the label is not rendered
            dot = item.select_one(".facilities-collapse-item__dot")
            classes = dot.get("class", []) if dot else []
            raw = "Closed" if "is-closed" in classes else "Open"
        out.append((name, raw))
    return out


def scrape(resort_id: str = "zermatt") -> ResortSnapshot:
    snapshot = ResortSnapshot(resort_id=resort_id, source="matterhornparadise.ch")

    found: list[tuple[str, str]] = []
    errors: list[str] = []
    for tab in SECTORS:
        try:
            resp = requests.post(f"{URL}?tab={tab}&date=", headers=HEADERS, timeout=20)
            resp.raise_for_status()
            found.extend(_parse_sector(resp.json().get("html", "")))
        except Exception as e:
            errors.append(f"{tab}: {e}")

    if not found:
        snapshot.error = ("No lift data from matterhornparadise.ch"
                          + (f" ({'; '.join(errors)})" if errors else ""))
        return snapshot

    # The same route can carry two parallel installations under one name.
    seen: dict[str, int] = {}
    for name, raw in found:
        seen[name] = seen.get(name, 0) + 1
        display = name if seen[name] == 1 else f"{name} {seen[name]}"
        snapshot.lifts.append(LiftStatus(
            name=display,
            status=normalise_status(raw),
            is_link=_is_link(name),
            raw_status=raw,
        ))

    # A sector failing while others succeed would quietly shrink the total.
    if errors:
        snapshot.error = None
        snapshot.source = f"matterhornparadise.ch (partial: {len(SECTORS) - len(errors)}/{len(SECTORS)} sectors)"

    return snapshot
