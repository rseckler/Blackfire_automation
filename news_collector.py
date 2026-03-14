#!/usr/bin/env python3
"""
News Collector — Fetches news for companies from RSS feeds and Brave Search API,
matches articles to companies, and stores in company_news table.

Sources:
  - RSS feeds (17 feeds: TechCrunch, Reuters, Yahoo Finance, MarketWatch, Seeking Alpha,
    Benzinga, CNBC, Bloomberg, FT, Barron's, CoinDesk, PR Newswire, GlobeNewsWire,
    VentureBeat, The Verge, Ars Technica, Hacker News)
  - Brave Search API (company-specific news search)

Usage:
  python3 news_collector.py                  # dry-run (preview only)
  python3 news_collector.py --apply          # insert into Supabase
  python3 news_collector.py --apply --limit 50  # limit to first 50 companies
  python3 news_collector.py --brave-only     # skip RSS, only Brave Search
  python3 news_collector.py --rss-only       # skip Brave, only RSS feeds
  python3 news_collector.py --no-sentiment   # skip sentiment analysis (faster)
  python3 news_collector.py --backfill-sentiment  # backfill sentiment on existing articles
"""

import argparse
import atexit
import json
import os
import re
import signal
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

try:
    from anthropic import Anthropic
except ImportError:
    print("Installing anthropic...")
    os.system(f"{sys.executable} -m pip install anthropic")
    from anthropic import Anthropic

import supabase_helper

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

RSS_FEEDS = [
    # --- Original 5 ---
    {"name": "TechCrunch", "url": "https://techcrunch.com/feed/"},
    {"name": "Reuters", "url": "https://www.reutersagency.com/feed/"},
    {"name": "Yahoo Finance", "url": "https://finance.yahoo.com/news/rssindex"},
    {"name": "MarketWatch", "url": "https://feeds.marketwatch.com/marketwatch/topstories/"},
    {"name": "Seeking Alpha", "url": "https://seekingalpha.com/market_currents.xml"},
    # --- Financial news ---
    {"name": "Benzinga", "url": "https://www.benzinga.com/feed"},
    {"name": "CNBC Tech", "url": "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=19854910"},
    {"name": "Bloomberg Markets", "url": "https://feeds.bloomberg.com/markets/news.rss"},
    {"name": "Financial Times", "url": "https://www.ft.com/rss/home"},
    {"name": "Barron's", "url": "https://www.barrons.com/market-data/rss"},
    {"name": "CoinDesk", "url": "https://www.coindesk.com/arc/outboundfeeds/rss/"},
    # --- Press releases ---
    {"name": "PR Newswire", "url": "https://www.prnewswire.com/rss/news-releases-list.rss"},
    {"name": "GlobeNewsWire", "url": "https://www.globenewswire.com/RssFeed/subjectcode/01-Business%20and%20Financial/feedTitle/GlobeNewswire%20-%20News%20Releases"},
    # --- Tech news ---
    {"name": "VentureBeat", "url": "https://venturebeat.com/feed/"},
    {"name": "The Verge", "url": "https://www.theverge.com/rss/index.xml"},
    {"name": "Ars Technica", "url": "https://feeds.arstechnica.com/arstechnica/index"},
    {"name": "Hacker News", "url": "https://hnrss.org/newest?points=100"},
]

# Brave Search API config
BRAVE_API_KEY = os.getenv('BRAVE_API_KEY', '')
BRAVE_SEARCH_URL = "https://api.search.brave.com/res/v1/news/search"
BRAVE_RATE_LIMIT_DELAY = 1.1  # seconds between requests (1 req/s free tier + margin)
BRAVE_BATCH_SIZE = 20  # companies per batch before pause
BRAVE_BATCH_PAUSE = 5.0  # seconds between batches
BRAVE_MAX_CONSECUTIVE_429 = 3  # stop Brave after this many consecutive 429s
BRAVE_BACKOFF_STEPS = [5, 15, 30]  # exponential backoff seconds on 429
BRAVE_MAX_RUNTIME_MINUTES = 90  # stop Brave Search after this many minutes
BRAVE_ROTATION_GROUPS = 7  # divide remaining companies into 7 daily groups (one per weekday)

# RSS config
RSS_TIMEOUT = 15  # seconds per feed fetch
RSS_MAX_ENTRIES = 30  # max entries per feed

# Sentiment analysis config
SENTIMENT_BATCH_SIZE = 10  # articles per API call
SENTIMENT_RATE_LIMIT_DELAY = 1.0  # seconds between API calls
ANTHROPIC_API_KEY = os.getenv('ANTHROPIC_API_KEY', '')

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
# PID lock — prevent multiple instances running simultaneously
# ---------------------------------------------------------------------------

PID_FILE = '/tmp/news_collector.pid'


def _is_process_running(pid: int) -> bool:
    """Check if a process with given PID is still running."""
    try:
        os.kill(pid, 0)  # signal 0 = check existence, no actual signal sent
        return True
    except (OSError, ProcessLookupError):
        return False


def acquire_pid_lock():
    """Acquire PID lock. Exits if another instance is already running."""
    if os.path.exists(PID_FILE):
        try:
            with open(PID_FILE, 'r') as f:
                old_pid = int(f.read().strip())
            if _is_process_running(old_pid):
                print(f"  Another instance is already running (PID {old_pid}). Exiting.")
                sys.exit(0)
            else:
                print(f"  Stale PID file found (PID {old_pid} not running). Removing.")
                os.remove(PID_FILE)
        except (ValueError, IOError):
            os.remove(PID_FILE)

    # Write our PID
    with open(PID_FILE, 'w') as f:
        f.write(str(os.getpid()))

    # Register cleanup on normal exit
    atexit.register(_remove_pid_lock)

    # Register cleanup on SIGTERM/SIGINT
    def _signal_handler(signum, frame):
        _remove_pid_lock()
        sys.exit(0)

    signal.signal(signal.SIGTERM, _signal_handler)
    signal.signal(signal.SIGINT, _signal_handler)


def _remove_pid_lock():
    """Remove PID file on exit."""
    try:
        if os.path.exists(PID_FILE):
            with open(PID_FILE, 'r') as f:
                pid = int(f.read().strip())
            if pid == os.getpid():
                os.remove(PID_FILE)
    except (ValueError, IOError, OSError):
        pass


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

def search_brave_news(company_name: str, symbol: str = None,
                      consecutive_429_count: int = 0) -> tuple:
    """Search Brave News API for a specific company.

    Returns tuple of (articles_list, was_429: bool).
    The caller uses was_429 to track consecutive rate limits.
    """
    if not BRAVE_API_KEY:
        return [], False

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
            # Exponential backoff: pick delay based on how many consecutive 429s
            backoff_idx = min(consecutive_429_count, len(BRAVE_BACKOFF_STEPS) - 1)
            backoff_secs = BRAVE_BACKOFF_STEPS[backoff_idx]
            print(f"      Rate limited (429), backoff {backoff_secs}s (consecutive: {consecutive_429_count + 1})")
            time.sleep(backoff_secs)
            # Retry once after backoff
            resp = requests.get(BRAVE_SEARCH_URL, headers=headers, params=params, timeout=10)
            if resp.status_code == 429:
                return [], True  # still rate limited

        if resp.status_code != 200:
            print(f"      Brave API error {resp.status_code} for {company_name}")
            return [], False

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

        return articles, False

    except Exception as e:
        print(f"      Brave error for {company_name}: {e}")
        return [], False


def get_watchlist_company_ids(client) -> set:
    """Fetch all company_ids from the watchlist table."""
    ids = set()
    page_size = 1000
    offset = 0
    while True:
        response = client.table('watchlist') \
            .select('company_id') \
            .range(offset, offset + page_size - 1) \
            .execute()
        batch = response.data
        for row in batch:
            cid = row.get('company_id')
            if cid:
                ids.add(cid)
        if len(batch) < page_size:
            break
        offset += page_size
    return ids


def get_high_value_company_ids(client) -> set:
    """Fetch company_ids that are high-value: thier_group top tiers, VIP Defcon 1, or prio_buy <= 2."""
    ids = set()

    # thier_group IN ('2026***', '2026**')
    for tg in ['2026***', '2026**']:
        response = client.table('companies') \
            .select('id') \
            .eq('thier_group', tg) \
            .execute()
        for row in response.data:
            ids.add(row['id'])

    # vip = 'Defcon 1'
    response = client.table('companies') \
        .select('id') \
        .eq('vip', 'Defcon 1') \
        .execute()
    for row in response.data:
        ids.add(row['id'])

    # prio_buy <= 2 (1 or 2)
    for pb in [1, 2]:
        response = client.table('companies') \
            .select('id') \
            .eq('prio_buy', pb) \
            .execute()
        for row in response.data:
            ids.add(row['id'])

    return ids


def build_brave_search_list(priority_companies: list, client, limit: int = 0) -> tuple:
    """Build ordered list of companies for Brave Search.

    Returns (always_search_list, rotation_list) where:
      - always_search_list: watchlist + high-value companies (searched every run)
      - rotation_list: today's rotation slice of remaining public companies
    """
    # Get high-priority company IDs
    watchlist_ids = get_watchlist_company_ids(client)
    highvalue_ids = get_high_value_company_ids(client)
    always_ids = watchlist_ids | highvalue_ids

    # Split priority_companies into always-search and rest
    always_search = []
    rest = []
    for c in priority_companies:
        if c['id'] in always_ids:
            always_search.append(c)
        else:
            rest.append(c)

    # Daily rotation: pick today's slice of the rest
    day_of_year = datetime.now().timetuple().tm_yday
    group_index = day_of_year % BRAVE_ROTATION_GROUPS
    group_size = max(1, len(rest) // BRAVE_ROTATION_GROUPS)
    start = group_index * group_size
    # Last group gets any remainder
    if group_index == BRAVE_ROTATION_GROUPS - 1:
        rotation_slice = rest[start:]
    else:
        rotation_slice = rest[start:start + group_size]

    # Apply --limit if set (applies to total, always_search first)
    if limit > 0:
        if len(always_search) >= limit:
            always_search = always_search[:limit]
            rotation_slice = []
        else:
            remaining = limit - len(always_search)
            rotation_slice = rotation_slice[:remaining]

    return always_search, rotation_slice


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
# Sentiment Analysis (Claude Haiku)
# ---------------------------------------------------------------------------

def _build_sentiment_prompt(articles: list, company_map: dict) -> str:
    """Build prompt for Claude Haiku sentiment analysis."""
    articles_text = []
    for i, article in enumerate(articles):
        company_name = company_map.get(article.get('company_id', ''), 'Unknown')
        summary = article.get('summary') or 'No summary'
        articles_text.append(
            f'{i+1}. "{article["title"]}" about {company_name} - {summary}'
        )

    return f"""Analyze the sentiment of these news articles about companies. For each article, return:
- index: article number (1-based)
- sentiment: "positive", "negative", or "neutral"
- catalyst_type: if this is a potential catalyst, what type? (earnings, fda, partnership, ipo, acquisition, product_launch, regulatory, leadership, funding, null)

Articles:
{chr(10).join(articles_text)}

Return ONLY a JSON array."""


def analyze_sentiment_batch(articles: list, anthropic_client: Anthropic, company_map: dict) -> list:
    """Analyze sentiment for a batch of articles using Claude Haiku.

    Args:
        articles: list of article dicts (must have title, summary, company_id)
        anthropic_client: Anthropic API client
        company_map: {company_id: company_name} mapping

    Returns:
        list of dicts with index, sentiment, catalyst_type
    """
    prompt = _build_sentiment_prompt(articles, company_map)

    try:
        response = anthropic_client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1500,
            messages=[{"role": "user", "content": prompt}]
        )
        text = response.content[0].text.strip()

        # Extract JSON from response (handle markdown code blocks)
        if text.startswith('```'):
            text = text.split('\n', 1)[1]
            text = text.rsplit('```', 1)[0]

        results = json.loads(text)
        return results

    except json.JSONDecodeError as e:
        print(f"    JSON parse error in sentiment analysis: {e}")
        return []
    except Exception as e:
        print(f"    Sentiment API error: {e}")
        return []


def run_sentiment_analysis(news_to_insert: list, company_map: dict, stats: Counter):
    """Run sentiment analysis on all articles to be inserted.

    Modifies articles in-place, setting 'sentiment' and 'catalyst_type' fields.
    """
    if not ANTHROPIC_API_KEY:
        print("\n  Sentiment: SKIPPED (no ANTHROPIC_API_KEY in .env)")
        return

    anthropic_client = Anthropic(api_key=ANTHROPIC_API_KEY)
    batches = [news_to_insert[i:i + SENTIMENT_BATCH_SIZE]
               for i in range(0, len(news_to_insert), SENTIMENT_BATCH_SIZE)]

    print(f"\n  Phase 3: Sentiment Analysis (Claude Haiku)")
    print("  " + "-" * 40)
    print(f"  Analyzing {len(news_to_insert)} articles in {len(batches)} batches...")

    for batch_idx, batch in enumerate(batches):
        results = analyze_sentiment_batch(batch, anthropic_client, company_map)

        if results:
            for result in results:
                idx = result.get('index', 0) - 1
                if 0 <= idx < len(batch):
                    sentiment = result.get('sentiment', '').lower()
                    if sentiment in ('positive', 'negative', 'neutral'):
                        batch[idx]['sentiment'] = sentiment
                        stats['sentiment_analyzed'] += 1

                    catalyst = result.get('catalyst_type')
                    if catalyst and str(catalyst).lower() not in ('null', 'none', ''):
                        batch[idx]['catalyst_type'] = str(catalyst).lower()
                        stats['catalyst_detected'] += 1

        if (batch_idx + 1) % 10 == 0 or batch_idx == len(batches) - 1:
            print(f"    ... {batch_idx + 1}/{len(batches)} batches done "
                  f"({stats.get('sentiment_analyzed', 0)} analyzed, "
                  f"{stats.get('catalyst_detected', 0)} catalysts)")

        time.sleep(SENTIMENT_RATE_LIMIT_DELAY)


def backfill_sentiment():
    """Backfill sentiment on existing articles where sentiment IS NULL."""
    if not ANTHROPIC_API_KEY:
        print("\n  ERROR: ANTHROPIC_API_KEY not set in .env")
        sys.exit(1)

    client = supabase_helper.get_client()
    anthropic_client = Anthropic(api_key=ANTHROPIC_API_KEY)

    # Load company names for the prompt
    companies = supabase_helper.get_all_companies('id, name')
    company_map = {c['id']: c.get('name', '?') for c in companies}

    # Fetch articles without sentiment
    print("\n  Loading articles without sentiment...")
    articles = []
    page_size = 1000
    offset = 0

    while True:
        response = client.table('company_news') \
            .select('id, company_id, title, summary') \
            .is_('sentiment', 'null') \
            .range(offset, offset + page_size - 1) \
            .execute()
        batch = response.data
        articles.extend(batch)
        if len(batch) < page_size:
            break
        offset += page_size

    if not articles:
        print("  No articles need sentiment backfill.")
        return

    print(f"  Found {len(articles)} articles without sentiment.")

    batches = [articles[i:i + SENTIMENT_BATCH_SIZE]
               for i in range(0, len(articles), SENTIMENT_BATCH_SIZE)]
    est_cost = len(batches) * 0.001
    print(f"  Estimated cost: ~${est_cost:.2f} ({len(batches)} batches x ~$0.001)")
    print(f"  Processing...")

    total_updated = 0
    total_catalysts = 0

    for batch_idx, batch in enumerate(batches):
        results = analyze_sentiment_batch(batch, anthropic_client, company_map)

        if results:
            for result in results:
                idx = result.get('index', 0) - 1
                if 0 <= idx < len(batch):
                    article = batch[idx]
                    update_data = {}

                    sentiment = result.get('sentiment', '').lower()
                    if sentiment in ('positive', 'negative', 'neutral'):
                        update_data['sentiment'] = sentiment

                    catalyst = result.get('catalyst_type')
                    if catalyst and str(catalyst).lower() not in ('null', 'none', ''):
                        update_data['catalyst_type'] = str(catalyst).lower()
                        total_catalysts += 1

                    if update_data:
                        try:
                            client.table('company_news') \
                                .update(update_data) \
                                .eq('id', article['id']) \
                                .execute()
                            total_updated += 1
                        except Exception as e:
                            print(f"    Error updating {article['id']}: {e}")

        if (batch_idx + 1) % 10 == 0 or batch_idx == len(batches) - 1:
            print(f"    ... {batch_idx + 1}/{len(batches)} batches done "
                  f"({total_updated} updated, {total_catalysts} catalysts)")

        time.sleep(SENTIMENT_RATE_LIMIT_DELAY)

    print(f"\n  Backfill complete: {total_updated} articles updated, "
          f"{total_catalysts} catalysts detected.")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    # Acquire PID lock to prevent multiple instances
    acquire_pid_lock()

    parser = argparse.ArgumentParser(description='Collect news for companies')
    parser.add_argument('--apply', action='store_true', help='Insert results into Supabase')
    parser.add_argument('--limit', type=int, default=0, help='Limit number of companies for Brave Search')
    parser.add_argument('--brave-only', action='store_true', help='Skip RSS, only use Brave Search')
    parser.add_argument('--rss-only', action='store_true', help='Skip Brave, only use RSS feeds')
    parser.add_argument('--no-sentiment', action='store_true', help='Skip sentiment analysis (faster runs)')
    parser.add_argument('--backfill-sentiment', action='store_true', help='Backfill sentiment on existing articles')
    args = parser.parse_args()

    # Handle backfill mode separately
    if args.backfill_sentiment:
        print("\n" + "=" * 70)
        print("  NEWS COLLECTOR — SENTIMENT BACKFILL")
        print(f"  Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print("=" * 70)
        backfill_sentiment()
        print("\n  Done!")
        return

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
    if args.no_sentiment:
        print("  Sentiment: DISABLED (--no-sentiment)")
    print("=" * 70)

    # Load companies (prioritize public and pre_ipo)
    print("\n  Loading companies...")
    companies = supabase_helper.get_all_companies('id, name, symbol, listing_status, thier_group, vip, prio_buy')
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
    # Phase 2: Brave Search — prioritized with watchlist, rotation, timeouts
    # -----------------------------------------------------------------------
    if not args.rss_only and BRAVE_API_KEY:
        print("\n  Phase 2: Brave Search (prioritized)")
        print("  " + "-" * 40)

        # Build prioritized search list
        print("  Loading watchlist and high-value company IDs...")
        always_search, rotation_slice = build_brave_search_list(
            priority_companies, client, args.limit
        )
        day_of_year = datetime.now().timetuple().tm_yday
        group_index = day_of_year % BRAVE_ROTATION_GROUPS

        print(f"  Always-search (watchlist + high-value): {len(always_search)}")
        print(f"  Rotation group {group_index + 1}/{BRAVE_ROTATION_GROUPS}: {len(rotation_slice)}")
        total_brave = len(always_search) + len(rotation_slice)
        print(f"  Total to search this run: {total_brave}")

        # Track rate limit state
        consecutive_429 = 0
        brave_stopped_reason = None
        brave_start_time = time.time()
        brave_deadline = brave_start_time + BRAVE_MAX_RUNTIME_MINUTES * 60

        # Process both lists in order: always_search first, then rotation
        all_brave_companies = []
        for c in always_search:
            all_brave_companies.append((c, 'priority'))
        for c in rotation_slice:
            all_brave_companies.append((c, 'rotation'))

        for i, (company, tier) in enumerate(all_brave_companies):
            # Check max runtime
            if time.time() > brave_deadline:
                brave_stopped_reason = f"max runtime ({BRAVE_MAX_RUNTIME_MINUTES} min)"
                print(f"\n    STOPPING: {brave_stopped_reason}")
                break

            # Check consecutive 429 limit
            if consecutive_429 >= BRAVE_MAX_CONSECUTIVE_429:
                brave_stopped_reason = f"{BRAVE_MAX_CONSECUTIVE_429} consecutive 429s"
                print(f"\n    STOPPING: {brave_stopped_reason}")
                break

            name = (company.get('name') or '').strip()
            symbol = (company.get('symbol') or '').strip()

            if not name:
                continue

            articles, was_429 = search_brave_news(name, symbol, consecutive_429)

            if was_429:
                consecutive_429 += 1
                stats['brave_429s'] += 1
                continue
            else:
                consecutive_429 = 0  # reset on success

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
                elapsed_min = (time.time() - brave_start_time) / 60
                print(f"    ... {i + 1}/{total_brave} searched ({tier}), "
                      f"{stats['brave_searches']} ok / {stats.get('brave_429s', 0)} 429s, "
                      f"{elapsed_min:.1f} min elapsed, pausing {BRAVE_BATCH_PAUSE}s...")
                time.sleep(BRAVE_BATCH_PAUSE)
            else:
                time.sleep(BRAVE_RATE_LIMIT_DELAY)

            # Progress every 50
            if (i + 1) % 50 == 0:
                elapsed_min = (time.time() - brave_start_time) / 60
                print(f"    ... {i + 1}/{total_brave} searched, "
                      f"{stats['brave_searches']} ok / {stats.get('brave_429s', 0)} 429s, "
                      f"{elapsed_min:.1f} min elapsed")

        # Final Brave stats
        brave_elapsed = (time.time() - brave_start_time) / 60
        success_rate = (stats['brave_searches'] / max(1, stats['brave_searches'] + stats.get('brave_429s', 0))) * 100
        print(f"\n  Brave Search completed in {brave_elapsed:.1f} min")
        print(f"  Success rate: {success_rate:.0f}% ({stats['brave_searches']} ok / {stats.get('brave_429s', 0)} rate-limited)")
        if brave_stopped_reason:
            print(f"  Stopped early: {brave_stopped_reason}")

    elif not args.rss_only and not BRAVE_API_KEY:
        print("\n  Phase 2: SKIPPED (no BRAVE_API_KEY in .env)")

    # -----------------------------------------------------------------------
    # Phase 3: Sentiment Analysis — analyze new articles before insert
    # -----------------------------------------------------------------------
    company_map = {c['id']: c.get('name', '?') for c in companies}

    if news_to_insert and not args.no_sentiment:
        run_sentiment_analysis(news_to_insert, company_map, stats)

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
    print(f"    Rate limited (429):  {stats.get('brave_429s', 0)}")
    print(f"    Articles found:      {stats.get('brave_matched', 0)}")
    print(f"    Duplicates skipped:  {stats.get('brave_duplicates', 0)}")

    print(f"\n  Sentiment Analysis:")
    print(f"    Articles analyzed:   {stats.get('sentiment_analyzed', 0)}")
    print(f"    Catalysts detected:  {stats.get('catalyst_detected', 0)}")

    print(f"\n  Total new articles:    {len(news_to_insert)}")

    # Show sample articles
    if news_to_insert:
        print(f"\n  Sample articles (first 15):")
        for article in news_to_insert[:15]:
            cname = company_map.get(article['company_id'], '?')[:30]
            title = article['title'][:40]
            sent = article.get('sentiment') or '?'
            cat = article.get('catalyst_type') or ''
            cat_str = f" [{cat}]" if cat else ''
            print(f"    [{article['source'][:15]:15s}] {cname:30s} | {title} ({sent}{cat_str})")

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
