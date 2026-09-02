"""Are a resort's closures nested — is there a fixed order in which lifts shut?

Layer 2 of the model rests on one claim: closures are approximately nested, so
a resort has a closure *order* (the wind-sensitive lifts go first) and knowing
how many lifts are running tells you *which* ones. That is one ordering plus a
threshold instead of a separate model per lift, and it degrades gracefully on
thin data — but only if the claim is true. This measures it.

The tool is Guttman scaling. Rank the lifts by how often they run, then predict
each snapshot from its open *count* alone: if k lifts are open, predict that the
k most reliable are the open ones. Errors are cells where that prediction is
wrong.

    python -m scraper.nested --report            # every resort with the data
    python -m scraper.nested --resort saas-fee   # + the closure ladder
    python -m scraper.nested --resort cervinia --all-kinds

Read CS, not CR. Reproducibility alone is inflated by lifts that are almost
always open — predict "open" for everything and a placid resort scores 95%.
Scalability measures the ordering against that do-nothing baseline, and it is
the number that decides whether layer 2 can be one ordering.
"""
import sys
from collections import defaultdict

from .db import cursor

# Same window as scraper.model, and for the same reason: outside it resorts
# wind down for reasons that have nothing to do with weather.
CORE_SEASON = ("12-15", "04-15")
# Guttman's conventional thresholds. CR is the weak test, CS the real one.
CR_GOOD, CS_GOOD = 0.90, 0.60
# A lift seen in only a handful of snapshots contributes noise, not ordering.
MIN_LIFT_READINGS = 10
MIN_SNAPSHOTS = 15
# The ordering is only tested by snapshots where some lifts ran and some did
# not. Below this many, CS rests on too few real observations to lean on.
MIN_MIXED_SNAPSHOTS = 20


def _matrices(resort_id: str | None = None, all_kinds: bool = False,
              core_season: bool = True) -> dict[str, tuple[dict, dict]]:
    """{resort: ({snapshot_id: {lift: is_open}}, {snapshot_id: date})}.

    Every resort in one query by default: the web view scores all thirteen at
    once, and thirteen round trips to a remote Postgres is the slow way to do
    that.

    Rows are snapshots rather than days: a wind hold declared at opening and
    lifted by lunchtime is two different states of the mountain, and collapsing
    them to a day would hide exactly the closures the project is about.
    """
    clauses = ["s.scrape_error IS NULL", "lr.status IN ('open', 'closed', 'hold')"]
    if not all_kinds:
        clauses.append("c.kind = 'lift'")
    if resort_id:
        clauses.append("c.resort_id = %(rid)s")
    if core_season:
        clauses.append("(TO_CHAR(s.snapshot_date, 'MM-DD') >= %(start)s"
                       " OR TO_CHAR(s.snapshot_date, 'MM-DD') <= %(end)s)")
    with cursor() as cur:
        cur.execute(f"""
            SELECT c.resort_id, s.id AS snapshot_id, s.snapshot_date, c.name,
                   (lr.status = 'open') AS is_open
            FROM lift_readings lr
            JOIN lifts l ON l.id = lr.lift_id
            JOIN lifts c ON c.id = COALESCE(l.alias_of, l.id)
            JOIN snapshots s ON s.id = lr.snapshot_id
            WHERE {' AND '.join(clauses)}
        """, {"rid": resort_id, "start": CORE_SEASON[0], "end": CORE_SEASON[1]})
        rows = cur.fetchall()

    mats = defaultdict(lambda: (defaultdict(dict), {}, defaultdict(int)))
    for r in rows:
        mat, dates, seen = mats[r["resort_id"]]
        # A lift read twice in one snapshot (two sources) counts once; 'closed'
        # wins, since a source that lists a lift as shut has seen something.
        prev = mat[r["snapshot_id"]].get(r["name"])
        mat[r["snapshot_id"]][r["name"]] = (
            r["is_open"] if prev is None else (prev and r["is_open"]))
        dates[r["snapshot_id"]] = r["snapshot_date"]
        seen[r["name"]] += 1

    out = {}
    for rid, (mat, dates, seen) in mats.items():
        keep = {n for n, c in seen.items() if c >= MIN_LIFT_READINGS}
        m = {sid: {n: v for n, v in row.items() if n in keep}
             for sid, row in mat.items()}
        m = {sid: row for sid, row in m.items() if len(row) >= 3}
        out[rid] = (m, {sid: dates[sid] for sid in m})
    return out


def _matrix(resort_id: str, all_kinds: bool = False, core_season: bool = True):
    """One resort's matrix and snapshot dates."""
    return _matrices(resort_id, all_kinds, core_season).get(resort_id, ({}, {}))


def _rates(mat: dict) -> dict[str, tuple[int, int]]:
    """{lift: (times open, times seen)}."""
    tally = defaultdict(lambda: [0, 0])
    for row in mat.values():
        for name, is_open in row.items():
            tally[name][1] += 1
            tally[name][0] += 1 if is_open else 0
    return {n: (v[0], v[1]) for n, v in tally.items()}


def closure_order(mat: dict) -> list[str]:
    """Lifts most likely to be shut first. Ties broken by name for determinism."""
    rates = _rates(mat)
    return sorted(rates, key=lambda n: (rates[n][0] / rates[n][1], n))


def score(mat: dict, order: list[str]) -> dict | None:
    """Guttman reproducibility and scalability of `order` against `mat`.

    Each snapshot is predicted from its open count alone. Lifts absent from a
    snapshot are skipped rather than imputed, so the ordering is applied to
    whichever lifts were actually reported that day.
    """
    if not mat:
        return None
    rank = {name: i for i, name in enumerate(order)}
    cells = errors = 0
    active_cells = active_errors = 0
    per_lift = defaultdict(int)

    for row in mat.values():
        present = [n for n in row if n in rank]
        if len(present) < 3:
            continue
        present.sort(key=lambda n: -rank[n])       # most reliable first
        k = sum(1 for n in present if row[n])
        mixed = 0 < k < len(present)
        for i, name in enumerate(present):
            predicted_open = i < k
            cells += 1
            if mixed:
                active_cells += 1
            if predicted_open != row[name]:
                errors += 1
                per_lift[name] += 1
                if mixed:
                    active_errors += 1
    if not cells:
        return None
    closed = sum(1 for row in mat.values() for v in row.values() if not v)
    mixed_snaps = sum(1 for row in mat.values()
                      if 0 < sum(1 for v in row.values() if v) < len(row))

    # Minimal marginal reproducibility: score of the do-nothing rule that always
    # predicts each lift's own most common state.
    rates = _rates(mat)
    modal = sum(max(o, n - o) for o, n in rates.values())
    total = sum(n for _, n in rates.values())
    mmr = modal / total if total else 0.0
    cr = 1 - errors / cells
    cs = (cr - mmr) / (1 - mmr) if mmr < 1 else 0.0
    return {
        "cells": cells, "errors": errors, "cr": cr, "mmr": mmr, "cs": cs,
        "pct_closed": 100 * closed / cells, "mixed_snaps": mixed_snaps,
        "active_cells": active_cells,
        "active_cr": 1 - active_errors / active_cells if active_cells else None,
        "per_lift": dict(per_lift),
    }


def holdout(mat: dict, dates: dict) -> dict | None:
    """Fit the order on half the days, score it on the other half.

    Days alternate rather than splitting front-to-back: a chronological split
    would put late season entirely in the test half and confound the ordering
    with the seasonal wind-down.
    """
    days = sorted({d for d in dates.values()})
    if len(days) < 8:
        return None
    test_days = set(days[1::2])
    train = {s: r for s, r in mat.items() if dates[s] not in test_days}
    test = {s: r for s, r in mat.items() if dates[s] in test_days}
    if not train or not test:
        return None
    return score(test, closure_order(train))


def _analyse_matrix(resort_id: str, mat: dict, dates: dict) -> dict | None:
    if len(mat) < MIN_SNAPSHOTS:
        return None
    order = closure_order(mat)
    fit = score(mat, order)
    if not fit:
        return None
    return {"resort_id": resort_id, "snapshots": len(mat), "lifts": len(order),
            "order": order, "rates": _rates(mat), "fit": fit,
            "holdout": holdout(mat, dates), "matrix": mat, "dates": dates}


def analyse(resort_id: str, all_kinds: bool = False) -> dict | None:
    mat, dates = _matrix(resort_id, all_kinds=all_kinds)
    return _analyse_matrix(resort_id, mat, dates)


def analyse_all(all_kinds: bool = False) -> dict[str, dict]:
    """Every resort that has enough per-lift readings to scale, in one query."""
    out = {}
    for rid, (mat, dates) in _matrices(all_kinds=all_kinds).items():
        a = _analyse_matrix(rid, mat, dates)
        if a:
            out[rid] = a
    return out


def _verdict(fit: dict) -> str:
    """The ordering is only observed on snapshots that are *partly* open.

    An all-open snapshot is free marks and an all-shut one is too, so a resort
    that barely closes can post a fine CS on a handful of real observations.
    Say so rather than let the verdict stand on its own.
    """
    if fit["cs"] >= CS_GOOD and fit["cr"] >= CR_GOOD:
        v = "nested"
    elif fit["cs"] >= 0.40:
        v = "partly"
    else:
        v = "not nested"
    return v + (" (thin)" if fit["mixed_snaps"] < MIN_MIXED_SNAPSHOTS else "")


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    args = sys.argv[1:]
    all_kinds = "--all-kinds" in args

    if "--resort" in args:
        rid = args[args.index("--resort") + 1]
        a = analyse(rid, all_kinds=all_kinds)
        if not a:
            print(f"\n  {rid}: not enough per-lift readings to scale.\n")
            return 1
        f = a["fit"]
        print(f"\nClosure ordering — {rid}"
              f"{'  (all kinds)' if all_kinds else ''}\n")
        print(f"  {a['snapshots']} snapshots x {a['lifts']} lifts = "
              f"{f['cells']} cells, {f['errors']} errors")
        print(f"  reproducibility  CR = {f['cr']:.3f}   (baseline MMR = {f['mmr']:.3f})")
        print(f"  scalability      CS = {f['cs']:.3f}   -> {_verdict(f)}")
        if f["active_cr"] is not None:
            print(f"  CR on the {f['active_cells']} cells in partly-open "
                  f"snapshots = {f['active_cr']:.3f}")
        h = a["holdout"]
        if h:
            print(f"  held-out days    CR = {h['cr']:.3f}, CS = {h['cs']:.3f} "
                  f"({h['cells']} cells)")
        print("\n  Closure ladder — first to shut at the top\n")
        print(f"  {'#':>3}  {'lift':<34} {'open':>6} {'seen':>5} {'errs':>5}")
        for i, name in enumerate(a["order"], 1):
            o, n = a["rates"][name]
            print(f"  {i:>3}  {name[:32]:<34} {100*o/n:>5.0f}% {n:>5} "
                  f"{f['per_lift'].get(name, 0):>5}")
        print()
        return 0

    print(f"\nNestedness of closures{'  (all kinds)' if all_kinds else ''} — "
          f"core season {CORE_SEASON[0]} to {CORE_SEASON[1]}\n")
    print(f"  {'resort':<16} {'snaps':>5} {'lifts':>5} {'shut':>6} {'mix':>4} "
          f"{'CR':>6} {'MMR':>6} {'CS':>6} {'CS(held)':>9}  verdict")
    for a in sorted(analyse_all(all_kinds=all_kinds).values(),
                    key=lambda a: -a["fit"]["cs"]):
        f, h = a["fit"], a["holdout"]
        held = f"{h['cs']:>9.3f}" if h else f"{'—':>9}"
        print(f"  {a['resort_id']:<16} {a['snapshots']:>5} {a['lifts']:>5} "
              f"{f['pct_closed']:>5.1f}% {f['mixed_snaps']:>4} "
              f"{f['cr']:>6.3f} {f['mmr']:>6.3f} {f['cs']:>6.3f} {held}  "
              f"{_verdict(f)}")
    print("\n  CR = share of cells the ordering gets right. MMR = share the "
          "do-nothing rule\n  gets right. CS = (CR-MMR)/(1-MMR): how much of the "
          "available headroom\n  the ordering captures. 'shut' is the share of "
          "cells closed; 'mix' counts the\n  partly-open snapshots, the only "
          "ones that test an ordering at all.\n"
          f"  Nested needs CR >= {CR_GOOD:.2f} and CS >= {CS_GOOD:.2f}; "
          f"(thin) means under {MIN_MIXED_SNAPSHOTS} mixed snapshots.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
