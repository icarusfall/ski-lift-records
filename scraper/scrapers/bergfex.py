"""
Scraper for resorts using bergfex.com.

bergfex.com serves server-side rendered HTML. Lift aggregate data is
embedded in spans with class tw-text-6xl directly below an "Open lifts"
heading. Piste km data appears similarly under "Open pistes".

This scraper returns aggregate (open/total) counts only — no individual
lift names. It is the fallback for resorts without a primary scraper.
"""

import re
import time
import requests
from .base import ResortSnapshot

BASE_URL = "https://www.bergfex.com"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/121.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-GB,en;q=0.9",
}

# Seconds to wait between requests to avoid rate-limiting
RATE_LIMIT_DELAY = 3.0

_last_request_time: float = 0.0


def _rate_limit():
    global _last_request_time
    elapsed = time.time() - _last_request_time
    if elapsed < RATE_LIMIT_DELAY:
        time.sleep(RATE_LIMIT_DELAY - elapsed)
    _last_request_time = time.time()


def _parse_count(html: str, heading: str) -> tuple[int | None, int | None]:
    """Extract open/total integers from an 'X / Y' block below a heading."""
    block_match = re.search(
        re.escape(heading) + r'.*?tw-text-6xl[^>]*>([0-9]+)<.*?tw-text-m[^>]*/\s*([0-9]+)<',
        html, re.DOTALL
    )
    if block_match:
        return int(block_match.group(1)), int(block_match.group(2))
    return None, None


def _parse_km(html: str) -> tuple[float | None, float | None]:
    """Extract open km / total km from the pistes section."""
    # bergfex shows km as decimal: "127.0 / 185.5 km"
    m = re.search(
        r'Open pistes.*?tw-text-6xl[^>]*>([0-9]+\.?[0-9]*)<.*?tw-text-m[^>]*/\s*([0-9]+\.?[0-9]*)',
        html, re.DOTALL
    )
    if m:
        return float(m.group(1)), float(m.group(2))
    return None, None


def scrape(resort_id: str, bergfex_slug: str) -> ResortSnapshot:
    _rate_limit()
    snapshot = ResortSnapshot(resort_id=resort_id, source="bergfex.com")
    url = f"{BASE_URL}/{bergfex_slug}/"

    try:
        resp = requests.get(url, headers=HEADERS, timeout=20)
        resp.raise_for_status()
        html = resp.text
    except Exception as e:
        snapshot.error = str(e)
        return snapshot

    # Aggregate lift count
    open_lifts, total_lifts = _parse_count(html, "Open lifts")
    if open_lifts is not None:
        # Build synthetic lift list (no individual names from bergfex)
        from .base import LiftStatus
        for i in range(open_lifts):
            snapshot.lifts.append(LiftStatus(name=f"lift_{i+1}", status="open"))
        for i in range(total_lifts - open_lifts):
            snapshot.lifts.append(LiftStatus(name=f"lift_closed_{i+1}", status="closed"))

    # Piste km
    open_km, total_km = _parse_km(html)
    snapshot.pistes_open_km = open_km
    snapshot.pistes_total_km = total_km

    if open_lifts is None:
        snapshot.error = "Could not parse lift counts from bergfex page"

    return snapshot
