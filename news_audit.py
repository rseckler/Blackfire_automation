import os
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), '.env'))
from supabase import create_client
from collections import Counter

client = create_client(os.getenv('SUPABASE_URL'), os.getenv('SUPABASE_SERVICE_ROLE_KEY'))

# News by source
r = client.table('company_news').select('source, company_id, fetched_at, sentiment').execute()
print("=== NEWS BY SOURCE ===")
sources = Counter(n.get('source', 'unknown') for n in r.data)
for s, cnt in sources.most_common(30):
    print(f"  {cnt:>5}  {s}")
print(f"  TOTAL: {len(r.data)} articles")

print()
print("=== NEWS BY DATE ===")
dates = Counter()
for n in r.data:
    fa = n.get('fetched_at', '')
    if fa:
        dates[fa[:10]] += 1
for d, cnt in sorted(dates.items(), reverse=True)[:10]:
    print(f"  {d}: {cnt} articles")

print()
print("=== SENTIMENT DISTRIBUTION ===")
sentiments = Counter(n.get('sentiment', 'none') for n in r.data)
for s, cnt in sentiments.most_common():
    print(f"  {s:>10}: {cnt}")

print()
matched = sum(1 for n in r.data if n.get('company_id'))
print(f"=== COMPANY MATCHING ===")
print(f"  With company_id: {matched}")
print(f"  Without:         {len(r.data) - matched}")
print(f"  Match rate:      {round(matched/max(len(r.data),1)*100,1)}%")

print()
print("=== EVENTS ===")
e = client.table('company_events').select('event_type').execute()
etypes = Counter(ev.get('event_type', '?') for ev in e.data)
for t, cnt in etypes.most_common():
    print(f"  {t}: {cnt}")
print(f"  TOTAL: {len(e.data)} events")
