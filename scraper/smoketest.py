"""Read-only health check of every scraper.

Run before the season starts (and after any site redesign) to find scrapers
broken by summer HTML drift, without writing anything to the database:

    python -m scraper.smoketest              # all resorts
    python -m scraper.smoketest cervinia     # one resort
    python -m scraper.smoketest --primary    # only resorts with a primary scraper

Exit code is non-zero if any resort failed, so it can gate a deploy.
"""
import sys
import time

from .collect import load_resorts
from .scrapers import run_scraper

# Below this, a scraper that "worked" is probably parsing only part of the page.
SUSPICIOUS_LIFT_TOTAL = 3


def check(resort: dict) -> dict:
    result = {"id": resort["id"], "name": resort["name"],
              "scraper": resort.get("scraper", "bergfex")}
    started = time.time()
    try:
        snap = run_scraper(resort)
    except Exception as e:
        result.update(status="EXCEPTION", detail=str(e)[:70])
        return result
    finally:
        result["seconds"] = round(time.time() - started, 1)

    readings = {r.source: r for r in snap.source_readings}
    result["readings"] = readings

    if snap.error:
        result.update(status="FAIL", detail=snap.error[:70])
        return result

    # A primary scraper silently falling back to bergfex still yields data,
    # but means the primary is broken — surface it rather than call it a pass.
    fell_back = "fallback from" in (snap.source or "")
    if fell_back:
        result.update(status="FALLBACK",
                      detail=f"primary failed; bergfex gave {snap.lifts_open}/{snap.lifts_total}")
        return result

    if snap.lifts_total <= SUSPICIOUS_LIFT_TOTAL:
        result.update(status="SUSPECT",
                      detail=f"only {snap.lifts_total} lifts parsed")
        return result

    detail = f"{snap.lifts_open}/{snap.lifts_total} lifts"
    if snap.snow_depth_mountain_cm is not None:
        detail += f", snow {snap.snow_depth_mountain_cm}cm"

    # Cross-check the primary against bergfex where both are available.
    pcts = [100 * r.lifts_open / r.lifts_total
            for r in readings.values() if r.lifts_total]
    if len(pcts) >= 2 and max(pcts) - min(pcts) > 20:
        result.update(status="DIVERGED",
                      detail=" vs ".join(
                          f"{r.source} {r.lifts_open}/{r.lifts_total}"
                          for r in readings.values() if r.lifts_total))
        return result

    result.update(status="OK", detail=detail)
    return result


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    args = sys.argv[1:]
    resorts = load_resorts()
    if "--primary" in args:
        resorts = [r for r in resorts if r.get("scraper", "bergfex") != "bergfex"]
    named = next((a for a in args if not a.startswith("--")), None)
    if named:
        resorts = [r for r in resorts if r["id"] == named]
        if not resorts:
            print(f"No resort found with id '{named}'")
            return 2

    print(f"\nSmoke-testing {len(resorts)} scraper(s) — nothing is written to the database.\n")
    results = []
    for resort in resorts:
        res = check(resort)
        results.append(res)
        icon = {"OK": "  ok  ", "FAIL": " FAIL ", "EXCEPTION": " EXC  ",
                "FALLBACK": " FBCK ", "SUSPECT": " SUSP ", "DIVERGED": " DIVG "}[res["status"]]
        print(f"[{icon}] {res['name'][:28]:<28} {res['scraper']:<12} "
              f"{res['seconds']:>5}s  {res['detail']}")

    counts = {}
    for r in results:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    print("\n  " + "   ".join(f"{k}: {v}" for k, v in sorted(counts.items())))

    bad = [r for r in results if r["status"] in ("FAIL", "EXCEPTION", "FALLBACK", "SUSPECT")]
    if bad:
        print(f"\n  {len(bad)} scraper(s) need attention: "
              f"{', '.join(r['id'] for r in bad)}\n")
        return 1
    print("\n  All scrapers healthy.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
