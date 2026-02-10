#!/usr/bin/env python3
"""
Shared Supabase client for Blackfire automation scripts.
Provides paginated reads, updates with retry, and sync history logging.
"""

import os
import time
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

try:
    from supabase import create_client, Client
except ImportError:
    import sys
    print("Installing supabase...")
    os.system(f"{sys.executable} -m pip install supabase")
    from supabase import create_client, Client

_client: Client = None


def get_client() -> Client:
    """Return a singleton Supabase client."""
    global _client
    if _client is None:
        url = os.getenv('SUPABASE_URL')
        key = os.getenv('SUPABASE_SERVICE_ROLE_KEY')
        if not url or not key:
            raise ValueError("Missing SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY in .env")
        _client = create_client(url, key)
    return _client


def get_all_companies(select_fields: str = '*') -> list:
    """Fetch all companies with pagination (Supabase default limit = 1000)."""
    client = get_client()
    all_rows = []
    page_size = 1000
    offset = 0

    while True:
        response = client.table('companies') \
            .select(select_fields) \
            .range(offset, offset + page_size - 1) \
            .execute()

        batch = response.data
        all_rows.extend(batch)

        if len(batch) < page_size:
            break
        offset += page_size

    return all_rows


def update_company(company_id: str, data: dict, max_retries: int = 3) -> bool:
    """Update a single company row with retry on failure."""
    client = get_client()

    for attempt in range(max_retries):
        try:
            client.table('companies').update(data).eq('id', company_id).execute()
            return True
        except Exception as e:
            if attempt < max_retries - 1:
                wait = 2 ** (attempt + 1)
                print(f"   Retry update in {wait}s... ({e})")
                time.sleep(wait)
            else:
                print(f"   Failed to update {company_id}: {e}")
                return False


def upsert_company(data: dict, max_retries: int = 3) -> bool:
    """Upsert a company row (conflict on satellog)."""
    client = get_client()

    for attempt in range(max_retries):
        try:
            client.table('companies').upsert(
                data, on_conflict='satellog'
            ).execute()
            return True
        except Exception as e:
            if attempt < max_retries - 1:
                wait = 2 ** (attempt + 1)
                print(f"   Retry upsert in {wait}s... ({e})")
                time.sleep(wait)
            else:
                print(f"   Failed to upsert: {e}")
                return False


def insert_companies(data_list: list, max_retries: int = 3) -> bool:
    """Batch insert new companies."""
    client = get_client()

    for attempt in range(max_retries):
        try:
            client.table('companies').insert(data_list).execute()
            return True
        except Exception as e:
            if attempt < max_retries - 1:
                wait = 2 ** (attempt + 1)
                print(f"   Retry insert in {wait}s... ({e})")
                time.sleep(wait)
            else:
                print(f"   Failed to insert: {e}")
                return False


def log_sync_history(stats: dict) -> None:
    """Write a row to the sync_history table."""
    client = get_client()

    duration = 0
    if stats.get('start_time') and stats.get('end_time'):
        duration = int((stats['end_time'] - stats['start_time']).total_seconds())

    total = stats.get('updates', 0) + stats.get('creates', 0)
    if stats.get('stocks_processed'):
        success_rate = round(
            (stats.get('stocks_updated', 0) / stats['stocks_processed']) * 100, 1
        ) if stats['stocks_processed'] > 0 else 0.0
    else:
        success_rate = 100.0 if total > 0 else 0.0

    if stats.get('success'):
        status = 'Success'
    elif stats.get('error_message'):
        status = 'Failed'
    else:
        status = 'Partial'

    row = {
        'name': stats.get('name', f"Sync {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"),
        'sync_date': datetime.now().isoformat(),
        'status': status,
        'excel_rows': stats.get('excel_rows', 0),
        'db_companies': stats.get('db_companies', 0),
        'updates': stats.get('updates', 0),
        'creates': stats.get('creates', 0),
        'duration_seconds': duration,
        'success_rate': success_rate,
        'error_message': str(stats['error_message'])[:2000] if stats.get('error_message') else None,
    }

    try:
        client.table('sync_history').insert(row).execute()
        print("   Logged to sync_history")
    except Exception as e:
        print(f"   Failed to log sync history: {e}")
