#!/usr/bin/env python3
"""Collect 90 days of confirmed BTC-USDT 1m candles from OKX public history API.
No trading, no credentials, no modifications to manual signal state.
"""
import argparse
import gzip
import json
import random
import time
from datetime import datetime, timezone
from pathlib import Path
import requests

URL = 'https://www.okx.com/api/v5/market/history-candles'
MINUTE = 60_000

def fetch(session, before=None):
    params = {'instId': 'BTC-USDT', 'bar': '1m', 'limit': '100'}
    if before is not None:
        params['after'] = str(before)  # OKX: 'after' retrieves candles older than timestamp
    for attempt in range(9):
        try:
            response = session.get(URL, params=params, timeout=35)
            if response.status_code in (429, 500, 502, 503, 504):
                raise RuntimeError(f'HTTP {response.status_code}')
            response.raise_for_status()
            data = response.json()
            if data.get('code') != '0':
                raise RuntimeError(f"OKX {data.get('code')}: {data.get('msg')}")
            return data.get('data', [])
        except (requests.RequestException, RuntimeError) as exc:
            if attempt == 8:
                raise RuntimeError(f'Failed to fetch candles: {exc}') from exc
            time.sleep(min(3 * (attempt + 1), 25) + random.random())
    return []

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--days', type=int, default=90)
    ap.add_argument('--output', default='btc_usdt_1m_90d.json.gz')
    args = ap.parse_args()
    if args.days < 1 or args.days > 180:
        ap.error('days must be between 1 and 180')
    end_ms = int(time.time() * 1000) // MINUTE * MINUTE
    start_ms = end_ms - args.days * 24 * 60 * MINUTE
    session = requests.Session()
    session.headers.update({'User-Agent': 'BTC-1m-Research-Collector/1.0'})
    candles = {}
    cursor = None
    page = 0
    while True:
        rows = fetch(session, cursor)
        if not rows:
            break
        page += 1
        timestamps = []
        for row in rows:
            ts = int(row[0])
            timestamps.append(ts)
            if start_ms <= ts < end_ms and str(row[8]) == '1':
                candles[ts] = [ts, row[1], row[2], row[3], row[4], row[5]]
        oldest = min(timestamps)
        if cursor is not None and oldest >= cursor:
            raise RuntimeError(f'Pagination stalled: oldest={oldest}, cursor={cursor}')
        cursor = oldest
        if page % 100 == 0:
            print(f'pages={page}, unique={len(candles)}, oldest={datetime.fromtimestamp(oldest/1000, timezone.utc).isoformat()}', flush=True)
        if oldest <= start_ms:
            break
        time.sleep(0.13)
        if page > args.days * 24 * 60 // 50 + 100:
            raise RuntimeError('Pagination safety limit exceeded')
    sorted_rows = [candles[k] for k in sorted(candles)]
    expected = (end_ms - start_ms) // MINUTE
    missing = expected - len(sorted_rows)
    meta = {'exchange': 'OKX', 'instrument': 'BTC-USDT', 'timeframe': '1m',
            'columns': ['ts_ms', 'open', 'high', 'low', 'close', 'volume'],
            'start_utc': datetime.fromtimestamp(start_ms/1000, timezone.utc).isoformat(),
            'end_utc_exclusive': datetime.fromtimestamp(end_ms/1000, timezone.utc).isoformat(),
            'expected_candles': expected, 'received_candles': len(sorted_rows),
            'missing_candles': missing, 'complete': missing == 0, 'source': URL,
            'downloaded_at_utc': datetime.now(timezone.utc).isoformat()}
    path = Path(args.output)
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, 'wt', encoding='utf-8', compresslevel=6) as f:
        json.dump({'metadata': meta, 'candles': sorted_rows}, f, separators=(',', ':'))
    path.with_suffix('.metadata.json').write_text(json.dumps(meta, indent=2), encoding='utf-8')
    print(json.dumps(meta, indent=2), flush=True)
    if missing:
        raise SystemExit(f'INCOMPLETE: {missing} missing candles; file retained for diagnosis')
    print(f'SUCCESS: {path} ({path.stat().st_size:,} bytes)', flush=True)

if __name__ == '__main__':
    main()
