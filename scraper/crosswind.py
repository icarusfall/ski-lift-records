"""Does the wind's *direction* predict closures better than its strength alone?

A cable car shuts when the wind blows across the cable, not along it. Every
lift matched to OpenStreetMap has a bearing, and ERA5 gives a dominant wind
direction for every day, so the component blowing across each lift is

    crosswind = gust * |sin(wind direction - lift bearing)|

A lift bearing is a line, not an arrow, so the |sin| handles the 180 degree
symmetry for free: a wind from the north and a wind from the south cross an
east-west lift equally.

    python -m scraper.crosswind --report          # every resort
    python -m scraper.crosswind --resort cervinia # per-lift, with bearings
    python -m scraper.crosswind --rose st-anton   # closure rate by direction

The test is discrimination, not fit. For each lift, AUC is the chance that a
snapshot where it was shut carried a higher value than one where it ran: 0.5
is a coin flip. Measuring per lift and then averaging is deliberate — pooling
lifts would mostly measure that some lifts shut more than others, which is
nestedness's question, not this one.

Comparing crosswind against gust directly is a loaded question, because
gust * |sin| can never exceed gust: multiply a good predictor by a mostly-noisy
factor and it has to lose. The honest test holds strength roughly fixed —
each resort's windiest days — and asks whether the angle alone still separates
shut from running.

## The answer, measured 4 Sep 2026: it does not.

Angle alone scores **0.453** over 1,932 shut-vs-running pairs; 18 of 42 lifts
land above a coin flip and 24 below. This is a real null, not a degenerate one:
ERA5 direction varies plenty (circular SD 56-116 degrees per resort), lift
bearings span up to 355 degrees within one resort, and |sin| itself has an
interquartile range near 0.5 on windy days. There is contrast to find a signal
in, and there is no signal.

The likeliest reason is the instrument, not the hypothesis. A daily *dominant*
direction on a 25 km grid is a poor description of what a ridge-top cable feels:
alpine valleys channel wind tens of degrees away from the synoptic flow, and a
wind hold declared at 08:00 is not necessarily aligned with the day's dominant
vector. Season 1 has no snapshot-level direction at all, so this is the best
test the data currently allows — not the best test there is. `scraper/weather.py`
now records direction at 10 m and at 700 hPa on every snapshot, which is what
would let season 2 answer the question properly.
"""
import math
import sys
from collections import defaultdict

from .db import cursor
from .nested import CORE_SEASON

# A lift needs both outcomes, and enough of the rarer one, before a rank
# statistic on it means anything.
MIN_READINGS = 20
MIN_MINORITY = 4
# Direction only gets a fair hearing on days when the wind could plausibly shut
# something; on a calm day the answer is "open", whatever the bearing. The cut
# is each resort's own gust quantile rather than a fixed speed, because a fixed
# one silently excludes whole resorts: Courchevel, Meribel and Lech never reach
# 60 km/h in this record at all.
WINDY_QUANTILE = 0.70
MIN_WINDY_MINORITY = 3
# Direction sectors for the rose. 30 degrees keeps ~12 buckets populated over
# a single season without splitting it into noise.
SECTOR_DEG = 30


def _cells(resort_id: str | None = None) -> list[dict]:
    """One row per lift-snapshot with a bearing and a wind direction."""
    clauses = ["c.kind = 'lift'", "lr.status IN ('open', 'closed', 'hold')",
               "s.scrape_error IS NULL", "g.bearing_deg IS NOT NULL",
               "cd.wind_dir_dominant_deg IS NOT NULL",
               "s.wind_gust_max_kmh IS NOT NULL",
               "(TO_CHAR(s.snapshot_date, 'MM-DD') >= %(start)s"
               " OR TO_CHAR(s.snapshot_date, 'MM-DD') <= %(end)s)"]
    if resort_id:
        clauses.append("c.resort_id = %(rid)s")
    with cursor() as cur:
        cur.execute(f"""
            SELECT c.resort_id, c.name, c.is_link, s.snapshot_date,
                   g.bearing_deg::float AS bearing,
                   -- Strength from the snapshot, angle from ERA5. ERA5's 25 km
                   -- grid smooths ridge-top gusts away (its p90 for Courchevel
                   -- is 28 km/h), but it is the only source of direction there
                   -- is. Both AUC columns use this same gust, so comparing them
                   -- still isolates the angle.
                   s.wind_gust_max_kmh::float AS gust,
                   cd.wind_dir_dominant_deg::float AS wind_dir,
                   (lr.status = 'open') AS is_open
            FROM lift_readings lr
            JOIN lifts l ON l.id = lr.lift_id
            JOIN lifts c ON c.id = COALESCE(l.alias_of, l.id)
            JOIN snapshots s ON s.id = lr.snapshot_id
            -- A lift can match several OSM ways (a gondola mapped in sections);
            -- their mean bearing is the line the cable actually runs along.
            JOIN (SELECT lift_id, AVG(bearing_deg) AS bearing_deg
                  FROM lift_geometry WHERE lift_id IS NOT NULL
                  GROUP BY lift_id) g ON g.lift_id = c.id
            JOIN climate_daily cd
              ON cd.resort_id = c.resort_id AND cd.date = s.snapshot_date
            WHERE {' AND '.join(clauses)}
        """, {"rid": resort_id, "start": CORE_SEASON[0], "end": CORE_SEASON[1]})
        rows = cur.fetchall()

    out = []
    for r in rows:
        delta = math.radians(r["wind_dir"] - r["bearing"])
        out.append({
            "resort_id": r["resort_id"], "name": r["name"], "link": r["is_link"],
            "date": r["snapshot_date"], "bearing": r["bearing"],
            "gust": r["gust"], "wind_dir": r["wind_dir"],
            "cross": r["gust"] * abs(math.sin(delta)),
            "along": r["gust"] * abs(math.cos(delta)),
            # The angle on its own, with strength divided out: 1 is square
            # across the cable, 0 is straight along it.
            "frac": abs(math.sin(delta)),
            "open": r["is_open"],
        })
    return out


def auc(pos: list[float], neg: list[float]) -> float | None:
    """P(a shut reading scored higher than a running one), ties counted half.

    Rank-based rather than pairwise so it stays linear in the number of
    readings, and so tied wind values do not silently count as wins.
    """
    if not pos or not neg:
        return None
    marked = sorted([(v, 1) for v in pos] + [(v, 0) for v in neg],
                    key=lambda t: t[0])
    ranks = [0.0] * len(marked)
    i = 0
    while i < len(marked):
        j = i
        while j + 1 < len(marked) and marked[j + 1][0] == marked[i][0]:
            j += 1
        avg = (i + j) / 2 + 1
        for k in range(i, j + 1):
            ranks[k] = avg
        i = j + 1
    n_pos, n_neg = len(pos), len(neg)
    sum_pos = sum(ranks[k] for k in range(len(marked)) if marked[k][1])
    return (sum_pos - n_pos * (n_pos + 1) / 2) / (n_pos * n_neg)


def windy_cut(cells: list[dict]) -> dict[str, float]:
    """Each resort's own gust quantile marking a day windy enough to matter."""
    by_resort = defaultdict(list)
    for c in cells:
        by_resort[c["resort_id"]].append(c["gust"])
    out = {}
    for rid, gusts in by_resort.items():
        gusts.sort()
        out[rid] = gusts[min(len(gusts) - 1, int(WINDY_QUANTILE * len(gusts)))]
    return out


def per_lift(cells: list[dict]) -> list[dict]:
    """AUC of gust and of crosswind, computed within each lift."""
    cut = windy_cut(cells)
    by_lift = defaultdict(list)
    for c in cells:
        by_lift[(c["resort_id"], c["name"])].append(c)

    out = []
    for (rid, name), rows in by_lift.items():
        shut = [r for r in rows if not r["open"]]
        ran = [r for r in rows if r["open"]]
        if len(rows) < MIN_READINGS or min(len(shut), len(ran)) < MIN_MINORITY:
            continue
        a_gust = auc([r["gust"] for r in shut], [r["gust"] for r in ran])
        a_cross = auc([r["cross"] for r in shut], [r["cross"] for r in ran])
        a_along = auc([r["along"] for r in shut], [r["along"] for r in ran])
        # gust * |sin| can only ever be <= gust, so scoring it against gust asks
        # a loaded question: a good predictor multiplied by a mostly-noisy
        # factor has to lose. The fair test holds strength roughly fixed and
        # asks whether the angle alone still separates shut from running.
        w_shut = [r for r in shut if r["gust"] >= cut[rid]]
        w_ran = [r for r in ran if r["gust"] >= cut[rid]]
        a_frac = (auc([r["frac"] for r in w_shut], [r["frac"] for r in w_ran])
                  if min(len(w_shut), len(w_ran)) >= MIN_WINDY_MINORITY else None)
        out.append({
            "resort_id": rid, "name": name, "link": rows[0]["link"],
            "bearing": rows[0]["bearing"], "n": len(rows), "shut": len(shut),
            "auc_gust": a_gust, "auc_cross": a_cross, "auc_along": a_along,
            "auc_frac_windy": a_frac, "windy_pairs": len(w_shut) * len(w_ran),
            "delta": a_cross - a_gust,
            # Pairs is what the AUC actually rests on, and it is the honest
            # weight when averaging lifts of very different exposure.
            "pairs": len(shut) * len(ran),
        })
    return sorted(out, key=lambda r: -r["delta"])


def by_resort(lifts: list[dict]) -> list[dict]:
    acc = defaultdict(lambda: {"lifts": 0, "better": 0, "pairs": 0, "wg": 0.0,
                               "wc": 0.0, "wlifts": 0, "wpairs": 0, "wf": 0.0})
    for l in lifts:
        a = acc[l["resort_id"]]
        a["lifts"] += 1
        a["better"] += 1 if l["delta"] > 0 else 0
        a["pairs"] += l["pairs"]
        a["wg"] += l["auc_gust"] * l["pairs"]
        a["wc"] += l["auc_cross"] * l["pairs"]
        if l["auc_frac_windy"] is not None:
            a["wlifts"] += 1
            a["wpairs"] += l["windy_pairs"]
            a["wf"] += l["auc_frac_windy"] * l["windy_pairs"]
    out = []
    for rid, a in acc.items():
        if not a["pairs"]:
            continue
        g, c = a["wg"] / a["pairs"], a["wc"] / a["pairs"]
        out.append({"resort_id": rid, "lifts": a["lifts"], "better": a["better"],
                    "pairs": a["pairs"], "auc_gust": g, "auc_cross": c,
                    "delta": c - g, "windy_lifts": a["wlifts"],
                    "windy_pairs": a["wpairs"],
                    "auc_frac_windy": (a["wf"] / a["wpairs"]) if a["wpairs"] else None})
    return sorted(out, key=lambda r: -r["delta"])


def rose(cells: list[dict], sector: int = SECTOR_DEG) -> list[dict]:
    """Closure rate by wind direction, for one resort.

    Split by whether the wind was strong, because a resort's calm days are
    spread over every direction and would otherwise flatten the pattern.
    """
    windy = windy_cut(cells).get(cells[0]["resort_id"], 0) if cells else 0
    acc = defaultdict(lambda: {"n": 0, "shut": 0, "windy_n": 0, "windy_shut": 0,
                               "days": set()})
    for c in cells:
        a = acc[int(c["wind_dir"] // sector) * sector]
        # n counts lift-readings, which is what the percentages divide by; days
        # counts dates, which is the honest measure of how much weather this
        # sector actually represents. One windy day across 40 lifts is 40
        # readings and would otherwise look like a well-sampled sector.
        a["n"] += 1
        a["days"].add(c["date"])
        a["shut"] += 0 if c["open"] else 1
        if c["gust"] >= windy:
            a["windy_n"] += 1
            a["windy_shut"] += 0 if c["open"] else 1
    return [{"from_deg": d, "n": v["n"], "days": len(v["days"]),
             "pct_shut": 100 * v["shut"] / v["n"], "windy_n": v["windy_n"],
             "windy_pct_shut": (100 * v["windy_shut"] / v["windy_n"])
                               if v["windy_n"] else None}
            for d, v in sorted(acc.items())]


def analyse_all() -> dict:
    cells = _cells()
    lifts = per_lift(cells)
    return {"cells": len(cells), "lifts": lifts, "resorts": by_resort(lifts)}


def _compass(deg: float) -> str:
    pts = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
           "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]
    return pts[int((deg % 360) / 22.5 + 0.5) % 16]


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    args = sys.argv[1:]

    if "--resort" in args:
        rid = args[args.index("--resort") + 1]
        cells = _cells(rid)
        lifts = per_lift(cells)
        if not lifts:
            print(f"\n  {rid}: no lift has both a bearing and enough of each outcome.\n")
            return 1
        print(f"\nCrosswind vs gust — {rid}   ({len(cells)} lift-snapshots)\n")
        print(f"  {'lift':<30} {'bearing':>8} {'shut':>5} {'gust':>6} {'cross':>6} {'gain':>6}")
        for l in lifts:
            tag = " LINK" if l["link"] else ""
            print(f"  {l['name'][:28]:<30} {l['bearing']:>5.0f}° "
                  f"{_compass(l['bearing']):<4} {l['shut']:>3} "
                  f"{l['auc_gust']:>6.2f} {l['auc_cross']:>6.2f} "
                  f"{l['delta']:>+6.2f}{tag}")
        s = by_resort(lifts)[0]
        print(f"\n  Weighted over {s['lifts']} lifts: gust {s['auc_gust']:.3f}, "
              f"crosswind {s['auc_cross']:.3f}  ({s['delta']:+.3f}); "
              f"{s['better']}/{s['lifts']} lifts improve.\n")
        return 0

    if "--rose" in args:
        rid = args[args.index("--rose") + 1]
        rows = rose(_cells(rid))
        print(f"\nClosure rate by wind direction — {rid}\n")
        cut = windy_cut(_cells(rid)).get(rid, 0)
        print(f"  {'from':<10} {'days':>5} {'readings':>9} {'% shut':>8} "
              f"{'windy readings':>15} {'% shut when windy':>19}")
        for r in rows:
            wp = "—" if r["windy_pct_shut"] is None else f"{r['windy_pct_shut']:.0f}%"
            print(f"  {str(r['from_deg']) + '° ' + _compass(r['from_deg']):<10} "
                  f"{r['days']:>5} {r['n']:>9} {r['pct_shut']:>7.1f}% "
                  f"{r['windy_n']:>15} {wp:>19}")
        print("\n  Wind direction is where the wind comes FROM. Percentages are "
              "over lift-readings;\n  'days' is how many distinct dates the "
              f"sector rests on. 'Windy' means a gust of\n  {cut:.0f} km/h or "
              f"more — this resort's own windiest "
              f"{100 - int(WINDY_QUANTILE * 100)}% of days, not a fixed speed.\n")
        return 0

    d = analyse_all()
    print(f"\nDoes crosswind beat gust?   ({d['cells']} lift-snapshots, "
          f"{len(d['lifts'])} lifts with a bearing)\n")
    print(f"  {'resort':<16} {'lifts':>5} {'AUC gust':>9} {'AUC cross':>10} "
          f"{'gain':>7} {'better':>8} {'angle|windy':>12}")
    for r in d["resorts"]:
        fw = "—" if r["auc_frac_windy"] is None else f"{r['auc_frac_windy']:.3f}"
        print(f"  {r['resort_id']:<16} {r['lifts']:>5} {r['auc_gust']:>9.3f} "
              f"{r['auc_cross']:>10.3f} {r['delta']:>+7.3f} "
              f"{str(r['better']) + '/' + str(r['lifts']):>8} {fw:>12}")
    tot = by_resort(d["lifts"])
    pairs = sum(r["pairs"] for r in tot)
    if pairs:
        g = sum(r["auc_gust"] * r["pairs"] for r in tot) / pairs
        c = sum(r["auc_cross"] * r["pairs"] for r in tot) / pairs
        print(f"\n  All resorts: gust {g:.3f}, crosswind {c:.3f} ({c - g:+.3f}).")
        wp = sum(r["windy_pairs"] for r in tot)
        if wp:
            f = sum(r["auc_frac_windy"] * r["windy_pairs"]
                    for r in tot if r["auc_frac_windy"] is not None) / wp
            print(f"  Angle alone, on windy days only: {f:.3f} "
                  f"({wp} shut-vs-running pairs).")
            # A single pooled number can hide a split verdict, so count which
            # side of a coin flip each lift actually landed on.
            tested = [l for l in d["lifts"] if l["auc_frac_windy"] is not None]
            up = sum(1 for l in tested if l["auc_frac_windy"] > 0.5)
            print(f"  Of {len(tested)} lifts tested individually, {up} score "
                  f"above 0.5 and {len(tested) - up} below.")
    print("\n  AUC is the chance a shut reading carried more wind than a running "
          "one, measured\n  within each lift and averaged by how many "
          "shut-vs-running pairs it contributes.\n  0.5 is a coin flip. Both wind "
          "columns use the same gust, so any gain is\n  direction alone. "
          "'angle|windy' is the fair test: among each resort's windiest "
          f"{100 - int(WINDY_QUANTILE * 100)}%\n  of days, where strength is "
          "roughly held fixed, does the angle across the\n  cable still "
          "separate shut from running?\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
