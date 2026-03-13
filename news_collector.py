#!/usr/bin/env python3
"""
News Collector — Fetches news for companies from RSS feeds and Brave Search API,
matches articles to companies, and stores in company_news table.

Sources:
  - RSS feeds (TechCrunch, Reuters, Yahoo Finance, MarketWatch, Seeking Alpha)
  - Brave Search API (company-specific news search)

Usage:
  python3 news_collector.py                  # dry-run (preview only)
  python3 news_collector.py --apply          # insert into Supabase
  python3 news_collector.py --apply --limit 50  # limit to first 50 companies
  python3 news_collector.py --brave-only     # skip RSS, only Brave Search
  python3 news_collector.py --rss-only       # skip Brave, only RSS feeds
"""

import argparse
import os
import re
import sys
import time
import hashlib
from collections import Counter
from datetime import datetime, timezone
from urllib.parse import urlparse

import requests
from dotenv import load_dotenv

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(SCRIPT_DIR, '.env'))

try:
    import feedparser
except ImportError:
    print("Installing feedparser...")
    os.system(f"{sys.executable} -m pip install feedparser")
    import feedparser

import supabase_helper

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

RSS_FEEDS = [
    {"name": "TechCrunch", "url": "https://techcrunch.com/feed/"},
    {"name": "Reuters", "url": "https://www.reutersagency.com/feed/"},
    {"name": "Yahoo Finance", "url": "https://finance.yahoo.com/news/rssindex"},
    {"name": "MarketWatch", "url": "https://feeds.marketwatch.com/marketwatch/topstories/"},
    {"name": "Seeking Alpha", "url": "https://seekingalpha.com/market_currents.xml"},
]

# Brave Search API config
BRAVE_API_KEY = os.getenv('BRAVE_API_KEY', '')
BRAVE_SEARCH_URL = "https://api.search.brave.com/res/v1/news/search"
BRAVE_RATE_LIMIT_DELAY = 1.0  # seconds between requests (1 req/s free tier)
BRAVE_BATCH_SIZE = 20  # companies per batch before pause
BRAVE_BATCH_PAUSE = 5.0  # seconds between batches

# RSS config
RSS_TIMEOUT = 15  # seconds per feed fetch
RSS_MAX_ENTRIES = 100  # max entries per feed

# Minimum company name length for matching (avoid false positives)
MIN_NAME_LENGTH = 3

# Words to exclude from matching (too generic, cause false positives)
GENERIC_WORDS = {
    'the', 'inc', 'corp', 'ltd', 'llc', 'ag', 'se', 'sa', 'plc', 'gmbh',
    'group', 'holding', 'holdings', 'company', 'technologies', 'technology',
    'systems', 'services', 'solutions', 'capital', 'partners', 'international',
    'global', 'digital', 'media', 'energy', 'bio', 'pharma', 'financial',
    'one', 'two', 'new', 'first', 'next', 'best', 'top', 'pro', 'air',
}


# ---------------------------------------------------------------------------
# Company matching helpers
# ---------------------------------------------------------------------------

def build_company_index(companies: list) -> dict:
    """Build lookup structures for matching news to companies.

    Returns dict with:
      - 'by_symbol': {symbol_upper: company}
      - 'by_name': [(name_lower, company)] sorted longest first
      - 'companies': original list
    """
    by_symbol = {}
    by_name = []

    for c in companies:
        # Index by symbol (exact match)
        symbol = (c.get('symbol') or '').strip().upper()
        if symbol and len(symbol) >= 1:
            # Strip exchange suffix for matching (e.g. TSLA.DE -> TSLA)
            base_symbol = symbol.split('.')[0]
            by_symbol[base_symbol] = c
            if base_symbol != symbol:
                by_symbol[symbol] = c

        # Index by name (substring match)
        name = (c.get('name') or '').strip()
        if len(name) >= MIN_NAME_LENGTH:
            by_name.append((name.lower(), c))

    # Sort by name length descending (match longer names first to avoid partial matches)
    by_name.sort(key=lambda x: -len(x[0]))

    return {
        'by_symbol': by_symbol,
        'by_name': by_name,
        'companies': companies,
    }


def _is_meaningful_name(name: str) -> bool:
    """Check if a company name is specific enough for substring matching."""
    words = re.split(r'[\s\-_]+', name.lower())
    meaningful = [w for w in words if w not in GENERIC_WORDS and len(w) > 2]
    return len(meaningful) >= 1 and len(name) >= 4


def match_article_to_companies(title: str, summary: str, index: dict) -> list:
    """Match an article to companies by symbol or name.

    Returns list of matched company dicts (may be empty).
    """
    text = f"{title} {summary or ''}".upper()
    text_lower = text.lower()
    matched = []
    matched_ids = set()

    # 1. Symbol matching — look for $SYMBOL or standalone symbol in text
    for symbol, company in index['by_symbol'].items():
        if len(symbol) < 2:
            continue
        cid = company['id']
        if cid in matched_ids:
            continue

        # Match $TSLA or word-boundary TSLA (but only for symbols >= 3 chars)
        if f"${symbol}" in text:
            matched.append(company)
            matched_ids.add(cid)
            continue

        if len(symbol) >= 3:
            # Word boundary match to avoid partial matches
            pattern = r'\b' + re.escape(symbol) + r'\b'
            if re.search(pattern, text):
                matched.append(company)
                matched_ids.add(cid)

    # 2. Company name matching — substring in title/summary
    for name_lower, company in index['by_name']:
        cid = company['id']
        if cid in matched_ids:
            continue
        if not _is_meaningful_name(name_lower):
            continue

        # For short names (< 6 chars), require word boundary match
        if len(name_lower) < 6:
            pattern = r'\b' + re.escape(name_lower) + r'\b'
            if re.search(pattern, text_lower):
                matched.append(company)
                matched_ids.add(cid)
        else:
            if name_lower in text_lower:
                matched.append(company)
                matched_ids.add(cid)

    return matched


# ---------------------------------------------------------------------------
# RSS Feed collection
# ---------------------------------------------------------------------------

def fetch_rss_feeds() -> list:
    """Fetch articles from all configured RSS feeds.

    Returns list of dicts with: title, summary, url, source, published_at
    """
    all_articles = []

    for feed_config in RSS_FEEDS:
        feed_name = feed_config['name']
        feed_url = feed_config['url']
        print(f"    Fetching RSS: {feed_name}...")

        try:
            feed = feedparser.parse(feed_url, request_headers={
                'User-Agent': 'BlackfireNewsCollector/1.0'
            })

            if feed.bozo and not feed.entries:
                print(f"      Warning: Feed error for {feed_name}: {feed.bozo_exception}")
                continue

            count = 0
            for entry in feed.entries[:RSS_MAX_ENTRIES]:
                title = (entry.get('title') or '').strip()
                if not title:
                    continue

                summary = (entry.get('summary') or entry.get('description') or '').strip()
                # Strip HTML tags from summary
                summary = re.sub(r'<[^>]+>', '', summary).strip()
                if len(summary) > 500:
                    summary = summary[:497] + '...'

                link = (entry.get('link') or '').strip()
                if not link:
                    continue

                # Parse published date
                published_at = None
                if entry.get('published_parsed'):
                    try:
                        published_at = datetime(*entry.published_parsed[:6],
                                                tzinfo=timezone.utc).isoformat()
                    except Exception:
                        pass
                elif entry.get('updated_parsed'):
                    try:
                        published_at = datetime(*entry.updated_parsed[:6],
                                                tzinfo=timezone.utc).isoformat()
                    except Exception:
                        pass

                all_articles.append({
                    'title': title,
                    'summary': summary if summary else None,
                    'url': link,
                    'source': feed_name,
                    'published_at': published_at,
                })
                count += 1

            print(f"      Got {count} articles")

        except Exception as e:
            print(f"      Error fetching {feed_name}: {e}")

    return all_articles


# ---------------------------------------------------------------------------
# Brave Search collection
# ---------------------------------------------------------------------------

def search_brave_news(company_name: str, symbol: str = None) -> list:
    """Search Brave News API for a specific company.

    Returns list of article dicts.
    """
    if not BRAVE_API_KEY:
        return []

    # Build search query
    query = f'"{company_name}" stock'
    if symbol:
        base_symbol = symbol.split('.')[0]
        query = f'"{company_name}" OR ${base_symbol} stock news'

    headers = {
        'Accept': 'application/json',
        'Accept-Encoding': 'gzip',
        'X-Subscription-Token': BRAVE_API_KEY,
    }
    params = {
        'q': query,
        'count': 5,  # max 5 results per company
        'freshness': 'pw',  # past week
    }

    try:
        resp = requests.get(BRAVE_SEARCH_URL, headers=headers, params=params, timeout=10)
        if resp.status_code == 429:
            print(f"      Rate limited, waiting 60s...")
            time.sleep(60)
            resp = requests.get(BRAVE_SEARCH_URL, headers=headers, params=params, timeout=10)

        if resp.status_code != 200:
            print(f"      Brave API error {resp.status_code} for {company_name}")
            return []

        data = resp.json()
        results = data.get('results', [])

        articles = []
        for r in results:
            title = (r.get('title') or '').strip()
            if not title:
                continue

            articles.append({
                'title': title,
                'summary': (r.get('description') or '')[:500] or None,
                'url': r.get('url', ''),
                'source': f"Brave Search ({urlparse(r.get('url', '')).netloc})",
                'published_at': r.get('age') or r.get('page_age') or None,
            })

        return articles

    except Exception as e:
        print(f"      Brave error for {company_name}: {e}")
        return []


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------

def get_existing_urls(client) -> set:
    """Fetch all existing news URLs from company_news to avoid duplicates."""
    existing = set()
    page_size = 1000
    offset = 0

    while True:
        response = client.table('company_news') \
            .select('url') \
            .range(offset, offset + page_size - 1) \
            .execute()
        batch = response.data
        for row in batch:
            url = row.get('url')
            if url:
                existing.add(url.strip().lower())
        if len(batch) < page_size:
            break
        offset += page_size

    return existing


def normalize_url(url: str) -> str:
    """Normalize URL for deduplication (lowercase, strip tracking params)."""
    url = url.strip().lower()
    # Remove common tracking parameters
    url = re.sub(r'[?&](utm_\w+|ref|source|fbclid|gclid)=[^&]*', '', url)
    # Remove trailing ? or &
    url = url.rstrip('?&')
    return url


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description='Collect news for companies')
    parser.add_argument('--apply', action='store_true', help='Insert results into Supabase')
    parser.add_argument('--limit', type=int, default=0, help='Limit number of companies for Brave Search')
    parser.add_argument('--brave-only', action='store_true', help='Skip RSS, only use Brave Search')
    parser.add_argument('--rss-only', action='store_true', help='Skip Brave, only use RSS feeds')
    args = parser.parse_args()

    dry_run = not args.apply
    start_time = datetime.now()

    print("\n" + "=" * 70)
    print("  NEWS COLLECTOR")
    print(f"  Mode: {'DRY-RUN (preview only)' if dry_run else 'APPLY (inserting to DB)'}")
    print(f"  Time: {start_time.strftime('%Y-%m-%d %H:%M:%S')}")
    if args.brave_only:
        print("  Source: Brave Search only")
    elif args.rss_only:
        print("  Source: RSS feeds only")
    else:
        print("  Source: RSS feeds + Brave Search")
    print("=" * 70)

    # Load companies (prioritize public and pre_ipo)
    print("\n  Loading companies...")
    companies = supabase_helper.get_all_companies('id, name, symbol, listing_status')
    total_companies = len(companies)

    # Filter to public/pre_ipo first
    priority_companies = [
        c for c in companies
        if (c.get('listing_status') or '').lower() in ('public', 'pre_ipo')
    ]
    other_companies = [
        c for c in companies
        if (c.get('listing_status') or '').lower() not in ('public', 'pre_ipo')
    ]

    print(f"  Total companies: {total_companies}")
    print(f"  Priority (public/pre_ipo): {len(priority_companies)}")
    print(f"  Other: {len(other_companies)}")

    # Build company index for matching
    company_index = build_company_index(companies)

    # Load existing URLs for deduplication
    client = supabase_helper.get_client()
    print("\n  Loading existing news URLs for deduplication...")
    existing_urls = get_existing_urls(client)
    print(f"  Existing news articles: {len(existing_urls)}")

    # Stats
    stats = Counter()
    news_to_insert = []  # list of dicts ready for DB insert

    # -----------------------------------------------------------------------
    # Phase 1: RSS Feeds — collect articles and match to companies
    # -----------------------------------------------------------------------
    if not args.brave_only:
        print("\n  Phase 1: RSS Feed Collection")
        print("  " + "-" * 40)

        rss_articles = fetch_rss_feeds()
        stats['rss_articles_total'] = len(rss_articles)
        print(f"\n  Total RSS articles: {len(rss_articles)}")

        print("  Matching articles to companies...")
        for article in rss_articles:
            url_normalized = normalize_url(article['url'])
            if url_normalized in existing_urls:
                stats['rss_duplicates'] += 1
                continue

            matched = match_article_to_companies(
                article['title'], article.get('summary', ''), company_index
            )

            if matched:
                for company in matched:
                    news_to_insert.append({
                        'company_id': company['id'],
                        'title': article['title'][:500],
                        'summary': article.get('summary'),
                        'url': article['url'][:2000],
                        'source': article['source'],
                        'sentiment': None,
                        'published_at': article.get('published_at'),
                        'fetched_at': datetime.now(timezone.utc).isoformat(),
                    })
                    stats['rss_matched'] += 1
                    # Add to existing URLs to prevent intra-run duplicates
                    existing_urls.add(url_normalized)
            else:
                stats['rss_unmatched'] += 1

    # -----------------------------------------------------------------------
    # Phase 2: Brave Search — targeted search for priority companies
    # -----------------------------------------------------------------------
    if not args.rss_only and BRAVE_API_KEY:
        print("\n  Phase 2: Brave Search (targeted)")
        print("  " + "-" * 40)

        # Use priority companies for Brave Search
        brave_companies = priority_companies
        if args.limit > 0:
            brave_companies = brave_companies[:args.limit]

        print(f"  Searching for {len(brave_companies)} companies...")

        for i, company in enumerate(brave_companies):
            name = (company.get('name') or '').strip()
            symbol = (company.get('symbol') or '').strip()

            if not name:
                continue

            articles = search_brave_news(name, symbol)
            stats['brave_searches'] += 1

            for article in articles:
                if not article.get('url'):
                    continue

                url_normalized = normalize_url(article['url'])
                if url_normalized in existing_urls:
                    stats['brave_duplicates'] += 1
                    continue

                # Parse published_at from Brave's age format (e.g. "2 hours ago")
                published_at = article.get('published_at')
                if published_at and not published_at.startswith('20'):
                    # It's a relative time string, not ISO — set to None
                    published_at = None

                news_to_insert.append({
                    'company_id': company['id'],
                    'title': article['title'][:500],
                    'summary': article.get('summary'),
                    'url': article['url'][:2000],
                    'source': article['source'],
                    'sentiment': None,
                    'published_at': published_at,
                    'fetched_at': datetime.now(timezone.utc).isoformat(),
                })
                stats['brave_matched'] += 1
                existing_urls.add(url_normalized)

            # Rate limiting
            if (i + 1) % BRAVE_BATCH_SIZE == 0:
                print(f"    ... {i + 1}/{len(brave_companies)} companies searched, pausing {BRAVE_BATCH_PAUSE}s...")
                time.sleep(BRAVE_BATCH_PAUSE)
            else:
                time.sleep(BRAVE_RATE_LIMIT_DELAY)

            # Progress
            if (i + 1) % 50 == 0:
                print(f"    ... {i + 1}/{len(brave_companies)} companies searched")

    elif not args.rss_only and not BRAVE_API_KEY:
        print("\n  Phase 2: SKIPPED (no BRAVE_API_KEY in .env)")

    # -----------------------------------------------------------------------
    # Summary & Insert
    # -----------------------------------------------------------------------
    print("\n  " + "=" * 50)
    print("  RESULTS")
    print("  " + "=" * 50)

    print(f"\n  RSS feeds:")
    print(f"    Articles fetched:    {stats.get('rss_articles_total', 0)}")
    print(f"    Matched to company:  {stats.get('rss_matched', 0)}")
    print(f"    Unmatched:           {stats.get('rss_unmatched', 0)}")
    print(f"    Duplicates skipped:  {stats.get('rss_duplicates', 0)}")

    print(f"\n  Brave Search:")
    print(f"    Companies searched:  {stats.get('brave_searches', 0)}")
    print(f"    Articles found:      {stats.get('brave_matched', 0)}")
    print(f"    Duplicates skipped:  {stats.get('brave_duplicates', 0)}")

    print(f"\n  Total new articles:    {len(news_to_insert)}")

    # Show sample articles
    if news_to_insert:
        print(f"\n  Sample articles (first 15):")
        # Find company names for display
        company_map = {c['id']: c.get('name', '?') for c in companies}
        for article in news_to_insert[:15]:
            cname = company_map.get(article['company_id'], '?')[:30]
            title = article['title'][:50]
            print(f"    [{article['source'][:15]:15s}] {cname:30s} | {title}")

    # Insert into Supabase
    if not dry_run and news_to_insert:
        print(f"\n  Inserting {len(news_to_insert)} articles into company_news...")
        inserted = 0
        errors = 0
        batch_size = 50  # insert in batches of 50

        for i in range(0, len(news_to_insert), batch_size):
            batch = news_to_insert[i:i + batch_size]
            try:
                client.table('company_news').insert(batch).execute()
                inserted += len(batch)
                if (i + batch_size) % 200 == 0 or i + batch_size >= len(news_to_insert):
                    print(f"    ... {inserted}/{len(news_to_insert)} inserted")
            except Exception as e:
                errors += len(batch)
                print(f"    Error inserting batch at offset {i}: {e}")
                # Try inserting one by one for this batch
                for single in batch:
                    try:
                        client.table('company_news').insert(single).execute()
                        inserted += 1
                        errors -= 1
                    except Exception as e2:
                        print(f"      Failed single insert: {single['title'][:40]}... ({e2})")

        print(f"\n  Inserted: {inserted}")
        if errors > 0:
            print(f"  Errors:   {errors}")
    elif dry_run and news_to_insert:
        print(f"\n  Run with --apply to insert {len(news_to_insert)} articles into Supabase")

    # Log to sync_history
    end_time = datetime.now()
    duration = (end_time - start_time).total_seconds()
    print(f"\n  Duration: {duration:.1f}s")

    if not dry_run:
        supabase_helper.log_sync_history({
            'name': 'News Collector',
            'start_time': start_time,
            'end_time': end_time,
            'success': True,
            'updates': len(news_to_insert),
            'creates': len(news_to_insert),
            'db_companies': total_companies,
        })

    print("\n  Done!")


if __name__ == '__main__':
    main()
