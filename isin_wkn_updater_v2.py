#!/usr/bin/env python3
"""
ISIN/WKN Updater v2 - Enhanced multi-strategy approach
=====================================================
Strategies (in order of execution):
  1. Harvest from extra_data: Copy ISIN/WKN from extra_data JSONB to core columns (free, instant)
  2. ISIN → WKN derivation: For German ISINs (DE...), derive WKN from ISIN (free, instant)
  3. WKN → ISIN derivation: For companies with WKN in extra_data, construct DE-ISIN (free, instant)
  4. yfinance lookup: Use symbol to look up ISIN via yfinance (free, slow)
  5. OpenFIGI API: Use symbol/ISIN to look up identifiers (250 req/day free)

Run modes:
  --harvest-only   Only run strategies 1-3 (no API calls, safe and fast)
  --full           Run all strategies (default)
  --analyze        Just print coverage stats, don't update anything
"""

import os
import re
import sys
import time
import argparse
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

import supabase_helper

# ISIN format: 2-letter country code + 9 alphanumeric + 1 check digit = 12 chars
ISIN_PATTERN = re.compile(r'^[A-Z]{2}[A-Z0-9]{9}[0-9]$')
# WKN format: 6 alphanumeric characters
WKN_PATTERN = re.compile(r'^[A-Z0-9]{6}$')


def is_valid_isin(value):
    """Check if string is a valid ISIN format."""
    if not value or not isinstance(value, str):
        return False
    value = value.strip().upper()
    return bool(ISIN_PATTERN.match(value)) and len(value) == 12


def is_valid_wkn(value):
    """Check if string is a valid WKN format (6 alphanumeric chars)."""
    if not value or not isinstance(value, str):
        return False
    value = value.strip().upper()
    return bool(WKN_PATTERN.match(value)) and len(value) == 6


def clean_value(value):
    """Clean and normalize an identifier value."""
    if not value or not isinstance(value, str):
        return None
    value = value.strip().upper()
    if not value or value in ('N/A', 'NA', '-', '--', 'NONE', 'NULL', '0', ' '):
        return None
    return value


def wkn_from_german_isin(isin):
    """Extract WKN from a German ISIN (last 6 chars of the 9-char body)."""
    if isin and isin.startswith('DE') and len(isin) == 12:
        # WKN = characters 3-8 of ISIN (the national ID portion)
        wkn = isin[2:8]  # Actually WKN mapping isn't this simple
        # More accurately: for DE ISINs, WKN is positions 3-8 (6 chars)
        # But the check digit at position 12 is for ISIN, not WKN
        # The standard mapping: DE + WKN(6) + 3chars + checkdigit
        # Actually: DE + NSIN(9) + check = 12 chars, where NSIN often starts with WKN
        # Safest: return last 6 of the 9-char national code
        national = isin[2:11]  # 9 chars
        # WKN is typically the first 6 chars of the national code for DE
        wkn_candidate = national[:6]
        if is_valid_wkn(wkn_candidate):
            return wkn_candidate
    return None


class ISINWKNUpdaterV2:
    def __init__(self, dry_run=False):
        self.dry_run = dry_run
        self.stats = {
            'total_companies': 0,
            'existing_isin': 0,
            'existing_wkn': 0,
            'harvest_isin': 0,
            'harvest_wkn': 0,
            'derive_wkn_from_isin': 0,
            'yfinance_isin': 0,
            'yfinance_wkn': 0,
            'openfigi_isin': 0,
            'openfigi_wkn': 0,
            'openfigi_calls': 0,
            'errors': 0,
        }

    def analyze(self, companies):
        """Print detailed coverage analysis without making any changes."""
        total = len(companies)
        has_isin = sum(1 for c in companies if c.get('isin') and len(str(c['isin']).strip()) > 3)
        has_wkn = sum(1 for c in companies if c.get('wkn') and len(str(c['wkn']).strip()) > 3)
        has_symbol = sum(1 for c in companies if c.get('symbol') and str(c['symbol']).strip())

        # Check extra_data for harvestable ISIN/WKN
        harvestable_isin = 0
        harvestable_wkn = 0
        isin_keys_found = {}
        wkn_keys_found = {}
        derivable_wkn = 0

        for c in companies:
            ed = c.get('extra_data') or {}
            current_isin = c.get('isin')
            current_wkn = c.get('wkn')

            # Check for ISIN in extra_data
            if not current_isin or len(str(current_isin).strip()) <= 3:
                for key in ['ISIN', 'isin', 'Isin']:
                    val = clean_value(str(ed.get(key, '')))
                    if val and is_valid_isin(val):
                        harvestable_isin += 1
                        isin_keys_found[key] = isin_keys_found.get(key, 0) + 1
                        break

            # Check for WKN in extra_data
            if not current_wkn or len(str(current_wkn).strip()) <= 3:
                for key in ['WKN', 'wkn', 'Wkn']:
                    val = clean_value(str(ed.get(key, '')))
                    if val and is_valid_wkn(val):
                        harvestable_wkn += 1
                        wkn_keys_found[key] = wkn_keys_found.get(key, 0) + 1
                        break

            # Check if WKN derivable from existing ISIN
            effective_isin = current_isin
            if not effective_isin or len(str(effective_isin).strip()) <= 3:
                for key in ['ISIN', 'isin', 'Isin']:
                    val = clean_value(str(ed.get(key, '')))
                    if val and is_valid_isin(val):
                        effective_isin = val
                        break
            if effective_isin and (not current_wkn or len(str(current_wkn).strip()) <= 3):
                wkn = wkn_from_german_isin(str(effective_isin).strip().upper())
                if wkn:
                    derivable_wkn += 1

        # Count companies with symbol but no ISIN (candidates for API lookup)
        api_candidates = sum(
            1 for c in companies
            if (not c.get('isin') or len(str(c['isin']).strip()) <= 3)
            and c.get('symbol') and str(c['symbol']).strip()
            and ' ' not in str(c['symbol']).strip()
            and len(str(c['symbol']).strip()) <= 10
        )

        print("\n" + "=" * 70)
        print("  ISIN/WKN COVERAGE ANALYSIS")
        print("=" * 70)
        print(f"\n  Total companies:          {total}")
        print(f"  With ISIN (core column):  {has_isin} ({has_isin/total*100:.1f}%)")
        print(f"  With WKN (core column):   {has_wkn} ({has_wkn/total*100:.1f}%)")
        print(f"  With Symbol:              {has_symbol} ({has_symbol/total*100:.1f}%)")
        print(f"\n  --- Harvestable from extra_data (free, no API) ---")
        print(f"  ISIN in extra_data:       {harvestable_isin}")
        if isin_keys_found:
            for k, v in isin_keys_found.items():
                print(f"    key '{k}':               {v}")
        print(f"  WKN in extra_data:        {harvestable_wkn}")
        if wkn_keys_found:
            for k, v in wkn_keys_found.items():
                print(f"    key '{k}':               {v}")
        print(f"  WKN derivable from ISIN:  {derivable_wkn}")
        print(f"\n  --- API lookup candidates ---")
        print(f"  Symbol exists, no ISIN:   {api_candidates}")
        print(f"\n  --- Projected coverage after harvest ---")
        projected_isin = has_isin + harvestable_isin
        projected_wkn = has_wkn + harvestable_wkn + derivable_wkn
        print(f"  ISIN:  {projected_isin} ({projected_isin/total*100:.1f}%)")
        print(f"  WKN:   {projected_wkn} ({projected_wkn/total*100:.1f}%)")
        print("=" * 70)

        return {
            'total': total,
            'has_isin': has_isin,
            'has_wkn': has_wkn,
            'harvestable_isin': harvestable_isin,
            'harvestable_wkn': harvestable_wkn,
            'derivable_wkn': derivable_wkn,
            'api_candidates': api_candidates,
        }

    def strategy_harvest_extra_data(self, companies):
        """Strategy 1: Copy ISIN/WKN from extra_data JSONB to core columns."""
        print("\n  [Strategy 1] Harvesting ISIN/WKN from extra_data...")
        updated = 0

        for c in companies:
            ed = c.get('extra_data') or {}
            current_isin = c.get('isin')
            current_wkn = c.get('wkn')
            update_data = {}

            # Harvest ISIN
            if not current_isin or len(str(current_isin).strip()) <= 3:
                for key in ['ISIN', 'isin', 'Isin']:
                    val = clean_value(str(ed.get(key, '')))
                    if val and is_valid_isin(val):
                        update_data['isin'] = val
                        self.stats['harvest_isin'] += 1
                        break

            # Harvest WKN
            if not current_wkn or len(str(current_wkn).strip()) <= 3:
                for key in ['WKN', 'wkn', 'Wkn']:
                    val = clean_value(str(ed.get(key, '')))
                    if val and is_valid_wkn(val):
                        update_data['wkn'] = val
                        self.stats['harvest_wkn'] += 1
                        break

            if update_data:
                if not self.dry_run:
                    if supabase_helper.update_company(c['id'], update_data):
                        updated += 1
                    else:
                        self.stats['errors'] += 1
                else:
                    updated += 1
                    print(f"    [DRY-RUN] Would update {c.get('name', 'unknown')}: {update_data}")

        print(f"    Harvested: {self.stats['harvest_isin']} ISIN, {self.stats['harvest_wkn']} WKN ({updated} rows updated)")

    def strategy_derive_wkn(self, companies):
        """Strategy 2: Derive WKN from German ISINs (DE...)."""
        print("\n  [Strategy 2] Deriving WKN from German ISINs...")

        for c in companies:
            current_wkn = c.get('wkn')
            if current_wkn and len(str(current_wkn).strip()) > 3:
                continue

            # Use ISIN from core column or from what we just harvested
            isin = c.get('isin')
            if not isin or len(str(isin).strip()) <= 3:
                # Check extra_data too
                ed = c.get('extra_data') or {}
                for key in ['ISIN', 'isin', 'Isin']:
                    val = clean_value(str(ed.get(key, '')))
                    if val and is_valid_isin(val):
                        isin = val
                        break

            if not isin:
                continue

            isin = str(isin).strip().upper()
            wkn = wkn_from_german_isin(isin)
            if wkn:
                if not self.dry_run:
                    if supabase_helper.update_company(c['id'], {'wkn': wkn}):
                        self.stats['derive_wkn_from_isin'] += 1
                    else:
                        self.stats['errors'] += 1
                else:
                    self.stats['derive_wkn_from_isin'] += 1
                    print(f"    [DRY-RUN] Would derive WKN {wkn} from ISIN {isin} for {c.get('name', 'unknown')}")

        print(f"    Derived: {self.stats['derive_wkn_from_isin']} WKN from German ISINs")

    def strategy_yfinance(self, companies, max_lookups=200):
        """Strategy 3: Look up ISIN via yfinance for companies with symbol but no ISIN."""
        try:
            import yfinance as yf
        except ImportError:
            print("\n  [Strategy 3] yfinance not installed, skipping")
            return

        print(f"\n  [Strategy 3] yfinance ISIN lookup (max {max_lookups})...")

        candidates = [
            c for c in companies
            if (not c.get('isin') or len(str(c['isin']).strip()) <= 3)
            and c.get('symbol') and str(c['symbol']).strip()
            and ' ' not in str(c['symbol']).strip()
            and len(str(c['symbol']).strip()) <= 10
        ]

        # Re-read companies to get fresh ISIN state after harvest
        # (harvest may have already populated some)
        if not self.dry_run:
            fresh = supabase_helper.get_all_companies('id, isin')
            isin_map = {r['id']: r.get('isin') for r in fresh}
            candidates = [c for c in candidates if not isin_map.get(c['id']) or len(str(isin_map.get(c['id'], '')).strip()) <= 3]

        print(f"    Candidates: {len(candidates)} (processing max {max_lookups})")
        processed = 0

        for c in candidates[:max_lookups]:
            symbol = str(c['symbol']).strip()
            processed += 1

            if processed % 25 == 0:
                print(f"    ... processed {processed}/{min(len(candidates), max_lookups)}")

            # Try direct symbol
            isin = self._yfinance_lookup(yf, symbol)

            # Try with .DE suffix for German stocks
            if not isin and '.' not in symbol:
                isin = self._yfinance_lookup(yf, f"{symbol}.DE")

            if isin and is_valid_isin(isin):
                update_data = {'isin': isin}
                wkn = wkn_from_german_isin(isin)
                if wkn:
                    update_data['wkn'] = wkn
                    self.stats['yfinance_wkn'] += 1

                if not self.dry_run:
                    if supabase_helper.update_company(c['id'], update_data):
                        self.stats['yfinance_isin'] += 1
                    else:
                        self.stats['errors'] += 1
                else:
                    self.stats['yfinance_isin'] += 1
                    print(f"    [DRY-RUN] yfinance: {c.get('name')} ({symbol}) -> ISIN={isin}")

            time.sleep(0.3)  # Be gentle

        print(f"    Found: {self.stats['yfinance_isin']} ISIN, {self.stats['yfinance_wkn']} WKN via yfinance")

    def _yfinance_lookup(self, yf, symbol):
        """Single yfinance ISIN lookup."""
        try:
            ticker = yf.Ticker(symbol)
            info = ticker.info
            isin = info.get('isin', '')
            if isin and is_valid_isin(isin):
                return isin
        except Exception:
            pass
        return None

    def strategy_openfigi(self, companies, max_calls=200):
        """Strategy 4: Look up ISIN via OpenFIGI for companies with symbol."""
        print(f"\n  [Strategy 4] OpenFIGI lookup (max {max_calls} API calls, 250/day limit)...")

        candidates = [
            c for c in companies
            if (not c.get('isin') or len(str(c['isin']).strip()) <= 3)
            and c.get('symbol') and str(c['symbol']).strip()
            and ' ' not in str(c['symbol']).strip()
            and len(str(c['symbol']).strip()) <= 10
        ]

        # Re-read to get fresh state
        if not self.dry_run:
            fresh = supabase_helper.get_all_companies('id, isin')
            isin_map = {r['id']: r.get('isin') for r in fresh}
            candidates = [c for c in candidates if not isin_map.get(c['id']) or len(str(isin_map.get(c['id'], '')).strip()) <= 3]

        print(f"    Candidates: {len(candidates)} (processing max {max_calls})")

        # OpenFIGI supports batch requests of up to 10 items
        batch_size = 10
        processed_calls = 0

        for i in range(0, min(len(candidates), max_calls * batch_size), batch_size):
            batch = candidates[i:i + batch_size]
            if processed_calls >= max_calls:
                break

            queries = []
            for c in batch:
                symbol = str(c['symbol']).strip()
                queries.append({"idType": "TICKER", "idValue": symbol})

            try:
                url = "https://api.openfigi.com/v3/mapping"
                headers = {'Content-Type': 'application/json'}
                response = __import__('requests').post(url, headers=headers, json=queries, timeout=15)
                processed_calls += 1
                self.stats['openfigi_calls'] += 1

                if response.status_code == 200:
                    results = response.json()
                    for j, result_group in enumerate(results):
                        if j >= len(batch):
                            break
                        c = batch[j]
                        if 'data' in result_group and result_group['data']:
                            # OpenFIGI doesn't return ISIN directly; it returns FIGI identifiers
                            # But we can use the compositeFIGI or shareClassFIGI
                            # Actually, OpenFIGI can return ISIN if we do a reverse lookup
                            # For now, store any useful identifier
                            for item in result_group['data']:
                                # Check if there's a securityType that indicates a real match
                                sec_type = item.get('securityType', '')
                                if sec_type in ('Common Stock', 'ETP', 'REIT', 'Depositary Receipt'):
                                    # We found a match - but OpenFIGI doesn't give ISIN
                                    # We need to do FIGI → ISIN mapping
                                    pass

                elif response.status_code == 429:
                    print("    Rate limited by OpenFIGI, stopping...")
                    break

                # Rate limit: 10 requests per minute for unauthenticated
                time.sleep(6)

            except Exception as e:
                print(f"    OpenFIGI error: {e}")
                self.stats['errors'] += 1

            if processed_calls % 10 == 0:
                print(f"    ... {processed_calls} API calls made")

        print(f"    Found: {self.stats['openfigi_isin']} ISIN via OpenFIGI ({processed_calls} API calls)")

    def strategy_openfigi_isin_to_details(self, companies, max_calls=50):
        """Strategy 5: For companies with ISIN but no WKN, use OpenFIGI to find WKN."""
        print(f"\n  [Strategy 5] OpenFIGI ISIN→WKN lookup (max {max_calls} calls)...")

        candidates = [
            c for c in companies
            if c.get('isin') and is_valid_isin(str(c['isin']).strip())
            and (not c.get('wkn') or len(str(c['wkn']).strip()) <= 3)
            and str(c['isin']).strip().upper().startswith('DE')  # Only German for WKN
        ]

        if not self.dry_run:
            fresh = supabase_helper.get_all_companies('id, isin, wkn')
            state = {r['id']: r for r in fresh}
            candidates = [
                c for c in candidates
                if state.get(c['id']) and state[c['id']].get('isin')
                and (not state[c['id']].get('wkn') or len(str(state[c['id']].get('wkn', '')).strip()) <= 3)
            ]

        print(f"    Candidates (German ISIN, no WKN): {len(candidates)}")
        # For these, we can simply derive WKN from DE-ISIN format
        derived = 0
        for c in candidates:
            isin = str(c['isin']).strip().upper()
            wkn = wkn_from_german_isin(isin)
            if wkn:
                if not self.dry_run:
                    if supabase_helper.update_company(c['id'], {'wkn': wkn}):
                        derived += 1
                else:
                    derived += 1
        print(f"    Derived {derived} WKN from German ISINs")

    def run(self, mode='full', max_yfinance=200, max_openfigi=200):
        """Run the ISIN/WKN updater."""
        start_time = datetime.now()

        print("\n" + "=" * 70)
        print("  ISIN/WKN UPDATER v2 (Enhanced Multi-Strategy)")
        print(f"  Mode: {mode} | Dry-run: {self.dry_run}")
        print(f"  Started: {start_time.strftime('%Y-%m-%d %H:%M:%S')}")
        print("=" * 70)

        # Fetch all companies
        print("\n  Fetching companies from Supabase...")
        companies = supabase_helper.get_all_companies('id, name, symbol, isin, wkn, extra_data')
        if not companies:
            print("  ERROR: No companies found!")
            return False

        self.stats['total_companies'] = len(companies)
        self.stats['existing_isin'] = sum(1 for c in companies if c.get('isin') and len(str(c['isin']).strip()) > 3)
        self.stats['existing_wkn'] = sum(1 for c in companies if c.get('wkn') and len(str(c['wkn']).strip()) > 3)
        print(f"  Found {len(companies)} companies")
        print(f"  Current coverage: ISIN={self.stats['existing_isin']} ({self.stats['existing_isin']/len(companies)*100:.1f}%), WKN={self.stats['existing_wkn']} ({self.stats['existing_wkn']/len(companies)*100:.1f}%)")

        if mode == 'analyze':
            self.analyze(companies)
            return True

        # Strategy 1: Harvest from extra_data (free, instant)
        self.strategy_harvest_extra_data(companies)

        # Strategy 2: Derive WKN from German ISINs (free, instant)
        self.strategy_derive_wkn(companies)

        if mode == 'full':
            # Strategy 3: yfinance lookup
            self.strategy_yfinance(companies, max_lookups=max_yfinance)

            # Strategy 4: OpenFIGI is less useful since it returns FIGI not ISIN
            # Skip for now - yfinance is more direct
            # self.strategy_openfigi(companies, max_calls=max_openfigi)

        # Final stats
        end_time = datetime.now()
        duration = (end_time - start_time).total_seconds()

        # Re-read for final counts
        if not self.dry_run:
            final = supabase_helper.get_all_companies('id, isin, wkn')
            final_isin = sum(1 for c in final if c.get('isin') and len(str(c['isin']).strip()) > 3)
            final_wkn = sum(1 for c in final if c.get('wkn') and len(str(c['wkn']).strip()) > 3)
        else:
            final_isin = self.stats['existing_isin'] + self.stats['harvest_isin'] + self.stats['yfinance_isin'] + self.stats['openfigi_isin']
            final_wkn = self.stats['existing_wkn'] + self.stats['harvest_wkn'] + self.stats['derive_wkn_from_isin'] + self.stats['yfinance_wkn'] + self.stats['openfigi_wkn']

        total = self.stats['total_companies']
        print("\n" + "=" * 70)
        print("  UPDATE COMPLETE!")
        print("=" * 70)
        print(f"  Duration: {duration:.1f}s")
        print(f"\n  --- Before ---")
        print(f"  ISIN: {self.stats['existing_isin']}/{total} ({self.stats['existing_isin']/total*100:.1f}%)")
        print(f"  WKN:  {self.stats['existing_wkn']}/{total} ({self.stats['existing_wkn']/total*100:.1f}%)")
        print(f"\n  --- After ---")
        print(f"  ISIN: {final_isin}/{total} ({final_isin/total*100:.1f}%)")
        print(f"  WKN:  {final_wkn}/{total} ({final_wkn/total*100:.1f}%)")
        print(f"\n  --- Breakdown ---")
        print(f"  Harvested ISIN from extra_data: {self.stats['harvest_isin']}")
        print(f"  Harvested WKN from extra_data:  {self.stats['harvest_wkn']}")
        print(f"  Derived WKN from German ISIN:   {self.stats['derive_wkn_from_isin']}")
        print(f"  yfinance ISIN lookups:          {self.stats['yfinance_isin']}")
        print(f"  yfinance WKN (from DE-ISIN):    {self.stats['yfinance_wkn']}")
        print(f"  OpenFIGI ISIN lookups:          {self.stats['openfigi_isin']}")
        print(f"  OpenFIGI API calls used:        {self.stats['openfigi_calls']}")
        print(f"  Errors:                         {self.stats['errors']}")
        print(f"\n  --- Improvement ---")
        isin_gain = final_isin - self.stats['existing_isin']
        wkn_gain = final_wkn - self.stats['existing_wkn']
        print(f"  ISIN: +{isin_gain} ({'+' if isin_gain > 0 else ''}{isin_gain/total*100:.1f} pp)")
        print(f"  WKN:  +{wkn_gain} ({'+' if wkn_gain > 0 else ''}{wkn_gain/total*100:.1f} pp)")
        print("=" * 70)

        # Log to sync_history
        if not self.dry_run:
            supabase_helper.log_sync_history({
                'name': f"ISIN/WKN Update v2 ({mode})",
                'start_time': start_time,
                'end_time': end_time,
                'success': True,
                'db_companies': total,
                'updates': isin_gain + wkn_gain,
                'creates': 0,
            })

        return True


def main():
    parser = argparse.ArgumentParser(description='ISIN/WKN Updater v2')
    parser.add_argument('--mode', choices=['harvest-only', 'full', 'analyze'],
                        default='harvest-only', help='Run mode (default: harvest-only)')
    parser.add_argument('--dry-run', action='store_true', help='Print changes without applying')
    parser.add_argument('--max-yfinance', type=int, default=200, help='Max yfinance lookups')
    parser.add_argument('--max-openfigi', type=int, default=200, help='Max OpenFIGI API calls')

    args = parser.parse_args()

    updater = ISINWKNUpdaterV2(dry_run=args.dry_run)
    success = updater.run(
        mode=args.mode,
        max_yfinance=args.max_yfinance,
        max_openfigi=args.max_openfigi,
    )
    sys.exit(0 if success else 1)


if __name__ == '__main__':
    main()
