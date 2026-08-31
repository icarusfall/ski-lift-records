"""
Simple Flask dashboard for the ski lift tracker.
Shows current status and historical trends per resort.
"""

import csv
import hashlib
import hmac
import io
import json
import os
import sys
from datetime import date, datetime, timezone
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
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
