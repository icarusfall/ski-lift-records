"""
Scraper for Breuil-Cervinia using the cervinia.it/en/impianti page.

The page is server-side rendered by Nuxt. Individual lift and piste
statuses are embedded in CSS classes:
  impianto-status-O  => lift open
  impianto-status-F  => lift closed (Ferma/Fermé)
  pista-status-O     => piste open
  pista-status-F     => piste closed

The Zermatt link lifts are identified by name keywords.
"""

import re
import requests
from .base import LiftStatus, PisteStatus, ResortSnapshot

URL = "https://www.cervinia.it/en/impianti"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; ski-lift-tracker/1.0)"
}

# Names that indicate a cross-resort summit link
LINK_KEYWORDS = ["zermatt", "plateau rosa", "plateau-rosa", "trockener steg",
                 "klein matterhorn", "furggsattel", "cime bianche"]

PISTE_COLOUR_MAP = {"B": "blue", "R": "red", "N": "black", "V": "green"}


def _is_link(name: str) -> bool:
    lower = name.lower()
    return any(k in lower for k in LINK_KEYWORDS)


def scrape(resort_id: str = "cervinia") -> ResortSnapshot:
    snapshot = ResortSnapshot(resort_id=resort_id, source="cervinia.it")
    try:
        resp = requests.get(URL, headers=HEADERS, timeout=20)
        resp.raise_for_status()
        html = resp.text
    except Exception as e:
        snapshot.error = str(e)
        return snapshot

    # ── Lifts ──────────────────────────────────────────────────────────────
    # Pattern: lift name inside u-capitalize span, followed by impianto-status-O/F
    lift_names_positions = [
        (m.start(), m.group(1).strip())
        for m in re.finditer(
            r'u-capitalize[^>]*>\s*(?:<[^>]+>\s*)*([A-Za-z\s\-\'àèéìòùü/\.]+?)\s*</span>',
            html, re.DOTALL
        )
        if len(m.group(1).strip()) > 2
    ]
    # Remove duplicate names (same name appearing twice due to HTML structure)
    seen_names: set[str] = set()
    deduped: list[tuple[int, str]] = []
    for pos, name in lift_names_positions:
        if name not in seen_names:
            seen_names.add(name)
            deduped.append((pos, name))
    lift_names_positions = deduped

    status_positions = [
        (m.start(), m.group(1))
        for m in re.finditer(r'impianto-status-([OF])"', html)
    ]

    for name_pos, name in lift_names_positions:
        following = [(pos, s) for pos, s in status_positions if pos > name_pos]
        if following:
            closest_pos, closest_status = following[0]
            if closest_pos - name_pos < 2000:
                status = "open" if closest_status == "O" else "closed"
                snapshot.lifts.append(LiftStatus(
                    name=name,
                    status=status,
                    is_link=_is_link(name)
                ))

    # ── Pistes ─────────────────────────────────────────────────────────────
    # Pattern: pista-level-{B/R/N}, then piste name, then pista-status-O/F
    piste_blocks = re.finditer(
        r'pista-level-([BRNV])[^>]*></span>\s*(.*?)</span>.*?pista-status-([OF])',
        html, re.DOTALL
    )
    for m in piste_blocks:
        colour_code, name_raw, status_code = m.group(1), m.group(2), m.group(3)
        name = re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', '', name_raw)).strip()
        if name and len(name) > 1:
            snapshot.pistes.append(PisteStatus(
                name=name,
                status="open" if status_code == "O" else "closed",
                colour=PISTE_COLOUR_MAP.get(colour_code, colour_code.lower())
            ))

    return snapshot
