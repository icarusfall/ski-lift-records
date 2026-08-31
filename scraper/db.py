"""Database connection and schema management."""
import os
from pathlib import Path
import psycopg2
import psycopg2.extras
from contextlib import contextmanager

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except ImportError:
    pass

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
    snow_condition          VARCHAR(100),
    last_snowfall_date      DATE,
    piste_conditions        VARCHAR(50),
    avalanche_danger        SMALLINT,
    wind_gust_max_kmh      NUMERIC(5,1),
    wind_speed_max_kmh     NUMERIC(5,1),
    temp_min_c             NUMERIC(4,1),
    temp_max_c             NUMERIC(4,1),
    fresh_snow_cm          NUMERIC(5,1),
    precipitation_mm       NUMERIC(5,1),
    weather_code           SMALLINT,
    slot            VARCHAR(10) NOT NULL DEFAULT 'midday',
    freezing_level_max_m   INTEGER,
    freezing_level_min_m   INTEGER,
    wind_700hpa_max_kmh    NUMERIC(5,1),
    sunshine_hours         NUMERIC(4,1),
    created_at      TIMESTAMP DEFAULT NOW()
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
ALTER TABLE snapshots ADD COLUMN IF NOT EXISTS snow_condition VARCHAR(100);
ALTER TABLE snapshots ADD COLUMN IF NOT EXISTS last_snowfall_date DATE;
ALTER TABLE snapshots ADD COLUMN IF NOT EXISTS piste_conditions VARCHAR(50);
ALTER TABLE snapshots ADD COLUMN IF NOT EXISTS avalanche_danger SMALLINT;
ALTER TABLE snapshots ADD COLUMN IF NOT EXISTS wind_gust_max_kmh NUMERIC(5,1);
ALTER TABLE snapshots ADD COLUMN IF NOT EXISTS wind_speed_max_kmh NUMERIC(5,1);
ALTER TABLE snapshots ADD COLUMN IF NOT EXISTS temp_min_c NUMERIC(4,1);
ALTER TABLE snapshots ADD COLUMN IF NOT EXISTS temp_max_c NUMERIC(4,1);
ALTER TABLE snapshots ADD COLUMN IF NOT EXISTS fresh_snow_cm NUMERIC(5,1);
ALTER TABLE snapshots ADD COLUMN IF NOT EXISTS precipitation_mm NUMERIC(5,1);
ALTER TABLE snapshots ADD COLUMN IF NOT EXISTS weather_code SMALLINT;
ALTER TABLE snapshots ADD COLUMN IF NOT EXISTS slot VARCHAR(10) NOT NULL DEFAULT 'midday';
ALTER TABLE snapshots ADD COLUMN IF NOT EXISTS freezing_level_max_m INTEGER;
ALTER TABLE snapshots ADD COLUMN IF NOT EXISTS freezing_level_min_m INTEGER;
ALTER TABLE snapshots ADD COLUMN IF NOT EXISTS wind_700hpa_max_kmh NUMERIC(5,1);
ALTER TABLE snapshots ADD COLUMN IF NOT EXISTS sunshine_hours NUMERIC(4,1);

-- Multiple captures per day: the old one-row-per-resort-per-day constraint is
-- replaced by a unique index that includes the slot (morning / midday).
ALTER TABLE snapshots DROP CONSTRAINT IF EXISTS snapshots_resort_id_snapshot_date_key;
CREATE UNIQUE INDEX IF NOT EXISTS idx_snapshots_resort_date_slot
    ON snapshots (resort_id, snapshot_date, slot);

CREATE TABLE IF NOT EXISTS app_settings (
    key         VARCHAR(50) PRIMARY KEY,
    value       TEXT,
    updated_at  TIMESTAMP DEFAULT NOW()
);

ALTER TABLE resorts ADD COLUMN IF NOT EXISTS enabled BOOLEAN DEFAULT TRUE;

CREATE TABLE IF NOT EXISTS source_readings (
    id              SERIAL PRIMARY KEY,
    snapshot_id     INTEGER REFERENCES snapshots(id) ON DELETE CASCADE,
    source          VARCHAR(50) NOT NULL,
    lifts_open      INTEGER,
    lifts_total     INTEGER,
    pistes_open_km  NUMERIC(7,1),
    pistes_total_km NUMERIC(7,1),
    error           TEXT,
    created_at      TIMESTAMP DEFAULT NOW(),
    UNIQUE (snapshot_id, source)
);
CREATE INDEX IF NOT EXISTS idx_source_readings_snapshot ON source_readings (snapshot_id);

CREATE TABLE IF NOT EXISTS holiday_periods (
    id          SERIAL PRIMARY KEY,
    country     CHAR(2) NOT NULL,
    region      VARCHAR(50) NOT NULL DEFAULT '',
    name        VARCHAR(100) NOT NULL,
    start_date  DATE NOT NULL,
    end_date    DATE NOT NULL,
    created_at  TIMESTAMP DEFAULT NOW(),
    UNIQUE (country, region, name)
);
CREATE INDEX IF NOT EXISTS idx_holiday_periods_dates ON holiday_periods (start_date, end_date);

-- The site's own wording for a lift's state, kept verbatim alongside our
-- normalised status so "closed" can later be split into wind hold, seasonal
-- closure, maintenance and so on.
ALTER TABLE lift_readings ADD COLUMN IF NOT EXISTS raw_status VARCHAR(100);

-- Renamed lifts: alias_of points at the canonical lift so history survives a
-- rename. Never set automatically — renames are only ever suggested, because
-- names like 'Loze A'/'Loze B' are different lifts, not a rename.
ALTER TABLE lifts ADD COLUMN IF NOT EXISTS alias_of INTEGER REFERENCES lifts(id);
CREATE INDEX IF NOT EXISTS idx_lifts_alias_of ON lifts (alias_of);

-- Long-run daily weather from the ERA5 reanalysis (Open-Meteo archive API),
-- one row per resort per day back to 1991. This is what makes a single season
-- of lift observations useful: the resort data measures how lifts RESPOND to
-- weather, and this measures how often that weather actually occurs.
CREATE TABLE IF NOT EXISTS climate_daily (
    resort_id       VARCHAR(50) REFERENCES resorts(id),
    date            DATE NOT NULL,
    wind_gust_max_kmh   NUMERIC(5,1),
    wind_speed_max_kmh  NUMERIC(5,1),
    temp_min_c          NUMERIC(4,1),
    temp_max_c          NUMERIC(4,1),
    temp_mean_c         NUMERIC(4,1),
    snowfall_cm         NUMERIC(6,2),
    precipitation_mm    NUMERIC(6,1),
    precip_hours        NUMERIC(4,1),
    sunshine_hours      NUMERIC(4,1),
    -- Modelled snow reservoir at the requested elevation. At glacier altitudes
    -- this runs to tens of metres (permanent snow/ice), so treat it as a
    -- year-on-year relative signal, never as piste depth in cm.
    snow_depth_model_m  NUMERIC(6,2),
    weather_code        SMALLINT,
    PRIMARY KEY (resort_id, date)
);
CREATE INDEX IF NOT EXISTS idx_climate_daily_date ON climate_daily (date);

-- Direction matters as much as strength: a lift shuts when the wind is across
-- it, so gust speed alone cannot explain which lifts go. Cloud cover stands in
-- for the visibility closures that wind cannot explain.
ALTER TABLE climate_daily ADD COLUMN IF NOT EXISTS wind_dir_dominant_deg SMALLINT;
ALTER TABLE climate_daily ADD COLUMN IF NOT EXISTS cloud_cover_pct SMALLINT;

-- Monthly teleconnection indices. NAO drives Alpine winters far more than
-- ENSO does, so both are recorded and neither is treated as a predictor.
CREATE TABLE IF NOT EXISTS climate_indices (
    index_name  VARCHAR(10) NOT NULL,
    year        SMALLINT NOT NULL,
    month       SMALLINT NOT NULL,
    value       NUMERIC(6,2),
    PRIMARY KEY (index_name, year, month)
);
"""


def get_setting(key: str, default: str | None = None) -> str | None:
    with cursor() as cur:
        cur.execute("SELECT value FROM app_settings WHERE key = %s", (key,))
        row = cur.fetchone()
        return row["value"] if row and row["value"] else default


def set_setting(key: str, value: str):
    with cursor() as cur:
        cur.execute("""
            INSERT INTO app_settings (key, value, updated_at)
            VALUES (%s, %s, NOW())
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()
        """, (key, value))


def get_disabled_resorts() -> set[str]:
    with cursor() as cur:
        cur.execute("SELECT id FROM resorts WHERE enabled = FALSE")
        return {row["id"] for row in cur.fetchall()}


def set_resort_enabled(resort_id: str, enabled: bool):
    with cursor() as cur:
        cur.execute("UPDATE resorts SET enabled = %s WHERE id = %s", (enabled, resort_id))


def get_collected_resorts(snapshot_date, slot: str) -> set[str]:
    """Resort ids that already have a clean snapshot for this date and slot."""
    with cursor() as cur:
        cur.execute("""
            SELECT resort_id FROM snapshots
            WHERE snapshot_date = %s AND slot = %s AND scrape_error IS NULL
        """, (snapshot_date, slot))
        return {row["resort_id"] for row in cur.fetchall()}


def get_last_collection() -> dict | None:
    """Most recent snapshot date plus how many resorts were clean that day."""
    with cursor() as cur:
        cur.execute("""
            SELECT snapshot_date,
                   COUNT(*) AS resorts,
                   SUM(CASE WHEN scrape_error IS NULL THEN 1 ELSE 0 END) AS clean
            FROM snapshots
            WHERE snapshot_date = (SELECT MAX(snapshot_date) FROM snapshots)
            GROUP BY snapshot_date
        """)
        return cur.fetchone()


def upsert_holiday(h: dict):
    with cursor() as cur:
        cur.execute("""
            INSERT INTO holiday_periods (country, region, name, start_date, end_date)
            VALUES (%(country)s, %(region)s, %(name)s, %(start)s, %(end)s)
            ON CONFLICT (country, region, name) DO UPDATE SET
                start_date = EXCLUDED.start_date,
                end_date   = EXCLUDED.end_date
        """, {**h, "region": h.get("region") or ""})


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
