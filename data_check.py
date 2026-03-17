#!/usr/bin/env python3
import os
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), '.env'))

from supabase import create_client
url = os.getenv('SUPABASE_URL')
key = os.getenv('SUPABASE_SERVICE_ROLE_KEY')
client = create_client(url, key)

print("=== SUPABASE DATA INVENTORY ===\n")
for table in ["companies","company_news","company_events","company_scores","alerts","briefings","notes","sync_history","watchlist"]:
    try:
        r = client.table(table).select("id", count="exact", head=True).execute()
        print(f"  {table:20s}: {r.count or 0:>6} rows")
    except Exception as e:
        print(f"  {table:20s}: ERROR - {str(e)[:60]}")

print("\n=== LATEST DATA ===\n")

r = client.table("company_scores").select("computed_at,score_type,score_value").order("computed_at", desc=True).limit(5).execute()
if r.data:
    print(f"  Scores last:    {r.data[0]['computed_at']}")
    for s in r.data[:5]:
        st = s['score_type']
        sv = s['score_value']
        print(f"    {st:20s} = {sv}")
else:
    print("  Scores:         EMPTY")

r = client.table("alerts").select("created_at,alert_type,title,priority").order("created_at", desc=True).limit(5).execute()
if r.data:
    print(f"  Alerts last:    {r.data[0]['created_at']}")
    for a in r.data[:5]:
        t = a.get("title") or "no title"
        p = a.get("priority") or "?"
        print(f"    [{p:6s}] {a['alert_type']:18s} {t[:40]}")
else:
    print("  Alerts:         EMPTY")

r = client.table("briefings").select("generated_at,model_used").order("generated_at", desc=True).limit(3).execute()
if r.data:
    for b in r.data:
        m = b.get("model_used") or "?"
        print(f"  Briefing:       {b['generated_at']} ({m})")
else:
    print("  Briefings:      EMPTY")

r = client.table("company_news").select("fetched_at,source").order("fetched_at", desc=True).limit(1).execute()
if r.data:
    src = r.data[0].get("source") or "?"
    print(f"  News last:      {r.data[0]['fetched_at']} ({src})")

r = client.table("company_events").select("created_at,event_type").order("created_at", desc=True).limit(1).execute()
if r.data:
    print(f"  Events last:    {r.data[0]['created_at']} ({r.data[0]['event_type']})")

r = client.table("companies").select("id", count="exact", head=True).not_.is_("current_price", "null").gt("current_price", 0).execute()
print(f"  Companies w/price: {r.count or 0}")

r = client.table("company_scores").select("company_id", count="exact", head=True).eq("score_type", "overall").execute()
print(f"  Companies scored:  {r.count or 0}")
