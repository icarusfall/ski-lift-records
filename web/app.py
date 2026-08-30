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
from datetime import datetime, timezone

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
            WHERE s.snapshot_date = (
                SELECT MAX(s2.snapshot_date)
                FROM snapshots s2
                WHERE s2.resort_id = s.resort_id
            )
            ORDER BY r.country, r.area, r.name
        """)
        return cur.fetchall()


def get_history(resort_id: str, days: int = 60):
    with cursor() as cur:
        cur.execute("""
            SELECT snapshot_date, lifts_open, lifts_total, pct_lifts_open,
                   pistes_open_km, pistes_total_km,
                   snow_depth_mountain_cm, snow_depth_valley_cm,
                   snow_condition, last_snowfall_date, piste_conditions, avalanche_danger,
                   wind_gust_max_kmh, wind_speed_max_kmh,
                   temp_min_c, temp_max_c,
                   fresh_snow_cm, precipitation_mm, weather_code,
                   is_uk_school_holiday, holiday_name
            FROM snapshots
            WHERE resort_id = %s
              AND snapshot_date >= CURRENT_DATE - %s
            ORDER BY snapshot_date
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


@app.route("/")
def index():
    rows = get_latest_snapshots()
    return render_template("index.html", rows=rows)


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
                   ls.snapshot_date AS last_date,
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
        return cur.fetchall()


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
