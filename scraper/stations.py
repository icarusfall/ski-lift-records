"""Measured snow depth from Alpine weather stations, to check ERA5 against.

ERA5 gives a modelled snowpack on a 25 km grid, which saturates over glaciers
and cannot know a valley. This ingests the real thing: the Matiu et al. dataset
of in-situ station observations across the Alps, 1971-2019, from Zenodo record
4572636 (CC BY 4.0).

    python -m scraper.stations --download    # fetch archives once, then cache
    python -m scraper.stations --load        # parse into the database
    python -m scraper.stations --match       # rank stations per resort
    python -m scraper.stations --status
    python -m scraper.stations --compare     # ERA5's model vs the measurements

This is a **bulk download of a published archive, not a scrape**. Roughly a
dozen files, fetched once and kept; a second run re-fetches nothing. Zenodo is
a free public service, so requests are paced and identified, and the two
gigabytes of paper-reproduction material in the record are left alone.

Cite as: Matiu et al. (2021), doi:10.5281/zenodo.4572636. The dataset's own
LICENSE file also carries per-provider terms and is downloaded alongside.

## What is actually available

Daily depth exists only for France and Italy. Austria and Switzerland appear
as **monthly means only** — their providers evidently did not licence daily
redistribution — which covers 14 of the 36 resorts. Monthly still answers
"how has February changed since 1971"; it cannot answer an arbitrary
14-22 February window. Germany is skipped: no German resorts in the roster.
"""
import csv
import io
import math
import sys
import time
import zipfile
from pathlib import Path

import requests
from psycopg2.extras import execute_values

from .collect import load_resorts
from .db import cursor

RECORD = "4572636"
API = f"https://zenodo.org/api/records/{RECORD}"
CACHE = Path(__file__).parent.parent / "data" / "stations"
UA = ("ski-lift-records/1.0 (hobby research project; "
      "github.com/icarusfall/ski-lift-records)")
# Be a good citizen of a free archive: a pause between files, and a long
# timeout rather than a retry storm if the server is thinking.
PAUSE_S = 3.0
TIMEOUT_S = 180

# Matching gates. Distance is not enough on its own — this is an Alpine
# dataset, so the two Pyrenean resorts must end up with nothing rather than
# with a station 300 km away.
MAX_KM = 15.0
MIN_YEARS = 20
# A record ending in 1982 is not useless: a resort often has an old station
# that closed and a newer one that opened, and together they reach further back
# than either alone. So this only decides which station is *primary*; the
# others are kept and offered as their own series. They are never spliced into
# one line — two stations at different elevations would fake a step change.
MUST_REACH_YEAR = 2010
DAILY_BONUS = 5.0          # in score-km: prefer daily over monthly
CURRENT_BONUS = 8.0        # prefer a station still reporting, for the primary
MAX_CANDIDATES = 4
# Every station sits far below its resort's summit — they measure valley and
# mid-mountain snowpack, which is what exists. Only an extreme gap is worth
# flagging, or the flag fires on everything and stops being read.
REVIEW_ELEV_M = 2000       # station this far below the summit wants a human
REVIEW_KM = 8.0
BATCH = 5000

# Only what the roster can use. The aux_paper_* archives are 2 GB of material
# for reproducing the paper's figures and are deliberately never requested.
WANTED = [
    "meta_all.csv",
    "meta_00_column_names_content.txt",
    "data_daily_00_column_names_content.txt",
    "data_monthly_00_column_names_content.txt",
    "00_DATA_LICENSE_AND_TERMS.pdf",
    # Daily — France and Italy.
    "data_daily_FR_METEOFRANCE.zip",
    "data_daily_IT_VDA_CF.zip",      # Valle d'Aosta: Cervinia, La Thuile, Courmayeur
    "data_daily_IT_LOMBARDIA.zip",   # Livigno
    "data_daily_IT_TN.zip",          # Trentino: Passo Tonale
    "data_daily_IT_BZ.zip",          # South Tyrol, adjacent to Passo Tonale
    # Monthly only — Austria and Switzerland.
    "data_monthly_AT_HZB.zip",
    "data_monthly_CH_SLF.zip",
    "data_monthly_CH_METEOSWISS.zip",
]


def _session() -> requests.Session:
    s = requests.Session()
    s.headers["User-Agent"] = UA
    return s


def download(force: bool = False) -> int:
    """Fetch the wanted files once. Anything already cached is left alone."""
    CACHE.mkdir(parents=True, exist_ok=True)
    s = _session()
    print(f"\n  Zenodo record {RECORD} -> {CACHE}\n")
    meta = s.get(API, timeout=TIMEOUT_S)
    meta.raise_for_status()
    files = {f["key"]: f for f in meta.json()["files"]}

    fetched = skipped = 0
    for name in WANTED:
        f = files.get(name)
        if not f:
            print(f"  {name:<44} MISSING from the record")
            continue
        dest = CACHE / name
        if dest.exists() and not force:
            print(f"  {name:<44} cached")
            skipped += 1
            continue
        url = f["links"]["self"]
        mb = f["size"] / 1e6
        print(f"  {name:<44} {mb:>7.1f} MB …", end="", flush=True)
        r = s.get(url, timeout=TIMEOUT_S)
        r.raise_for_status()
        dest.write_bytes(r.content)
        print(" done")
        fetched += 1
        time.sleep(PAUSE_S)

    print(f"\n  {fetched} downloaded, {skipped} already cached.")
    if fetched:
        print("  Matiu et al. (2021), doi:10.5281/zenodo.4572636, CC BY 4.0.")
    print()
    return 0


def archives(kind: str) -> list[Path]:
    """Cached zips of a given kind ('daily' or 'monthly')."""
    return sorted(CACHE.glob(f"data_{kind}_*.zip"))


def peek(name: str, n: int = 5) -> None:
    """Print the first few lines of every member of a cached archive."""
    path = CACHE / name
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        print(f"\n  {name}: {len(names)} member(s)")
        for member in names[:3]:
            with z.open(member) as fh:
                head = io.TextIOWrapper(fh, encoding="utf-8", errors="replace")
                print(f"\n  --- {member} ---")
                for i, line in enumerate(head):
                    if i >= n:
                        break
                    print("   ", line.rstrip()[:160])


def _providers() -> dict[str, str]:
    """Provider -> cadence, from the archives actually cached.

    Four providers appear in meta_all.csv but publish no data at all
    (IT_VDA_AIBM, IT_SMI, IT_PIEMONTE, IT_TN_TUM) — they were used in the
    paper's consistency analysis but not redistributed. Deriving cadence from
    the files on disk means those stations are never matched to a resort and
    then found to be empty. That trap cost a first pass: Cervinia's nearest
    station, 100 m away, is one of them.
    """
    out = {}
    for kind in ("monthly", "daily"):       # daily wins where both exist
        for p in archives(kind):
            out[p.stem.replace(f"data_{kind}_", "")] = kind
    return out


def read_meta() -> list[dict]:
    """Stations with a snow-depth record and an archive we actually hold."""
    cadence = _providers()
    out = []
    with open(CACHE / "meta_all.csv", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            if not r["HS_year_start"] or r["Provider"] not in cadence:
                continue
            out.append({
                "id": r["Name"], "provider": r["Provider"],
                "country": r["Provider"][:2],
                "lat": float(r["Latitude"]), "lon": float(r["Longitude"]),
                "elev": int(float(r["Elevation"])),
                "y0": int(r["HS_year_start"]), "y1": int(r["HS_year_end"]),
                "cadence": cadence[r["Provider"]],
            })
    return out


def _km(lat1, lon1, lat2, lon2) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def rank_candidates(resort: dict, meta: list[dict]) -> list[dict]:
    """Stations that could stand for this resort, best first.

    The gates matter more than the score. A record that stops in 1982 is
    useless for a climate trend however close the station is, and a station in
    the Pyrenees cannot stand in for anything — this is an Alpine dataset, so
    Grandvalira and Baqueira-Beret have no candidate at all and should not be
    given a bad one.
    """
    out = []
    for s in meta:
        d = _km(resort["latitude"], resort["longitude"], s["lat"], s["lon"])
        if d > MAX_KM or (s["y1"] - s["y0"] + 1) < MIN_YEARS:
            continue
        gap = s["elev"] - resort["top_altitude_m"]
        score = (d + abs(gap) / 200
                 - (DAILY_BONUS if s["cadence"] == "daily" else 0)
                 - (CURRENT_BONUS if s["y1"] >= MUST_REACH_YEAR else 0))
        out.append({**s, "distance_km": round(d, 2), "elev_diff_m": gap,
                    "current": s["y1"] >= MUST_REACH_YEAR, "score": score})
    return sorted(out, key=lambda c: c["score"])


def match(verbose: bool = True) -> dict:
    resorts = [r for r in load_resorts() if r.get("latitude")]
    meta = read_meta()
    by_id = {s["id"]: s for s in meta}
    picks, unmatched = {}, []
    for r in resorts:
        cands = rank_candidates(r, meta)[:MAX_CANDIDATES]
        if not cands:
            unmatched.append(r["id"])
            continue
        picks[r["id"]] = cands

    used = {c["id"] for cs in picks.values() for c in cs}
    with cursor() as cur:
        for sid in used:
            s = by_id[sid]
            cur.execute("""
                INSERT INTO snow_stations
                    (id, provider, country, latitude, longitude, elevation_m,
                     hs_year_start, hs_year_end, cadence)
                VALUES (%(id)s, %(provider)s, %(country)s, %(lat)s, %(lon)s,
                        %(elev)s, %(y0)s, %(y1)s, %(cadence)s)
                ON CONFLICT (id) DO UPDATE SET
                    hs_year_start = EXCLUDED.hs_year_start,
                    hs_year_end   = EXCLUDED.hs_year_end,
                    cadence       = EXCLUDED.cadence
            """, s)
        cur.execute("DELETE FROM resort_stations")
        for rid, cands in picks.items():
            for i, c in enumerate(cands, 1):
                cur.execute("""
                    INSERT INTO resort_stations
                        (resort_id, station_id, rank, distance_km, elev_diff_m, chosen)
                    VALUES (%s, %s, %s, %s, %s, %s)
                """, (rid, c["id"], i, c["distance_km"], c["elev_diff_m"], i == 1))

    if verbose:
        print(f"\nStation matches — within {MAX_KM} km, 20+ years, reaching "
              f"{MUST_REACH_YEAR} or later\n")
        print(f"  {'resort':<17}{'station':<32}{'km':>6}{'Δelev':>8}{'record':>12}  kind")
        for rid in sorted(picks):
            c = picks[rid][0]
            flag = "  ← review" if abs(c["elev_diff_m"]) > REVIEW_ELEV_M or c["distance_km"] > REVIEW_KM else ""
            print(f"  {rid:<17}{c['id'][:30]:<32}{c['distance_km']:>5.1f}"
                  f"{c['elev_diff_m']:>+8}{c['y0']:>7}-{c['y1']}  {c['cadence']}{flag}")
        for rid in unmatched:
            print(f"  {rid:<17}— no station qualifies")
        # What the older, closed stations buy: how much further back the resort
        # can be seen once they are kept as separate series.
        gained = [(rid, min(c["y0"] for c in cs), cs[0]["y0"])
                  for rid, cs in picks.items()]
        gained = [g for g in gained if g[1] < g[2]]
        if gained:
            print(f"\n  Secondary stations extend {len(gained)} resort(s) further back:")
            for rid, earliest, primary in sorted(gained, key=lambda g: g[1] - g[2])[:8]:
                print(f"    {rid:<17}{primary} → {earliest}  "
                      f"({primary - earliest} extra years)")
        print(f"\n  {len(picks)} matched, {len(unmatched)} without a candidate, "
              f"{len(used)} stations to load.")
        print("  Δelev is station minus resort summit. It is large and negative "
              "everywhere, because\n  snow is measured on flat undisturbed ground "
              "in valleys, not on a summit and\n  never on a piste — that is the "
              "nature of the measurement, not a fault in the\n  match. Rows marked "
              "review are unusually far or unusually low even by that\n  standard.\n")
    return picks


def _rows_for(stations: set[str]):
    """Yield (station, date, hs, hs_filled, hn) for the wanted stations only.

    Streams each archive rather than reading it whole: the daily files hold
    4.7 million rows between them and we want perhaps thirty stations.
    """
    for path in archives("daily"):
        with zipfile.ZipFile(path) as z:
            with z.open(z.namelist()[0]) as fh:
                for r in csv.DictReader(io.TextIOWrapper(fh, encoding="utf-8",
                                                         errors="replace")):
                    if r["Name"] not in stations:
                        continue
                    yield (r["Name"], r["Date"], r["HS_after_qc"] or None,
                           r["HS_after_gapfill"] or None, r["HN_after_qc"] or None)
    for path in archives("monthly"):
        with zipfile.ZipFile(path) as z:
            with z.open(z.namelist()[0]) as fh:
                for r in csv.DictReader(io.TextIOWrapper(fh, encoding="utf-8",
                                                         errors="replace")):
                    if r["Name"] not in stations:
                        continue
                    yield (r["Name"], f"{int(r['year']):04d}-{int(r['month']):02d}-01",
                           r["HS"] or None, r["HS_gapfill"] or None, r["HN"] or None)


def load() -> int:
    with cursor() as cur:
        # Every candidate, not only the primary: an older station that closed
        # is exactly what extends a resort's record backwards, and it is drawn
        # as its own series rather than joined onto the newer one.
        cur.execute("SELECT DISTINCT station_id FROM resort_stations")
        wanted = {r["station_id"] for r in cur.fetchall()}
    if not wanted:
        print("\n  No matches yet — run --match first.\n")
        return 1
    print(f"\n  Loading {len(wanted)} station(s) from the cached archives…")

    batch, total, empty = [], 0, 0
    with cursor() as cur:
        for row in _rows_for(wanted):
            if row[2] is None and row[3] is None and row[4] is None:
                empty += 1
                continue
            batch.append(row)
            if len(batch) >= BATCH:
                execute_values(cur, """
                    INSERT INTO station_snow (station_id, date, hs_cm, hs_filled_cm, hn_cm)
                    VALUES %s ON CONFLICT (station_id, date) DO UPDATE SET
                        hs_cm = EXCLUDED.hs_cm,
                        hs_filled_cm = EXCLUDED.hs_filled_cm,
                        hn_cm = EXCLUDED.hn_cm
                """, batch)
                total += len(batch)
                print(f"    {total:,} rows…", end="\r", flush=True)
                batch = []
        if batch:
            execute_values(cur, """
                INSERT INTO station_snow (station_id, date, hs_cm, hs_filled_cm, hn_cm)
                VALUES %s ON CONFLICT (station_id, date) DO UPDATE SET
                    hs_cm = EXCLUDED.hs_cm,
                    hs_filled_cm = EXCLUDED.hs_filled_cm,
                    hn_cm = EXCLUDED.hn_cm
            """, batch)
            total += len(batch)
    print(f"    {total:,} rows loaded; {empty:,} all-null rows skipped.\n")
    return 0


def status() -> int:
    with cursor() as cur:
        cur.execute("""
            SELECT rs.resort_id, rs.station_id, s.cadence, s.elevation_m,
                   rs.distance_km, rs.elev_diff_m,
                   COUNT(ss.date) AS n,
                   MIN(ss.date) AS first, MAX(ss.date) AS last
            FROM resort_stations rs
            JOIN snow_stations s ON s.id = rs.station_id
            LEFT JOIN station_snow ss ON ss.station_id = rs.station_id
            WHERE rs.chosen
            GROUP BY rs.resort_id, rs.station_id, s.cadence, s.elevation_m,
                     rs.distance_km, rs.elev_diff_m
            ORDER BY rs.resort_id
        """)
        rows = cur.fetchall()
    print(f"\n  {'resort':<17}{'station':<30}{'kind':<9}{'rows':>8}  span")
    for r in rows:
        span = f"{r['first']} → {r['last']}" if r["n"] else "— nothing loaded"
        print(f"  {r['resort_id']:<17}{r['station_id'][:28]:<30}{r['cadence']:<9}"
              f"{r['n']:>8,}  {span}")
    print(f"\n  {len(rows)} resorts with a chosen station.\n")
    return 0


def compare() -> int:
    """ERA5's modelled snowpack against the measured one, where both exist.

    The point of the ingest. ERA5 is downscaled to each resort's summit and the
    stations sit a kilometre or more below, so the two are not measuring the
    same snow and the absolute numbers should differ. What matters is whether
    they move together: a strong correlation means the modelled series is a
    usable stand-in for year-to-year variation even where its level is wrong.
    """
    with cursor() as cur:
        cur.execute("""
            SELECT rs.resort_id, rs.station_id, s.elevation_m, rs.elev_diff_m,
                   COUNT(*) AS n,
                   CORR(cd.snow_depth_model_m * 100, ss.hs_cm) AS r,
                   AVG(cd.snow_depth_model_m * 100) AS model_cm,
                   AVG(ss.hs_cm) AS measured_cm
            FROM resort_stations rs
            JOIN snow_stations s  ON s.id = rs.station_id
            JOIN station_snow ss  ON ss.station_id = rs.station_id
            JOIN climate_daily cd ON cd.resort_id = rs.resort_id AND cd.date = ss.date
            WHERE rs.chosen AND s.cadence = 'daily' AND ss.hs_cm IS NOT NULL
              AND EXTRACT(MONTH FROM ss.date) IN (12, 1, 2, 3)
            GROUP BY rs.resort_id, rs.station_id, s.elevation_m, rs.elev_diff_m
            HAVING COUNT(*) > 500
            ORDER BY CORR(cd.snow_depth_model_m * 100, ss.hs_cm) DESC NULLS LAST
        """)
        rows = cur.fetchall()
    if not rows:
        print("\n  Nothing to compare — run --match and --load first.\n")
        return 1
    print("\nERA5 modelled snow depth vs measured, December to March\n")
    print(f"  {'resort':<17}{'days':>7}{'corr':>7}{'model':>9}{'measured':>10}"
          f"{'Δelev':>8}")
    for r in rows:
        rr = float(r["r"]) if r["r"] is not None else float("nan")
        print(f"  {r['resort_id']:<17}{r['n']:>7,}{rr:>7.2f}"
              f"{float(r['model_cm']):>8.0f}cm{float(r['measured_cm']):>9.0f}cm"
              f"{r['elev_diff_m']:>+8}")
    good = [r for r in rows if r["r"] is not None and float(r["r"]) >= 0.7]
    print(f"\n  {len(good)} of {len(rows)} resorts correlate at 0.7 or better.")
    print("  The levels are not meant to agree — ERA5 is modelled at the summit "
          "and the station\n  sits a kilometre or more below, so it should read "
          "far deeper. What is being tested\n  is whether they rise and fall "
          "together, which is what the climate tab's trends rest on.\n")
    return 0


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    args = sys.argv[1:]
    if "--download" in args:
        return download(force="--force" in args)
    if "--peek" in args:
        peek(args[args.index("--peek") + 1])
        return 0
    if "--match" in args:
        match()
        return 0
    if "--load" in args:
        return load()
    if "--status" in args:
        return status()
    if "--compare" in args:
        return compare()
    print(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(main())
