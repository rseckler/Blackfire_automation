#!/usr/bin/env python3
"""
Stock Price Updater v3 - Supabase Edition
- Validates tickers before updating (4-tier: Symbol -> Symbol.DE -> ISIN -> WKN)
- Persistent blacklist for invalid companies
- Updates Supabase directly
"""

import os
import sys
import json
import fcntl
from datetime import datetime
from dotenv import load_dotenv
import time
import yfinance as yf
from isin_ticker_mapper import HybridISINMapper

load_dotenv()

import supabase_helper

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
LOCK_FILE = os.path.join(SCRIPT_DIR, '.stock_updater.lock')


class StockPriceUpdater:
    def __init__(self):
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

        # Persistent blacklist file for companies where ALL validation failed
        self.blacklist_file = os.path.join(SCRIPT_DIR, 'invalid_companies.json')

        # Company-level blacklist: companies where symbol + ISIN + WKN all failed
        self.blacklisted_companies = self._load_blacklist()

        # In-memory caches for within-run dedup (not persisted)
        self.valid_tickers = set()
        self.invalid_tickers = set()

        # ISIN mapper for intelligent lookup
        self.isin_mapper = HybridISINMapper()

    def _load_blacklist(self):
        """Load persistent blacklist of company IDs from JSON file"""
        try:
            with open(self.blacklist_file, 'r') as f:
                data = json.load(f)
                # Blacklist entries older than 30 days get retried
                cutoff = datetime.now().timestamp() - (30 * 86400)
                valid = {k for k, v in data.items() if v > cutoff}
                if len(valid) < len(data):
                    print(f"   {len(data) - len(valid)} blacklisted companies expired, will retry")
                return valid
        except (FileNotFoundError, json.JSONDecodeError):
            return set()

    def _save_blacklist(self):
        """Save blacklisted company IDs to persistent JSON file"""
        try:
            existing = {}
            try:
                with open(self.blacklist_file, 'r') as f:
                    existing = json.load(f)
            except (FileNotFoundError, json.JSONDecodeError):
                pass

            now = datetime.now().timestamp()
            for company_id in self.blacklisted_companies:
                if company_id not in existing:
                    existing[company_id] = now

            # Prune expired entries
            cutoff = datetime.now().timestamp() - (30 * 86400)
            existing = {k: v for k, v in existing.items() if v > cutoff}

            with open(self.blacklist_file, 'w') as f:
                json.dump(existing, f, indent=2)

            print(f"   Blacklist saved: {len(existing)} invalid companies")
        except Exception as e:
            print(f"   Failed to save blacklist: {e}")

    def is_market_hours(self):
        """Check if current time is within market hours (7-23 Uhr)"""
        hour = datetime.now().hour
        return 7 <= hour < 23

    def normalize_ticker(self, symbol):
        """Normalize ticker symbol"""
        if not symbol:
            return None

        symbol = symbol.strip().upper()

        # If it's a full company name, skip it
        if ' ' in symbol or len(symbol) > 6:
            return None

        return symbol

    def get_ticker_from_openfigi(self, isin):
        """Get Ticker from ISIN using OpenFIGI API"""
        if not isin or len(isin) != 12:
            return None

        try:
            url = "https://api.openfigi.com/v3/mapping"
            headers_figi = {'Content-Type': 'application/json'}
            query = {"idType": "ID_ISIN", "idValue": isin}

            response = __import__('requests').post(
                url, headers=headers_figi, json=[query], timeout=10
            )

            if response.status_code == 200:
                data = response.json()
                if data and len(data) > 0 and 'data' in data[0]:
                    results = data[0]['data']
                    if results and len(results) > 0:
                        for result in results:
                            ticker = result.get('ticker', '')
                            exch_code = result.get('exchCode', '')
                            if exch_code in ['GY', 'GR', 'GF', 'US']:
                                if ticker:
                                    if exch_code in ['GY', 'GR', 'GF']:
                                        return f"{ticker}.DE"
                                    return ticker

                        ticker = results[0].get('ticker', '')
                        if ticker:
                            if isin.startswith('DE'):
                                return f"{ticker}.DE"
                            return ticker

            time.sleep(6)

        except Exception:
            pass

        return None

    def find_ticker_from_isin_wkn(self, isin=None, wkn=None):
        """Try to find ticker using ISIN or WKN with Hybrid Mapper"""
        if isin:
            ticker = self.isin_mapper.map_isin_openfigi(isin)
            if ticker and self.isin_mapper.validate_ticker(ticker):
                return ticker

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

    def validate_ticker(self, symbol=None, isin=None, wkn=None):
        """Validate if ticker exists - tries multiple methods"""
        if symbol:
            if symbol in self.valid_tickers:
                return symbol
            if symbol in self.invalid_tickers:
                symbol = None
            else:
                # Try US ticker first
                try:
                    ticker = yf.Ticker(symbol)
                    info = ticker.info
                    price = info.get('currentPrice') or info.get('regularMarketPrice')
                    if price and price > 0:
                        self.valid_tickers.add(symbol)
                        return symbol
                except Exception:
                    pass

                # Try German exchange (.DE)
                if symbol and '.' not in symbol:
                    symbol_de = f"{symbol}.DE"
                    try:
                        ticker = yf.Ticker(symbol_de)
                        info = ticker.info
                        price = info.get('currentPrice') or info.get('regularMarketPrice')
                        if price and price > 0:
                            self.valid_tickers.add(symbol_de)
                            return symbol_de
                    except Exception:
                        pass

        # If symbol didn't work, try ISIN/WKN
        if isin or wkn:
            result = self.find_ticker_from_isin_wkn(isin, wkn)
            if result:
                self.valid_tickers.add(result)
                return result

        if symbol:
            self.invalid_tickers.add(symbol)
        return None

    def get_stocks(self):
        """Get all companies that have a symbol, ISIN or WKN"""
        print("\n  Getting stocks from Supabase...")

        try:
            companies = supabase_helper.get_all_companies(
                'id, symbol, isin, wkn, extra_data'
            )

            stocks = []
            for company in companies:
                symbol = company.get('symbol')
                isin = company.get('isin')
                wkn = company.get('wkn')

                # Also check extra_data for Company_Symbol
                extra = company.get('extra_data') or {}
                if not symbol and extra.get('Company_Symbol'):
                    symbol = extra['Company_Symbol']

                if symbol or isin or wkn:
                    normalized = self.normalize_ticker(symbol) if symbol else None
                    stocks.append({
                        'company_id': company['id'],
                        'symbol': normalized,
                        'isin': isin,
                        'wkn': wkn,
                        'original_symbol': symbol
                    })

            print(f"   Found {len(stocks)} stocks with identifiers")
            return stocks

        except Exception as e:
            print(f"   Error: {e}")
            self.stats['error_message'] = str(e)
            return None

    def fetch_stock_price(self, symbol):
        """Fetch stock price using yfinance"""
        try:
            self.stats['api_calls'] += 1

            ticker = yf.Ticker(symbol)
            info = ticker.info

            current_price = info.get('currentPrice') or info.get('regularMarketPrice')
            if not current_price or current_price == 0:
                return None

            return {
                'current_price': current_price,
                'change_percent': info.get('regularMarketChangePercent', 0),
                'high': info.get('dayHigh', 0),
                'low': info.get('dayLow', 0),
                'volume': info.get('volume', 0),
                'market_cap': info.get('marketCap', 0)
            }

        except Exception:
            return None

    def get_market_status(self):
        """Determine current market status"""
        now = datetime.now()
        hour = now.hour

        if 9 <= hour < 17:
            return 'Open'
        elif hour == 17 and now.minute < 30:
            return 'Open'
        elif 8 <= hour < 9:
            return 'Pre-Market'
        elif (hour == 17 and now.minute >= 30) or (17 < hour < 22):
            return 'After-Hours'
        else:
            return 'Closed'

    def update_stock(self, company_id, symbol, price_data):
        """Update company with stock price data in Supabase"""
        if not price_data:
            return False

        update_data = {
            'price_update': datetime.now().isoformat(),
            'market_status': self.get_market_status(),
            'exchange': 'XETRA',
            'currency': 'EUR',
        }

        if price_data.get('current_price'):
            update_data['current_price'] = round(price_data['current_price'], 4)

        if price_data.get('change_percent') is not None:
            update_data['price_change_percent'] = round(price_data['change_percent'], 4)

        if price_data.get('high'):
            update_data['day_high'] = round(price_data['high'], 4)

        if price_data.get('low'):
            update_data['day_low'] = round(price_data['low'], 4)

        if price_data.get('volume'):
            update_data['volume'] = int(price_data['volume'])

        if price_data.get('market_cap'):
            update_data['market_cap'] = int(price_data['market_cap'])

        return supabase_helper.update_company(company_id, update_data)

    def update_stock_prices(self):
        """Main function to update all stock prices"""
        print("\n" + "="*70)
        print("  STOCK PRICE UPDATE v3 (Supabase)")
        print("="*70)

        self.stats['start_time'] = datetime.now()

        # Check market hours
        if not self.is_market_hours():
            print("  Outside market hours (7-23 Uhr) - skipping update")
            return False

        print(f"  Current time: {datetime.now().strftime('%H:%M:%S')}")
        print(f"  Market status: {self.get_market_status()}")

        # Get stocks from Supabase
        stocks = self.get_stocks()
        if not stocks:
            print("  No stocks found")
            return False

        self.stats['stocks_processed'] = len(stocks)

        blacklisted = sum(1 for s in stocks if s['company_id'] in self.blacklisted_companies)
        print(f"\n  Processing {len(stocks)} stocks...")
        print(f"   {blacklisted} known-invalid companies will be skipped (blacklist)")
        print(f"   {len(stocks) - blacklisted} stocks to validate")

        # Process each stock
        updated_count = 0
        skipped_count = 0
        api_calls_made = False

        for i, stock in enumerate(stocks):
            symbol = stock.get('symbol')
            company_id = stock['company_id']
            original = stock.get('original_symbol', '')
            isin = stock.get('isin')
            wkn = stock.get('wkn')

            # Skip blacklisted companies immediately
            if company_id in self.blacklisted_companies:
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
                self.blacklisted_companies.add(company_id)
                continue

            # Fetch price
            price_data = self.fetch_stock_price(valid_symbol)

            if price_data:
                success = self.update_stock(company_id, valid_symbol, price_data)
                if success:
                    updated_count += 1
                    self.stats['stocks_updated'] += 1
                    if updated_count % 10 == 0:
                        print(f"   {updated_count} stocks updated...")
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
        print("  UPDATE COMPLETE!")
        print("="*70)
        print(f"   Processed: {self.stats['stocks_processed']}")
        print(f"   Updated: {self.stats['stocks_updated']}")
        print(f"   Skipped: {skipped_count} (invalid tickers)")
        print(f"   API Calls: {self.stats['api_calls']}")

        if self.stats['skipped_tickers'][:10]:
            skipped_preview = ', '.join(self.stats['skipped_tickers'][:10])
            if len(self.stats['skipped_tickers']) > 10:
                skipped_preview += f" ... (+{len(self.stats['skipped_tickers']) - 10} more)"
            print(f"   Skipped tickers: {skipped_preview}")

        duration = (self.stats['end_time'] - self.stats['start_time']).total_seconds()
        print(f"   Duration: {int(duration)} seconds")

        return True

    def log_sync(self):
        """Log update to sync_history table"""
        print("\n  Logging to sync_history...")

        self.stats['name'] = f"Stock Update {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"

        if self.stats['skipped_tickers']:
            skipped_str = ', '.join(self.stats['skipped_tickers'][:50])
            if len(self.stats['skipped_tickers']) > 50:
                skipped_str += f" ... (+{len(self.stats['skipped_tickers']) - 50} more)"
            self.stats['error_message'] = f"Skipped {len(self.stats['skipped_tickers'])} invalid tickers: {skipped_str}"[:2000]

        supabase_helper.log_sync_history(self.stats)

    def run(self):
        """Run complete stock price update"""
        try:
            success = self.update_stock_prices()

            if self.stats['start_time']:
                self.log_sync()

            return success

        except Exception as e:
            self.stats['error_message'] = str(e)
            self.stats['success'] = False
            self.stats['end_time'] = datetime.now()

            print(f"\n  Error: {e}")

            if self.stats['start_time']:
                self.log_sync()

            return False


if __name__ == "__main__":
    # Prevent concurrent runs
    lock_fd = open(LOCK_FILE, 'w')
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("  Another stock_price_updater instance is already running. Exiting.")
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
