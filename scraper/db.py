"""Database connection and schema management."""
import os
import psycopg2
import psycopg2.extras
from contextlib import contextmanager

DATABASE_URL = os.environ.get("DATABASE_URL", "")


def get_conn():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL environment variable not set")
    return psycopg2.connect(DATABASE_URL)


@contextmanager
def cursor():
    conn = get_conn()
    try:
        with conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                yield cur
    finally:
        conn.close()


SCHEMA = """
CREATE TABLE IF NOT EXISTS resorts (
    id          VARCHAR(50) PRIMARY KEY,
    name        VARCHAR(200) NOT NULL,
    country     CHAR(2),
    area        VARCHAR(100),
    top_altitude_m  INTEGER,
    bergfex_slug    VARCHAR(100),
    primary_url     TEXT,
    scraper_type    VARCHAR(50),
    notes           TEXT,
    created_at  TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS lifts (
    id          SERIAL PRIMARY KEY,
    resort_id   VARCHAR(50) REFERENCES resorts(id),
    name        VARCHAR(200) NOT NULL,
    lift_type   VARCHAR(50),
    is_link     BOOLEAN DEFAULT FALSE,
    first_seen  DATE,
    last_seen   DATE,
    UNIQUE (resort_id, name)
);

CREATE TABLE IF NOT EXISTS pistes (
    id          SERIAL PRIMARY KEY,
    resort_id   VARCHAR(50) REFERENCES resorts(id),
    name        VARCHAR(200) NOT NULL,
    colour      VARCHAR(20),
    first_seen  DATE,
    last_seen   DATE,
    UNIQUE (resort_id, name)
);

CREATE TABLE IF NOT EXISTS snapshots (
    id              SERIAL PRIMARY KEY,
    resort_id       VARCHAR(50) REFERENCES resorts(id),
    snapshot_time   TIMESTAMP NOT NULL,
    snapshot_date   DATE NOT NULL,
    lifts_open      INTEGER,
    lifts_total     INTEGER,
    pct_lifts_open  NUMERIC(5,1),
    pistes_open_km  NUMERIC(7,1),
    pistes_total_km NUMERIC(7,1),
    source          VARCHAR(50),
    is_uk_school_holiday    BOOLEAN DEFAULT FALSE,
    holiday_name    VARCHAR(100),
    scrape_error    TEXT,
    snow_depth_mountain_cm  INTEGER,
    snow_depth_valley_cm    INTEGER,
    created_at      TIMESTAMP DEFAULT NOW(),
    UNIQUE (resort_id, snapshot_date)
);

CREATE TABLE IF NOT EXISTS lift_readings (
    id          SERIAL PRIMARY KEY,
    snapshot_id INTEGER REFERENCES snapshots(id) ON DELETE CASCADE,
    lift_id     INTEGER REFERENCES lifts(id),
    status      VARCHAR(20) NOT NULL,
    created_at  TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS piste_readings (
    id          SERIAL PRIMARY KEY,
    snapshot_id INTEGER REFERENCES snapshots(id) ON DELETE CASCADE,
    piste_id    INTEGER REFERENCES pistes(id),
    status      VARCHAR(20) NOT NULL,
    colour      VARCHAR(20),
    created_at  TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_snapshots_resort_date ON snapshots (resort_id, snapshot_date DESC);
CREATE INDEX IF NOT EXISTS idx_lift_readings_snapshot ON lift_readings (snapshot_id);
CREATE INDEX IF NOT EXISTS idx_piste_readings_snapshot ON piste_readings (snapshot_id);

ALTER TABLE snapshots ADD COLUMN IF NOT EXISTS snow_depth_mountain_cm INTEGER;
ALTER TABLE snapshots ADD COLUMN IF NOT EXISTS snow_depth_valley_cm INTEGER;
"""


def init_db():
    with cursor() as cur:
        cur.execute(SCHEMA)
    print("Database schema initialised.")


def upsert_resort(resort: dict):
    with cursor() as cur:
        cur.execute("""
            INSERT INTO resorts (id, name, country, area, top_altitude_m,
                                 bergfex_slug, primary_url, scraper_type, notes)
            VALUES (%(id)s, %(name)s, %(country)s, %(area)s, %(top_altitude_m)s,
                    %(bergfex_slug)s, %(primary_url)s, %(scraper)s, %(notes)s)
            ON CONFLICT (id) DO UPDATE SET
                name = EXCLUDED.name,
                country = EXCLUDED.country,
                area = EXCLUDED.area,
                top_altitude_m = EXCLUDED.top_altitude_m,
                bergfex_slug = EXCLUDED.bergfex_slug,
                primary_url = EXCLUDED.primary_url,
                scraper_type = EXCLUDED.scraper_type,
                notes = EXCLUDED.notes
        """, {**resort, "notes": resort.get("notes"), "primary_url": resort.get("primary_url")})
