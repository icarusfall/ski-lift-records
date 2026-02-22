"""Persist a ResortSnapshot to the database."""
from datetime import date, datetime, timezone
from .db import cursor
from .scrapers.base import ResortSnapshot
from .holidays import is_uk_school_holiday


def save_snapshot(snap: ResortSnapshot, snapshot_date: date | None = None) -> int | None:
    if snapshot_date is None:
        snapshot_date = datetime.now(timezone.utc).date()

    is_hol, hol_name = is_uk_school_holiday(snapshot_date)

    with cursor() as cur:
        # Upsert snapshot (one per resort per day)
        cur.execute("""
            INSERT INTO snapshots
                (resort_id, snapshot_time, snapshot_date,
                 lifts_open, lifts_total, pct_lifts_open,
                 pistes_open_km, pistes_total_km,
                 source, is_uk_school_holiday, holiday_name, scrape_error)
            VALUES
                (%(resort_id)s, %(now)s, %(date)s,
                 %(lifts_open)s, %(lifts_total)s, %(pct_open)s,
                 %(pistes_open_km)s, %(pistes_total_km)s,
                 %(source)s, %(is_hol)s, %(hol_name)s, %(error)s)
            ON CONFLICT (resort_id, snapshot_date) DO UPDATE SET
                snapshot_time   = EXCLUDED.snapshot_time,
                lifts_open      = EXCLUDED.lifts_open,
                lifts_total     = EXCLUDED.lifts_total,
                pct_lifts_open  = EXCLUDED.pct_lifts_open,
                pistes_open_km  = EXCLUDED.pistes_open_km,
                pistes_total_km = EXCLUDED.pistes_total_km,
                source          = EXCLUDED.source,
                scrape_error    = EXCLUDED.scrape_error
            RETURNING id
        """, {
            "resort_id":     snap.resort_id,
            "now":           datetime.now(timezone.utc),
            "date":          snapshot_date,
            "lifts_open":    snap.lifts_open if not snap.error else None,
            "lifts_total":   snap.lifts_total if not snap.error else None,
            "pct_open":      snap.pct_open if not snap.error else None,
            "pistes_open_km":  snap.pistes_open_km,
            "pistes_total_km": snap.pistes_total_km,
            "source":        snap.source,
            "is_hol":        is_hol,
            "hol_name":      hol_name,
            "error":         snap.error,
        })
        row = cur.fetchone()
        if row is None:
            return None
        snapshot_id = row["id"]

        # Save individual lifts (only if we have named lifts, i.e. from primary scrapers)
        named_lifts = [l for l in snap.lifts if not l.name.startswith("lift_")]
        if named_lifts:
            for lift in named_lifts:
                # Upsert lift record
                cur.execute("""
                    INSERT INTO lifts (resort_id, name, is_link, first_seen, last_seen)
                    VALUES (%(resort_id)s, %(name)s, %(is_link)s, %(date)s, %(date)s)
                    ON CONFLICT (resort_id, name) DO UPDATE SET
                        last_seen = EXCLUDED.last_seen,
                        is_link   = EXCLUDED.is_link
                    RETURNING id
                """, {
                    "resort_id": snap.resort_id,
                    "name":      lift.name,
                    "is_link":   lift.is_link,
                    "date":      snapshot_date,
                })
                lift_row = cur.fetchone()
                if lift_row:
                    cur.execute("""
                        INSERT INTO lift_readings (snapshot_id, lift_id, status)
                        VALUES (%s, %s, %s)
                        ON CONFLICT DO NOTHING
                    """, (snapshot_id, lift_row["id"], lift.status))

        # Save individual pistes (only if we have named pistes)
        named_pistes = [p for p in snap.pistes if p.name]
        if named_pistes:
            for piste in named_pistes:
                cur.execute("""
                    INSERT INTO pistes (resort_id, name, colour, first_seen, last_seen)
                    VALUES (%(resort_id)s, %(name)s, %(colour)s, %(date)s, %(date)s)
                    ON CONFLICT (resort_id, name) DO UPDATE SET
                        last_seen = EXCLUDED.last_seen,
                        colour    = EXCLUDED.colour
                    RETURNING id
                """, {
                    "resort_id": snap.resort_id,
                    "name":      piste.name,
                    "colour":    piste.colour,
                    "date":      snapshot_date,
                })
                piste_row = cur.fetchone()
                if piste_row:
                    cur.execute("""
                        INSERT INTO piste_readings (snapshot_id, piste_id, status, colour)
                        VALUES (%s, %s, %s, %s)
                        ON CONFLICT DO NOTHING
                    """, (snapshot_id, piste_row["id"], piste.status, piste.colour))

    return snapshot_id
