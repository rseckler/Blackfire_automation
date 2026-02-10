#!/usr/bin/env python3
"""
ISIN/WKN Updater - Researches ISIN and WKN for all stocks
Runs mornings after sync_final.py
"""

import os
import requests
from dotenv import load_dotenv
import yfinance as yf
import time

load_dotenv()

import supabase_helper


class ISINWKNUpdater:
    def __init__(self):
        self.stats = {
            'processed': 0,
            'updated': 0,
            'skipped': 0
        }

    def get_isin_wkn_from_yfinance(self, symbol):
        """Try to get ISIN from yfinance"""
        try:
            ticker = yf.Ticker(symbol)
            info = ticker.info

            isin = info.get('isin', '')

            # WKN is typically last 6 chars of German ISIN
            wkn = ''
            if isin and isin.startswith('DE') and len(isin) == 12:
                wkn = isin[-6:]

            if isin:
                return {'isin': isin, 'wkn': wkn}

        except Exception:
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
            except Exception:
                pass

        return None

    def get_isin_from_openfigi(self, symbol):
        """Get ISIN from Symbol using OpenFIGI API (free)"""
        try:
            url = "https://api.openfigi.com/v3/mapping"
            headers_figi = {'Content-Type': 'application/json'}

            queries = [
                {"idType": "TICKER", "idValue": symbol, "exchCode": "GY"},
                {"idType": "TICKER", "idValue": symbol, "exchCode": "US"},
                {"idType": "TICKER", "idValue": symbol}
            ]

            for query in queries:
                response = requests.post(
                    url, headers=headers_figi, json=[query], timeout=10
                )

                if response.status_code == 200:
                    data = response.json()
                    if data and len(data) > 0 and 'data' in data[0]:
                        results = data[0]['data']
                        if results and len(results) > 0:
                            result = results[0]
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

        except Exception:
            pass

        return None

    def run(self):
        """Run ISIN/WKN update"""
        print("\n" + "="*70)
        print("  ISIN/WKN UPDATER (Supabase)")
        print("="*70)

        # Get all companies
        print("\n  Getting companies from Supabase...")
        companies = supabase_helper.get_all_companies('id, symbol, isin, wkn, extra_data')
        if not companies:
            print("  No companies found")
            return False

        print(f"   Found {len(companies)} companies")
        print(f"\n  Processing...")

        for company in companies:
            company_id = company['id']

            # Get symbol from column or extra_data
            symbol = company.get('symbol')
            extra = company.get('extra_data') or {}
            if not symbol and extra.get('Company_Symbol'):
                symbol = extra['Company_Symbol']

            # Skip if no symbol
            if not symbol or ' ' in symbol or len(symbol) > 10:
                self.stats['skipped'] += 1
                continue

            # Skip if ISIN already exists
            existing_isin = company.get('isin')
            if existing_isin and len(existing_isin) > 5:
                self.stats['skipped'] += 1
                continue

            self.stats['processed'] += 1

            # Try to get ISIN/WKN
            result = self.get_isin_wkn_from_yfinance(symbol)

            if not result:
                result = self.get_isin_from_openfigi(symbol)

            if result and result.get('isin'):
                update_data = {}
                if result.get('isin'):
                    update_data['isin'] = result['isin']
                if result.get('wkn'):
                    update_data['wkn'] = result['wkn']

                if update_data:
                    if supabase_helper.update_company(company_id, update_data):
                        self.stats['updated'] += 1
                        if self.stats['updated'] % 10 == 0:
                            print(f"   ... {self.stats['updated']} updated")

            # Rate limiting
            time.sleep(0.5)

        print("\n" + "="*70)
        print("  UPDATE COMPLETE!")
        print("="*70)
        print(f"   Processed: {self.stats['processed']}")
        print(f"   Updated: {self.stats['updated']}")
        print(f"   Skipped: {self.stats['skipped']}")

        return True


if __name__ == "__main__":
    updater = ISINWKNUpdater()
    success = updater.run()
    exit(0 if success else 1)
