"""
Simple Flask dashboard for the ski lift tracker.
Shows current status and historical trends per resort.
"""

import csv
import io
import json
import os
import sys

# Allow importing scraper package from parent directory
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from flask import Flask, jsonify, render_template, Response
from scraper.db import cursor

app = Flask(__name__)


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
    history = get_history(resort_id)
    lift_history = get_lift_history(resort_id)
    return render_template("resort.html",
                           resort_id=resort_id,
                           history=history,
                           lift_history=lift_history)


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


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
