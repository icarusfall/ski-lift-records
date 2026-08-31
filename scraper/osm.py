"""Lift geometry from OpenStreetMap, via the Overpass API.

Gives every lift a real position, a type, and — the useful part — a bearing.
A lift shuts when the wind blows across it, so bearing combined with the
dominant wind direction yields a crosswind component, which is a far better
predictor than raw gust speed.

    python -m scraper.osm --fetch            # all resorts (paced)
    python -m scraper.osm --fetch cervinia
    python -m scraper.osm --match            # suggest name matches to our lifts
    python -m scraper.osm --status

Overpass is a free community service. Queries are one per resort, spaced, with
an identifying User-Agent — and the whole job runs once.
"""
import json
import math
import sys
import time

import psycopg2.extras
import requests

from .collect import load_resorts
from .db import cursor

OVERPASS_URL = "https://overpass-api.de/api/interpreter"
HEADERS = {"User-Agent": "ski-lift-records/1.0 (hobby ski-lift research; contact via github icarusfall)"}

# Degrees of latitude/longitude around a resort centre. Large ski areas sprawl,
# so this is generous; stray neighbouring lifts are filtered by name matching.
BBOX_PAD = 0.11
REQUEST_PAUSE_S = 6.0

# Only things that carry people uphill.
AERIALWAY_TYPES = {"chair_lift", "gondola", "cable_car", "t-bar", "j-bar",
                   "platter", "drag_lift", "rope_tow", "magic_carpet", "mixed_lift",
                   "funicular"}


def _bearing(a: dict, b: dict) -> float:
    """Compass bearing from the first point to the last, in degrees."""
    lat1, lat2 = math.radians(a["lat"]), math.radians(b["lat"])
    dlon = math.radians(b["lon"] - a["lon"])
    y = math.sin(dlon) * math.cos(lat2)
    x = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(dlon)
    return (math.degrees(math.atan2(y, x)) + 360) % 360


def _length_m(geom: list[dict]) -> int:
    total = 0.0
    for p, q in zip(geom, geom[1:]):
        dlat = math.radians(q["lat"] - p["lat"])
        dlon = math.radians(q["lon"] - p["lon"])
        mlat = math.radians((p["lat"] + q["lat"]) / 2)
        total += 6371000 * math.hypot(dlat, dlon * math.cos(mlat))
    return int(total)


def fetch_resort(resort: dict) -> list[dict]:
    lat, lon = resort["latitude"], resort["longitude"]
    bbox = f"{lat - BBOX_PAD},{lon - BBOX_PAD},{lat + BBOX_PAD},{lon + BBOX_PAD}"
    query = f'[out:json][timeout:90];way["aerialway"]({bbox});out tags geom;'
    resp = requests.post(OVERPASS_URL, data={"data": query}, headers=HEADERS, timeout=120)
    if resp.status_code == 429:
        raise RuntimeError("Overpass is busy (429) — try again shortly")
    resp.raise_for_status()

    out = []
    for el in resp.json().get("elements", []):
        tags = el.get("tags", {})
        kind = tags.get("aerialway")
        geom = el.get("geometry") or []
        if kind not in AERIALWAY_TYPES or len(geom) < 2:
            continue
        out.append({
            "osm_id": el["id"],
            "resort_id": resort["id"],
            "name": (tags.get("name") or "").strip() or None,
            "aerialway": kind,
            "bearing_deg": round(_bearing(geom[0], geom[-1])),
            "length_m": _length_m(geom),
            "bottom_lat": geom[0]["lat"], "bottom_lon": geom[0]["lon"],
            "top_lat": geom[-1]["lat"], "top_lon": geom[-1]["lon"],
            "geometry": json.dumps([[round(p["lon"], 5), round(p["lat"], 5)] for p in geom]),
        })
    return out


def _store(rows: list[dict]) -> int:
    if not rows:
        return 0
    with cursor() as cur:
        psycopg2.extras.execute_values(cur, """
            INSERT INTO lift_geometry
                (osm_id, resort_id, name, aerialway, bearing_deg, length_m,
                 bottom_lat, bottom_lon, top_lat, top_lon, geometry)
            VALUES %s
            ON CONFLICT (osm_id) DO UPDATE SET
                resort_id  = EXCLUDED.resort_id,
                name       = EXCLUDED.name,
                aerialway  = EXCLUDED.aerialway,
                bearing_deg = EXCLUDED.bearing_deg,
                length_m   = EXCLUDED.length_m,
                bottom_lat = EXCLUDED.bottom_lat, bottom_lon = EXCLUDED.bottom_lon,
                top_lat    = EXCLUDED.top_lat,    top_lon    = EXCLUDED.top_lon,
                geometry   = EXCLUDED.geometry
        """, [(r["osm_id"], r["resort_id"], r["name"], r["aerialway"],
               r["bearing_deg"], r["length_m"], r["bottom_lat"], r["bottom_lon"],
               r["top_lat"], r["top_lon"], r["geometry"]) for r in rows])
    return len(rows)


def fetch(resort_filter: str | None = None):
    resorts = [r for r in load_resorts()
               if r.get("latitude") and (resort_filter is None or r["id"] == resort_filter)]
    if not resorts:
        print(f"No resort found with id '{resort_filter}'")
        return
    print(f"\nFetching lift geometry for {len(resorts)} resort(s) from OpenStreetMap\n")
    total = 0
    for resort in resorts:
        try:
            rows = fetch_resort(resort)
            n = _store(rows)
            total += n
            named = sum(1 for r in rows if r["name"])
            print(f"  {resort['id']:<16} {n:>3} lifts ({named} named)")
        except Exception as e:
            print(f"  {resort['id']:<16} FAILED: {str(e)[:70]}")
        time.sleep(REQUEST_PAUSE_S)
    print(f"\n  {total} lift geometries stored.\n")


def suggest_matches(resort_id: str | None = None, threshold: float = 0.72):
    """Pair OSM ways with the lifts we actually observe, by name similarity.

    Only near-certain matches are stored automatically; the rest are printed
    for a human, for the same reason renames are never auto-merged — similar
    names are routinely different lifts.
    """
    import difflib
    import re
    import unicodedata

    def norm(s: str) -> str:
        """Accents and punctuation differ between OSM and resort sites for what
        is plainly the same lift, so compare on a folded form."""
        s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()
        return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]+", " ", s.lower())).strip()

    with cursor() as cur:
        cur.execute("""
            SELECT g.osm_id, g.resort_id, g.name AS osm_name, g.aerialway
            FROM lift_geometry g
            WHERE g.name IS NOT NULL AND g.lift_id IS NULL
              AND (%s IS NULL OR g.resort_id = %s)
        """, (resort_id, resort_id))
        geoms = cur.fetchall()
        cur.execute("""
            SELECT id, resort_id, name FROM lifts
            WHERE alias_of IS NULL AND name NOT LIKE 'lift\\_%%'
              AND (%s IS NULL OR resort_id = %s)
        """, (resort_id, resort_id))
        lifts = cur.fetchall()

    by_resort = {}
    for l in lifts:
        by_resort.setdefault(l["resort_id"], []).append(l)

    auto, review = [], []
    for g in geoms:
        best, score = None, 0.0
        for l in by_resort.get(g["resort_id"], []):
            r = difflib.SequenceMatcher(None, norm(g["osm_name"]), norm(l["name"])).ratio()
            if r > score:
                best, score = l, r
        if not best:
            continue
        (auto if score >= 0.92 else review).append((g, best, score))

    if auto:
        with cursor() as cur:
            for g, l, score in auto:
                cur.execute("UPDATE lift_geometry SET lift_id = %s, match_score = %s "
                            "WHERE osm_id = %s", (l["id"], round(score, 3), g["osm_id"]))
    print(f"\n  matched automatically (>=92% similar): {len(auto)}")
    for g, l, s in auto[:10]:
        print(f"     {g['osm_name'][:30]:<32} -> {l['name'][:30]}  ({s:.0%})")
    plausible = [x for x in review if x[2] >= threshold]
    print(f"\n  needs a human ({threshold:.0%}-92%): {len(plausible)}")
    for g, l, s in plausible[:15]:
        print(f"     {g['osm_name'][:30]:<32} ?  {l['name'][:30]}  ({s:.0%})")
    print()


def status():
    with cursor() as cur:
        cur.execute("""
            SELECT resort_id, COUNT(*) n,
                   COUNT(name) named, COUNT(lift_id) matched
            FROM lift_geometry GROUP BY resort_id ORDER BY resort_id
        """)
        rows = cur.fetchall()
    print(f"\n  lift_geometry: {len(rows)} resort(s)")
    for r in rows:
        print(f"    {r['resort_id']:<16} {r['n']:>3} ways, {r['named']:>3} named, "
              f"{r['matched']:>3} matched to observed lifts")
    print()


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    args = sys.argv[1:]
    named = next((a for a in args if not a.startswith("--")), None)

    if "--fetch" in args:
        fetch(named)
        return 0
    if "--match" in args:
        suggest_matches(named)
        return 0
    if "--status" in args:
        status()
        return 0
    print(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(main())
