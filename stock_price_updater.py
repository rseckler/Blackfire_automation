#!/usr/bin/env python3
"""
Stock Price Updater v2 - Smart Ticker Validation
- Validates tickers before updating
- Skips invalid tickers silently
- Optional: Search for ticker by company name
"""

import os
import sys
import json
import fcntl
import requests
from datetime import datetime
from dotenv import load_dotenv
import time
import yfinance as yf
from isin_ticker_mapper import HybridISINMapper

load_dotenv()

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
LOCK_FILE = os.path.join(SCRIPT_DIR, '.stock_updater.lock')

class StockPriceUpdater:
    def __init__(self):
        self.notion_api_key = os.getenv('NOTION_API_KEY')
        self.notion_db_id = os.getenv('NOTION_DATABASE_ID')
        self.sync_history_db_id = os.getenv('SYNC_HISTORY_DB_ID')

        self.notion_headers = {
            'Authorization': f'Bearer {self.notion_api_key}',
            'Notion-Version': '2022-06-28',
            'Content-Type': 'application/json'
        }

        # Stats
        self.stats = {
            'start_time': None,
            'end_time': None,
            'stocks_processed': 0,
            'stocks_updated': 0,
            'stocks_skipped': 0,
            'api_calls': 0,
            'skipped_tickers': [],
            'success': False,
            'error_message': None
        }

        # Persistent blacklist file for pages where ALL validation failed
        self.script_dir = os.path.dirname(os.path.abspath(__file__))
        self.blacklist_file = os.path.join(self.script_dir, 'invalid_pages.json')

        # Page-level blacklist: pages where symbol + ISIN + WKN all failed
        self.blacklisted_pages = self._load_blacklist()

        # In-memory caches for within-run dedup (not persisted)
        self.valid_tickers = set()
        self.invalid_tickers = set()

        # ISIN mapper for intelligent lookup
        self.isin_mapper = HybridISINMapper()

    def _load_blacklist(self):
        """Load persistent blacklist of page IDs from JSON file"""
        try:
            with open(self.blacklist_file, 'r') as f:
                data = json.load(f)
                # Blacklist entries older than 30 days get retried
                cutoff = datetime.now().timestamp() - (30 * 86400)
                valid = {k for k, v in data.items() if v > cutoff}
                if len(valid) < len(data):
                    print(f"   ♻️  {len(data) - len(valid)} blacklisted pages expired, will retry")
                return valid
        except (FileNotFoundError, json.JSONDecodeError):
            return set()

    def _save_blacklist(self):
        """Save blacklisted page IDs to persistent JSON file"""
        try:
            # Merge with existing (in case another process updated it)
            existing = {}
            try:
                with open(self.blacklist_file, 'r') as f:
                    existing = json.load(f)
            except (FileNotFoundError, json.JSONDecodeError):
                pass

            now = datetime.now().timestamp()
            for page_id in self.blacklisted_pages:
                if page_id not in existing:
                    existing[page_id] = now

            # Prune expired entries
            cutoff = datetime.now().timestamp() - (30 * 86400)
            existing = {k: v for k, v in existing.items() if v > cutoff}

            with open(self.blacklist_file, 'w') as f:
                json.dump(existing, f, indent=2)

            print(f"   💾 Blacklist saved: {len(existing)} invalid pages")
        except Exception as e:
            print(f"   ⚠️  Failed to save blacklist: {e}")

    def is_market_hours(self):
        """Check if current time is within market hours (7-23 Uhr)"""
        now = datetime.now()
        hour = now.hour

        if 7 <= hour < 23:
            return True
        return False

    def normalize_ticker(self, symbol):
        """Normalize ticker symbol"""
        if not symbol:
            return None

        symbol = symbol.strip().upper()

        # If it's a full company name, skip it
        if ' ' in symbol or len(symbol) > 6:
            return None

        # Return as-is (we'll try both US and DE in validation)
        return symbol

    def get_ticker_from_openfigi(self, isin):
        """Get Ticker from ISIN using OpenFIGI API"""
        if not isin or len(isin) != 12:
            return None

        try:
            url = "https://api.openfigi.com/v3/mapping"

            headers_figi = {
                'Content-Type': 'application/json'
            }

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

                            # Prefer German/US exchanges
                            if exch_code in ['GY', 'GR', 'GF', 'US']:
                                if ticker:
                                    # Add .DE for German exchanges
                                    if exch_code in ['GY', 'GR', 'GF']:
                                        return f"{ticker}.DE"
                                    return ticker

                        # Fallback: return first ticker
                        ticker = results[0].get('ticker', '')
                        if ticker:
                            # Guess exchange based on ISIN
                            if isin.startswith('DE'):
                                return f"{ticker}.DE"
                            return ticker

            # Rate limit
            time.sleep(6)

        except Exception as e:
            pass

        return None

    def find_ticker_from_isin_wkn(self, isin=None, wkn=None):
        """Try to find ticker using ISIN or WKN with Hybrid Mapper"""
        # Use Hybrid Mapper for ISIN
        if isin:
            ticker = self.isin_mapper.map_isin_openfigi(isin)
            if ticker and self.isin_mapper.validate_ticker(ticker):
                return ticker

        # Try WKN-based ISIN construction
        if wkn and not isin and len(wkn) == 6:
            possible_isins = [
                f"DE000{wkn}",
                f"DE0000{wkn[:-1]}{wkn[-1]}"
            ]

            for test_isin in possible_isins:
                ticker = self.isin_mapper.map_isin_openfigi(test_isin)
                if ticker and self.isin_mapper.validate_ticker(ticker):
                    return ticker

        return None

    def search_by_isin(self, isin):
        """Search for stock by ISIN (direct yfinance attempt)"""
        if not isin or len(isin) != 12:
            return None

        # For German ISINs (DE...), try .DE suffix
        if isin.startswith('DE'):
            # Extract WKN (characters 5-10)
            wkn = isin[4:10]

            # Try with WKN as ticker
            test_symbols = [
                f"{wkn}.DE",
                wkn,
                f"{wkn}.F"  # Frankfurt exchange
            ]

            for symbol in test_symbols:
                try:
                    ticker = yf.Ticker(symbol)
                    info = ticker.info
                    price = info.get('currentPrice') or info.get('regularMarketPrice')

                    if price and price > 0:
                        return symbol
                except:
                    pass

        return None

    def validate_ticker(self, symbol=None, isin=None, wkn=None):
        """Validate if ticker exists - tries multiple methods"""
        # Try symbol first if provided
        if symbol:
            # Check cache first
            if symbol in self.valid_tickers:
                return symbol
            if symbol in self.invalid_tickers:
                symbol = None  # Try other methods
            else:
                # Try US ticker first (NASDAQ/NYSE)
                try:
                    ticker = yf.Ticker(symbol)
                    info = ticker.info
                    price = info.get('currentPrice') or info.get('regularMarketPrice')

                    if price and price > 0:
                        self.valid_tickers.add(symbol)
                        return symbol
                except:
                    pass

                # Try German exchange (.DE)
                if '.' not in symbol:
                    symbol_de = f"{symbol}.DE"
                    try:
                        ticker = yf.Ticker(symbol_de)
                        info = ticker.info
                        price = info.get('currentPrice') or info.get('regularMarketPrice')

                        if price and price > 0:
                            self.valid_tickers.add(symbol_de)
                            return symbol_de
                    except:
                        pass

        # If symbol didn't work, try ISIN/WKN
        if isin or wkn:
            result = self.find_ticker_from_isin_wkn(isin, wkn)
            if result:
                self.valid_tickers.add(result)
                return result

        # Nothing worked
        if symbol:
            self.invalid_tickers.add(symbol)
        return None

    def get_notion_stocks(self):
        """Get all Notion pages that have a Company_Symbol"""
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
                    print(f"   ❌ Error: {response.status_code}")
                    return None

            # Filter pages that have Company_Symbol, ISIN or WKN
            stocks = []
            for page in all_pages:
                props = page.get('properties', {})

                # Get Company_Symbol
                symbol = None
                if 'Company_Symbol' in props:
                    symbol_prop = props['Company_Symbol']
                    if 'rich_text' in symbol_prop and len(symbol_prop['rich_text']) > 0:
                        symbol = symbol_prop['rich_text'][0]['text']['content']

                # Get ISIN
                isin = None
                if 'ISIN' in props:
                    isin_prop = props['ISIN']
                    if 'rich_text' in isin_prop and len(isin_prop['rich_text']) > 0:
                        isin = isin_prop['rich_text'][0]['text']['content']

                # Get WKN
                wkn = None
                if 'WKN' in props:
                    wkn_prop = props['WKN']
                    if 'rich_text' in wkn_prop and len(wkn_prop['rich_text']) > 0:
                        wkn = wkn_prop['rich_text'][0]['text']['content']

                # Need at least one identifier
                if symbol or isin or wkn:
                    normalized = self.normalize_ticker(symbol) if symbol else None
                    stocks.append({
                        'page_id': page['id'],
                        'symbol': normalized,
                        'isin': isin,
                        'wkn': wkn,
                        'original_symbol': symbol
                    })

            print(f"   ✅ Found {len(stocks)} stocks with symbols")
            return stocks

        except Exception as e:
            print(f"   ❌ Error: {e}")
            self.stats['error_message'] = str(e)
            return None

    def fetch_stock_price(self, symbol):
        """Fetch stock price using yfinance"""
        try:
            self.stats['api_calls'] += 1

            ticker = yf.Ticker(symbol)
            info = ticker.info

            # Get current price
            current_price = info.get('currentPrice') or info.get('regularMarketPrice')
            if not current_price or current_price == 0:
                return None

            # Get change percentage
            change_percent = info.get('regularMarketChangePercent', 0)

            # Get high/low
            day_high = info.get('dayHigh', 0)
            day_low = info.get('dayLow', 0)

            # Get volume and market cap
            volume = info.get('volume', 0)
            market_cap = info.get('marketCap', 0)

            return {
                'current_price': current_price,
                'change_percent': change_percent,
                'high': day_high,
                'low': day_low,
                'volume': volume,
                'market_cap': market_cap
            }

        except Exception as e:
            return None

    def get_market_status(self):
        """Determine current market status"""
        now = datetime.now()
        hour = now.hour

        # German market hours: 9:00 - 17:30
        if 9 <= hour < 17:
            return '🟢 Open'
        elif hour == 17 and now.minute < 30:
            return '🟢 Open'
        elif 8 <= hour < 9:
            return '⏸️ Pre-Market'
        elif (hour == 17 and now.minute >= 30) or (17 < hour < 22):
            return '🌙 After-Hours'
        else:
            return '🔴 Closed'

    def update_notion_stock(self, page_id, symbol, price_data):
        """Update Notion page with stock price data"""

        if not price_data:
            return False

        try:
            # Build properties
            properties = {}

            if price_data.get('current_price'):
                properties['Current_Price'] = {
                    'number': round(price_data['current_price'], 2)
                }

            if price_data.get('change_percent') is not None:
                properties['Price_Change_Percent'] = {
                    'number': round(price_data['change_percent'], 2)
                }

            if price_data.get('high'):
                properties['Day_High'] = {
                    'number': round(price_data['high'], 2)
                }

            if price_data.get('low'):
                properties['Day_Low'] = {
                    'number': round(price_data['low'], 2)
                }

            if price_data.get('volume'):
                properties['Volume'] = {
                    'number': int(price_data['volume'])
                }

            if price_data.get('market_cap'):
                properties['Market_Cap'] = {
                    'number': int(price_data['market_cap'])
                }

            # Price update timestamp
            properties['Price_Update'] = {
                'date': {
                    'start': datetime.now().isoformat()
                }
            }

            # Market status
            properties['Market_Status'] = {
                'select': {
                    'name': self.get_market_status()
                }
            }

            # Currency (EUR for German stocks)
            properties['Currency'] = {
                'select': {
                    'name': 'EUR'
                }
            }

            # Exchange (XETRA for German stocks)
            properties['Exchange'] = {
                'select': {
                    'name': 'XETRA'
                }
            }

            # Update Notion page
            response = requests.patch(
                f'https://api.notion.com/v1/pages/{page_id}',
                headers=self.notion_headers,
                json={'properties': properties},
                timeout=30
            )

            if response.status_code == 200:
                return True
            else:
                return False

        except Exception as e:
            return False

    def update_stock_prices(self):
        """Main function to update all stock prices"""
        print("\n" + "="*70)
        print("📈 STOCK PRICE UPDATE v2 (Smart Validation)")
        print("="*70)

        self.stats['start_time'] = datetime.now()

        # Check market hours
        if not self.is_market_hours():
            print("⏰ Outside market hours (7-23 Uhr) - skipping update")
            return False

        print(f"⏰ Current time: {datetime.now().strftime('%H:%M:%S')}")
        print(f"📊 Market status: {self.get_market_status()}")

        # Get stocks from Notion
        stocks = self.get_notion_stocks()
        if not stocks:
            print("❌ No stocks found")
            return False

        self.stats['stocks_processed'] = len(stocks)

        blacklisted = sum(1 for s in stocks if s['page_id'] in self.blacklisted_pages)
        print(f"\n📊 Processing {len(stocks)} stocks...")
        print(f"   ⏩ {blacklisted} known-invalid pages will be skipped (blacklist)")
        print(f"   ⏩ {len(stocks) - blacklisted} stocks to validate")

        # Process each stock
        updated_count = 0
        skipped_count = 0
        api_calls_made = False

        for i, stock in enumerate(stocks):
            symbol = stock.get('symbol')
            page_id = stock['page_id']
            original = stock.get('original_symbol', '')
            isin = stock.get('isin')
            wkn = stock.get('wkn')

            # Skip blacklisted pages immediately (no sleep, no API calls)
            if page_id in self.blacklisted_pages:
                skipped_count += 1
                self.stats['skipped_tickers'].append(original or isin or wkn or 'Unknown')
                continue

            # Rate limiting: only sleep before actual API calls
            if api_calls_made:
                time.sleep(1)

            # Validate ticker (tries Symbol, then ISIN, then WKN)
            api_calls_made = True
            valid_symbol = self.validate_ticker(symbol=symbol, isin=isin, wkn=wkn)
            if not valid_symbol:
                skipped_count += 1
                self.stats['skipped_tickers'].append(original or isin or wkn or 'Unknown')
                self.blacklisted_pages.add(page_id)
                continue

            # Fetch price (using validated symbol)
            price_data = self.fetch_stock_price(valid_symbol)

            if price_data:
                # Update Notion
                success = self.update_notion_stock(page_id, valid_symbol, price_data)

                if success:
                    updated_count += 1
                    self.stats['stocks_updated'] += 1

                    # Show progress
                    if (updated_count) % 10 == 0:
                        print(f"   ✅ {updated_count} stocks updated...")
                else:
                    skipped_count += 1
                    self.stats['stocks_skipped'] += 1
            else:
                skipped_count += 1
                self.stats['stocks_skipped'] += 1
                self.stats['skipped_tickers'].append(original)

        self.stats['success'] = True
        self.stats['end_time'] = datetime.now()

        # Save blacklist for next run
        self._save_blacklist()

        print("\n" + "="*70)
        print("✅ UPDATE COMPLETE!")
        print("="*70)
        print(f"   Processed: {self.stats['stocks_processed']}")
        print(f"   ✅ Updated: {self.stats['stocks_updated']}")
        print(f"   ⏩ Skipped: {skipped_count} (invalid tickers)")
        print(f"   API Calls: {self.stats['api_calls']}")

        if self.stats['skipped_tickers'][:10]:
            skipped_preview = ', '.join(self.stats['skipped_tickers'][:10])
            if len(self.stats['skipped_tickers']) > 10:
                skipped_preview += f" ... (+{len(self.stats['skipped_tickers']) - 10} more)"
            print(f"   Skipped tickers: {skipped_preview}")

        duration = (self.stats['end_time'] - self.stats['start_time']).total_seconds()
        print(f"   Duration: {int(duration)} seconds")

        return True

    def log_to_notion(self):
        """Log update to Sync History database"""
        print("\n📝 Logging to Notion Sync History...")

        if not self.sync_history_db_id:
            print("   ⚠️  Sync History DB ID not found")
            return

        duration = 0
        if self.stats['start_time'] and self.stats['end_time']:
            duration = int((self.stats['end_time'] - self.stats['start_time']).total_seconds())

        # Calculate success rate
        if self.stats['stocks_processed'] > 0:
            success_rate = (self.stats['stocks_updated'] / self.stats['stocks_processed']) * 100
        else:
            success_rate = 0.0

        # Determine status
        if self.stats['success'] and self.stats['stocks_skipped'] == 0:
            status = "Success"
        elif self.stats['success'] and self.stats['stocks_skipped'] > 0:
            status = "Partial"
        else:
            status = "Failed"

        # Create log entry
        log_entry = {
            'parent': {
                'database_id': self.sync_history_db_id
            },
            'properties': {
                'Name': {
                    'title': [{
                        'text': {
                            'content': f"Stock Update {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
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
                'Updates': {
                    'number': self.stats['stocks_updated']
                },
                'Duration_Seconds': {
                    'number': duration
                },
                'Success_Rate': {
                    'number': round(success_rate, 1)
                }
            }
        }

        # Add info about skipped tickers
        if self.stats['skipped_tickers']:
            skipped_str = ', '.join(self.stats['skipped_tickers'][:50])
            if len(self.stats['skipped_tickers']) > 50:
                skipped_str += f" ... (+{len(self.stats['skipped_tickers']) - 50} more)"

            log_entry['properties']['Error_Message'] = {
                'rich_text': [{
                    'text': {
                        'content': f"Skipped {len(self.stats['skipped_tickers'])} invalid tickers: {skipped_str}"[:2000]
                    }
                }]
            }

        try:
            response = requests.post(
                'https://api.notion.com/v1/pages',
                headers=self.notion_headers,
                json=log_entry,
                timeout=30
            )

            if response.status_code == 200:
                print("   ✅ Logged to Notion Sync History")
            else:
                print(f"   ⚠️  Failed to log: {response.status_code}")

        except Exception as e:
            print(f"   ⚠️  Error logging: {e}")

    def run(self):
        """Run complete stock price update"""
        try:
            success = self.update_stock_prices()

            # Log to Notion (even if update failed)
            if self.stats['start_time']:
                self.log_to_notion()

            return success

        except Exception as e:
            self.stats['error_message'] = str(e)
            self.stats['success'] = False
            self.stats['end_time'] = datetime.now()

            print(f"\n❌ Error: {e}")

            # Log error to Notion
            if self.stats['start_time']:
                self.log_to_notion()

            return False


if __name__ == "__main__":
    # Prevent concurrent runs
    lock_fd = open(LOCK_FILE, 'w')
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("⚠️  Another stock_price_updater instance is already running. Exiting.")
        sys.exit(0)

    lock_fd.write(str(os.getpid()))
    lock_fd.flush()

    try:
        updater = StockPriceUpdater()
        success = updater.run()
        sys.exit(0 if success else 1)
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        lock_fd.close()
        try:
            os.remove(LOCK_FILE)
        except OSError:
            pass
