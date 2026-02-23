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
from datetime import date
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
        re.escape(heading) + r'.*?tw-text-6xl[^>]*>([0-9]+)<.*?tw-text-m[^>]*>/\s*([0-9]+)<',
        html, re.DOTALL
    )
    if block_match:
        return int(block_match.group(1)), int(block_match.group(2))
    return None, None


def _parse_snow_depth(html: str) -> tuple[int | None, int | None]:
    """Extract mountain/valley snow depth from main bergfex page (fallback)."""
    m_match = re.search(r'Mountain:</span>\s*<span[^>]*>(\d+)\s*cm</span>', html)
    v_match = re.search(r'Valley:</span>\s*<span[^>]*>(\d+)\s*cm</span>', html)
    mountain = int(m_match.group(1)) if m_match else None
    valley = int(v_match.group(1)) if v_match else None
    return mountain, valley


def _parse_dt_dd(html: str) -> dict[str, str]:
    """Parse all <dt>/<dd> pairs from a bergfex schneebericht page."""
    dts = re.findall(r'<dt[^>]*>(.*?)</dt>', html, re.DOTALL)
    dds = re.findall(r'<dd[^>]*>(.*?)</dd>', html, re.DOTALL)
    result = {}
    for dt, dd in zip(dts, dds):
        key = re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', '', dt)).strip()
        val = re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', '', dd)).strip()
        if key:
            result[key] = val
    return result


def _parse_date_short(s: str) -> date | None:
    """Parse bergfex short date 'Sat, 21.02.' to a date object."""
    m = re.match(r'\w+,\s*(\d{1,2})\.(\d{2})\.', s)
    if not m:
        return None
    day, month = int(m.group(1)), int(m.group(2))
    today = date.today()
    try:
        candidate = date(today.year, month, day)
    except ValueError:
        return None
    # If the parsed date is in the future, it must be from last year
    if candidate > today:
        candidate = date(today.year - 1, month, day)
    return candidate


def get_snow_report(bergfex_slug: str) -> dict:
    """Fetch the /schneebericht/ page and return a dict of snow condition fields."""
    _rate_limit()
    url = f"{BASE_URL}/{bergfex_slug}/schneebericht/"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=20)
        resp.raise_for_status()
        html = resp.text
    except Exception:
        return {}

    data = _parse_dt_dd(html)
    result = {}

    for key, val in data.items():
        key_lower = key.lower()
        if key_lower.startswith('mountain'):
            m = re.match(r'(\d+)', val)
            if m:
                result["snow_depth_mountain_cm"] = int(m.group(1))
        elif key_lower.startswith('valley'):
            m = re.match(r'(\d+)', val)
            if m:
                result["snow_depth_valley_cm"] = int(m.group(1))

    snow_cond = data.get('Snow condition', '').strip()
    if snow_cond and snow_cond.lower() not in ('no info', ''):
        result["snow_condition"] = snow_cond

    last_sf = data.get('Latest snowfall Region', '')
    if last_sf:
        d = _parse_date_short(last_sf)
        if d:
            result["last_snowfall_date"] = d

    piste_cond = data.get('Piste conditions', '').strip()
    if piste_cond and piste_cond.lower() not in ('no info', ''):
        result["piste_conditions"] = piste_cond

    aval = data.get('Avalanche alert level', '')
    aval_match = re.match(r'([1-5])\b', aval)
    if aval_match:
        result["avalanche_danger"] = int(aval_match.group(1))

    return result


def _parse_km(html: str) -> tuple[float | None, float | None]:
    """Extract open km / total km from the pistes section."""
    # bergfex shows km as decimal: "127.0 / 185.5 km"
    m = re.search(
        r'Open pistes.*?tw-text-6xl[^>]*>([0-9]+\.?[0-9]*)<.*?tw-text-m[^>]*>/\s*([0-9]+\.?[0-9]*)',
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

    # Snow depth from main page (already fetched — used as fallback)
    mountain, valley = _parse_snow_depth(html)
    snapshot.snow_depth_mountain_cm = mountain
    snapshot.snow_depth_valley_cm = valley

    # Richer snow/conditions data from schneebericht subpage
    snow = get_snow_report(bergfex_slug)
    snapshot.snow_depth_mountain_cm = snow.get("snow_depth_mountain_cm") or snapshot.snow_depth_mountain_cm
    snapshot.snow_depth_valley_cm   = snow.get("snow_depth_valley_cm")   or snapshot.snow_depth_valley_cm
    snapshot.snow_condition         = snow.get("snow_condition")
    snapshot.last_snowfall_date     = snow.get("last_snowfall_date")
    snapshot.piste_conditions       = snow.get("piste_conditions")
    snapshot.avalanche_danger       = snow.get("avalanche_danger")

    if open_lifts is None:
        snapshot.error = "Could not parse lift counts from bergfex page"

    return snapshot
