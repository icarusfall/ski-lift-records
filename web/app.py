"""
Simple Flask dashboard for the ski lift tracker.
Shows current status and historical trends per resort.
"""

import csv
import hashlib
import hmac
import io
import json
import math
import os
import sys
import re
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

# Allow importing scraper package from parent directory
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from flask import (Flask, flash, jsonify, redirect, render_template,
                   request, session, url_for, Response)
from scraper.db import (cursor, get_setting, set_setting, set_resort_enabled)

app = Flask(__name__)

ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "")
# Sessions only need to outlive a deploy if the token does; deriving the
# signing key from the token keeps logins valid across restarts.
app.secret_key = (
    hashlib.sha256(f"ski-lift-admin:{ADMIN_TOKEN}".encode()).digest()
    if ADMIN_TOKEN else os.urandom(32)
)


def get_latest_snapshots():
    with cursor() as cur:
        cur.execute("""
            SELECT
                s.resort_id,
                r.name,
                r.country,
                r.area,
                r.top_altitude_m,
                s.snapshot_date,
                s.lifts_open,
                s.lifts_total,
                s.pct_lifts_open,
                s.pistes_open_km,
                s.pistes_total_km,
                s.snow_depth_mountain_cm,
                s.snow_depth_valley_cm,
                s.snow_condition,
                s.last_snowfall_date,
                s.piste_conditions,
                s.avalanche_danger,
                s.wind_gust_max_kmh,
                s.wind_speed_max_kmh,
                s.temp_min_c,
                s.temp_max_c,
                s.fresh_snow_cm,
                s.precipitation_mm,
                s.weather_code,
                s.source,
                s.is_uk_school_holiday,
                s.holiday_name,
                s.scrape_error
            FROM snapshots s
            JOIN resorts r ON r.id = s.resort_id
            WHERE s.id = (
                SELECT s2.id FROM snapshots s2
                WHERE s2.resort_id = s.resort_id
                ORDER BY s2.snapshot_date DESC, s2.snapshot_time DESC
                LIMIT 1
            )
            ORDER BY r.country, r.area, r.name
        """)
        return cur.fetchall()


def get_history(resort_id: str, days: int = 60):
    with cursor() as cur:
        cur.execute("""
            SELECT * FROM (
                SELECT DISTINCT ON (snapshot_date)
                       snapshot_date, lifts_open, lifts_total, pct_lifts_open,
                       pistes_open_km, pistes_total_km,
                       snow_depth_mountain_cm, snow_depth_valley_cm,
                       snow_condition, last_snowfall_date, piste_conditions, avalanche_danger,
                       wind_gust_max_kmh, wind_speed_max_kmh,
                       temp_min_c, temp_max_c,
                       fresh_snow_cm, precipitation_mm, weather_code,
                       sunshine_hours, freezing_level_max_m, wind_700hpa_max_kmh,
                       is_uk_school_holiday, holiday_name, slot,
                       MIN(pct_lifts_open) OVER (PARTITION BY snapshot_date) AS pct_min_day
                FROM snapshots
                WHERE resort_id = %s
                  AND snapshot_date >= CURRENT_DATE - %s
                -- one row per day: the midday capture is the canonical one,
                -- so it stays comparable with the single-capture season 1
                ORDER BY snapshot_date, (slot <> 'midday'), snapshot_time DESC
            ) d ORDER BY snapshot_date
        """, (resort_id, days))
        return cur.fetchall()


def get_lift_history(resort_id: str, days: int = 60):
    """Per-lift open/closed history for a resort (primary scrapers only)."""
    with cursor() as cur:
        cur.execute("""
            SELECT
                l.name,
                l.is_link,
                s.snapshot_date,
                lr.status
            FROM lift_readings lr
            JOIN lifts l ON l.id = lr.lift_id
            JOIN snapshots s ON s.id = lr.snapshot_id
            WHERE l.resort_id = %s
              AND s.snapshot_date >= CURRENT_DATE - %s
              AND NOT l.name LIKE 'lift\\_%%'
            ORDER BY l.name, s.snapshot_date
        """, (resort_id, days))
        return cur.fetchall()


def collection_status() -> dict:
    """Freshness of the most recent collection, for the dashboard banner."""
    from scraper.db import get_last_collection
    last = get_last_collection()
    paused = get_setting("collection_paused", "false") == "true"
    today = datetime.now(timezone.utc).date()
    age = (today - last["snapshot_date"]).days if last else None
    return {
        "paused": paused,
        "last_date": last["snapshot_date"] if last else None,
        "clean": last["clean"] if last else 0,
        "resorts": last["resorts"] if last else 0,
        "age_days": age,
        # Paused is a deliberate state, so only an unpaused gap is a problem.
        "stale": (not paused) and age is not None and age >= 2,
        "resume_date": get_setting("auto_resume_date", "") or "",
    }


@app.route("/")
def index():
    rows = get_latest_snapshots()
    return render_template("index.html", rows=rows, status=collection_status())


@app.route("/resort/<resort_id>")
def resort_detail(resort_id: str):
    days = request.args.get("days", 20, type=int)
    days = max(1, min(days, 365))
    history = get_history(resort_id, days)
    lift_history = get_lift_history(resort_id, days)
    return render_template("resort.html",
                           resort_id=resort_id,
                           history=history,
                           lift_history=lift_history,
                           days=days)


def _plain(v):
    """JSON-friendly scalar: ISO dates, float decimals."""
    if isinstance(v, (date, datetime)):
        return v.isoformat()
    if isinstance(v, Decimal):
        return float(v)
    return v


@app.route("/explore")
def explore():
    return render_template("explore.html")


@app.route("/api/explorer.json")
def api_explorer():
    """Whole dataset in compact columnar form for the client-side explorer."""
    with cursor() as cur:
        cur.execute("""
            SELECT * FROM (
                SELECT DISTINCT ON (s.resort_id, s.snapshot_date)
                       s.resort_id, r.name, r.country, r.area, r.top_altitude_m,
                       s.snapshot_date, s.lifts_open, s.lifts_total, s.pct_lifts_open,
                       s.snow_depth_mountain_cm, s.snow_depth_valley_cm,
                       s.fresh_snow_cm, s.precipitation_mm,
                       s.wind_gust_max_kmh, s.wind_speed_max_kmh,
                       s.temp_min_c, s.temp_max_c,
                       s.sunshine_hours, s.freezing_level_max_m, s.wind_700hpa_max_kmh,
                       s.is_uk_school_holiday, s.holiday_name,
                       -- lowest reading of the day across capture slots, so a
                       -- morning wind hold that cleared by noon is not lost
                       MIN(s.pct_lifts_open) OVER (
                           PARTITION BY s.resort_id, s.snapshot_date) AS pct_min_day,
                       (s.scrape_error IS NOT NULL) AS error,
                       r.country AS _c, r.area AS _a, r.name AS _n
                FROM snapshots s
                JOIN resorts r ON r.id = s.resort_id
                ORDER BY s.resort_id, s.snapshot_date,
                         (s.slot <> 'midday'), s.snapshot_time DESC
            ) d ORDER BY snapshot_date, _c, _a, _n
        """)
        rows = cur.fetchall()
    # "_"-prefixed columns exist only to drive the outer sort
    fields = [k for k in (rows[0].keys() if rows else []) if not k.startswith("_")]
    payload = {
        "fields": fields,
        "rows": [[_plain(row[k]) for k in fields] for row in rows],
    }
    resp = jsonify(payload)
    resp.headers["Cache-Control"] = "public, max-age=1800"
    return resp


@app.route("/api/holidays.json")
def api_holidays():
    """School holiday periods (UK, FR zones, DE Länder, NL regions)."""
    with cursor() as cur:
        cur.execute("""
            SELECT country, region, name, start_date, end_date
            FROM holiday_periods
            ORDER BY country, region, start_date
        """)
        rows = cur.fetchall()
    resp = jsonify([{k: _plain(v) for k, v in row.items()} for row in rows])
    resp.headers["Cache-Control"] = "public, max-age=1800"
    return resp


@app.route("/map")
def map_view():
    with cursor() as cur:
        cur.execute("""
            SELECT r.id, r.name, COUNT(g.osm_id) AS ways
            FROM resorts r JOIN lift_geometry g ON g.resort_id = r.id
            GROUP BY r.id, r.name ORDER BY r.name
        """)
        resorts = cur.fetchall()
    return render_template("map.html", resorts=resorts,
                           mapbox_token=os.environ.get("MAPBOX_TOKEN", ""))


@app.route("/api/geometry/<resort_id>.json")
def api_geometry(resort_id: str):
    """Lift geometry as GeoJSON, carrying each lift's observed reliability."""
    with cursor() as cur:
        cur.execute("""
            SELECT g.osm_id, g.name, g.aerialway, g.bearing_deg, g.length_m,
                   g.geometry, g.elevations, g.lift_id, c.name AS lift_name, c.is_link,
                   COUNT(lr.id) FILTER (WHERE lr.status IN ('open','closed','hold'))
                       AS days_operational,
                   COUNT(lr.id) FILTER (WHERE lr.status = 'open') AS days_open
            FROM lift_geometry g
            LEFT JOIN lifts c ON c.id = g.lift_id
            LEFT JOIN lifts l2 ON COALESCE(l2.alias_of, l2.id) = c.id
            LEFT JOIN lift_readings lr ON lr.lift_id = l2.id
            WHERE g.resort_id = %s
            GROUP BY g.osm_id, g.resort_id, g.name, g.aerialway, g.bearing_deg,
                     g.length_m, g.geometry, g.elevations, g.lift_id, c.name, c.is_link
        """, (resort_id,))
        rows = cur.fetchall()

    features = []
    for r in rows:
        coords = r["geometry"] if isinstance(r["geometry"], list) else json.loads(r["geometry"] or "[]")
        if len(coords) < 2:
            continue
        operational = r["days_operational"] or 0
        pct = round(100 * r["days_open"] / operational, 1) if operational else None
        features.append({
            "type": "Feature",
            "geometry": {"type": "LineString", "coordinates": coords},
            "properties": {
                "osm_id": r["osm_id"],
                "name": r["name"] or "(unnamed lift)",
                "aerialway": r["aerialway"],
                "bearing": r["bearing_deg"],
                "length_m": r["length_m"],
                # Ground height per vertex, sampled from a local DEM at ingest.
                # The browser cannot supply this — queryTerrainElevation returns
                # null — so it has to travel with the geometry.
                "elevations": (r["elevations"] if isinstance(r["elevations"], list)
                               else json.loads(r["elevations"] or "null")),
                "lift_name": r["lift_name"],
                "is_link": bool(r["is_link"]),
                # None where the lift was never matched to an observed one,
                # which the map shows as "no data" rather than as zero.
                "pct_open": pct,
                "days_operational": operational,
            },
        })
    resp = jsonify({"type": "FeatureCollection", "features": features})
    resp.headers["Cache-Control"] = "public, max-age=1800"
    return resp


def _lift_metadata() -> dict:
    """(resort, lift name) -> type, link flag and OSM bearing.

    Bearing is what turns the closure ladder into an argument rather than a
    list: if the lifts at the top of the order share an orientation, the
    ordering is really a crosswind effect wearing a disguise.
    """
    with cursor() as cur:
        cur.execute("""
            SELECT c.resort_id, c.name, c.is_link, c.lift_type,
                   AVG(g.bearing_deg) AS bearing, AVG(g.length_m) AS length_m
            FROM lifts c
            LEFT JOIN lift_geometry g ON g.lift_id = c.id
            WHERE c.alias_of IS NULL
            GROUP BY c.resort_id, c.name, c.is_link, c.lift_type
        """)
        return {(r["resort_id"], r["name"]): r for r in cur.fetchall()}


@app.route("/api/model.json")
def api_model():
    """Everything the model knows, so the explorer can show its working.

    Three things per resort: the dose-response bins that turn wind into an
    expected share open, the closure ladder that turns that share into named
    lifts, and the raw scalogram both rest on. Errors are deliberately *not*
    pre-computed — the client re-derives them from each row's open count, so
    the page performs the prediction rather than being told the answer.
    """
    from scraper import model as m
    from scraper import nested as n
    from scraper import crosswind as x

    with cursor() as cur:
        cur.execute("SELECT id, name, area, country FROM resorts")
        names = {r["id"]: r for r in cur.fetchall()}
    meta = _lift_metadata()
    pooled = m.pooled_response()
    out = {}

    for rid, a in n.analyse_all().items():
        fit, hold = a["fit"], a["holdout"]
        ladder = []
        for name in a["order"]:
            opens, seen = a["rates"][name]
            md = meta.get((rid, name)) or {}
            ladder.append({
                "name": name, "open": round(100 * opens / seen, 1), "seen": seen,
                "errs": fit["per_lift"].get(name, 0),
                "link": bool(md.get("is_link")), "type": md.get("lift_type"),
                "bearing": _plain(md.get("bearing")),
                "length_m": _plain(md.get("length_m")),
            })
        # Rows ordered by how much of the mountain was running: a nested resort
        # then draws a staircase, and that is the whole claim in one picture.
        rows = []
        for sid, row in a["matrix"].items():
            cells = "".join(
                "-" if name not in row else ("o" if row[name] else "x")
                for name in a["order"])
            rows.append({"date": str(a["dates"][sid]),
                         "open": sum(1 for v in row.values() if v),
                         "seen": len(row), "cells": cells})
        rows.sort(key=lambda r: (-r["open"] / r["seen"], r["date"]))

        resp = m.shrunk_response(rid, pooled)
        own = m.observed_response(rid)
        out[rid] = {
            "name": names.get(rid, {}).get("name", rid),
            "area": names.get(rid, {}).get("area"),
            "country": names.get(rid, {}).get("country"),
            "fit": {k: _plain(v) for k, v in fit.items() if k != "per_lift"},
            "holdout": {"cr": hold["cr"], "cs": hold["cs"], "cells": hold["cells"]}
                       if hold else None,
            "verdict": n._verdict(fit),
            "ladder": ladder,
            "scalogram": rows,
            "response": {b: {"n": own.get(b, {}).get("n", 0),
                             "pct_open": v["pct_open"], "p_lost": v["p_lost"],
                             "shrunk": bool(v.get("shrunk"))}
                         for b, v in resp.items()},
        }

    # Crosswind: measured, null, and shown anyway. A hypothesis the project
    # leaned on for months earns its disproof being visible.
    cells = x._cells()
    cw_lifts = x.per_lift(cells)
    by_res = {r["resort_id"]: r for r in x.by_resort(cw_lifts)}
    roses = defaultdict(list)
    for c in cells:
        roses[c["resort_id"]].append(c)
    for rid, block in out.items():
        summary = by_res.get(rid)
        block["crosswind"] = {
            "summary": {k: _plain(v) for k, v in summary.items()} if summary else None,
            "lifts": [{k: _plain(v) for k, v in l.items()}
                      for l in cw_lifts if l["resort_id"] == rid],
            "rose": x.rose(roses[rid]) if roses.get(rid) else [],
        }
    tested = [l for l in cw_lifts if l["auc_frac_windy"] is not None]
    windy_pairs = sum(l["windy_pairs"] for l in tested)

    payload = {
        "core_season": list(n.CORE_SEASON),
        "bins": [{"upper": u, "name": b} for u, b in m.BINS],
        "crosswind": {
            "cells": len(cells),
            "lifts_tested": len(tested),
            "lifts_above_half": sum(1 for l in tested if l["auc_frac_windy"] > 0.5),
            "windy_pairs": windy_pairs,
            "angle_auc": (sum(l["auc_frac_windy"] * l["windy_pairs"] for l in tested)
                          / windy_pairs) if windy_pairs else None,
            "windy_quantile": x.WINDY_QUANTILE,
            "sector_deg": x.SECTOR_DEG,
        },
        "thresholds": {"cr": n.CR_GOOD, "cs": n.CS_GOOD,
                       "mixed": n.MIN_MIXED_SNAPSHOTS},
        "pooled": {b: {"n": v["n"], "pct_open": v["pct_open"],
                       "p_lost": v["p_lost"]} for b, v in pooled.items()},
        "resorts": out,
    }
    resp = jsonify(payload)
    resp.headers["Cache-Control"] = "public, max-age=1800"
    return resp


# Environmental lapse rate. ERA5 was fetched with elevation set to each
# resort's summit, so the temperature has a known reference height and the
# 0 °C line can be placed relative to it. This is an estimate from temperature,
# not an observed snow line, and the page says so.
LAPSE_C_PER_KM = 6.5


def _resort_centres():
    """Resort coordinates from the roster config, memoised."""
    global _CENTRES
    if _CENTRES is None:
        from scraper.collect import load_resorts
        _CENTRES = [r for r in load_resorts() if r.get("latitude")]
    return _CENTRES


_CENTRES = None

# ERA5's modelled snow depth is credible up to a few metres at a summit but
# runs away over permanently glaciated cells, pinning at 33.33 m. Anything
# above this is the glacier, not the winter's snowpack.
GLACIER_CEILING_M = 10.0


@app.route("/api/climate.json")
def api_climate():
    """One calendar window, every resort, every season since 1991.

    Aggregated in the database rather than shipped raw: the table is 468,900
    rows and the question only ever concerns a week or two of them.
    """
    def _md(v, fallback):
        v = (v or "").strip()
        return v if re.fullmatch(r"\d{2}-\d{2}", v) else fallback

    start = _md(request.args.get("from"), "02-14")
    end = _md(request.args.get("to"), "02-22")
    # A window may wrap the new year (Christmas into January), so the two cases
    # differ; seasons are labelled by the year the season started either way.
    window = ("TO_CHAR(date, 'MM-DD') BETWEEN %(a)s AND %(b)s" if start <= end
              else "(TO_CHAR(date, 'MM-DD') >= %(a)s OR TO_CHAR(date, 'MM-DD') <= %(b)s)")
    with cursor() as cur:
        cur.execute(f"""
            SELECT c.resort_id,
                   (EXTRACT(YEAR FROM c.date)
                    - CASE WHEN EXTRACT(MONTH FROM c.date) >= 8 THEN 0 ELSE 1 END)::int
                       AS season,
                   COUNT(*) AS n,
                   AVG(c.temp_mean_c) AS temp_mean,
                   AVG(c.temp_max_c)  AS temp_max,
                   AVG(c.temp_min_c)  AS temp_min,
                   SUM(c.snowfall_cm) AS snowfall,
                   AVG(c.snow_depth_model_m) AS snow_depth,
                   -- ERA5's modelled depth is sound for 35 of 36 resorts
                   -- (0.2-4.5 m at the summit) but saturates at 33.33 m over
                   -- permanently glaciated cells, which is why Cervinia averages
                   -- 26 m. The maximum is carried so a season sitting on the
                   -- ceiling can be dropped rather than plotted as a snowpack.
                   MAX(c.snow_depth_model_m) AS snow_depth_max,
                   SUM(c.precipitation_mm)   AS precip,
                   AVG(c.sunshine_hours)     AS sunshine,
                   AVG(c.wind_gust_max_kmh)  AS gust,
                   AVG(c.cloud_cover_pct)    AS cloud
            FROM climate_daily c
            WHERE {window}
            GROUP BY c.resort_id, season
            ORDER BY c.resort_id, season
        """, {"a": start, "b": end})
        rows = cur.fetchall()

    with cursor() as cur:
        cur.execute("SELECT id, name, country, area, top_altitude_m FROM resorts")
        meta = {r["id"]: r for r in cur.fetchall()}

    out = {}
    for r in rows:
        m = meta.get(r["resort_id"])
        if not m:
            continue
        block = out.setdefault(r["resort_id"], {
            "name": m["name"], "country": m["country"], "area": m["area"],
            "top_m": m["top_altitude_m"], "years": [],
        })
        t = float(r["temp_mean"])
        top = m["top_altitude_m"]
        block["years"].append({
            "season": r["season"], "n": r["n"],
            "temp_mean": round(t, 2),
            "temp_max": round(float(r["temp_max"]), 2),
            "temp_min": round(float(r["temp_min"]), 2),
            "snowfall": round(float(r["snowfall"]), 1),
            # Null rather than a number where the cell is glaciated: a wrong
            # value plotted confidently is worse than a gap in the line.
            "snow_depth": (None if float(r["snow_depth_max"]) >= GLACIER_CEILING_M
                           else round(float(r["snow_depth"]) * 100, 1)),
            "precip": round(float(r["precip"]), 1),
            "sunshine": round(float(r["sunshine"]), 2),
            "gust": round(float(r["gust"]), 1),
            "cloud": round(float(r["cloud"]), 1),
            # Height of the 0 °C line, from the summit temperature and a
            # standard lapse rate. Above the summit means it rained on top.
            "freezing_m": round(top + t / LAPSE_C_PER_KM * 1000) if top else None,
        })

    _attach_stations(out, start, end)

    resp = jsonify({
        "window": {"from": start, "to": end},
        "lapse_c_per_km": LAPSE_C_PER_KM,
        "resorts": out,
    })
    resp.headers["Cache-Control"] = "public, max-age=3600"
    return resp


def _months_spanned(start: str, end: str) -> list[int]:
    """Months a MM-DD window touches, wrapping the new year if it must."""
    a, b = int(start[:2]), int(end[:2])
    if a <= b:
        return list(range(a, b + 1))
    return list(range(a, 13)) + list(range(1, b + 1))


def _attach_stations(out: dict, start: str, end: str) -> None:
    """Add measured station series beside the modelled ones.

    Daily stations are windowed on the exact calendar dates. Monthly stations
    cannot be: their rows sit on the first of the month, so a 14-22 February
    window would match nothing at all. They are therefore aggregated over the
    whole months the window touches, which is a coarser question — the payload
    says which each series is so the page can label it rather than quietly
    mixing the two.
    """
    daily_window = ("TO_CHAR(ss.date, 'MM-DD') BETWEEN %(a)s AND %(b)s"
                    if start <= end
                    else "(TO_CHAR(ss.date, 'MM-DD') >= %(a)s"
                         " OR TO_CHAR(ss.date, 'MM-DD') <= %(b)s)")
    params = {"a": start, "b": end, "months": tuple(_months_spanned(start, end))}

    with cursor() as cur:
        cur.execute(f"""
            SELECT rs.resort_id, rs.station_id, rs.rank, rs.chosen,
                   rs.distance_km, rs.elev_diff_m,
                   s.elevation_m, s.cadence, s.provider,
                   (EXTRACT(YEAR FROM ss.date)
                    - CASE WHEN EXTRACT(MONTH FROM ss.date) >= 8 THEN 0 ELSE 1 END)::int
                       AS season,
                   COUNT(*) AS n,
                   AVG(ss.hs_cm) AS hs,
                   SUM(ss.hn_cm) AS hn
            FROM resort_stations rs
            JOIN snow_stations s ON s.id = rs.station_id
            JOIN station_snow ss ON ss.station_id = rs.station_id
            WHERE (s.cadence = 'daily'  AND {daily_window})
               OR (s.cadence = 'monthly' AND EXTRACT(MONTH FROM ss.date) IN %(months)s)
            GROUP BY rs.resort_id, rs.station_id, rs.rank, rs.chosen,
                     rs.distance_km, rs.elev_diff_m, s.elevation_m, s.cadence,
                     s.provider, season
            ORDER BY rs.resort_id, rs.rank, season
        """, params)
        rows = cur.fetchall()

    for r in rows:
        block = out.get(r["resort_id"])
        if not block:
            continue
        stations = block.setdefault("stations", {})
        st = stations.setdefault(r["station_id"], {
            "id": r["station_id"], "rank": r["rank"], "primary": r["chosen"],
            "elevation_m": r["elevation_m"], "cadence": r["cadence"],
            "provider": r["provider"], "distance_km": _plain(r["distance_km"]),
            "elev_diff_m": r["elev_diff_m"], "years": [],
        })
        st["years"].append({
            "season": r["season"], "n": r["n"],
            "hs_cm": round(float(r["hs"]), 1) if r["hs"] is not None else None,
            "hn_cm": round(float(r["hn"]), 1) if r["hn"] is not None else None,
        })
    for block in out.values():
        block["stations"] = sorted(block.get("stations", {}).values(),
                                   key=lambda s: s["rank"])


@app.route("/api/pistes/<resort_id>.json")
def api_pistes(resort_id: str):
    """Downhill runs as GeoJSON, served apart from the lifts.

    A big resort carries several hundred runs against a few dozen lifts, so
    folding these into /api/geometry would triple the payload every visitor
    pays for whether or not they ever switch the layer on.
    """
    with cursor() as cur:
        cur.execute("""
            SELECT osm_id, name, difficulty, length_m, geometry
            FROM piste_geometry WHERE resort_id = %s
            ORDER BY length_m DESC
        """, (resort_id,))
        rows = cur.fetchall()

    features = []
    for r in rows:
        coords = r["geometry"] if isinstance(r["geometry"], list) else json.loads(r["geometry"] or "[]")
        if len(coords) < 2:
            continue
        features.append({
            "type": "Feature",
            "geometry": {"type": "LineString", "coordinates": coords},
            "properties": {
                "osm_id": r["osm_id"],
                "name": r["name"] or "",
                # Empty string rather than null: a Mapbox `match` expression
                # needs a value it can fall through on.
                "difficulty": r["difficulty"] or "",
                "length_m": r["length_m"],
            },
        })
    resp = jsonify({"type": "FeatureCollection", "features": features})
    resp.headers["Cache-Control"] = "public, max-age=86400"
    return resp


@app.route("/api/sun/<resort_id>.json")
def api_sun(resort_id: str):
    """When each lift station gains and loses the sun on a given date.

    The horizon profiles are precomputed; everything here is arithmetic, so a
    different date costs nothing but the walk through the day.
    """
    from scraper import sun as sunmod

    raw = (request.args.get("date") or "").strip()
    try:
        day = date.fromisoformat(raw) if raw else date(date.today().year, 2, 14)
    except ValueError:
        day = date(date.today().year, 2, 14)

    with cursor() as cur:
        cur.execute("""
            SELECT name, kind, latitude, longitude, elevation_m, horizon,
                   azimuth_step
            FROM sun_points WHERE resort_id = %s
            ORDER BY elevation_m DESC NULLS LAST
        """, (resort_id,))
        rows = cur.fetchall()

    points = []
    for r in rows:
        prof = r["horizon"] if isinstance(r["horizon"], list) else json.loads(r["horizon"])
        lat, lon = float(r["latitude"]), float(r["longitude"])
        w = sunmod.sun_windows(lat, lon, prof, day)
        points.append({
            "name": r["name"] or "—", "kind": r["kind"],
            "lat": lat, "lon": lon, "elevation_m": r["elevation_m"],
            "azimuth_step": r["azimuth_step"], "horizon": prof,
            "centre": False, **w,
        })

    # Mark the station nearest the resort's own coordinates. "Lowest station"
    # is not the village: the OSM bounding box reaches down the valley, so
    # Alpe d'Huez's lowest is Bourg-d'Oisans at 725 m and Val Thorens' is
    # Moutiers at 882 m. Those really are dark in February, but they are not
    # the resort anyone means.
    centre = next((r for r in _resort_centres() if r["id"] == resort_id), None)
    if centre and points:
        def d2(p):
            return ((p["lat"] - centre["latitude"]) ** 2
                    + ((p["lon"] - centre["longitude"])
                       * math.cos(math.radians(centre["latitude"]))) ** 2)
        min(points, key=d2)["centre"] = True

    # The sun's own track for the day, so the page can draw it against a
    # horizon: elevation and azimuth every ten minutes, at the resort centre.
    track = []
    if points:
        lat0 = sum(p["lat"] for p in points) / len(points)
        lon0 = sum(p["lon"] for p in points) / len(points)
        t = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
        stop = t + timedelta(days=1)
        while t < stop:
            az, elev = sunmod.solar_position(lat0, lon0, t)
            if elev > -2:
                local = t + timedelta(hours=sunmod.cet_offset(t))
                track.append({"t": local.strftime("%H:%M"),
                              "az": round(az, 2), "el": round(elev, 2)})
            t += timedelta(minutes=10)

    resp = jsonify({"resort_id": resort_id, "date": day.isoformat(),
                    "points": points, "track": track})
    resp.headers["Cache-Control"] = "public, max-age=3600"
    return resp


@app.route("/api/outlook.json")
def api_outlook():
    """Expected conditions for a calendar window, per resort.

    Combines each resort's observed response to wind with 35 years of ERA5
    frequency for that window.
    """
    from scraper.model import all_window_stats, forecast_from, pooled_response

    start_md = request.args.get("from", "02-14")
    end_md = request.args.get("to", "02-22")
    trip_days = request.args.get("days", 7, type=int)
    if not (re.fullmatch(r"\d{2}-\d{2}", start_md) and re.fullmatch(r"\d{2}-\d{2}", end_md)):
        return jsonify({"error": "from/to must look like MM-DD"}), 400
    trip_days = max(1, min(trip_days, 21))

    with cursor() as cur:
        cur.execute("SELECT id, name, country, area, top_altitude_m FROM resorts ORDER BY name")
        resorts = cur.fetchall()

    pooled = pooled_response()
    stats = all_window_stats(start_md, end_md)
    out = []
    for r in resorts:
        s = stats.get(r["id"])
        if not s:
            continue
        f = forecast_from(r["id"], s["freq"], pooled, trip_days=trip_days)
        if not f:
            continue
        runs = s["runs"]
        out.append({
            "resort_id": r["id"], "name": r["name"], "country": r["country"],
            "area": r["area"], "top_altitude_m": r["top_altitude_m"],
            "expected_pct_open": round(f["expected_pct_open"], 1),
            "p_lost_day": round(f["p_lost_day"], 4),
            "p_any_lost": round(f["p_any_lost"], 4),
            "p_storm_run": round(runs.get("p_run", 0), 4),
            "worst_run": runs.get("worst_run"),
            "climate_years": runs.get("years"),
            "obs_days": f["obs_days"],
            "thin": f["thin"],
        })
    out.sort(key=lambda x: -x["expected_pct_open"])
    resp = jsonify({"from": start_md, "to": end_md, "trip_days": trip_days,
                    "resorts": out})
    resp.headers["Cache-Control"] = "public, max-age=1800"
    return resp


@app.route("/api/lift-stats.json")
def api_lift_stats():
    """Per-lift open-day counts (whole history, primary-scraped resorts only)."""
    with cursor() as cur:
        cur.execute("""
            SELECT c.resort_id, r.name AS resort_name, c.name, c.is_link,
                   COUNT(*) FILTER (WHERE lr.status = 'open')     AS days_open,
                   COUNT(*) FILTER (WHERE lr.status = 'closed')   AS days_closed,
                   COUNT(*) FILTER (WHERE lr.status = 'hold')     AS days_hold,
                   COUNT(*) FILTER (WHERE lr.status = 'seasonal') AS days_seasonal,
                   -- days the lift was meant to be running: seasonal closures
                   -- are excluded so they don't dilute a lift's reliability
                   COUNT(*) FILTER (WHERE lr.status IN ('open', 'closed', 'hold'))
                       AS days_operational,
                   COUNT(*) AS days_total,
                   ARRAY_REMOVE(ARRAY_AGG(DISTINCT lr.raw_status)
                       FILTER (WHERE lr.status <> 'open'), NULL) AS closed_reasons
            FROM lift_readings lr
            JOIN lifts l ON l.id = lr.lift_id
            -- roll a renamed lift's readings up into its canonical lift
            JOIN lifts c ON c.id = COALESCE(l.alias_of, l.id)
            JOIN snapshots s ON s.id = lr.snapshot_id
            JOIN resorts r ON r.id = c.resort_id
            WHERE NOT c.name LIKE 'lift\\_%%'
            GROUP BY c.resort_id, r.name, c.name, c.is_link
            ORDER BY c.resort_id, c.name
        """)
        rows = cur.fetchall()
    resp = jsonify([{k: _plain(v) for k, v in row.items()} for row in rows])
    resp.headers["Cache-Control"] = "public, max-age=1800"
    return resp


@app.route("/api/snapshots.json")
def api_json():
    rows = get_latest_snapshots()
    return jsonify([dict(r) for r in rows])


@app.route("/api/history/<resort_id>.json")
def api_history_json(resort_id: str):
    days = 90
    rows = get_history(resort_id, days)
    return jsonify([dict(r) for r in rows])


@app.route("/api/snapshots.csv")
def api_csv():
    rows = get_latest_snapshots()
    output = io.StringIO()
    if rows:
        writer = csv.DictWriter(output, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)
    return Response(output.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=ski-lifts.csv"})


@app.route("/api/full-history.csv")
def api_full_csv():
    with cursor() as cur:
        cur.execute("""
            SELECT r.name, r.country, r.area, r.top_altitude_m,
                   s.snapshot_date, s.lifts_open, s.lifts_total,
                   s.pct_lifts_open, s.pistes_open_km, s.pistes_total_km,
                   s.is_uk_school_holiday, s.holiday_name, s.source
            FROM snapshots s
            JOIN resorts r ON r.id = s.resort_id
            ORDER BY s.snapshot_date DESC, r.name
        """)
        rows = cur.fetchall()
    output = io.StringIO()
    if rows:
        writer = csv.DictWriter(output, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)
    return Response(output.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=ski-lift-history.csv"})


# ---------------------------------------------------------------------------
# Admin control panel
# ---------------------------------------------------------------------------

def is_admin() -> bool:
    return bool(ADMIN_TOKEN) and session.get("admin") is True


def get_resort_health():
    """Per-resort status for the admin health board."""
    with cursor() as cur:
        cur.execute("""
            SELECT r.id, r.name, r.enabled, r.scraper_type,
                   ls.id AS snap_id, ls.snapshot_date AS last_date,
                   ls.lifts_open, ls.lifts_total, ls.source, ls.scrape_error,
                   err.err_days
            FROM resorts r
            LEFT JOIN LATERAL (
                SELECT * FROM snapshots s
                WHERE s.resort_id = r.id
                ORDER BY s.snapshot_date DESC LIMIT 1
            ) ls ON TRUE
            LEFT JOIN LATERAL (
                SELECT COUNT(*) AS err_days FROM (
                    SELECT s2.scrape_error FROM snapshots s2
                    WHERE s2.resort_id = r.id
                    ORDER BY s2.snapshot_date DESC LIMIT 7
                ) t WHERE t.scrape_error IS NOT NULL
            ) err ON TRUE
            ORDER BY r.name
        """)
        rows = cur.fetchall()

        # Per-source readings for each resort's latest snapshot, plus a
        # divergence flag when two sources disagree by >20 pct-points.
        snap_ids = [r["snap_id"] for r in rows if r["snap_id"] is not None]
        readings = {}
        if snap_ids:
            cur.execute("""
                SELECT snapshot_id, source, lifts_open, lifts_total, error
                FROM source_readings WHERE snapshot_id = ANY(%s)
                ORDER BY source
            """, (snap_ids,))
            for rd in cur.fetchall():
                readings.setdefault(rd["snapshot_id"], []).append(rd)

    result = []
    for r in rows:
        r = dict(r)
        rds = readings.get(r["snap_id"], [])
        pcts = [100 * rd["lifts_open"] / rd["lifts_total"]
                for rd in rds if rd["lifts_total"]]
        r["readings"] = rds
        r["diverged"] = len(pcts) >= 2 and (max(pcts) - min(pcts)) > 20
        result.append(r)
    return result


@app.route("/admin")
def admin():
    if not ADMIN_TOKEN:
        return render_template("admin.html", setup_needed=True), 503
    if not is_admin():
        return render_template("admin.html", login_needed=True)

    paused = get_setting("collection_paused", "false") == "true"
    return render_template(
        "admin.html",
        paused=paused,
        auto_resume_date=get_setting("auto_resume_date", "") or "",
        auto_pause_date=get_setting("auto_pause_date", "") or "",
        health=get_resort_health(),
        today=datetime.now(timezone.utc).date(),
    )


@app.route("/admin/roster")
def admin_roster():
    if not is_admin():
        return redirect(url_for("admin"))
    from scraper.roster import all_changes
    gap = request.args.get("gap", 30, type=int)
    return render_template("roster.html", changes=all_changes(gap), gap=gap)


@app.route("/admin/merge-lift", methods=["POST"])
def admin_merge_lift():
    if not is_admin():
        return redirect(url_for("admin"))
    from scraper.roster import merge_lift
    try:
        old_name, new_name = merge_lift(int(request.form["old_id"]),
                                        int(request.form["new_id"]))
        flash(f"Merged: {old_name!r} now rolls up into {new_name!r}.")
    except (ValueError, KeyError) as e:
        flash(f"Could not merge: {e}")
    return redirect(url_for("admin_roster", gap=request.form.get("gap", 30)))


@app.route("/admin/login", methods=["POST"])
def admin_login():
    token = request.form.get("token", "")
    if ADMIN_TOKEN and hmac.compare_digest(token, ADMIN_TOKEN):
        session["admin"] = True
        session.permanent = True
    else:
        flash("Wrong token.")
    return redirect(url_for("admin"))


@app.route("/admin/logout", methods=["POST"])
def admin_logout():
    session.pop("admin", None)
    return redirect(url_for("admin"))


@app.route("/admin/pause", methods=["POST"])
def admin_pause():
    if not is_admin():
        return redirect(url_for("admin"))
    action = request.form.get("action")
    if action in ("pause", "resume"):
        set_setting("collection_paused", "true" if action == "pause" else "false")
        flash(f"Collection {'paused' if action == 'pause' else 'resumed'}.")
    return redirect(url_for("admin"))


@app.route("/admin/schedule", methods=["POST"])
def admin_schedule():
    if not is_admin():
        return redirect(url_for("admin"))
    for field in ("auto_resume_date", "auto_pause_date"):
        set_setting(field, request.form.get(field, "").strip())
    flash("Season schedule saved.")
    return redirect(url_for("admin"))


@app.route("/admin/resort/<resort_id>/toggle", methods=["POST"])
def admin_toggle_resort(resort_id: str):
    if not is_admin():
        return redirect(url_for("admin"))
    enable = request.form.get("enable") == "1"
    set_resort_enabled(resort_id, enable)
    flash(f"{resort_id} {'enabled' if enable else 'disabled'}.")
    return redirect(url_for("admin"))


@app.route("/admin/run/<resort_id>", methods=["POST"])
def admin_run_resort(resort_id: str):
    if not is_admin():
        return redirect(url_for("admin"))

    from scraper.collect import load_resorts
    from scraper.scrapers import run_scraper
    from scraper.store import save_snapshot
    from scraper.weather import fetch_weather

    resort = next((r for r in load_resorts() if r["id"] == resort_id), None)
    if resort is None:
        flash(f"Unknown resort '{resort_id}'.")
        return redirect(url_for("admin"))

    try:
        snap = run_scraper(resort)
        if resort.get("latitude") and resort.get("longitude"):
            weather = fetch_weather(resort["latitude"], resort["longitude"],
                                    resort.get("top_altitude_m"))
            snap.wind_gust_max_kmh  = weather.get("wind_gust_max_kmh")
            snap.wind_speed_max_kmh = weather.get("wind_speed_max_kmh")
            snap.temp_min_c         = weather.get("temp_min_c")
            snap.temp_max_c         = weather.get("temp_max_c")
            snap.fresh_snow_cm      = weather.get("fresh_snow_cm")
            snap.precipitation_mm   = weather.get("precipitation_mm")
            snap.weather_code       = weather.get("weather_code")
        save_snapshot(snap)
        if snap.error:
            flash(f"{resort['name']}: scrape error — {snap.error}")
        else:
            flash(f"{resort['name']}: {snap.lifts_open}/{snap.lifts_total} lifts open "
                  f"({snap.source}). Snapshot saved.")
    except Exception as e:
        flash(f"{resort['name']}: exception — {e}")
    return redirect(url_for("admin"))


if __name__ == "__main__":
    # Only reached when run directly, i.e. locally — production is gunicorn.
    # Jinja caches templates unless debug is on, which meant every edit to
    # explore.html looked like it had done nothing until the server restarted.
    app.config["TEMPLATES_AUTO_RELOAD"] = True
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
