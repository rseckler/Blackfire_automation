#!/usr/bin/env python3
"""
Final Sync mit automatischem Logging in Notion
"""

import os
import requests
import pandas as pd
from io import BytesIO
from dotenv import load_dotenv
from datetime import datetime
import time

load_dotenv()

# Column Mapping
COLUMN_MAPPING = {
    'Company_Name': 'Name'
}

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
        self.notion_api_key = os.getenv('NOTION_API_KEY')
        self.notion_db_id = os.getenv('NOTION_DATABASE_ID')
        self.sync_history_db_id = os.getenv('SYNC_HISTORY_DB_ID')

        self.notion_headers = {
            'Authorization': f'Bearer {self.notion_api_key}',
            'Notion-Version': '2022-06-28',
            'Content-Type': 'application/json'
        }

        # Reuse TCP connections for all Notion API calls
        self.session = requests.Session()
        self.session.headers.update(self.notion_headers)

        self.property_types = {}
        self.valid_columns = set()

        # Stats for logging
        self.stats = {
            'start_time': None,
            'end_time': None,
            'excel_rows': 0,
            'excel_columns': 0,
            'notion_pages': 0,
            'updates': 0,
            'creates': 0,
            'archives': 0,
            'success': False,
            'error_message': None
        }

    def _notion_request(self, method, url, max_retries=3, **kwargs):
        """Make a Notion API request with retry on 429/5xx and rate limiting"""
        kwargs.setdefault('timeout', 120)

        for attempt in range(max_retries):
            try:
                response = getattr(self.session, method)(url, **kwargs)

                if response.status_code == 429:
                    retry_after = int(response.headers.get('Retry-After', 2))
                    wait = retry_after * (attempt + 1)
                    print(f"   ⏳ Rate limited, waiting {wait}s...")
                    time.sleep(wait)
                    continue

                if response.status_code >= 500:
                    wait = 2 ** (attempt + 1)
                    print(f"   ⏳ Server error {response.status_code}, retry in {wait}s...")
                    time.sleep(wait)
                    continue

                return response

            except requests.exceptions.Timeout:
                if attempt < max_retries - 1:
                    wait = 5 * (attempt + 1)
                    print(f"   ⏳ Timeout, retry in {wait}s...")
                    time.sleep(wait)
                    continue
                raise

        return response

    def get_database_schema(self):
        """Get database schema"""
        print("🔍 Getting database schema...")

        try:
            response = self._notion_request(
                'get',
                f'https://api.notion.com/v1/databases/{self.notion_db_id}'
            )

            if response.status_code == 200:
                db = response.json()
                properties = db.get('properties', {})

                for prop_name, prop_info in properties.items():
                    self.property_types[prop_name] = prop_info.get('type')
                    self.valid_columns.add(prop_name)

                print(f"   ✅ Got schema for {len(self.property_types)} properties")
                return True
            else:
                print(f"   ❌ Error: {response.status_code}")
                return False

        except Exception as e:
            print(f"   ❌ Error: {e}")
            self.stats['error_message'] = str(e)
            return False

    def map_column_name(self, excel_col):
        """Map Excel column to Notion property"""
        return COLUMN_MAPPING.get(excel_col, excel_col)

    def download_and_parse(self):
        """Download and parse Excel"""
        print("\n📥 Downloading Excel...")

        try:
            response = requests.get(self.dropbox_url, timeout=60)
            if response.status_code != 200:
                self.stats['error_message'] = f"Dropbox download failed: {response.status_code}"
                return None

            print(f"   ✅ Downloaded {len(response.content)} bytes")

            print("\n📊 Parsing Excel...")
            df = pd.read_excel(BytesIO(response.content))

            self.stats['excel_rows'] = len(df)
            self.stats['excel_columns'] = len(df.columns)

            print(f"   ✅ Parsed {len(df)} rows, {len(df.columns)} columns")

            return df

        except Exception as e:
            print(f"   ❌ Error: {e}")
            self.stats['error_message'] = str(e)
            return None

    def get_notion_pages(self):
        """Get all Notion pages"""
        print("\n🟣 Getting Notion pages...")

        all_pages = []
        has_more = True
        start_cursor = None

        try:
            while has_more:
                body = {}
                if start_cursor:
                    body['start_cursor'] = start_cursor

                response = self._notion_request(
                    'post',
                    f'https://api.notion.com/v1/databases/{self.notion_db_id}/query',
                    json=body
                )

                if response.status_code == 200:
                    data = response.json()
                    all_pages.extend(data.get('results', []))
                    has_more = data.get('has_more', False)
                    start_cursor = data.get('next_cursor')
                else:
                    self.stats['error_message'] = f"Notion query failed: {response.status_code}"
                    return None

            self.stats['notion_pages'] = len(all_pages)
            print(f"   ✅ Found {len(all_pages)} pages")
            return all_pages

        except Exception as e:
            print(f"   ❌ Error: {e}")
            self.stats['error_message'] = str(e)
            return None

    def compare_data(self, df, notion_pages):
        """Compare Excel vs Notion"""
        print("\n🔍 Comparing data...")

        identifier_col = df.columns[0]
        mapped_identifier = self.map_column_name(identifier_col)

        print(f"   Using '{identifier_col}' (→ '{mapped_identifier}') as identifier")

        excel_map = {}
        for idx, row in df.iterrows():
            row_id = str(row[identifier_col])
            excel_map[row_id] = row.to_dict()

        notion_map = {}
        for page in notion_pages:
            props = page.get('properties', {})
            notion_id = None

            if mapped_identifier in props:
                prop = props[mapped_identifier]
                if 'title' in prop and len(prop['title']) > 0:
                    notion_id = prop['title'][0]['text']['content']
                elif 'rich_text' in prop and len(prop['rich_text']) > 0:
                    notion_id = prop['rich_text'][0]['text']['content']

            if notion_id:
                notion_map[str(notion_id)] = page

        to_update = []
        to_create = []

        for excel_id, excel_data in excel_map.items():
            if excel_id in notion_map:
                to_update.append({
                    'page_id': notion_map[excel_id]['id'],
                    'data': excel_data
                })
            else:
                to_create.append(excel_data)

        self.stats['updates'] = len(to_update)
        self.stats['creates'] = len(to_create)

        print(f"   📊 Updates: {len(to_update)}")
        print(f"   📊 Creates: {len(to_create)}")

        return {'updates': to_update, 'creates': to_create}

    def format_property_value(self, prop_name, value):
        """Format value based on property type"""
        if pd.isna(value) or value == '':
            return None

        prop_type = self.property_types.get(prop_name, 'rich_text')

        try:
            if prop_type == 'title':
                return {'title': [{'text': {'content': str(value)[:2000]}}]}

            elif prop_type == 'rich_text':
                return {'rich_text': [{'text': {'content': str(value)[:2000]}}]}

            elif prop_type == 'number':
                try:
                    return {'number': float(value)}
                except:
                    return None

            elif prop_type == 'checkbox':
                bool_val = value in [True, 'True', 'true', 1, '1', 'yes', 'Yes']
                return {'checkbox': bool_val}

            elif prop_type == 'select':
                return {'select': {'name': str(value)[:100]}}

            elif prop_type == 'url':
                url_str = str(value)
                if url_str.startswith('http'):
                    return {'url': url_str[:2000]}
                return None

            else:
                return {'rich_text': [{'text': {'content': str(value)[:2000]}}]}

        except:
            return None

    def build_notion_properties(self, excel_data):
        """Build Notion properties from Excel data"""
        properties = {}

        for excel_col, value in excel_data.items():
            notion_col = self.map_column_name(excel_col)

            # Skip properties that don't exist in Notion
            if notion_col not in self.valid_columns:
                continue

            # Skip protected properties (managed by stock_price_updater.py)
            if notion_col in PROTECTED_PROPERTIES:
                continue

            formatted = self.format_property_value(notion_col, value)
            if formatted:
                properties[notion_col] = formatted

        return properties

    def update_pages(self, updates):
        """Update Notion pages"""
        print("\n✏️  Updating Notion pages...")
        print(f"   Processing {len(updates)} updates...")

        success = 0
        failed = 0

        for i, update in enumerate(updates):
            page_id = update['page_id']
            data = update['data']

            properties = self.build_notion_properties(data)

            # Rate limiting: stay under 3 req/sec
            time.sleep(0.35)

            try:
                response = self._notion_request(
                    'patch',
                    f'https://api.notion.com/v1/pages/{page_id}',
                    json={'properties': properties}
                )

                if response.status_code == 200:
                    success += 1
                    if (i + 1) % 100 == 0:
                        print(f"   ... {i + 1}/{len(updates)} done")
                else:
                    failed += 1
                    if failed <= 2:
                        print(f"   ⚠️  Error: {response.status_code}")

            except Exception as e:
                failed += 1

        print(f"   ✅ Updated: {success}")
        if failed > 0:
            print(f"   ⚠️  Failed: {failed}")

        return success

    def create_pages(self, creates):
        """Create Notion pages"""
        print("\n➕ Creating Notion pages...")
        print(f"   Processing {len(creates)} creates...")

        success = 0
        failed = 0

        for data in creates:
            properties = self.build_notion_properties(data)

            time.sleep(0.35)

            try:
                response = self._notion_request(
                    'post',
                    'https://api.notion.com/v1/pages',
                    json={
                        'parent': {'database_id': self.notion_db_id},
                        'properties': properties
                    }
                )

                if response.status_code == 200:
                    success += 1
                else:
                    failed += 1
                    if failed <= 2:
                        print(f"   ⚠️  Error: {response.status_code}")

            except Exception as e:
                failed += 1

        print(f"   ✅ Created: {success}")
        if failed > 0:
            print(f"   ⚠️  Failed: {failed}")

        return success

    def log_to_notion(self):
        """Log sync result to Notion Sync History database"""
        print("\n📝 Logging to Notion...")

        if not self.sync_history_db_id:
            print("   ⚠️  Sync History DB ID not found")
            return

        duration = 0
        if self.stats['start_time'] and self.stats['end_time']:
            duration = int((self.stats['end_time'] - self.stats['start_time']).total_seconds())

        # Calculate success rate
        total_operations = self.stats['updates'] + self.stats['creates']
        success_rate = 100.0 if total_operations > 0 else 0.0

        # Determine status
        if self.stats['success']:
            status = "Success"
        elif self.stats['error_message']:
            status = "Failed"
        else:
            status = "Partial"

        # Create log entry
        log_entry = {
            'parent': {
                'database_id': self.sync_history_db_id
            },
            'properties': {
                'Name': {
                    'title': [{
                        'text': {
                            'content': f"Sync {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
                        }
                    }]
                },
                'Sync_Date': {
                    'date': {
                        'start': datetime.now().isoformat()
                    }
                },
                'Status': {
                    'select': {
                        'name': status
                    }
                },
                'Excel_Rows': {
                    'number': self.stats['excel_rows']
                },
                'Notion_Pages': {
                    'number': self.stats['notion_pages']
                },
                'Updates': {
                    'number': self.stats['updates']
                },
                'Creates': {
                    'number': self.stats['creates']
                },
                'Archives': {
                    'number': self.stats['archives']
                },
                'Duration_Seconds': {
                    'number': duration
                },
                'Success_Rate': {
                    'number': success_rate
                }
            }
        }

        # Add error message if exists
        if self.stats['error_message']:
            log_entry['properties']['Error_Message'] = {
                'rich_text': [{
                    'text': {
                        'content': str(self.stats['error_message'])[:2000]
                    }
                }]
            }

        try:
            response = self._notion_request(
                'post',
                'https://api.notion.com/v1/pages',
                json=log_entry
            )

            if response.status_code == 200:
                print("   ✅ Logged to Notion Sync History")
            else:
                print(f"   ⚠️  Failed to log: {response.status_code}")

        except Exception as e:
            print(f"   ⚠️  Error logging: {e}")

    def run(self):
        """Run complete sync with logging"""
        print("\n" + "="*70)
        print("🔄 FINAL EXCEL → NOTION SYNC (WITH LOGGING)")
        print("="*70)

        self.stats['start_time'] = datetime.now()

        try:
            # 1. Get schema
            if not self.get_database_schema():
                self.stats['end_time'] = datetime.now()
                self.log_to_notion()
                return False

            # 2. Download & parse
            df = self.download_and_parse()
            if df is None:
                self.stats['end_time'] = datetime.now()
                self.log_to_notion()
                return False

            # 3. Get Notion pages
            notion_pages = self.get_notion_pages()
            if notion_pages is None:
                self.stats['end_time'] = datetime.now()
                self.log_to_notion()
                return False

            # 4. Compare
            comparison = self.compare_data(df, notion_pages)

            # 5. Update
            if comparison['updates']:
                self.update_pages(comparison['updates'])

            # 6. Create
            if comparison['creates']:
                self.create_pages(comparison['creates'])

            self.stats['success'] = True
            self.stats['end_time'] = datetime.now()

            print("\n" + "="*70)
            print("✅ SYNC COMPLETE!")
            print("="*70)

            # 7. Log to Notion
            self.log_to_notion()

            return True

        except Exception as e:
            self.stats['error_message'] = str(e)
            self.stats['success'] = False
            self.stats['end_time'] = datetime.now()

            print(f"\n❌ Error: {e}")

            # Log error to Notion
            self.log_to_notion()

            return False


if __name__ == "__main__":
    sync = SyncWithLogging()
    success = sync.run()

    exit(0 if success else 1)
