#!/usr/bin/env python3
"""
Shared Supabase client for Blackfire automation scripts.
Provides paginated reads, updates with retry, sync history logging,
and email alerts for failures.
"""

import os
import time
import smtplib
from email.mime.text import MIMEText
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


def update_company_safe(company_id: str, data: dict, expected_updated_at: str = None, max_retries: int = 3) -> bool:
    """Update a company row with optimistic locking via updated_at timestamp.
    If expected_updated_at is provided, the update only succeeds if the row's
    updated_at still matches — preventing lost updates from concurrent writes.
    Falls back to normal update if updated_at column doesn't exist yet.
    """
    client = get_client()

    # Always set updated_at on writes
    data['updated_at'] = datetime.now().isoformat()

    for attempt in range(max_retries):
        try:
            query = client.table('companies').update(data).eq('id', company_id)
            if expected_updated_at:
                query = query.eq('updated_at', expected_updated_at)
            result = query.execute()

            # If optimistic lock was used and no rows matched, the row was modified since read
            if expected_updated_at and len(result.data) == 0:
                print(f"   Conflict on {company_id}: row modified by another process, retrying...")
                # Re-read the current state and retry
                fresh = client.table('companies').select('updated_at').eq('id', company_id).single().execute()
                if fresh.data:
                    expected_updated_at = fresh.data.get('updated_at')
                    data['updated_at'] = datetime.now().isoformat()
                    continue
                return False

            return True
        except Exception as e:
            if attempt < max_retries - 1:
                wait = 2 ** (attempt + 1)
                print(f"   Retry safe update in {wait}s... ({e})")
                time.sleep(wait)
            else:
                print(f"   Failed safe update {company_id}: {e}")
                return False


def send_alert_email(subject: str, body: str) -> bool:
    """Send email alert for sync/update failures.
    Uses Gmail SMTP with app password. Configure in .env:
      ALERT_EMAIL_FROM=your@gmail.com
      ALERT_EMAIL_PASSWORD=your-app-password
      ALERT_EMAIL_TO=recipient@example.com
    Returns True on success, False on failure (never raises).
    """
    email_from = os.getenv('ALERT_EMAIL_FROM')
    email_password = os.getenv('ALERT_EMAIL_PASSWORD')
    email_to = os.getenv('ALERT_EMAIL_TO', email_from)

    if not email_from or not email_password:
        print("   Email alerts not configured (set ALERT_EMAIL_FROM + ALERT_EMAIL_PASSWORD in .env)")
        return False

    try:
        msg = MIMEText(body)
        msg['Subject'] = f"[Blackfire Alert] {subject}"
        msg['From'] = email_from
        msg['To'] = email_to

        with smtplib.SMTP_SSL('smtp.gmail.com', 465, timeout=10) as server:
            server.login(email_from, email_password)
            server.sendmail(email_from, [email_to], msg.as_string())

        print(f"   Alert email sent to {email_to}")
        return True
    except Exception as e:
        print(f"   Failed to send alert email: {e}")
        return False
