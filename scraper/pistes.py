"""Piste geometry from OpenStreetMap, so the map reads as a ski area.

The lift map shows how people get up the mountain but nothing about what they
came for. This fetches the runs — `piste:type=downhill` — and stores them with
their difficulty, which is what turns a diagram of cables into somewhere you
can imagine skiing.

    python -m scraper.pistes --fetch            # all resorts (paced)
    python -m scraper.pistes --fetch cervinia
    python -m scraper.pistes --status

Overpass is a free community service, so this reuses `scraper.osm`'s retry and
pacing: one query per resort, spaced, identified, and the whole job runs once.

## Difficulty is rendered on the European convention
`piste:difficulty` is a word, not a colour, and the two schemes disagree in a
way that would mislead exactly the audience this is for. In Europe **easy is
blue and intermediate is red**; North America has no red at all and calls the
same gradient green/blue/black. Every resort here is Alpine or Pyrenean, so
the European mapping is the correct one — see PISTE_COLOURS in the map page.
"""
import json
import sys
import time

import psycopg2.extras

from .collect import load_resorts
from .db import cursor
from .osm import REQUEST_PAUSE_S, bbox_for, overpass, _length_m

# Only downhill runs. Nordic, skitour and sled tracks are real pistes in OSM's
# vocabulary but are not what a lift map is about, and including them would
# bury the resort in cross-country loops.
PISTE_TYPES = {"downhill"}
# A short stub is usually a connector, a slip road between two runs, or a
# mapping artefact. Below this they add clutter and no information.
MIN_LENGTH_M = 80


def fetch_resort(resort: dict, attempts: int = 3) -> list[dict]:
    bbox = bbox_for(resort)
    query = (f'[out:json][timeout:180];'
             f'way["piste:type"="downhill"]({bbox});'
             f'out tags geom;')
    resp = overpass(query, attempts)

    out = []
    for el in resp.json().get("elements", []):
        tags = el.get("tags", {})
        geom = el.get("geometry") or []
        if tags.get("piste:type") not in PISTE_TYPES or len(geom) < 2:
            continue
        length = _length_m(geom)
        if length < MIN_LENGTH_M:
            continue
        # `piste:name` is the run's own name where it differs from the way's.
        name = (tags.get("piste:name") or tags.get("name") or "").strip() or None
        out.append({
            "osm_id": el["id"],
            "resort_id": resort["id"],
            "name": name,
            "difficulty": (tags.get("piste:difficulty") or "").strip() or None,
            "piste_type": tags.get("piste:type"),
            "length_m": length,
            "geometry": json.dumps(
                [[round(p["lon"], 5), round(p["lat"], 5)] for p in geom]),
        })
    return out


def _store(rows: list[dict]) -> int:
    if not rows:
        return 0
    with cursor() as cur:
        psycopg2.extras.execute_values(cur, """
            INSERT INTO piste_geometry
                (osm_id, resort_id, name, difficulty, piste_type, length_m, geometry)
            VALUES %s
            ON CONFLICT (resort_id, osm_id) DO UPDATE SET
                name       = EXCLUDED.name,
                difficulty = EXCLUDED.difficulty,
                piste_type = EXCLUDED.piste_type,
                length_m   = EXCLUDED.length_m,
                geometry   = EXCLUDED.geometry,
                fetched_at = NOW()
        """, [(r["osm_id"], r["resort_id"], r["name"], r["difficulty"],
               r["piste_type"], r["length_m"], r["geometry"]) for r in rows])
    return len(rows)


def fetch(resort_filter: str | None = None):
    resorts = [r for r in load_resorts()
               if r.get("latitude") and (resort_filter is None or r["id"] == resort_filter)]
    if not resorts:
        print(f"No resort found with id '{resort_filter}'")
        return 1
    print(f"\nFetching piste geometry for {len(resorts)} resort(s) from OpenStreetMap\n")
    total, failed = 0, []
    for i, r in enumerate(resorts, 1):
        try:
            rows = fetch_resort(r)
            n = _store(rows)
            total += n
            named = sum(1 for x in rows if x["name"])
            graded = sum(1 for x in rows if x["difficulty"])
            print(f"  {r['id']:<18} {n:>5} runs  ({named} named, {graded} graded)")
        except Exception as e:
            failed.append(r["id"])
            print(f"  {r['id']:<18} FAILED: {e}")
        if i < len(resorts):
            time.sleep(REQUEST_PAUSE_S)
    print(f"\n  {total:,} piste ways stored.")
    if failed:
        print(f"  {len(failed)} resort(s) failed and are worth a single retry: "
              + ", ".join(failed))
        print("  Overpass 429/504 is about its load, not about us — rerun later.")
    print()
    return 0


def status() -> int:
    with cursor() as cur:
        cur.execute("""
            SELECT resort_id, COUNT(*) AS n,
                   COUNT(*) FILTER (WHERE difficulty IS NOT NULL) AS graded,
                   COUNT(*) FILTER (WHERE name IS NOT NULL) AS named,
                   SUM(length_m) / 1000 AS km
            FROM piste_geometry GROUP BY resort_id ORDER BY resort_id
        """)
        rows = cur.fetchall()
        cur.execute("""
            SELECT COALESCE(difficulty, '(ungraded)') AS d, COUNT(*) AS n
            FROM piste_geometry GROUP BY d ORDER BY COUNT(*) DESC
        """)
        grades = cur.fetchall()
    if not rows:
        print("\n  No piste geometry yet — run --fetch.\n")
        return 1
    print(f"\n  {'resort':<18}{'runs':>6}{'graded':>8}{'named':>7}{'km':>8}")
    for r in rows:
        print(f"  {r['resort_id']:<18}{r['n']:>6}{r['graded']:>8}{r['named']:>7}"
              f"{(r['km'] or 0):>8}")
    print(f"\n  {sum(r['n'] for r in rows):,} runs across {len(rows)} resort(s).")
    print("  By difficulty: " + ", ".join(f"{g['d']} {g['n']}" for g in grades))
    print()
    return 0


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    args = sys.argv[1:]
    if "--fetch" in args:
        i = args.index("--fetch")
        target = args[i + 1] if len(args) > i + 1 and not args[i + 1].startswith("-") else None
        return fetch(target)
    if "--status" in args:
        return status()
    print(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(main())
