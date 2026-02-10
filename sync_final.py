#!/usr/bin/env python3
"""
Excel -> Supabase Sync with automatic logging
"""

import os
import requests
import pandas as pd
from io import BytesIO
from dotenv import load_dotenv
from datetime import datetime

load_dotenv()

import supabase_helper

# Column Mapping (Excel column -> Supabase core field)
COLUMN_MAPPING = {
    'Company_Name': 'name',
    'Name': 'name'
}

# Core Supabase columns (everything else -> extra_data JSONB)
CORE_FIELDS = {'name', 'symbol', 'wkn', 'isin', 'satellog', 'current_price'}

# Protected Properties - NEVER overwrite these (managed by other scripts)
PROTECTED_PROPERTIES = {
    # Stock prices (managed by stock_price_updater.py)
    'Current_Price',
    'Currency',
    'Price_Change_Percent',
    'Price_Update',
    'Exchange',
    'Market_Status',
    'Day_High',
    'Day_Low',
    'Volume',
    'Market_Cap',
    # ISIN/WKN (managed by isin_wkn_updater.py)
    'ISIN',
    'WKN'
}


class SyncWithLogging:
    def __init__(self):
        self.dropbox_url = os.getenv('DROPBOX_URL')

        # Stats for logging
        self.stats = {
            'start_time': None,
            'end_time': None,
            'excel_rows': 0,
            'excel_columns': 0,
            'db_companies': 0,
            'updates': 0,
            'creates': 0,
            'success': False,
            'error_message': None
        }

    def map_column_name(self, excel_col):
        """Map Excel column to Supabase field"""
        return COLUMN_MAPPING.get(excel_col, excel_col)

    def download_and_parse(self):
        """Download and parse Excel"""
        print("\n  Downloading Excel...")

        try:
            response = requests.get(self.dropbox_url, timeout=60)
            if response.status_code != 200:
                self.stats['error_message'] = f"Dropbox download failed: {response.status_code}"
                return None

            print(f"   Downloaded {len(response.content)} bytes")

            print("\n  Parsing Excel...")
            df = pd.read_excel(BytesIO(response.content))

            self.stats['excel_rows'] = len(df)
            self.stats['excel_columns'] = len(df.columns)

            print(f"   Parsed {len(df)} rows, {len(df.columns)} columns")

            return df

        except Exception as e:
            print(f"   Error: {e}")
            self.stats['error_message'] = str(e)
            return None

    def get_existing_companies(self):
        """Get all existing companies from Supabase"""
        print("\n  Getting existing companies from Supabase...")

        try:
            companies = supabase_helper.get_all_companies(
                'id, name, satellog, symbol, wkn, isin, extra_data'
            )
            self.stats['db_companies'] = len(companies)
            print(f"   Found {len(companies)} companies in database")

            # Build lookup maps
            by_satellog = {}
            by_name = {}
            for company in companies:
                satellog = (company.get('satellog') or '').strip()
                name = (company.get('name') or '').strip()
                if satellog:
                    by_satellog[satellog] = company
                if name:
                    by_name[name] = company

            return {'by_satellog': by_satellog, 'by_name': by_name}

        except Exception as e:
            print(f"   Error: {e}")
            self.stats['error_message'] = str(e)
            return None

    def build_company_data(self, excel_row, identifier, satellog_value):
        """Build company data dict for Supabase"""
        company_data = {
            'name': identifier,
            'satellog': str(satellog_value).strip() if satellog_value and str(satellog_value) != 'nan' else identifier
        }

        extra_data = {}

        for excel_col, value in excel_row.items():
            if pd.isna(value) or value == '':
                continue

            # Convert date/time objects to ISO strings for JSON
            if hasattr(value, 'isoformat'):
                value = value.isoformat()

            mapped_col = self.map_column_name(excel_col)

            # Skip protected fields
            if mapped_col in PROTECTED_PROPERTIES:
                continue

            # Handle core fields
            if mapped_col in CORE_FIELDS:
                if mapped_col == 'symbol':
                    company_data['symbol'] = str(value).strip() if value else None
                elif mapped_col == 'wkn':
                    company_data['wkn'] = str(value).strip() if value else None
                elif mapped_col == 'isin':
                    company_data['isin'] = str(value).strip() if value else None
            else:
                # Everything else goes to extra_data JSONB
                extra_data[excel_col] = value

        if extra_data:
            company_data['extra_data'] = extra_data

        return company_data

    def merge_extra_data(self, new_data, existing_data):
        """Merge new extra_data with existing, preserving protected fields"""
        merged = (existing_data or {}).copy()
        for key, value in new_data.items():
            if key not in PROTECTED_PROPERTIES:
                merged[key] = value
        return merged

    def compare_data(self, df, existing):
        """Compare Excel vs Supabase"""
        print("\n  Comparing data...")

        identifier_col = df.columns[0]
        satellog_col = None

        # Check if there's an explicit 'satellog' column
        for col in df.columns:
            if col.lower() == 'satellog':
                satellog_col = col
                break

        # Determine name column
        if satellog_col and satellog_col == identifier_col:
            name_col = None
            for col in df.columns:
                if col in ('Name', 'Company_Name', 'name'):
                    name_col = col
                    break
        else:
            name_col = None

        to_update = []
        to_create = []
        skipped = 0

        for idx, row in df.iterrows():
            # Get satellog value
            if satellog_col:
                satellog_value = str(row[satellog_col]).strip()
            else:
                satellog_value = str(row[identifier_col]).strip()

            # Get display name
            if name_col:
                identifier = str(row[name_col]).strip()
                if not identifier or identifier == 'nan':
                    identifier = satellog_value
            else:
                identifier = satellog_value

            if not satellog_value or satellog_value == 'nan':
                skipped += 1
                continue

            # Match by satellog first, then by name
            existing_company = existing['by_satellog'].get(satellog_value) or existing['by_name'].get(identifier)

            company_data = self.build_company_data(row.to_dict(), identifier, satellog_value)

            if existing_company:
                to_update.append({
                    'id': existing_company['id'],
                    'data': company_data,
                    'existing_extra_data': existing_company.get('extra_data', {})
                })
            else:
                to_create.append(company_data)

        self.stats['updates'] = len(to_update)
        self.stats['creates'] = len(to_create)

        print(f"   Updates: {len(to_update)}")
        print(f"   Creates: {len(to_create)}")
        if skipped:
            print(f"   Skipped: {skipped}")

        return {'updates': to_update, 'creates': to_create}

    def update_companies(self, updates):
        """Update existing companies in Supabase"""
        print(f"\n  Updating {len(updates)} companies...")

        success = 0
        failed = 0

        for i, update in enumerate(updates):
            company_id = update['id']
            data = update['data']
            existing_extra = update.get('existing_extra_data', {})

            # Merge extra_data
            if 'extra_data' in data:
                data['extra_data'] = self.merge_extra_data(data['extra_data'], existing_extra)

            data['last_synced_at'] = datetime.now().isoformat()

            if supabase_helper.update_company(company_id, data):
                success += 1
                if success % 100 == 0:
                    print(f"   ... {success}/{len(updates)} done")
            else:
                failed += 1

        print(f"   Updated: {success}")
        if failed > 0:
            print(f"   Failed: {failed}")

        return success

    def create_companies(self, creates):
        """Create new companies in Supabase"""
        print(f"\n  Creating {len(creates)} companies...")

        if not creates:
            return 0

        if supabase_helper.insert_companies(creates):
            print(f"   Created {len(creates)} companies")
            return len(creates)
        else:
            print(f"   Failed to create companies")
            return 0

    def log_sync(self):
        """Log sync result to sync_history table"""
        print("\n  Logging sync result...")
        self.stats['name'] = f"Excel Sync {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        supabase_helper.log_sync_history(self.stats)

    def run(self):
        """Run complete sync with logging"""
        print("\n" + "="*70)
        print("  EXCEL -> SUPABASE SYNC")
        print("="*70)

        self.stats['start_time'] = datetime.now()

        try:
            # 1. Download & parse Excel
            df = self.download_and_parse()
            if df is None:
                self.stats['end_time'] = datetime.now()
                self.log_sync()
                return False

            # 2. Get existing companies from Supabase
            existing = self.get_existing_companies()
            if existing is None:
                self.stats['end_time'] = datetime.now()
                self.log_sync()
                return False

            # 3. Compare
            comparison = self.compare_data(df, existing)

            # 4. Update existing
            if comparison['updates']:
                self.update_companies(comparison['updates'])

            # 5. Create new
            if comparison['creates']:
                self.create_companies(comparison['creates'])

            self.stats['success'] = True
            self.stats['end_time'] = datetime.now()

            print("\n" + "="*70)
            print("  SYNC COMPLETE!")
            print("="*70)

            # 6. Log to sync_history
            self.log_sync()

            return True

        except Exception as e:
            self.stats['error_message'] = str(e)
            self.stats['success'] = False
            self.stats['end_time'] = datetime.now()

            print(f"\n  Error: {e}")

            self.log_sync()
            return False


if __name__ == "__main__":
    sync = SyncWithLogging()
    success = sync.run()
    exit(0 if success else 1)
