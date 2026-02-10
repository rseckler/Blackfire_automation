#!/usr/bin/env python3
"""
Complete System Test - First 10 stocks
1. Get stocks from Supabase
2. Map ISIN/WKN -> Ticker
3. Fetch prices
4. Update Supabase
"""

from datetime import datetime
from dotenv import load_dotenv
import yfinance as yf
from isin_ticker_mapper import HybridISINMapper

load_dotenv()

import supabase_helper

print("=" * 70)
print("  COMPLETE SYSTEM TEST - First 10 Stocks (Supabase)")
print("=" * 70)

# Step 1: Get stocks from Supabase
print("\n  Step 1: Getting stocks from Supabase...")
companies = supabase_helper.get_all_companies('id, name, symbol, isin, wkn, extra_data')
print(f"   Found {len(companies)} companies total")

# Filter to those with at least one identifier
stocks = []
for c in companies:
    symbol = c.get('symbol')
    extra = c.get('extra_data') or {}
    if not symbol and extra.get('Company_Symbol'):
        symbol = extra['Company_Symbol']
    isin = c.get('isin')
    wkn = c.get('wkn')

    if symbol or isin or wkn:
        stocks.append({
            'id': c['id'],
            'name': (c.get('name') or 'Unknown')[:30],
            'symbol': symbol,
            'isin': isin,
            'wkn': wkn
        })

print(f"   {len(stocks)} stocks with identifiers")

# Step 2: Test first 10
print("\n  Step 2: Testing first 10 stocks...")
mapper = HybridISINMapper()

test_stocks = stocks[:10]
updated_count = 0
failed_count = 0

for i, stock in enumerate(test_stocks, 1):
    print(f"\n   [{i}/10] {stock['name']}")
    print(f"      Symbol: {stock['symbol'] or 'None'}")
    print(f"      ISIN: {stock['isin'] or 'None'}")
    print(f"      WKN: {stock['wkn'] or 'None'}")

    valid_ticker = None

    # Try symbol first
    if stock['symbol'] and ' ' not in stock['symbol'] and len(stock['symbol']) <= 6:
        try:
            t = yf.Ticker(stock['symbol'])
            price = t.info.get('currentPrice') or t.info.get('regularMarketPrice', 0)
            if price and price > 0:
                valid_ticker = stock['symbol']
                print(f"      Found as US ticker: ${price:.2f}")
        except Exception:
            pass

        if not valid_ticker and stock['symbol'] and '.' not in stock['symbol']:
            try:
                t = yf.Ticker(f"{stock['symbol']}.DE")
                price = t.info.get('currentPrice') or t.info.get('regularMarketPrice', 0)
                if price and price > 0:
                    valid_ticker = f"{stock['symbol']}.DE"
                    print(f"      Found as German ticker: {price:.2f} EUR")
            except Exception:
                pass

    # Try ISIN mapping
    if not valid_ticker and stock['isin']:
        print(f"      Mapping ISIN via OpenFIGI...")
        ticker = mapper.map_isin_openfigi(stock['isin'])
        if ticker and mapper.validate_ticker(ticker):
            valid_ticker = ticker
            print(f"      Mapped to: {ticker}")

    if valid_ticker:
        try:
            t = yf.Ticker(valid_ticker)
            info = t.info

            current_price = info.get('currentPrice') or info.get('regularMarketPrice')
            if not current_price:
                raise ValueError("No price data")

            update_data = {
                'current_price': round(current_price, 4),
                'price_change_percent': round(info.get('regularMarketChangePercent', 0), 4),
                'day_high': round(info.get('dayHigh', 0), 4),
                'day_low': round(info.get('dayLow', 0), 4),
                'volume': int(info.get('volume', 0)),
                'market_cap': int(info.get('marketCap', 0)),
                'price_update': datetime.now().isoformat(),
                'currency': 'EUR',
                'exchange': 'XETRA',
                'market_status': 'Test',
            }

            if supabase_helper.update_company(stock['id'], update_data):
                updated_count += 1
                print(f"      Updated in Supabase! ${current_price:.2f}")
            else:
                failed_count += 1
                print(f"      Supabase update failed")

        except Exception as e:
            failed_count += 1
            print(f"      Error: {e}")
    else:
        failed_count += 1
        print(f"      Skipped (no valid ticker)")

print("\n" + "=" * 70)
print("  TEST COMPLETE!")
print("=" * 70)
print(f"   Processed: {len(test_stocks)}")
print(f"   Updated: {updated_count}")
print(f"   Failed: {failed_count}")
print("=" * 70)
