import sys, os
sys.path.insert(0, os.path.dirname(__file__))

from scraper.scrapers import cervinia

print("Fetching cervinia.it...", flush=True)
snap = cervinia.scrape("cervinia")
print(f"Error:  {snap.error}", flush=True)
print(f"Lifts:  {snap.lifts_open}/{snap.lifts_total} ({snap.pct_open}%)", flush=True)
print(f"Pistes: {len(snap.pistes)}", flush=True)

print("\n=== SUMMIT LINKS ===", flush=True)
for l in snap.lifts:
    if l.is_link:
        print(f"  [{l.status.upper()}] {l.name}", flush=True)

print("\n=== OPEN ===", flush=True)
for l in snap.lifts:
    if l.status == "open":
        print(f"  {l.name}", flush=True)

print("\n=== CLOSED ===", flush=True)
for l in snap.lifts:
    if l.status == "closed":
        print(f"  {l.name}", flush=True)
