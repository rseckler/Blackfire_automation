import os
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), '.env'))
from supabase import create_client
client = create_client(os.getenv('SUPABASE_URL'), os.getenv('SUPABASE_SERVICE_ROLE_KEY'))

r = client.table('company_events').select('*').execute()
print("Total events:", len(r.data))
for e in r.data[:10]:
    et = e.get('event_type', '?')
    ed = e.get('event_date', '?')
    desc = (e.get('description') or '?')[:50]
    cid = e.get('company_id', '?')[:8]
    print(f"  {et:10s} | {ed} | {desc} | company:{cid}")

print()
r2 = client.table('company_events').select('*, companies(id, name)').eq('event_type', 'ipo').execute()
print("IPO events:", len(r2.data))
for e in r2.data[:10]:
    c = e.get('companies') or {}
    name = c.get('name', '?')
    ed = e.get('event_date', 'NO DATE')
    desc = (e.get('description') or '')[:40]
    print(f"  {name:30s} | date: {ed} | {desc}")
