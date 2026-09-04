"""When the sun clears the ridge, and when the mountain takes it away again.

In a deep valley you do not lose the sun at sunset. You lose it when a
mountain gets in the way, which can be the middle of the afternoon in
midwinter — the thing you actually want to know about an apres bar.

Two halves. A **horizon profile** for a point: for every compass direction, the
angle up to the highest terrain in that direction, computed once from the local
DEM. And a **solar position** for any moment, which is pure arithmetic. The sun
is up when its elevation clears the horizon at its own azimuth.

    python -m scraper.sun --build            # horizon profiles for lift stations
    python -m scraper.sun --build cervinia
    python -m scraper.sun --times cervinia --date 2027-02-14
    python -m scraper.sun --status

Times are local (Central European), which every resort in the roster uses. EU
summer time is applied with the actual rule — last Sunday in March to last
Sunday in October — rather than a fixed offset, because a February answer and
an April answer would otherwise differ by an hour for no reason.
"""
import json
import math
import sys
from datetime import date as _date, datetime, timedelta, timezone

from .collect import load_resorts
from .db import cursor
from .elevation import elevation

# Compass resolution of a stored profile. 5 degrees is finer than the sun moves
# in 20 minutes, so interpolating between two entries costs nothing real.
AZIMUTH_STEP = 5
N_AZIMUTHS = 360 // AZIMUTH_STEP

# How far to look for the ridge that blocks the sun. A 3,000 m peak 20 km away
# still stands 8 degrees up, which in midwinter is the difference between sun
# and shade; beyond that it stops mattering.
MAX_RANGE_M = 20000
# Sampling gets coarser with distance: near ground decides steep local horizons
# and needs resolution, far ground only contributes broad ridges.
def _ray_steps() -> list[float]:
    steps, d = [], 40.0
    while d < MAX_RANGE_M:
        steps.append(d)
        d *= 1.12
    return steps


RAY = _ray_steps()
EARTH_R = 6371000.0
# Standard atmospheric refraction coefficient for terrestrial sight lines: the
# atmosphere bends light down slightly, so a distant ridge sits a little lower
# than raw geometry says.
REFRACTION_K = 0.13
# The sun's disc is about half a degree across, so its upper limb clears a
# ridge before its centre does.
SUN_SEMIDIAMETER_DEG = 0.267


def refraction_deg(true_elev_deg: float) -> float:
    """How much the atmosphere lifts an object at this altitude (Bennett).

    Strongly altitude-dependent: about 0.57 degrees at the horizon but only
    0.04 at 20 degrees up. Using a single horizon-sized allowance for a ridge
    high above you would push every answer several minutes out, which is
    exactly the error this replaced.
    """
    h = max(true_elev_deg, -1.0)
    return (1.0 / math.tan(math.radians(h + 7.31 / (h + 4.4)))) / 60.0


def apparent_top(true_elev_deg: float) -> float:
    """Altitude of the sun's upper limb as seen through the atmosphere."""
    return true_elev_deg + refraction_deg(true_elev_deg) + SUN_SEMIDIAMETER_DEG


def _offset(lat: float, lon: float, bearing_deg: float, dist_m: float):
    """Point dist_m away along a bearing, flat-earth approximation.

    Fine at 20 km: the error is metres, and the terrain sample spacing is 40 m
    at its finest.
    """
    b = math.radians(bearing_deg)
    dlat = (dist_m * math.cos(b)) / 111320.0
    dlon = (dist_m * math.sin(b)) / (111320.0 * math.cos(math.radians(lat)))
    return lat + dlat, lon + dlon


def horizon_profile(lat: float, lon: float, observer_m: float | None = None,
                    elev_fn=None) -> list[float] | None:
    """Angle to the highest terrain in each compass direction, in degrees.

    `elev_fn` lets this run against a stored terrain patch instead of the local
    SRTM tiles, which is how the deployed app answers for a point someone has
    just clicked — production has no tiles.
    """
    elev_fn = elev_fn or elevation
    h0 = observer_m if observer_m is not None else elev_fn(lat, lon)
    if h0 is None:
        return None
    # Stand a person on the ground rather than in it.
    h0 += 2.0
    out = []
    for i in range(N_AZIMUTHS):
        bearing = i * AZIMUTH_STEP
        best = 0.0
        for d in RAY:
            p_lat, p_lon = _offset(lat, lon, bearing, d)
            h = elev_fn(p_lat, p_lon)
            if h is None:
                continue
            # Curvature drops distant ground away; refraction lifts it back a
            # little. Both are small at 20 km but they partly cancel, and
            # leaving them out biases every far ridge upwards.
            drop = d * d * (1 - REFRACTION_K) / (2 * EARTH_R)
            ang = math.degrees(math.atan2(h - h0 - drop, d))
            if ang > best:
                best = ang
        out.append(round(best, 2))
    return out


def horizon_at(profile: list[float], azimuth_deg: float) -> float:
    """Horizon angle at an arbitrary bearing, linearly interpolated."""
    a = azimuth_deg % 360.0
    i = a / AZIMUTH_STEP
    lo = int(i) % N_AZIMUTHS
    hi = (lo + 1) % N_AZIMUTHS
    f = i - int(i)
    return profile[lo] * (1 - f) + profile[hi] * f


def solar_position(lat: float, lon: float, when: datetime) -> tuple[float, float]:
    """(azimuth from north clockwise, elevation) in degrees, NOAA algorithm.

    `when` must be timezone-aware UTC.
    """
    jd = when.timestamp() / 86400.0 + 2440587.5
    t = (jd - 2451545.0) / 36525.0

    mean_long = (280.46646 + t * (36000.76983 + t * 0.0003032)) % 360.0
    mean_anom = 357.52911 + t * (35999.05029 - 0.0001537 * t)
    eccent = 0.016708634 - t * (0.000042037 + 0.0000001267 * t)
    m = math.radians(mean_anom)
    centre = (math.sin(m) * (1.914602 - t * (0.004817 + 0.000014 * t))
              + math.sin(2 * m) * (0.019993 - 0.000101 * t)
              + math.sin(3 * m) * 0.000289)
    true_long = mean_long + centre
    omega = 125.04 - 1934.136 * t
    app_long = true_long - 0.00569 - 0.00478 * math.sin(math.radians(omega))

    obliq = (23.0 + (26.0 + (21.448 - t * (46.815 + t * (0.00059 - t * 0.001813))) / 60.0) / 60.0)
    obliq_corr = obliq + 0.00256 * math.cos(math.radians(omega))

    decl = math.degrees(math.asin(math.sin(math.radians(obliq_corr))
                                  * math.sin(math.radians(app_long))))

    y = math.tan(math.radians(obliq_corr / 2)) ** 2
    eq_time = 4 * math.degrees(
        y * math.sin(2 * math.radians(mean_long))
        - 2 * eccent * math.sin(m)
        + 4 * eccent * y * math.sin(m) * math.cos(2 * math.radians(mean_long))
        - 0.5 * y * y * math.sin(4 * math.radians(mean_long))
        - 1.25 * eccent * eccent * math.sin(2 * m))

    minutes = when.hour * 60 + when.minute + when.second / 60.0
    true_solar = (minutes + eq_time + 4 * lon) % 1440.0
    hour_angle = true_solar / 4.0 - 180.0

    lat_r, decl_r, ha_r = math.radians(lat), math.radians(decl), math.radians(hour_angle)
    cos_zen = (math.sin(lat_r) * math.sin(decl_r)
               + math.cos(lat_r) * math.cos(decl_r) * math.cos(ha_r))
    cos_zen = max(-1.0, min(1.0, cos_zen))
    zenith = math.degrees(math.acos(cos_zen))
    elev = 90.0 - zenith

    # Azimuth from north, clockwise.
    denom = math.cos(lat_r) * math.sin(math.radians(zenith))
    if abs(denom) < 1e-9:
        az = 180.0
    else:
        c = (math.sin(lat_r) * math.cos(math.radians(zenith)) - math.sin(decl_r)) / denom
        c = max(-1.0, min(1.0, c))
        az = math.degrees(math.acos(c))
        if hour_angle > 0:
            az = (az + 180.0) % 360.0
        else:
            az = (540.0 - az) % 360.0
    return az, elev


def _last_sunday(year: int, month: int) -> _date:
    d = _date(year, month, 31) if month != 3 else _date(year, 3, 31)
    while d.weekday() != 6:
        d -= timedelta(days=1)
    return d


def cet_offset(when_utc: datetime) -> int:
    """Hours to add to UTC for Central European (summer) time.

    EU rule: 01:00 UTC on the last Sunday of March until 01:00 UTC on the last
    Sunday of October. Every resort in the roster is in this zone.
    """
    y = when_utc.year
    start = datetime.combine(_last_sunday(y, 3), datetime.min.time(), timezone.utc) + timedelta(hours=1)
    end = datetime.combine(_last_sunday(y, 10), datetime.min.time(), timezone.utc) + timedelta(hours=1)
    return 2 if start <= when_utc < end else 1


def sun_windows(lat: float, lon: float, profile: list[float], day: _date,
                step_min: int = 2) -> dict:
    """When the sun is above the ridge on this date, in local time.

    Returns the first and last moment it is visible, the total hours, and
    whether the day is broken into more than one window — a peak due south can
    genuinely take the sun away and give it back.
    """
    visible = []
    t = datetime(day.year, day.month, day.day, 0, 0, tzinfo=timezone.utc)
    end = t + timedelta(days=1)
    while t < end:
        az, elev = solar_position(lat, lon, t)
        if apparent_top(elev) > horizon_at(profile, az):
            visible.append(t)
        t += timedelta(minutes=step_min)

    if not visible:
        return {"first": None, "last": None, "hours": 0.0, "windows": 0,
                "flat_first": None, "flat_last": None}

    windows, prev = 1, visible[0]
    for v in visible[1:]:
        if (v - prev).total_seconds() > step_min * 60 * 1.5:
            windows += 1
        prev = v

    # The same day against a flat horizon, so the loss to terrain is legible.
    flat = []
    t = datetime(day.year, day.month, day.day, 0, 0, tzinfo=timezone.utc)
    while t < end:
        _, elev = solar_position(lat, lon, t)
        if apparent_top(elev) > 0:
            flat.append(t)
        t += timedelta(minutes=step_min)

    def local(dt):
        return (dt + timedelta(hours=cet_offset(dt))).strftime("%H:%M")

    return {
        "first": local(visible[0]), "last": local(visible[-1]),
        "hours": round(len(visible) * step_min / 60.0, 1),
        "windows": windows,
        "flat_first": local(flat[0]) if flat else None,
        "flat_last": local(flat[-1]) if flat else None,
    }


def station_points(resort_id: str | None = None) -> list[dict]:
    """Lift base and top stations, deduplicated.

    Base stations are where the bars are; top stations answer the other half
    of the question. Rounding to five decimals (about a metre) collapses the
    dozen lifts that all leave from the same square of village.
    """
    clause = "WHERE resort_id = %s" if resort_id else ""
    with cursor() as cur:
        cur.execute(f"""
            SELECT resort_id, name, bottom_lat, bottom_lon, top_lat, top_lon,
                   elevations
            FROM lift_geometry {clause}
        """, (resort_id,) if resort_id else ())
        rows = cur.fetchall()

    seen, out = set(), []
    for r in rows:
        els = r["elevations"] if isinstance(r["elevations"], list) else json.loads(r["elevations"] or "null")
        for kind, lat, lon, el in (
                ("base", r["bottom_lat"], r["bottom_lon"], els[0] if els else None),
                ("top", r["top_lat"], r["top_lon"], els[-1] if els else None)):
            if lat is None or lon is None:
                continue
            key = (r["resort_id"], round(float(lat), 4), round(float(lon), 4))
            if key in seen:
                continue
            seen.add(key)
            out.append({"resort_id": r["resort_id"], "kind": kind,
                        "name": r["name"], "lat": float(lat), "lon": float(lon),
                        "elev": el})
    return out


def build(resort_id: str | None = None) -> int:
    pts = station_points(resort_id)
    if not pts:
        print("\n  No lift geometry to work from.\n")
        return 1
    print(f"\n  Building horizon profiles for {len(pts):,} station(s)"
          f"{' in ' + resort_id if resort_id else ''}…\n")
    done = skipped = 0
    batch = []
    # Committed in batches rather than one transaction for the whole run. At
    # roughly a third of a second per profile this is a twenty-minute job, and
    # holding it all open means a failure at minute nineteen loses everything
    # and a --status halfway through shows nothing.
    def flush(rows):
        if not rows:
            return
        with cursor() as cur:
            for r in rows:
                cur.execute("""
                    INSERT INTO sun_points
                        (resort_id, kind, name, latitude, longitude, elevation_m,
                         azimuth_step, horizon)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (resort_id, latitude, longitude) DO UPDATE SET
                        kind = EXCLUDED.kind, name = EXCLUDED.name,
                        elevation_m = EXCLUDED.elevation_m,
                        azimuth_step = EXCLUDED.azimuth_step,
                        horizon = EXCLUDED.horizon, built_at = NOW()
                """, r)

    for i, p in enumerate(pts, 1):
        prof = horizon_profile(p["lat"], p["lon"], p["elev"])
        if prof is None:
            skipped += 1
            continue
        batch.append((p["resort_id"], p["kind"], p["name"], round(p["lat"], 6),
                      round(p["lon"], 6), p["elev"], AZIMUTH_STEP, json.dumps(prof)))
        done += 1
        if len(batch) >= 100:
            flush(batch)
            batch = []
            print(f"    {i:,} / {len(pts):,}…", end="\r", flush=True)
    flush(batch)
    print(f"    {done:,} profiles stored, {skipped:,} skipped for missing DEM.\n")
    return 0


def times(resort_id: str, day: _date, limit: int = 25) -> int:
    with cursor() as cur:
        cur.execute("""
            SELECT name, kind, latitude, longitude, elevation_m, horizon
            FROM sun_points WHERE resort_id = %s ORDER BY elevation_m
        """, (resort_id,))
        rows = cur.fetchall()
    if not rows:
        print(f"\n  No horizon profiles for {resort_id} — run --build first.\n")
        return 1
    print(f"\nSun over the ridge — {resort_id}, {day}\n")
    print(f"  {'station':<34}{'m':>6}{'kind':>6}{'sun from':>10}{'until':>8}"
          f"{'hours':>7}   flat horizon")
    out = []
    for r in rows:
        prof = r["horizon"] if isinstance(r["horizon"], list) else json.loads(r["horizon"])
        w = sun_windows(float(r["latitude"]), float(r["longitude"]), prof, day)
        out.append((r, w))
    out.sort(key=lambda x: (x[1]["last"] or "99:99"))
    for r, w in out[:limit]:
        note = f"  ({w['windows']} windows)" if w["windows"] > 1 else ""
        print(f"  {(r['name'] or '—')[:32]:<34}{r['elevation_m'] or 0:>6}"
              f"{r['kind']:>6}{w['first'] or '—':>10}{w['last'] or '—':>8}"
              f"{w['hours']:>7.1f}   {w['flat_first']}–{w['flat_last']}{note}")
    print("\n  Sorted by who loses the sun first. 'flat horizon' is the same day "
          "with no\n  mountains in the way — the gap between the two is what the "
          "terrain costs you.\n")
    return 0


def status() -> int:
    with cursor() as cur:
        cur.execute("""
            SELECT resort_id, COUNT(*) AS n,
                   MIN(elevation_m) AS lo, MAX(elevation_m) AS hi
            FROM sun_points GROUP BY resort_id ORDER BY resort_id
        """)
        rows = cur.fetchall()
    if not rows:
        print("\n  No horizon profiles yet — run --build.\n")
        return 1
    for r in rows:
        print(f"  {r['resort_id']:<18}{r['n']:>5} points   {r['lo']}–{r['hi']} m")
    print(f"\n  {sum(r['n'] for r in rows):,} profiles across {len(rows)} resort(s).\n")
    return 0


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    args = sys.argv[1:]
    if "--build" in args:
        i = args.index("--build")
        target = args[i + 1] if len(args) > i + 1 and not args[i + 1].startswith("-") else None
        return build(target)
    if "--times" in args:
        i = args.index("--times")
        rid = args[i + 1]
        day = _date.today()
        if "--date" in args:
            day = _date.fromisoformat(args[args.index("--date") + 1])
        return times(rid, day)
    if "--status" in args:
        return status()
    print(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(main())
