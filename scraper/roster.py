"""Detect changes in a resort's lift roster between two points in time.

Resorts add, retire and rename lifts between seasons, and a rename silently
splits a lift's history in two — which matters most for the lifts we care
about, like the Cervinia–Zermatt links.

This module only ever *reports*. Renames are suggestions for a human to
confirm, never applied automatically: season-1 data is full of name pairs
like 'Loze A'/'Loze B' and 'Rüfikopfbahn I'/'Rüfikopfbahn II' that are
genuinely different lifts, so auto-merging would corrupt the record.

    python -m scraper.roster                    # every resort, vs 30+ days ago
    python -m scraper.roster cervinia --gap 60
    python -m scraper.roster --merge 412 517    # 412 was renamed to 517
"""
import difflib
import sys

from .db import cursor

# Only pairs at least this similar are worth a human's attention.
RENAME_THRESHOLD = 0.6


def _lift_map(resort_id: str, snapshot_date) -> dict[str, int]:
    """Lift name -> id for one resort-day (ignoring bergfex synthetic lifts)."""
    with cursor() as cur:
        cur.execute("""
            SELECT DISTINCT l.name, l.id
            FROM lift_readings lr
            JOIN lifts l ON l.id = lr.lift_id
            JOIN snapshots s ON s.id = lr.snapshot_id
            WHERE l.resort_id = %s AND s.snapshot_date = %s
              AND l.name NOT LIKE 'lift\\_%%'
        """, (resort_id, snapshot_date))
        return {r["name"]: r["id"] for r in cur.fetchall()}


def _reference_dates(resort_id: str, gap_days: int):
    """Latest snapshot date, and the most recent one at least `gap` days before.

    The gap is what makes this useful at season start: it reaches back across
    the summer rather than to yesterday, when the roster is unchanged.
    """
    with cursor() as cur:
        cur.execute("""
            SELECT DISTINCT s.snapshot_date
            FROM snapshots s
            JOIN lift_readings lr ON lr.snapshot_id = s.id
            JOIN lifts l ON l.id = lr.lift_id AND l.name NOT LIKE 'lift\\_%%'
            WHERE s.resort_id = %s
            ORDER BY s.snapshot_date DESC
        """, (resort_id,))
        dates = [r["snapshot_date"] for r in cur.fetchall()]
    if len(dates) < 2:
        return None, None
    latest = dates[0]
    for d in dates[1:]:
        if (latest - d).days >= gap_days:
            return latest, d
    return latest, dates[-1]


def _suggest_renames(gone: list[str], added: list[str]) -> list[tuple[str, str, float]]:
    """Greedily pair disappeared names with the most similar new name."""
    pairs = []
    for old in gone:
        best, score = None, 0.0
        for new in added:
            r = difflib.SequenceMatcher(None, old.lower(), new.lower()).ratio()
            if r > score:
                best, score = new, r
        if best and score >= RENAME_THRESHOLD:
            pairs.append((old, best, score))
    # One-to-one: a new name can only stand in for a single old one.
    pairs.sort(key=lambda p: -p[2])
    used_new, used_old, out = set(), set(), []
    for old, new, score in pairs:
        if new in used_new or old in used_old:
            continue
        used_new.add(new)
        used_old.add(old)
        out.append((old, new, score))
    return out


def changes_for(resort_id: str, gap_days: int = 30) -> dict | None:
    latest, previous = _reference_dates(resort_id, gap_days)
    if not latest or not previous or latest == previous:
        return None
    now = _lift_map(resort_id, latest)
    before = _lift_map(resort_id, previous)
    if not now or not before:
        return None
    added = sorted(set(now) - set(before))
    gone = sorted(set(before) - set(now))
    return {
        "resort_id": resort_id,
        "latest": latest, "previous": previous,
        "added": added, "gone": gone,
        "ids": {**before, **now},
        "unchanged": len(set(now) & set(before)),
        "renames": _suggest_renames(gone, added),
    }


def all_changes(gap_days: int = 30) -> list[dict]:
    with cursor() as cur:
        cur.execute("SELECT id FROM resorts ORDER BY name")
        ids = [r["id"] for r in cur.fetchall()]
    out = []
    for rid in ids:
        c = changes_for(rid, gap_days)
        if c and (c["added"] or c["gone"]):
            out.append(c)
    return out


def merge_lift(old_id: int, new_id: int):
    """Record that lift `old_id` is the same physical lift as `new_id`."""
    with cursor() as cur:
        cur.execute("SELECT id, resort_id, name FROM lifts WHERE id IN (%s, %s)",
                    (old_id, new_id))
        rows = {r["id"]: r for r in cur.fetchall()}
        if old_id not in rows or new_id not in rows:
            raise ValueError("both lift ids must exist")
        if rows[old_id]["resort_id"] != rows[new_id]["resort_id"]:
            raise ValueError("lifts belong to different resorts")
        if old_id == new_id:
            raise ValueError("cannot alias a lift to itself")
        cur.execute("UPDATE lifts SET alias_of = %s WHERE id = %s", (new_id, old_id))
        # Re-point anything that already aliased the old lift.
        cur.execute("UPDATE lifts SET alias_of = %s WHERE alias_of = %s", (new_id, old_id))
    return rows[old_id]["name"], rows[new_id]["name"]


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    args = sys.argv[1:]

    if "--merge" in args:
        i = args.index("--merge")
        try:
            old_id, new_id = int(args[i + 1]), int(args[i + 2])
        except (IndexError, ValueError):
            print("usage: --merge <old_lift_id> <new_lift_id>")
            return 2
        old_name, new_name = merge_lift(old_id, new_id)
        print(f"Merged: {old_name!r} ({old_id}) now rolls up into {new_name!r} ({new_id}).")
        return 0

    gap = 30
    if "--gap" in args:
        i = args.index("--gap")
        gap = int(args[i + 1])
        args = args[:i] + args[i + 2:]

    named = next((a for a in args if not a.startswith("--")), None)
    reports = [changes_for(named, gap)] if named else all_changes(gap)
    reports = [r for r in reports if r]

    if not reports:
        print("\nNo lift roster changes found.\n")
        return 0

    print(f"\nLift roster changes (latest snapshot vs {gap}+ days earlier)\n")
    for c in reports:
        print(f"  {c['resort_id']}  {c['previous']} -> {c['latest']}  "
              f"({c['unchanged']} unchanged)")
        renamed_old = {o for o, _, _ in c["renames"]}
        renamed_new = {n for _, n, _ in c["renames"]}
        for old, new, score in c["renames"]:
            print(f"     possible rename ({score:.0%}): {old!r} -> {new!r}")
        for n in c["added"]:
            if n not in renamed_new:
                print(f"     added:   {n!r}")
        for n in c["gone"]:
            if n not in renamed_old:
                print(f"     gone:    {n!r}")
        print()
    print("  Renames are suggestions only — confirm before merging with:")
    print("    python -m scraper.roster --merge <old_lift_id> <new_lift_id>\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
