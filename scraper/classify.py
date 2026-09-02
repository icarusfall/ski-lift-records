"""Decide which roster rows are actually lifts.

Resort sites list more than lifts under "lifts": sector status for a whole
village or linked area, groomed routes, bike parks, kids' play areas. Cervinia
alone carries `parco giochi`, `percorso battuto fornet`, `principianti` and the
sector names `valtournenche`, `torgnon`, `chamois` — each with dozens of
readings, each counted in pct_lifts_open.

Tignes and Val d'Isère each carry a `TIGNES > VAL D'ISERE` row, which is not a
lift at all but a statement that the crossing can be made by any means — useful,
and an aggregate of the link lifts, so it must not be counted alongside them.

Measured, the effect of excluding all of this on average % open is small and not
consistently signed: Cervinia −0.5pt (its parks and sectors do sit above its
lifts), Saas-Fee +2.3pt (its sledging runs and race courses sit below). The
core-season wind drop barely moves. So this is worth doing for correctness and
for the map — a playground has no bearing and should not be modelled — but it
does not rescue the headline metric, and it was wrong to claim it would.

    python -m scraper.classify --propose            # all resorts, no writes
    python -m scraper.classify --propose cervinia
    python -m scraper.classify --apply              # write kind/lift_type
    python -m scraper.classify --prune              # drop settled review entries
    python -m scraper.classify --impact             # what recounting would do
    python -m scraper.classify --recount            # rewrite snapshot counts

Classification is deliberately not fully automatic — the same discipline as
OSM name matching. Rules catch the confident cases; everything else lands in
config/lift_kinds.json as "review" for a human to settle, and a manual entry
always wins. The default for anything unrecognised is 'lift', so a row can
never silently drop out of the record.
"""
import json
import os
import re
import sys

import psycopg2.extras

from .db import cursor

OVERRIDES_PATH = os.path.join("config", "lift_kinds.json")

# Kinds that are not lifts and must not count toward pct_lifts_open.
#
# `crossing` is the interesting one: `TIGNES > VAL D'ISERE` is not a lift but a
# statement that the crossing can be made *by any means*. It must be excluded
# from lift counts twice over — it is not a lift, and it is an aggregate of the
# link lifts, so counting it double-counts them. But it is the most directly
# useful row in the dataset: "can I ski across today?" is the question, and the
# resort answering it itself beats inferring it from individual lift statuses.
NON_LIFT_KINDS = {"sector", "piste", "park", "service", "crossing"}

# Name patterns, checked against an accent-folded lowercase name. Kept
# conservative: a false "not a lift" silently deletes a real closure signal,
# which is far worse than leaving a row for review.
RULES: list[tuple[str, str]] = [
    # Terrain parks and children's areas — open in weather that shuts lifts.
    (r"\bbike\s*park\b", "park"),
    (r"\bsnow\s*park\b", "park"),
    (r"\bfun\s*park\b", "park"),
    (r"\bsnowpark\b", "park"),
    (r"\bfunpark\b", "park"),
    (r"\bboardercross\b", "park"),
    (r"\bhalfpipe\b", "park"),
    (r"\bairbag\b", "park"),
    (r"\bparco giochi\b", "park"),
    (r"\barea giochi\b", "park"),
    (r"\bbaby park\b", "park"),
    (r"\bkinderland\b", "park"),
    (r"\bkids?\s*park\b", "park"),
    (r"\bfamily park\b", "park"),
    (r"\bplayground\b", "park"),
    (r"\bjardin des neiges\b", "park"),
    (r"\bvillage des enfants\b", "park"),

    # Pistes, routes and non-lift activities.
    (r"\bpercorso battuto\b", "piste"),
    (r"^pista\b", "piste"),
    (r"^piste\b", "piste"),
    (r"\bitinerario\b", "piste"),
    (r"\btalabfahrt\b", "piste"),
    (r"\brodelbahn\b", "piste"),
    (r"\bsnowtubing\b", "piste"),
    (r"\bsnowshoe\b", "piste"),
    (r"\bwinterwanderweg\b", "piste"),
    (r"\bski de fond\b", "piste"),
    (r"\blanglauf\b", "piste"),

    # Timed race courses and sledging runs — scored as "open" all winter.
    (r"\bslalom\b", "piste"),
    (r"\briesenslalom\b", "piste"),
    (r"\bfun slope\b", "piste"),
    (r"schlitteln", "piste"),
    (r"schlittelweg", "piste"),
    (r"\brodeln\b", "piste"),

    # Beginner zones (an area, not the lift serving it).
    (r"\bprincipianti\b", "piste"),
    (r"\bcampo scuola\b", "piste"),
    (r"\bzona debuttanti\b", "piste"),

    # Services.
    (r"\bristorante\b", "service"),
    (r"\brestaurant\b", "service"),
    (r"\bnoleggio\b", "service"),
    (r"\bskibus\b", "service"),
    (r"\bnavetta\b", "service"),
    (r"\bparcheggio\b", "service"),
    (r"\bparking\b", "service"),
]

# Words that positively mark a row as a lift even with no OSM match. Italian
# `tappeto` and French `tapis` are magic carpets — real lifts, and easily
# mistaken for play areas by a looser rule.
LIFT_WORDS = re.compile(
    r"\b(seggiovia|funivia|cabinovia|telecabina|telesiege|telesiège|téléphérique|"
    r"telepherique|funicular|funiculaire|funivia|skilift|sessellift|gondelbahn|"
    r"bahn|lift|express|tappeto|tapis|magic carpet|chairlift|gondola|cable car|"
    r"teleski|sciovia|manovia|tsd|tsf|tsc|ts|tp|tph|tc|tk|tmx|dmc)\b"
)

# German and Austrian resorts write the lift type as a suffix rather than a
# separate word — Hexenbodenbahn, Zürserseebahn, Übungslift — so the word-
# boundary test in LIFT_WORDS never fires. This is the compound-name pattern
# that OSM matching also trips over (Rüfikopfbahn vs OSM's Rüfikopf).
LIFT_SUFFIX = re.compile(r"(bahn|lift|lifte|seilbahn|sessellift|schlepper)$")

# Physical type from the name, in OSM's vocabulary so both sources agree.
# French rosters lead with the type — TK is a téléski (drag), TS a télésiège
# (chair), TPH a téléphérique — which is the only type signal available for the
# rows OSM never matched. Order matters: TSD/TSF before TS, TCD before TC.
TYPE_PREFIXES: list[tuple[str, str]] = [
    (r"^(tsd|tsf|tsc|ts)[ .\-]", "chair_lift"),
    (r"^(tcd|tc)[ .\-]", "gondola"),
    (r"^(tkd|tke|tk)[ .\-]", "drag_lift"),
    (r"^(tph)[ .\-]", "cable_car"),
    (r"^(tmx)[ .\-]", "mixed_lift"),
    (r"^(dmc)[ .\-]", "gondola"),
    (r"^(tapis|tappeto)\b", "magic_carpet"),
    (r"^(funiculaire|funicolare|funival)\b", "funicular"),
    # Italian and German write the type as a word or a suffix instead.
    (r"\bseggiovia\b", "chair_lift"),
    (r"\bcabinovia\b|\btelecabina\b", "gondola"),
    (r"\bfunivia\b", "cable_car"),
    (r"\bsciovia\b|\bmanovia\b", "drag_lift"),
    (r"sessellift$|sesselbahn$", "chair_lift"),
    (r"gondelbahn$", "gondola"),
    (r"seilbahn$", "cable_car"),
    (r"schlepplift$|schlepper$", "drag_lift"),
]

_FOLD = str.maketrans("àáâãäåèéêëìíîïòóôõöùúûüýÿñç", "aaaaaaeeeeiiiiooooouuuuyync")


def fold(name: str) -> str:
    return name.lower().strip().translate(_FOLD)


def load_overrides() -> dict:
    if not os.path.exists(OVERRIDES_PATH):
        return {}
    with open(OVERRIDES_PATH, encoding="utf-8") as fh:
        return json.load(fh)


def save_overrides(data: dict):
    os.makedirs(os.path.dirname(OVERRIDES_PATH), exist_ok=True)
    with open(OVERRIDES_PATH, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False, sort_keys=True)
        fh.write("\n")


def _sector_names() -> set:
    """Resort and village names, which sites use as sector-status rows.

    A row called `valtournenche` inside Cervinia's roster is the status of a
    whole linked area, not a lift.
    """
    names = set()
    path = os.path.join("config", "resorts.json")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            for r in json.load(fh):
                names.add(fold(r["id"].replace("-", " ")))
                names.add(fold(r["name"]))
                for part in re.split(r"[/&,]| - ", r["name"]):
                    if len(part.strip()) > 3:
                        names.add(fold(part))
    return names


def physical_type(name: str, aerialway: str | None) -> str | None:
    """OSM's type where a way is matched, else what the name declares."""
    if aerialway:
        return aerialway
    f = fold(name)
    for pattern, kind in TYPE_PREFIXES:
        if re.search(pattern, f):
            return kind
    return None


def tokens(name: str) -> set:
    """Meaningful words in a folded name, ignoring short connectives."""
    return {t for t in re.split(r"[^a-z0-9]+", fold(name)) if len(t) >= 4}


def osm_candidate(name: str, ways: list[str]) -> str | None:
    """An unclaimed OSM way whose name contains this lift's, or vice versa.

    Whole-string similarity misses compounds and endpoint pairs — `furggsattel`
    against `Furggsattel Gletscherbahn`, `goillet` against `Lago Goillet`,
    `trockener steg - klein matterhorn` against the same plus
    `Glacier Ride`. Token containment catches them, and finding an aerialway
    that plausibly *is* this row is strong evidence the row is a lift.
    """
    mine = tokens(name)
    if not mine:
        return None
    for way in ways:
        theirs = tokens(way)
        if not theirs:
            continue
        # One name's words must be wholly contained in the other's, and the
        # smaller side needs real substance — a single short token matching
        # is how `Golf` becomes `Olaf`.
        small, large = (mine, theirs) if len(mine) <= len(theirs) else (theirs, mine)
        if small <= large and sum(len(t) for t in small) >= 6:
            return way
    return None


def classify(name: str, has_geometry: bool, sectors: set,
             ways: list[str] | None = None,
             is_link: bool = False) -> tuple[str, str]:
    """Return (kind, reason). Manual overrides are applied by the caller."""
    f = fold(name)

    # An OSM aerialway match is the strongest evidence there is: something that
    # carries people uphill exists on this line. It must outrank the name rules,
    # because a drag lift is routinely named after what it serves — Val d'Isère's
    # `TK SNOWPARK` is a téléski (85 readings), not a terrain park, and Cervinia
    # and Les Arcs both have an OSM-matched `Snowpark` lift too.
    if has_geometry:
        return "lift", "osm match"

    # With no such confirmation, an explicit non-lift name decides it — ahead of
    # the link flag, because `bike park sez. cime bianche laghi` is flagged
    # is_link from the real lift beside it and would otherwise be claimed as one.
    for pattern, kind in RULES:
        if re.search(pattern, f):
            return kind, f"rule /{pattern}/"

    # Two resort names joined is a crossing, not a lift: `TIGNES > VAL D'ISERE`,
    # `VAL D'ISERE - TIGNES`. Both ends must be known resorts and there must be
    # no matched aerialway, so a gondola named after the places it joins is not
    # caught by this.
    parts = [p.strip() for p in re.split(r"[>/–—]|\s-\s|\bto\b", f) if p.strip()]
    if len(parts) >= 2 and all(p in sectors for p in parts):
        return "crossing", "joins two resorts"

    # Cross-resort links are lifts, and they are the most valuable rows in the
    # dataset — Cervinia's five link lifts are the season-1 headline. A link is
    # named after the resort it reaches, so the sector rule below would
    # otherwise delete exactly the rows the project exists to measure.
    #
    # Except when the link row is a bare resort name with no aerialway behind
    # it: `cervinia → zermatt` could equally be the link lift or the same
    # by-any-means crossing statement as Tignes'. That distinction decides how
    # the season-1 "Zermatt connection 80%" figure should be read, so it is a
    # decision for a human, not a rule.
    if is_link:
        if f in sectors and not has_geometry:
            return "review", "link lift, or a by-any-means crossing?"
        return "lift", "cross-resort link"

    # A row named after a resort or village is ambiguous, not settled: tested
    # against season 1, `cervinia`, `valtournenche`, `torgnon` and `zermatt`
    # were each closed on days when a lift inside their own area was open —
    # which a sector aggregate cannot be. So this goes to a human, never
    # straight to 'sector'.
    if f in sectors:
        return "review", "resort or village name — lift or area?"

    if LIFT_WORDS.search(f):
        return "lift", "lift-type word"

    if LIFT_SUFFIX.search(f.replace(" ", "")):
        return "lift", "compound lift suffix"

    candidate = osm_candidate(name, ways or [])
    if candidate:
        return "lift", f"osm candidate: {candidate}"

    return "review", "no signal"


def _rows(resort: str | None):
    sql = """
        SELECT l.id, l.resort_id, l.name, l.kind, l.is_link,
               -- A lift can sit in two resorts' overlapping search boxes and so
               -- carry several geometry rows (`plan maison` has four). Reduce
               -- to one value per lift, or every count here is inflated.
               (SELECT g.aerialway FROM lift_geometry g
                 WHERE g.lift_id = l.id ORDER BY g.match_score DESC NULLS LAST
                 LIMIT 1) AS aerialway,
               (SELECT COUNT(*) FROM lift_readings lr
                 WHERE lr.lift_id = l.id) AS readings
        FROM lifts l
        WHERE l.alias_of IS NULL
    """
    params = []
    if resort:
        sql += " AND l.resort_id = %s"
        params.append(resort)
    sql += " ORDER BY l.resort_id, l.name"
    with cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def _free_ways() -> dict:
    """Unclaimed OSM way names per resort, for candidate lookup."""
    with cursor() as cur:
        cur.execute("""
            SELECT resort_id, name FROM lift_geometry
            WHERE lift_id IS NULL AND name IS NOT NULL AND name <> ''
        """)
        out: dict[str, list[str]] = {}
        for r in cur.fetchall():
            out.setdefault(r["resort_id"], []).append(r["name"])
        return out


def _decide(rows, overrides, sectors):
    """Attach a kind to every row: manual override first, then rules."""
    ways = _free_ways()
    out = []
    for r in rows:
        manual = overrides.get(r["resort_id"], {}).get(r["name"])
        if manual and manual != "review":
            kind, reason = manual, "manual"
        else:
            kind, reason = classify(r["name"], bool(r["aerialway"]), sectors,
                                    ways.get(r["resort_id"], []), r["is_link"])
        out.append((r, kind, reason))
    return out


def propose(resort: str | None = None):
    overrides = load_overrides()
    sectors = _sector_names()
    decided = _decide(_rows(resort), overrides, sectors)

    by_kind: dict[str, list] = {}
    for r, kind, reason in decided:
        by_kind.setdefault(kind, []).append((r, reason))

    print(f"\n  {len(decided)} roster rows\n")
    for kind in ("lift", "crossing", "sector", "piste", "park", "service", "review"):
        items = by_kind.get(kind, [])
        if not items:
            continue
        print(f"  {kind.upper()}  ({len(items)})")
        if kind == "lift":
            print(f"    ... {len(items)} rows")
        else:
            for r, reason in items:
                print(f"    {r['resort_id']:<14} {r['name'][:42]:<44}"
                      f"{r['readings']:>5} readings   [{reason}]")
        print()

    # Anything unsettled goes into the overrides file for a human decision,
    # preserving whatever is already there.
    new = 0
    for r, kind, _ in decided:
        if kind != "review":
            continue
        bucket = overrides.setdefault(r["resort_id"], {})
        if r["name"] not in bucket:
            bucket[r["name"]] = "review"
            new += 1
    if new:
        save_overrides(overrides)
        print(f"  {new} row(s) added to {OVERRIDES_PATH} as \"review\".")
        print("  Edit them to one of: lift, sector, piste, park, service.\n")
    else:
        print("  Nothing new needs review.\n")


def prune():
    """Drop override entries the rules now reproduce on their own.

    The file is a worklist, so it should hold only what a rule cannot settle.
    An entry is removed only when classifying *without* it gives the same
    answer — so a decision the rules would disagree with is always kept, and
    pruning can never quietly revert one. A stale `"review"` on a row a newer
    rule has since settled goes the same way.
    """
    overrides = load_overrides()
    sectors = _sector_names()
    ways = _free_ways()

    dropped, kept = [], 0
    for r in _rows(None):
        entry = overrides.get(r["resort_id"], {}).get(r["name"])
        if entry is None:
            continue
        auto, reason = classify(r["name"], bool(r["aerialway"]), sectors,
                                ways.get(r["resort_id"], []), r["is_link"])
        # An entry earns its place if it still asks a question ("review" that
        # the rules also cannot settle) or answers one the rules would get
        # wrong. It is redundant only when a rule now reaches the same verdict.
        settled_by_rule = entry == "review" and auto != "review"
        redundant_decision = entry != "review" and auto == entry
        if settled_by_rule or redundant_decision:
            del overrides[r["resort_id"]][r["name"]]
            dropped.append((r["resort_id"], r["name"], entry, auto, reason))
        else:
            kept += 1

    for resort in [k for k, v in overrides.items() if not v]:
        del overrides[resort]

    if dropped:
        save_overrides(overrides)
    print(f"\n  Dropped {len(dropped)} entry(ies); kept {kept}.")
    for rid, name, was, now, reason in dropped:
        print(f"    {rid:<13}{name[:34]:<36} {was} -> {now}  [{reason}]")
    print()


def apply(resort: str | None = None):
    overrides = load_overrides()
    sectors = _sector_names()
    decided = _decide(_rows(resort), overrides, sectors)

    # "review" is not a decision — such rows keep counting as lifts until a
    # human says otherwise, so nothing disappears by inaction. Writing 'lift'
    # rather than skipping them also undoes any earlier misclassification when
    # a rule is corrected, instead of leaving a stale kind behind.
    updates = [(("lift" if kind == "review" else kind), r["aerialway"],
                physical_type(r["name"], r["aerialway"]), r["id"])
               for r, kind, _ in decided]
    with cursor() as cur:
        psycopg2.extras.execute_batch(cur, """
            UPDATE lifts SET kind = %s,
                             osm_type = COALESCE(%s, osm_type),
                             lift_type = COALESCE(%s, lift_type)
            WHERE id = %s
        """, updates)
    counts: dict[str, int] = {}
    for _, kind, _ in decided:
        counts[kind] = counts.get(kind, 0) + 1
    print(f"\n  Wrote {len(updates)} row(s): "
          + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())) + "\n")


def _recount_sql() -> str:
    """Recompute a snapshot's lift counts over real lifts only.

    Mirrors ResortSnapshot.lifts_total: 'unknown' readings are excluded rather
    than counted as closures, so a parse failure cannot look like a bad day.
    """
    return """
        SELECT lr.snapshot_id,
               COUNT(*) FILTER (WHERE lr.status = 'open') AS opens,
               COUNT(*) AS total
        FROM lift_readings lr
        JOIN lifts raw ON raw.id = lr.lift_id
        JOIN lifts l ON l.id = COALESCE(raw.alias_of, raw.id)
        WHERE lr.status IN ('open', 'closed', 'hold', 'seasonal')
          AND l.kind = 'lift'
        GROUP BY lr.snapshot_id
    """


def impact():
    """Show what recounting would change, without writing anything."""
    with cursor() as cur:
        cur.execute(f"""
            WITH fresh AS ({_recount_sql()})
            SELECT s.resort_id,
                   COUNT(*) AS days,
                   AVG(s.pct_lifts_open) AS old_pct,
                   AVG(100.0 * f.opens / NULLIF(f.total, 0)) AS new_pct
            FROM snapshots s JOIN fresh f ON f.snapshot_id = s.id
            WHERE s.scrape_error IS NULL AND s.pct_lifts_open IS NOT NULL
            GROUP BY s.resort_id
            HAVING ABS(AVG(s.pct_lifts_open)
                       - AVG(100.0 * f.opens / NULLIF(f.total, 0))) > 0.05
            ORDER BY AVG(s.pct_lifts_open)
                     - AVG(100.0 * f.opens / NULLIF(f.total, 0)) DESC
        """)
        rows = cur.fetchall()
    if not rows:
        print("\n  No resort's average would change.\n")
        return
    print(f"\n  {'resort':<16}{'days':>6}{'now':>9}{'recounted':>11}{'change':>9}")
    for r in rows:
        old, new = float(r["old_pct"]), float(r["new_pct"])
        print(f"  {r['resort_id']:<16}{r['days']:>6}{old:>9.1f}{new:>11.1f}"
              f"{new - old:>+9.1f}")
    print()


def recount():
    """Rewrite stored snapshot counts. Destructive — impact() shows it first."""
    with cursor() as cur:
        cur.execute(f"""
            WITH fresh AS ({_recount_sql()})
            UPDATE snapshots s
               SET lifts_open = f.opens,
                   lifts_total = f.total,
                   pct_lifts_open = ROUND(100.0 * f.opens / NULLIF(f.total, 0), 1)
              FROM fresh f
             WHERE f.snapshot_id = s.id
               AND f.total > 0
        """)
        print(f"\n  Recounted {cur.rowcount} snapshot(s).\n")


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    args = sys.argv[1:]
    named = next((a for a in args if not a.startswith("--")), None)

    if "--propose" in args:
        propose(named)
        return 0
    if "--apply" in args:
        apply(named)
        return 0
    if "--prune" in args:
        prune()
        return 0
    if "--impact" in args:
        impact()
        return 0
    if "--recount" in args:
        recount()
        return 0
    print(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(main())
