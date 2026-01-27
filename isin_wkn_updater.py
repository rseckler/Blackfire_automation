#!/usr/bin/env python3
"""
ISIN/WKN Updater - Recherchiert ISIN und WKN für alle Aktien
Läuft morgens nach sync_final.py
"""

import os
import requests
from dotenv import load_dotenv
import yfinance as yf
import time

load_dotenv()

class ISINWKNUpdater:
    def __init__(self):
        self.notion_api_key = os.getenv('NOTION_API_KEY')
        self.notion_db_id = os.getenv('NOTION_DATABASE_ID')

        self.notion_headers = {
            'Authorization': f'Bearer {self.notion_api_key}',
            'Notion-Version': '2022-06-28',
            'Content-Type': 'application/json'
        }

        self.stats = {
            'processed': 0,
            'updated': 0,
            'skipped': 0
        }

    def get_isin_wkn_from_yfinance(self, symbol):
        """Try to get ISIN from yfinance"""
        try:
            # Try with symbol as-is
            ticker = yf.Ticker(symbol)
            info = ticker.info

            isin = info.get('isin', '')

            # WKN is typically last 6 chars of German ISIN
            wkn = ''
            if isin and isin.startswith('DE') and len(isin) == 12:
                wkn = isin[-6:]  # Last 6 digits

            if isin:
                return {'isin': isin, 'wkn': wkn}

        except:
            pass

        # Try with .DE suffix for German stocks
        if '.' not in symbol:
            try:
                ticker = yf.Ticker(f"{symbol}.DE")
                info = ticker.info

                isin = info.get('isin', '')
                wkn = ''
                if isin and isin.startswith('DE') and len(isin) == 12:
                    wkn = isin[-6:]

                if isin:
                    return {'isin': isin, 'wkn': wkn}
            except:
                pass

        return None

    def get_ticker_from_openfigi_by_isin(self, isin):
        """Get Ticker from ISIN using OpenFIGI"""
        if not isin or len(isin) != 12:
            return None

        try:
            url = "https://api.openfigi.com/v3/mapping"

            headers_figi = {
                'Content-Type': 'application/json'
            }

            # Search by ISIN
            query = {
                "idType": "ID_ISIN",
                "idValue": isin
            }

            response = requests.post(
                url,
                headers=headers_figi,
                json=[query],
                timeout=10
            )

            if response.status_code == 200:
                data = response.json()
                if data and len(data) > 0 and 'data' in data[0]:
                    results = data[0]['data']
                    if results and len(results) > 0:
                        # Try to find best match
                        for result in results:
                            ticker = result.get('ticker', '')
                            exch_code = result.get('exchCode', '')
                            market_sector = result.get('marketSector', '')

                            # Prefer XETRA or German exchanges
                            if exch_code in ['GY', 'GR', 'GF']:  # German exchanges
                                if ticker:
                                    return ticker

                        # Fallback: return first ticker found
                        if results[0].get('ticker'):
                            return results[0].get('ticker')

            # Rate limit: 10 requests per minute (wait 6 seconds)
            time.sleep(6)

        except Exception as e:
            pass

        return None

    def get_isin_from_openfigi(self, symbol):
        """Get ISIN from Symbol using OpenFIGI API (free)"""
        try:
            # OpenFIGI API
            url = "https://api.openfigi.com/v3/mapping"

            headers_figi = {
                'Content-Type': 'application/json'
            }

            # Try different identifier types
            queries = [
                {"idType": "TICKER", "idValue": symbol, "exchCode": "GY"},  # German exchange
                {"idType": "TICKER", "idValue": symbol, "exchCode": "US"},  # US exchange
                {"idType": "TICKER", "idValue": symbol}  # Any exchange
            ]

            for query in queries:
                response = requests.post(
                    url,
                    headers=headers_figi,
                    json=[query],
                    timeout=10
                )

                if response.status_code == 200:
                    data = response.json()
                    if data and len(data) > 0 and 'data' in data[0]:
                        results = data[0]['data']
                        if results and len(results) > 0:
                            result = results[0]

                            # Get ISIN
                            isin = result.get('shareClassFIGI', '')
                            if not isin:
                                isin = result.get('compositeFIGI', '')

                            wkn = ''
                            if isin and isin.startswith('DE') and len(isin) == 12:
                                wkn = isin[-6:]

                            if isin:
                                return {'isin': isin, 'wkn': wkn}

                # Rate limit: 10 requests per minute
                time.sleep(6)

        except Exception as e:
            pass

        return None

    def get_notion_stocks(self):
        """Get all Notion pages"""
        print("\n🔍 Getting stocks from Notion...")

        all_pages = []
        has_more = True
        start_cursor = None

        try:
            while has_more:
                body = {}
                if start_cursor:
                    body['start_cursor'] = start_cursor

                response = requests.post(
                    f'https://api.notion.com/v1/databases/{self.notion_db_id}/query',
                    headers=self.notion_headers,
                    json=body,
                    timeout=30
                )

                if response.status_code == 200:
                    data = response.json()
                    all_pages.extend(data.get('results', []))
                    has_more = data.get('has_more', False)
                    start_cursor = data.get('next_cursor')
                else:
                    return None

            print(f"   ✅ Found {len(all_pages)} pages")
            return all_pages

        except Exception as e:
            print(f"   ❌ Error: {e}")
            return None

    def update_page_isin_wkn(self, page_id, isin, wkn):
        """Update page with ISIN and WKN"""
        try:
            properties = {}

            if isin:
                properties['ISIN'] = {
                    'rich_text': [{
                        'text': {'content': isin}
                    }]
                }

            if wkn:
                properties['WKN'] = {
                    'rich_text': [{
                        'text': {'content': wkn}
                    }]
                }

            if not properties:
                return False

            response = requests.patch(
                f'https://api.notion.com/v1/pages/{page_id}',
                headers=self.notion_headers,
                json={'properties': properties},
                timeout=30
            )

            return response.status_code == 200

        except:
            return False

    def run(self):
        """Run ISIN/WKN update"""
        print("\n" + "="*70)
        print("🔍 ISIN/WKN UPDATER")
        print("="*70)

        # Get all pages
        pages = self.get_notion_stocks()
        if not pages:
            print("❌ No pages found")
            return False

        print(f"\n📊 Processing {len(pages)} pages...")

        for i, page in enumerate(pages):
            page_id = page['id']
            props = page.get('properties', {})

            # Get Company_Symbol
            symbol = None
            if 'Company_Symbol' in props:
                symbol_prop = props['Company_Symbol']
                if 'rich_text' in symbol_prop and len(symbol_prop['rich_text']) > 0:
                    symbol = symbol_prop['rich_text'][0]['text']['content'].strip()

            # Skip if no symbol
            if not symbol or ' ' in symbol or len(symbol) > 10:
                self.stats['skipped'] += 1
                continue

            # Check if ISIN already exists
            has_isin = False
            if 'ISIN' in props:
                isin_prop = props['ISIN']
                if 'rich_text' in isin_prop and len(isin_prop['rich_text']) > 0:
                    existing_isin = isin_prop['rich_text'][0]['text']['content']
                    if existing_isin and len(existing_isin) > 5:
                        has_isin = True

            # Skip if ISIN already exists
            if has_isin:
                self.stats['skipped'] += 1
                continue

            self.stats['processed'] += 1

            # Try to get ISIN/WKN
            result = self.get_isin_wkn_from_yfinance(symbol)

            if not result:
                result = self.get_isin_from_openfigi(symbol)

            if result and result.get('isin'):
                success = self.update_page_isin_wkn(
                    page_id,
                    result.get('isin', ''),
                    result.get('wkn', '')
                )

                if success:
                    self.stats['updated'] += 1
                    if self.stats['updated'] % 10 == 0:
                        print(f"   ... {self.stats['updated']} updated")

            # Rate limiting
            time.sleep(0.5)

        print("\n" + "="*70)
        print("✅ UPDATE COMPLETE!")
        print("="*70)
        print(f"   Processed: {self.stats['processed']}")
        print(f"   Updated: {self.stats['updated']}")
        print(f"   Skipped: {self.stats['skipped']}")

        return True


if __name__ == "__main__":
    updater = ISINWKNUpdater()
    success = updater.run()
    exit(0 if success else 1)
