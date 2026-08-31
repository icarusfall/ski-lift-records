"""Combine observed lift behaviour with long-run climate to estimate risk.

Two layers:

  1. RESPONSE  - from observed snapshots: how much of a resort stays open at a
     given wind strength. This is the part only this project has.
  2. FREQUENCY - from 35 years of ERA5: how often that wind strength actually
     occurs at that resort in that part of the season.

Multiplying them gives expected openness, and the probability of losing a day,
for any resort in any week — after one season rather than ten.

    python -m scraper.model --week 02-14 02-22     # rank resorts for a trip
    python -m scraper.model --response cervinia    # observed wind response
    python -m scraper.model --lifts cervinia       # per-lift wind response

A "lost day" defaults to under half the lifts running, because that is the
failure mode that actually spoils a trip: not one link lift shut, but the
mountain collapsing to a couple of chairs.
"""
import sys
from collections import defaultdict

from .db import cursor

# Upper bound of each wind band, in km/h.
BINS = [(40, "calm"), (60, "breezy"), (80, "windy"), (999, "severe")]
LOST_DAY_PCT = 50.0
# Weather-driven closures are only separable from seasonal wind-down inside
# the core season; the window wraps across the new year.
CORE_SEASON = ("12-15", "04-15")
# Shrinkage strength: a bin with this many observed days counts equally with
# the pooled all-resort estimate. Keeps thin bins from producing wild numbers.
SHRINK_K = 8.0


def _bin_for(gust: float | None) -> str | None:
    if gust is None:
        return None
    for upper, name in BINS:
        if gust < upper:
            return name
    return BINS[-1][1]


_OBS_CACHE: list | None = None


def clear_cache():
    """Drop the memoised observation set (after new data is collected)."""
    global _OBS_CACHE
    _OBS_CACHE = None


def _observations():
    """Clean resort-days with both a lift reading and a wind reading.

    Memoised: ranking every resort re-reads this set once per resort, which
    turns a single query into dozens of full scans if it is not cached.

    Restricted to the core season. Outside it, resorts wind down for reasons
    that have nothing to do with weather, and since season 1 only began on
    23 February, late-season closures would otherwise be read as wind damage —
    which showed up as a nonsensically *better* response in severe wind than
    in moderate wind.
    """
    global _OBS_CACHE
    if _OBS_CACHE is not None:
        return _OBS_CACHE
    with cursor() as cur:
        cur.execute("""
            SELECT resort_id, snapshot_date, pct_lifts_open, wind_gust_max_kmh
            FROM snapshots
            WHERE scrape_error IS NULL AND pct_lifts_open IS NOT NULL
              AND wind_gust_max_kmh IS NOT NULL AND lifts_total > 3
              AND (TO_CHAR(snapshot_date, 'MM-DD') >= %s
                   OR TO_CHAR(snapshot_date, 'MM-DD') <= %s)
        """, (CORE_SEASON[0], CORE_SEASON[1]))
        _OBS_CACHE = cur.fetchall()
    return _OBS_CACHE


def _aggregate(rows):
    acc = defaultdict(lambda: {"n": 0, "sum_pct": 0.0, "lost": 0})
    for r in rows:
        b = _bin_for(float(r["wind_gust_max_kmh"]))
        if not b:
            continue
        a = acc[b]
        a["n"] += 1
        a["sum_pct"] += float(r["pct_lifts_open"])
        a["lost"] += 1 if float(r["pct_lifts_open"]) < LOST_DAY_PCT else 0
    return {b: {"n": v["n"],
                "pct_open": v["sum_pct"] / v["n"],
                "p_lost": v["lost"] / v["n"]}
            for b, v in acc.items() if v["n"]}


def pooled_response() -> dict:
    return _aggregate(_observations())


def observed_response(resort_id: str) -> dict:
    return _aggregate([r for r in _observations() if r["resort_id"] == resort_id])


def shrunk_response(resort_id: str, pooled: dict | None = None) -> dict:
    """Resort estimate pulled toward the all-resort average in proportion to
    how little data supports it — one partial season is thin evidence."""
    pooled = pooled or pooled_response()
    own = observed_response(resort_id)
    out = {}
    for _, b in BINS:
        p = pooled.get(b)
        o = own.get(b)
        if not p and not o:
            continue
        if not o:
            out[b] = {**p, "n": 0, "shrunk": True}
            continue
        w = o["n"] / (o["n"] + SHRINK_K)
        out[b] = {
            "n": o["n"],
            "pct_open": w * o["pct_open"] + (1 - w) * p["pct_open"],
            "p_lost": w * o["p_lost"] + (1 - w) * p["p_lost"],
            "shrunk": w < 0.75,
        }
    return out


def week_frequency(resort_id: str, start_md: str, end_md: str) -> dict:
    """Historical share of days in each wind band for a calendar window."""
    with cursor() as cur:
        cur.execute("""
            SELECT wind_gust_max_kmh AS g
            FROM climate_daily
            WHERE resort_id = %s
              AND TO_CHAR(date, 'MM-DD') BETWEEN %s AND %s
              AND wind_gust_max_kmh IS NOT NULL
        """, (resort_id, start_md, end_md))
        gusts = [float(r["g"]) for r in cur.fetchall()]
    if not gusts:
        return {}
    counts = defaultdict(int)
    for g in gusts:
        counts[_bin_for(g)] += 1
    return {"n_days": len(gusts),
            "p": {b: counts[b] / len(gusts) for _, b in BINS if counts[b]}}


def forecast(resort_id: str, start_md: str, end_md: str,
             trip_days: int = 7, pooled: dict | None = None) -> dict | None:
    freq = week_frequency(resort_id, start_md, end_md)
    if not freq:
        return None
    resp = shrunk_response(resort_id, pooled)
    if not resp:
        return None

    exp_open, p_lost, covered = 0.0, 0.0, 0.0
    for b, p in freq["p"].items():
        r = resp.get(b)
        if not r:
            continue
        exp_open += p * r["pct_open"]
        p_lost += p * r["p_lost"]
        covered += p
    if covered < 0.5:
        return None
    exp_open, p_lost = exp_open / covered, p_lost / covered
    return {
        "resort_id": resort_id,
        "expected_pct_open": exp_open,
        "p_lost_day": p_lost,
        "p_any_lost": 1 - (1 - p_lost) ** trip_days,
        "climate_days": freq["n_days"],
        "obs_days": sum(r["n"] for r in resp.values()),
        "thin": any(r.get("shrunk") for r in resp.values()),
    }


def storm_runs(resort_id: str, start_md: str, end_md: str,
               threshold: float = 60.0, min_run: int = 3) -> dict | None:
    """How often a calendar window contains a *run* of consecutive windy days.

    Run length is the thing that ruins a trip: one shut day is a long lunch,
    three in a row is the holiday. Wind is strongly autocorrelated — storms sit
    over a range for days — so this is measured from the 35-year record rather
    than inferred from a handful of observed days.
    """
    with cursor() as cur:
        cur.execute("""
            SELECT EXTRACT(YEAR FROM date)::int AS yr, date,
                   wind_gust_max_kmh AS g
            FROM climate_daily
            WHERE resort_id = %s
              AND TO_CHAR(date, 'MM-DD') BETWEEN %s AND %s
              AND wind_gust_max_kmh IS NOT NULL
            ORDER BY date
        """, (resort_id, start_md, end_md))
        rows = cur.fetchall()
    if not rows:
        return None

    by_year = defaultdict(list)
    for r in rows:
        by_year[r["yr"]].append(float(r["g"]) >= threshold)

    years_with_run, longest = 0, []
    for windy_days in by_year.values():
        run = best = 0
        for w in windy_days:
            run = run + 1 if w else 0
            best = max(best, run)
        longest.append(best)
        if best >= min_run:
            years_with_run += 1
    n = len(by_year)
    return {
        "years": n,
        "p_run": years_with_run / n,
        "mean_longest_run": sum(longest) / n,
        "worst_run": max(longest),
    }


def all_window_stats(start_md: str, end_md: str, threshold: float = 60.0,
                     min_run: int = 3) -> dict[str, dict]:
    """Wind-band frequency and storm-run stats for every resort in one query.

    Ranking the whole field otherwise costs two indexless scans per resort,
    which is far too slow to serve from a web request.
    """
    with cursor() as cur:
        cur.execute("""
            SELECT resort_id, EXTRACT(YEAR FROM date)::int AS yr, date,
                   wind_gust_max_kmh AS g
            FROM climate_daily
            WHERE TO_CHAR(date, 'MM-DD') BETWEEN %s AND %s
              AND wind_gust_max_kmh IS NOT NULL
            ORDER BY resort_id, date
        """, (start_md, end_md))
        rows = cur.fetchall()

    per_resort = defaultdict(lambda: {"counts": defaultdict(int), "n": 0,
                                      "years": defaultdict(list)})
    for r in rows:
        acc = per_resort[r["resort_id"]]
        g = float(r["g"])
        acc["counts"][_bin_for(g)] += 1
        acc["n"] += 1
        acc["years"][r["yr"]].append(g >= threshold)

    out = {}
    for rid, acc in per_resort.items():
        longest, with_run = [], 0
        for windy_days in acc["years"].values():
            run = best = 0
            for w in windy_days:
                run = run + 1 if w else 0
                best = max(best, run)
            longest.append(best)
            if best >= min_run:
                with_run += 1
        n_years = len(acc["years"]) or 1
        out[rid] = {
            "freq": {"n_days": acc["n"],
                     "p": {b: c / acc["n"] for b, c in acc["counts"].items()}},
            "runs": {"years": len(acc["years"]),
                     "p_run": with_run / n_years,
                     "mean_longest_run": sum(longest) / n_years,
                     "worst_run": max(longest) if longest else 0},
        }
    return out


def forecast_from(resort_id: str, freq: dict, pooled: dict,
                  trip_days: int = 7) -> dict | None:
    """forecast() for a frequency distribution already in hand."""
    resp = shrunk_response(resort_id, pooled)
    if not resp or not freq or not freq.get("p"):
        return None
    exp_open = p_lost = covered = 0.0
    for b, p in freq["p"].items():
        r = resp.get(b)
        if not r:
            continue
        exp_open += p * r["pct_open"]
        p_lost += p * r["p_lost"]
        covered += p
    if covered < 0.5:
        return None
    exp_open, p_lost = exp_open / covered, p_lost / covered
    return {
        "resort_id": resort_id,
        "expected_pct_open": exp_open,
        "p_lost_day": p_lost,
        "p_any_lost": 1 - (1 - p_lost) ** trip_days,
        "climate_days": freq["n_days"],
        "obs_days": sum(r["n"] for r in resp.values()),
        "thin": any(r.get("shrunk") for r in resp.values()),
    }


def lift_response(resort_id: str) -> list[dict]:
    """Per-lift open rate in calm vs windy conditions."""
    with cursor() as cur:
        cur.execute("""
            SELECT c.name, c.is_link,
                   s.wind_gust_max_kmh AS gust,
                   COUNT(*) FILTER (WHERE lr.status = 'open') AS opens,
                   COUNT(*) AS n
            FROM lift_readings lr
            JOIN lifts l ON l.id = lr.lift_id
            JOIN lifts c ON c.id = COALESCE(l.alias_of, l.id)
            JOIN snapshots s ON s.id = lr.snapshot_id
            WHERE c.resort_id = %s AND s.wind_gust_max_kmh IS NOT NULL
              AND lr.status IN ('open', 'closed', 'hold')
            GROUP BY c.name, c.is_link, s.wind_gust_max_kmh
        """, (resort_id,))
        rows = cur.fetchall()

    acc = defaultdict(lambda: {"link": False, "calm_n": 0, "calm_open": 0,
                               "windy_n": 0, "windy_open": 0})
    for r in rows:
        a = acc[r["name"]]
        a["link"] = r["is_link"]
        windy = float(r["gust"]) >= 60
        key = "windy" if windy else "calm"
        a[f"{key}_n"] += r["n"]
        a[f"{key}_open"] += r["opens"]

    out = []
    for name, a in acc.items():
        if a["calm_n"] < 5 or a["windy_n"] < 3:
            continue
        calm = 100 * a["calm_open"] / a["calm_n"]
        windy = 100 * a["windy_open"] / a["windy_n"]
        out.append({"name": name, "is_link": a["link"],
                    "calm_pct": calm, "windy_pct": windy, "drop": calm - windy,
                    "calm_n": a["calm_n"], "windy_n": a["windy_n"]})
    return sorted(out, key=lambda x: -x["drop"])


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    args = sys.argv[1:]

    if "--response" in args:
        rid = args[args.index("--response") + 1]
        print(f"\nObserved wind response — {rid}\n")
        own, shr = observed_response(rid), shrunk_response(rid)
        print(f"  {'band':<9} {'days':>5} {'% open':>8} {'lost-day rate':>14}")
        for _, b in BINS:
            if b in shr:
                s, o = shr[b], own.get(b, {})
                flag = "  (thin — pulled toward average)" if s.get("shrunk") else ""
                print(f"  {b:<9} {o.get('n', 0):>5} {s['pct_open']:>7.1f}% "
                      f"{100*s['p_lost']:>13.0f}%{flag}")
        print()
        return 0

    if "--lifts" in args:
        rid = args[args.index("--lifts") + 1]
        rows = lift_response(rid)
        print(f"\nPer-lift wind response — {rid}  (calm = gust <60 km/h)\n")
        print(f"  {'lift':<34} {'calm':>7} {'windy':>7} {'drop':>7}")
        for r in rows[:25]:
            tag = " LINK" if r["is_link"] else ""
            print(f"  {r['name'][:32]:<34} {r['calm_pct']:>6.0f}% {r['windy_pct']:>6.0f}% "
                  f"{r['drop']:>6.0f}pt{tag}")
        print()
        return 0

    if "--week" in args:
        i = args.index("--week")
        start_md, end_md = args[i + 1], args[i + 2]
        with cursor() as cur:
            cur.execute("SELECT id, name FROM resorts ORDER BY name")
            resorts = cur.fetchall()
        pooled = pooled_response()
        out = []
        for r in resorts:
            f = forecast(r["id"], start_md, end_md, pooled=pooled)
            if f:
                f["name"] = r["name"]
                out.append(f)
        out.sort(key=lambda x: -x["expected_pct_open"])
        print(f"\nTrip outlook for {start_md} to {end_md} "
              f"(a lost day = under {LOST_DAY_PCT:.0f}% of lifts running)\n")
        print(f"  {'resort':<26} {'exp. open':>10} {'lost-day':>9} {'≥1 lost in 7d':>15}")
        for f in out:
            thin = " ~" if f["thin"] else ""
            print(f"  {f['name'][:24]:<26} {f['expected_pct_open']:>9.1f}% "
                  f"{100*f['p_lost_day']:>8.0f}% {100*f['p_any_lost']:>14.0f}%{thin}")
        print("\n  ~ = response estimated from thin observations, pulled toward the average.")
        print("  Frequencies come from 35 years of ERA5; response from observed seasons only.\n")
        return 0

    print(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(main())
