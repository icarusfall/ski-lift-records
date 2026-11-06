"""Scraper registry — maps scraper type name to scrape function."""
from .base import ResortSnapshot, SourceReading
from . import cervinia, saas_fee, lumiplan, kgpistes, laplagne, lesmenuires, les3vallees, skiarlberg, zermatt, bergfex


def _reading(snap: ResortSnapshot) -> SourceReading:
    """Condense a snapshot into a per-source aggregate reading."""
    ok = not snap.error
    return SourceReading(
        source=snap.source,
        lifts_open=snap.lifts_open if ok and snap.lifts_total else None,
        lifts_total=snap.lifts_total if ok and snap.lifts_total else None,
        pistes_open_km=snap.pistes_open_km if ok else None,
        pistes_total_km=snap.pistes_total_km if ok else None,
        error=snap.error,
    )


def _run_primary(resort: dict) -> ResortSnapshot:
    scraper_type = resort.get("scraper")

    if scraper_type == "cervinia":
        return cervinia.scrape(resort["id"])
    if scraper_type == "saas-fee":
        return saas_fee.scrape(resort["id"])
    if scraper_type == "lumiplan":
        return lumiplan.scrape(resort["id"], resort["primary_url"])
    if scraper_type == "kgpistes":
        return kgpistes.scrape(resort["id"], resort["primary_url"])
    if scraper_type == "laplagne":
        return laplagne.scrape(resort["id"])
    if scraper_type == "lesmenuires":
        return lesmenuires.scrape(resort["id"])
    if scraper_type == "les3vallees":
        return les3vallees.scrape(resort["id"])
    if scraper_type == "skiarlberg":
        return skiarlberg.scrape(resort["id"])
    if scraper_type == "zermatt":
        return zermatt.scrape(resort["id"])

    snap = ResortSnapshot(resort_id=resort["id"], source="unknown")
    snap.error = f"Unknown scraper type: {scraper_type}"
    return snap


def bergfex_counts_trusted(resort: dict) -> bool:
    """Whether bergfex's lift counts may stand as the headline figure.

    Measured Aug-Nov 2026 against the resorts' own sites, bergfex outside
    Austria was missing (7 resorts never parse), frozen (Zermatt 1/54 for a
    week from a 29 Oct report) or wrong while current (Les Menuires 22/30 when
    the resort said nothing was open). In Austria, its home market, it parses
    every time and agreed with skiarlberg.at. Accuracy beats continuity: a
    missing day is honest, a wrong one is not. "bergfex_lifts" in
    resorts.json overrides the country rule for one resort.
    """
    return resort.get("bergfex_lifts", resort.get("country") == "AT")


NOT_TRUSTED = "No primary source; bergfex lift counts not used outside Austria"


def run_scraper(resort: dict) -> ResortSnapshot:
    scraper_type = resort.get("scraper", "bergfex")

    # Bergfex-only resorts: one source, one reading.
    if scraper_type == "bergfex":
        snap = bergfex.scrape(resort["id"], resort["bergfex_slug"])
        # Its numbers are still recorded as a reading, for comparison once
        # the resort gets a primary scraper.
        snap.source_readings = [_reading(snap)]
        if not snap.error and not bergfex_counts_trusted(resort):
            # Keep bergfex's snow report; drop its lift and piste counts.
            snap.lifts = []
            snap.pistes_open_km = snap.pistes_total_km = None
            snap.error = NOT_TRUSTED
            snap.error_is_final = True
        return snap

    primary = _run_primary(resort)
    readings = [_reading(primary)]

    # Always also scrape bergfex so both sources are recorded side by side —
    # this catches primary scrapers that go wrong without erroring.
    bfx = None
    if resort.get("bergfex_slug"):
        bfx = bergfex.scrape(resort["id"], resort["bergfex_slug"])
        readings.append(_reading(bfx))

    # Resorts where bergfex is known to be wrong set "bergfex_fallback": false,
    # so a failed primary records a failed day rather than bergfex's numbers.
    if (primary.error and bfx is not None and resort.get("bergfex_fallback", True)
            and bergfex_counts_trusted(resort)):
        # Fall back to the bergfex snapshot for the headline numbers
        bfx.source = f"bergfex.com (fallback from {scraper_type})"
        bfx.source_readings = readings
        return bfx

    # Enrich the primary with bergfex snow/conditions data
    if bfx is not None:
        primary.snow_depth_mountain_cm = bfx.snow_depth_mountain_cm
        primary.snow_depth_valley_cm   = bfx.snow_depth_valley_cm
        primary.snow_condition         = bfx.snow_condition
        primary.last_snowfall_date     = bfx.last_snowfall_date
        primary.piste_conditions       = bfx.piste_conditions
        primary.avalanche_danger       = bfx.avalanche_danger

    primary.source_readings = readings
    return primary
