#!/usr/bin/env python3
"""
Hybrid ISIN → Ticker Mapper
1. Primary: OpenFIGI (präzise, kostenlos)
2. Fallback: ChatGPT (für unbekannte ISINs)
3. Validation: yfinance (Preis-Check)
"""

import os
import json
import requests
import time
import yfinance as yf
from dotenv import load_dotenv

load_dotenv()

class HybridISINMapper:
    def __init__(self):
        self.openai_api_key = os.getenv('OPENAI_API_KEY')
        self.cache = {}  # In-memory cache
        self.batch_size = 100  # Process 100 ISINs per batch

    def map_isin_openfigi(self, isin):
        """Map ISIN using OpenFIGI (primary method)"""
        if not isin or len(isin) != 12:
            return None

        try:
            url = "https://api.openfigi.com/v3/mapping"

            headers = {
                'Content-Type': 'application/json'
            }

            query = {"idType": "ID_ISIN", "idValue": isin}

            response = requests.post(
                url,
                headers=headers,
                json=[query],
                timeout=10
            )

            if response.status_code == 200:
                data = response.json()
                if data and len(data) > 0 and 'data' in data[0]:
                    results = data[0]['data']
                    if results:
                        # Find best match
                        for result in results:
                            ticker = result.get('ticker', '')
                            exch_code = result.get('exchCode', '')

                            # Prefer major exchanges
                            if exch_code in ['GY', 'GR', 'GF']:  # German
                                return f"{ticker}.DE"
                            elif exch_code == 'US':
                                return ticker
                            elif exch_code in ['SW', 'VX']:  # Swiss
                                return ticker

                        # Fallback to first result
                        ticker = results[0].get('ticker', '')
                        if ticker:
                            if isin.startswith('DE'):
                                return f"{ticker}.DE"
                            return ticker

            # Rate limit (10/min = 6 sec wait)
            time.sleep(0.3)

        except Exception as e:
            pass

        return None

    def validate_ticker(self, ticker):
        """Validate ticker with yfinance"""
        try:
            yf_ticker = yf.Ticker(ticker)
            info = yf_ticker.info
            price = info.get('currentPrice') or info.get('regularMarketPrice')
            return price and price > 0
        except:
            return False

    def map_batch_openfigi(self, isin_list):
        """Map batch of ISINs using OpenFIGI"""
        results = {}

        print(f"   Processing {len(isin_list)} ISINs with OpenFIGI...")

        for i, isin in enumerate(isin_list):
            if isin in self.cache:
                results[isin] = self.cache[isin]
                continue

            ticker = self.map_isin_openfigi(isin)

            if ticker and self.validate_ticker(ticker):
                results[isin] = ticker
                self.cache[isin] = ticker

            if (i + 1) % 10 == 0:
                print(f"      ... {i + 1}/{len(isin_list)} processed")

        return results

    def map_with_chatgpt_fallback(self, failed_isins):
        """Use ChatGPT for ISINs where OpenFIGI failed"""
        if not failed_isins or not self.openai_api_key:
            return {}

        print(f"   Using ChatGPT fallback for {len(failed_isins)} ISINs...")

        # Batch into groups of 20
        results = {}

        for i in range(0, len(failed_isins), 20):
            batch = failed_isins[i:i+20]

            isins_text = "\n".join([f"- {isin}" for isin in batch])

            prompt = f"""Map these ISINs to Yahoo Finance ticker symbols. Output ONLY valid JSON.

ISINs:
{isins_text}

Rules:
- German stocks: Add .DE (e.g., SAP.DE, BMW.DE, SIE.DE)
- US stocks: No suffix (e.g., AAPL, MSFT)
- Unknown: Skip it

JSON format:
{{"ISIN1": "TICKER1", "ISIN2": "TICKER2"}}"""

            headers = {
                'Authorization': f'Bearer {self.openai_api_key}',
                'Content-Type': 'application/json'
            }

            payload = {
                'model': 'gpt-4o-mini',
                'messages': [
                    {'role': 'system', 'content': 'Financial data expert. Output ONLY valid JSON, no markdown.'},
                    {'role': 'user', 'content': prompt}
                ],
                'temperature': 0.1,
                'max_tokens': 500
            }

            try:
                response = requests.post(
                    'https://api.openai.com/v1/chat/completions',
                    headers=headers,
                    json=payload,
                    timeout=30
                )

                if response.status_code == 200:
                    data = response.json()
                    content = data['choices'][0]['message']['content'].strip()

                    # Clean response
                    if '```' in content:
                        content = content.split('```')[1]
                        if content.startswith('json'):
                            content = content[4:]
                        content = content.strip()

                    # Parse JSON
                    mappings = json.loads(content)

                    # Validate each ticker
                    for isin, ticker in mappings.items():
                        if ticker and ticker != 'UNKNOWN':
                            if self.validate_ticker(ticker):
                                results[isin] = ticker
                                self.cache[isin] = ticker

            except Exception as e:
                print(f"      ⚠️  ChatGPT batch failed: {e}")

        return results


if __name__ == "__main__":
    # Test
    mapper = HybridISINMapper()

    print("=" * 70)
    print("🧪 Testing Hybrid ISIN Mapper")
    print("=" * 70)

    test_isins = [
        'DE0007164600',  # SAP
        'DE0005190003',  # BMW
        'DE0007236101',  # Siemens
        'US0378331005',  # Apple
        'US5949181045'   # Microsoft
    ]

    print(f"\nTesting with {len(test_isins)} ISINs...")

    results = mapper.map_batch_openfigi(test_isins)

    print("\n✅ Results:")
    for isin, ticker in results.items():
        print(f"  {isin} → {ticker}")

    print("\n" + "=" * 70)
