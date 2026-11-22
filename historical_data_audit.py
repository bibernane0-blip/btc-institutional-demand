#!/usr/bin/env python3
"""Audit historical data coverage; collect OKX funding history without fabricating OI.
Run: python -m pip install requests && python historical_data_audit.py
Outputs: historical_funding.json, backtest_data_audit.json
No trading, no modifications to market_history.json.
"""
import json
import time
from datetime import datetime, timezone
from pathlib import Path
import requests

ROOT = Path(__file__).resolve().parent
SOURCE = ROOT / 'market_history.json'
FUNDING = ROOT / 'historical_funding.json'
REPORT = ROOT / 'backtest_data_audit.json'
BASE = 'https://www.okx.com'
INST = 'BTC-USDT-SWAP'
DAY = 86_400_000
NOW = int(time.time() * 1000)
CUTOFF = NOW - 90 * DAY
session = requests.Session()
session.headers.update({'User-Agent': 'BTC-Backtest-Data-Audit/1.0'})


def fetch(path, params):
    for attempt in range(3):
        try:
            response = session.get(BASE + path, params=params, timeout=25)
            response.raise_for_status()
            obj = response.json()
            if str(obj.get('code')) != '0' or not isinstance(obj.get('data'), list):
                raise ValueError(f"OKX {path}: {obj.get('code')} {obj.get('msg')}")
            return obj['data']
        except (requests.RequestException, ValueError):
            if attempt == 2:
                raise
            time.sleep(2 ** attempt)


def coverage(rows, field='ts'):
    stamps = sorted({int(r[field]) for r in rows if isinstance(r, dict) and r.get(field) is not None and CUTOFF <= int(r[field]) <= NOW})
    return {'count': len(stamps), 'first_utc': datetime.fromtimestamp(stamps[0] / 1000, timezone.utc).isoformat() if stamps else None,
            'last_utc': datetime.fromtimestamp(stamps[-1] / 1000, timezone.utc).isoformat() if stamps else None,
            'span_days': round((stamps[-1] - stamps[0]) / DAY, 2) if len(stamps) > 1 else 0}


def main():
    if not SOURCE.exists():
        raise SystemExit('market_history.json bulunamadı; dosyayı script ile aynı dizine koyun.')
    history = json.loads(SOURCE.read_text(encoding='utf-8'))
    existing = json.loads(FUNDING.read_text(encoding='utf-8')) if FUNDING.exists() else {'version': 1, 'instId': INST, 'rows': []}
    by_ts = {int(x['ts']): x for x in existing.get('rows', []) if isinstance(x, dict) and 'ts' in x}
    errors = []
    cursor = None
    pages = 0
    # OKX history endpoint: after is a fundingTime cursor for older records.
    for _ in range(100):
        params = {'instId': INST, 'limit': '100'}
        if cursor is not None:
            params['after'] = str(cursor)
        try:
            data = fetch('/api/v5/public/funding-rate-history', params)
        except (requests.RequestException, ValueError) as exc:
            errors.append(str(exc))
            break
        if not data:
            break
        pages += 1
        stamps = []
        for row in data:
            try:
                ts = int(row['fundingTime'])
                rate = float(row['realizedRate']) if row.get('realizedRate') not in (None, '') else float(row['fundingRate'])
                if CUTOFF <= ts <= NOW and abs(rate) < 1:
                    by_ts[ts] = {'ts': ts, 'rate': rate, 'source': 'OKX funding-rate-history'}
                stamps.append(ts)
            except (KeyError, ValueError, TypeError):
                continue
        if not stamps:
            break
        oldest = min(stamps)
        if oldest <= CUTOFF or (cursor is not None and oldest >= cursor):
            break
        cursor = oldest
        time.sleep(0.2)
    rows = sorted(by_ts.values(), key=lambda x: x['ts'])
    FUNDING.write_text(json.dumps({'version': 1, 'instId': INST, 'rows': rows, 'checked_at': datetime.now(timezone.utc).isoformat()}, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')

    oi = history.get('oi', [])
    oi_stats = coverage(oi)
    funding_stats = coverage(rows)
    # No historical oiCcy backfill is asserted: rubik aggregate OI is not necessarily equivalent to the swap's oiCcy.
    result = {
        'checked_at': datetime.now(timezone.utc).isoformat(), 'target_days': 90,
        'candles_1h': coverage(history.get('candles', [])),
        'candles_15m': coverage(history.get('candles_15m', [])),
        'oi_btc_swap': oi_stats,
        'funding': funding_stats,
        'funding_pages_requested': pages,
        'errors': errors,
        'full_v7_16_backtest_ready': False,
        'reason': '90 günlük aynı enstrümana ait tarihsel oiCcy doğrulanmadan tam skor yeniden üretilemez.',
        'notes': ['Funding geçmişi yalnızca OKX API tarafından döndürülen aralığı kapsar.',
                  'OI geçmişi başka bir enstrüman/aggregate seriyle otomatik ikame edilmez.',
                  'Fiyat mumları ve OI geçmişi için geleceğe bakma (look-ahead) kontrolü gerekir.']
    }
    REPORT.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False, indent=2))

if __name__ == '__main__':
    main()
