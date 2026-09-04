"""Terrain elevation, held locally, so the project stops asking anyone for it.

Two things wanted elevation and neither could have it. Cables drawn on the map
crawl into every hollow they actually fly over, because the browser's
`queryTerrainElevation` returns null. And "when do I lose the sun at the bottom
station" is a horizon calculation that needs terrain in every direction.

    python -m scraper.elevation --tiles       # which DEM tiles are needed
    python -m scraper.elevation --download    # fetch them once, then cache
    python -m scraper.elevation --lifts       # elevations per lift vertex
    python -m scraper.elevation --status

Source: SRTM 1-arcsec (30 m) via the AWS Open Data terrain tiles bucket. That
choice is deliberate. Sampling 30,000 vertices from a free community elevation
API would be thirty thousand requests against someone's donated capacity; this
is a bulk bucket built for exactly this, fetched once, and every query
afterwards is a local array lookup that costs nobody anything.

Tiles are cached in `data/dem/` (gitignored) and never re-fetched. No numpy:
these are point lookups into a byte array, and the web service should not grow
a dependency for a job that only ever runs offline.
"""
import gzip
import json
import math
import struct
import sys
import time
from pathlib import Path

import requests

from .collect import load_resorts
from .db import cursor

TILE_URL = "https://s3.amazonaws.com/elevation-tiles-prod/skadi/{ns}/{name}.hgt.gz"
CACHE = Path(__file__).parent.parent / "data" / "dem"
UA = ("ski-lift-records/1.0 (hobby research project; "
      "github.com/icarusfall/ski-lift-records)")
PAUSE_S = 1.0
TIMEOUT_S = 300

# SRTM 1-arcsec: 3601x3601 big-endian int16, one degree square, first row is
# the NORTH edge. -32768 marks a void.
SAMPLES = 3601
STEP = SAMPLES - 1          # arcseconds per degree covered by the grid
VOID = -32768
# Generous enough that a horizon profile can see the ridge that actually blocks
# the sun: about 25 km at these latitudes.
TILE_PAD_DEG = 0.25

_tiles: dict[str, bytes] = {}


def tile_name(lat: int, lon: int) -> str:
    return (f"{'N' if lat >= 0 else 'S'}{abs(lat):02d}"
            f"{'E' if lon >= 0 else 'W'}{abs(lon):03d}")


def needed_tiles() -> list[str]:
    """Every 1-degree tile any resort might need, padded for horizons."""
    names = set()
    for r in load_resorts():
        if not r.get("latitude"):
            continue
        for dlat in (-TILE_PAD_DEG, 0, TILE_PAD_DEG):
            for dlon in (-TILE_PAD_DEG, 0, TILE_PAD_DEG):
                names.add(tile_name(math.floor(r["latitude"] + dlat),
                                    math.floor(r["longitude"] + dlon)))
    return sorted(names)


def download(force: bool = False) -> int:
    CACHE.mkdir(parents=True, exist_ok=True)
    s = requests.Session()
    s.headers["User-Agent"] = UA
    names = needed_tiles()
    print(f"\n  {len(names)} DEM tile(s) -> {CACHE}\n")
    got = skipped = 0
    for name in names:
        dest = CACHE / f"{name}.hgt.gz"
        if dest.exists() and not force:
            print(f"  {name}  cached")
            skipped += 1
            continue
        url = TILE_URL.format(ns=name[:3], name=name)
        print(f"  {name}  fetching …", end="", flush=True)
        r = s.get(url, timeout=TIMEOUT_S)
        if r.status_code == 404:
            # Ocean squares simply do not exist in the dataset. Not an error.
            print(" not in dataset (all sea)")
            continue
        r.raise_for_status()
        dest.write_bytes(r.content)
        print(f" {len(r.content) / 1e6:.0f} MB")
        got += 1
        time.sleep(PAUSE_S)
    print(f"\n  {got} downloaded, {skipped} already cached.")
    print("  SRTM 1-arcsec via AWS Open Data terrain tiles (public domain).\n")
    return 0


def _load(name: str) -> bytes | None:
    """Decompressed tile bytes, memoised."""
    if name in _tiles:
        return _tiles[name]
    path = CACHE / f"{name}.hgt.gz"
    if not path.exists():
        _tiles[name] = None
        return None
    with gzip.open(path, "rb") as fh:
        data = fh.read()
    if len(data) != SAMPLES * SAMPLES * 2:
        _tiles[name] = None
        return None
    # Two tiles held at once is enough when work is grouped by resort, and
    # keeps this well clear of half a gigabyte of resident arrays.
    if len(_tiles) > 2:
        _tiles.clear()
    _tiles[name] = data
    return data


def _sample(data: bytes, row: int, col: int) -> int | None:
    row = min(max(row, 0), SAMPLES - 1)
    col = min(max(col, 0), SAMPLES - 1)
    v = struct.unpack_from(">h", data, (row * SAMPLES + col) * 2)[0]
    return None if v == VOID else v


def elevation(lat: float, lon: float) -> float | None:
    """Metres above sea level, bilinearly interpolated between samples."""
    base_lat, base_lon = math.floor(lat), math.floor(lon)
    data = _load(tile_name(base_lat, base_lon))
    if data is None:
        return None
    # Row 0 is the north edge, so latitude counts downwards.
    y = (base_lat + 1 - lat) * STEP
    x = (lon - base_lon) * STEP
    r0, c0 = int(y), int(x)
    dy, dx = y - r0, x - c0
    q = [_sample(data, r0, c0), _sample(data, r0, c0 + 1),
         _sample(data, r0 + 1, c0), _sample(data, r0 + 1, c0 + 1)]
    good = [v for v in q if v is not None]
    if not good:
        return None
    if len(good) < 4:
        # A void corner near a lake or steep face: fall back to the mean of
        # what is there rather than interpolating through a -32768.
        return sum(good) / len(good)
    top = q[0] * (1 - dx) + q[1] * dx
    bottom = q[2] * (1 - dx) + q[3] * dx
    return top * (1 - dy) + bottom * dy


def lifts() -> int:
    """Store an elevation per vertex on every lift geometry."""
    with cursor() as cur:
        cur.execute("""
            SELECT resort_id, osm_id, geometry FROM lift_geometry
            ORDER BY resort_id, osm_id
        """)
        rows = cur.fetchall()
    if not rows:
        print("\n  No lift geometry — run scraper.osm --fetch first.\n")
        return 1

    done = missing = 0
    updates = []
    for r in rows:
        coords = r["geometry"] if isinstance(r["geometry"], list) else json.loads(r["geometry"] or "[]")
        els = [elevation(c[1], c[0]) for c in coords]
        if not els or any(e is None for e in els):
            missing += 1
            continue
        updates.append((json.dumps([round(e) for e in els]),
                        r["resort_id"], r["osm_id"]))
        done += 1

    with cursor() as cur:
        for payload, rid, osm_id in updates:
            cur.execute("""
                UPDATE lift_geometry SET elevations = %s
                WHERE resort_id = %s AND osm_id = %s
            """, (payload, rid, osm_id))

    print(f"\n  {done:,} lift ways given per-vertex elevations; "
          f"{missing:,} skipped for missing DEM coverage.\n")
    return 0


def status() -> int:
    have = sorted(p.stem.replace(".hgt", "") for p in CACHE.glob("*.hgt.gz")) if CACHE.exists() else []
    want = needed_tiles()
    print(f"\n  DEM tiles: {len(have)} cached of {len(want)} wanted")
    if set(want) - set(have):
        print("    missing: " + ", ".join(sorted(set(want) - set(have))))
    with cursor() as cur:
        cur.execute("""
            SELECT COUNT(*) AS ways,
                   COUNT(elevations) AS with_elev,
                   MIN((elevations ->> 0)::int) AS lo,
                   MAX((elevations ->> 0)::int) AS hi
            FROM lift_geometry
        """)
        r = cur.fetchone()
    print(f"  lift_geometry: {r['with_elev']:,} of {r['ways']:,} ways carry "
          f"elevations (first-vertex range {r['lo']}–{r['hi']} m)\n")
    return 0


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    args = sys.argv[1:]
    if "--tiles" in args:
        names = needed_tiles()
        print(f"\n  {len(names)} tile(s): " + ", ".join(names) + "\n")
        return 0
    if "--download" in args:
        return download(force="--force" in args)
    if "--lifts" in args:
        return lifts()
    if "--status" in args:
        return status()
    print(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(main())
