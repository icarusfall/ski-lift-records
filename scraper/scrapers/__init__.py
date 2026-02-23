"""Scraper registry — maps scraper type name to scrape function."""
from .base import ResortSnapshot
from . import cervinia, saas_fee, lumiplan, kgpistes, laplagne, lesmenuires, les3vallees, skiarlberg, bergfex


def run_scraper(resort: dict) -> ResortSnapshot:
    scraper_type = resort.get("scraper", "bergfex")

    if scraper_type == "bergfex":
        return bergfex.scrape(resort["id"], resort["bergfex_slug"])

    # Primary scrapers
    if scraper_type == "cervinia":
        snap = cervinia.scrape(resort["id"])
    elif scraper_type == "saas-fee":
        snap = saas_fee.scrape(resort["id"])
    elif scraper_type == "lumiplan":
        snap = lumiplan.scrape(resort["id"], resort["primary_url"])
    elif scraper_type == "kgpistes":
        snap = kgpistes.scrape(resort["id"], resort["primary_url"])
    elif scraper_type == "laplagne":
        snap = laplagne.scrape(resort["id"])
    elif scraper_type == "lesmenuires":
        snap = lesmenuires.scrape(resort["id"])
    elif scraper_type == "les3vallees":
        snap = les3vallees.scrape(resort["id"])
    elif scraper_type == "skiarlberg":
        snap = skiarlberg.scrape(resort["id"])
    else:
        snap = ResortSnapshot(resort_id=resort["id"], source="unknown")
        snap.error = f"Unknown scraper type: {scraper_type}"
        return snap

    # Automatic bergfex fallback if primary scraper fails
    if snap.error and resort.get("bergfex_slug"):
        fallback = bergfex.scrape(resort["id"], resort["bergfex_slug"])
        fallback.source = f"bergfex.com (fallback from {scraper_type})"
        return fallback

    # Fetch snow depth from bergfex for primary-scraped resorts
    if resort.get("bergfex_slug"):
        m, v = bergfex.get_snow_depth(resort["bergfex_slug"])
        snap.snow_depth_mountain_cm = m
        snap.snow_depth_valley_cm = v

    return snap
