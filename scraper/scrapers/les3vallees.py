"""
Scraper for resorts on les3vallees.com/en/live/lifts-and-trails-opening.

Covers: Courchevel, Méribel, Les Menuires–Saint-Martin, Val Thorens–Orelle.
Page is SSR'd — lift rows are in accordion sections.

HTML structure:
  h4 > button.prl__state-action > span.prl__state-action-name  (section title)
  h4 + div.prl__accordion-body
    div.prl__state-table-item  (Lifts accordion)
      button.prl__state-table-action > span.prl__state-table-action-name  contains "Lifts"
      div.prl__accordion-body
        div.prl__state-track-item
          p.prl__state-track-label  SVG icon + name text + "HH:MM HH:MM"
          span.tag.tag--{success|error|warning|disabled}

Status mapping:
  tag--success -> open
  tag--error   -> closed
  tag--warning -> open  (Forecast = scheduled, lifts will open)
  tag--disabled -> skip (off season)
"""

import re
import requests
from bs4 import BeautifulSoup
from .base import LiftStatus, ResortSnapshot

URL = "https://www.les3vallees.com/en/live/lifts-and-trails-opening"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/121.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-GB,en;q=0.9",
}

# Maps resort_id -> section title (or substring thereof)
SECTION_MAP = {
    "courchevel": "Courchevel Valley",
    "meribel": "Meribel Valley",
    "val-thorens": "Val Thorens",
}

STATUS_MAP = {
    "tag--success": "open",
    "tag--error": "closed",
    "tag--warning": "open",    # Forecast = scheduled opening
    "tag--disabled": None,     # off season
}

# Strips trailing "HH:MM HH:MM" from the concatenated label text
_TIME_RE = re.compile(r"\s*\d{2}:\d{2}\s+\d{2}:\d{2}\s*$")


def _parse_section(body) -> list[LiftStatus]:
    """Parse lift items from an accordion body div."""
    lifts: list[LiftStatus] = []
    for table_item in body.find_all(class_="prl__state-table-item"):
        name_span = table_item.find(class_="prl__state-table-action-name")
        if not (name_span and "Lifts" in name_span.get_text()):
            continue
        for track in table_item.find_all(class_="prl__state-track-item"):
            label = track.find(class_="prl__state-track-label")
            tag = track.find(class_="tag")
            if not (label and tag):
                continue

            # Remove SVG type icon from label before reading text
            for svg in label.find_all("svg"):
                svg.decompose()
            name = _TIME_RE.sub("", label.get_text(strip=True)).rstrip(".")
            if not name:
                continue

            status = None
            for cls in tag.get("class", []):
                if cls in STATUS_MAP:
                    status = STATUS_MAP[cls]
                    break
            if status:
                lifts.append(LiftStatus(name=name, status=status))
    return lifts


def scrape(resort_id: str) -> ResortSnapshot:
    snapshot = ResortSnapshot(resort_id=resort_id, source="les3vallees.com")
    section_keyword = SECTION_MAP.get(resort_id)
    if not section_keyword:
        snapshot.error = f"No section mapping for resort_id '{resort_id}'"
        return snapshot

    try:
        resp = requests.get(URL, headers=HEADERS, timeout=20)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "lxml")
    except Exception as e:
        snapshot.error = str(e)
        return snapshot

    for btn in soup.find_all("button", class_="prl__state-action"):
        name_el = btn.find(class_="prl__state-action-name")
        if name_el and section_keyword.lower() in name_el.get_text().lower():
            parent = btn.find_parent()
            body = parent.find_next_sibling("div", class_="prl__accordion-body")
            if body:
                lifts = _parse_section(body)
                if lifts:
                    snapshot.lifts = lifts
                else:
                    snapshot.error = "No lift data found in section"
                return snapshot

    snapshot.error = f"Section '{section_keyword}' not found on page"
    return snapshot
