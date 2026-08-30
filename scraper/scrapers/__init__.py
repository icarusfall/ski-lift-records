"""Scraper registry — maps scraper type name to scrape function."""
from .base import ResortSnapshot, SourceReading
from . import cervinia, saas_fee, lumiplan, kgpistes, laplagne, lesmenuires, les3vallees, skiarlberg, bergfex


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

    snap = ResortSnapshot(resort_id=resort["id"], source="unknown")
    snap.error = f"Unknown scraper type: {scraper_type}"
    return snap


def run_scraper(resort: dict) -> ResortSnapshot:
    scraper_type = resort.get("scraper", "bergfex")

    # Bergfex-only resorts: one source, one reading.
    if scraper_type == "bergfex":
        snap = bergfex.scrape(resort["id"], resort["bergfex_slug"])
        snap.source_readings = [_reading(snap)]
        return snap

    primary = _run_primary(resort)
    readings = [_reading(primary)]

    # Always also scrape bergfex so both sources are recorded side by side —
    # this catches primary scrapers that go wrong without erroring.
    bfx = None
    if resort.get("bergfex_slug"):
        bfx = bergfex.scrape(resort["id"], resort["bergfex_slug"])
        readings.append(_reading(bfx))

    if primary.error and bfx is not None:
        # Fall back to the bergfex snapshot for the headline numbers
        bfx.source = f"bergfex.com (fallback from {scraper_type})"
        bfx.source_readings = readings
        return bfx

    # Primary succeeded: enrich it with bergfex snow/conditions data
    if bfx is not None:
        primary.snow_depth_mountain_cm = bfx.snow_depth_mountain_cm
        primary.snow_depth_valley_cm   = bfx.snow_depth_valley_cm
        primary.snow_condition         = bfx.snow_condition
        primary.last_snowfall_date     = bfx.last_snowfall_date
        primary.piste_conditions       = bfx.piste_conditions
        primary.avalanche_danger       = bfx.avalanche_danger

    primary.source_readings = readings
    return primary
