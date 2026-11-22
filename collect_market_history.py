#!/usr/bin/env python3
"""V7.9: OKX BTC hourly/15m candles and OI, with data-quality diagnostics."""
import json
import math
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

BASE = 'https://www.okx.com'
OUT = Path('market_history.json')
NOW = int(time.time() * 1000)
HOUR = 3_600_000
MIN15 = 900_000
DAY = 24 * HOUR
CUTOFF = NOW - 90 * DAY
SESSION = requests.Session()
SESSION.headers.update({'User-Agent': 'BTC-Demand-History/7.9'})


def fetch(path, params, retries=3):
    for attempt in range(retries):
        try:
            response = SESSION.get(BASE + path, params=params, timeout=25)
            response.raise_for_status()
            payload = response.json()
            if str(payload.get('code')) != '0' or not isinstance(payload.get('data'), list):
                raise ValueError(f"OKX response: {payload.get('msg', 'invalid data')}")
            return payload['data']
        except (requests.RequestException, ValueError) as exc:
            if attempt == retries - 1:
                raise RuntimeError(f'{path}: {exc}') from exc
            time.sleep(2 ** attempt)


def dedup(rows, cutoff, limit, prefer_confirmed=False):
    valid = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        ts = row.get('ts')
        if type(ts) is not int or not cutoff <= ts <= NOW + 60_000:
            continue
        if prefer_confirmed and row.get('confirm') not in ('0', '1'):
            continue
        previous = valid.get(ts)
        if prefer_confirmed and previous and previous.get('confirm') == '1' and row.get('confirm') != '1':
            continue
        valid[ts] = row
    return sorted(valid.values(), key=lambda item: item['ts'])[-limit:]


def parse_candle(row, interval):
    item = {
        'ts': int(row[0]), 'open': float(row[1]), 'high': float(row[2]),
        'low': float(row[3]), 'close': float(row[4]),
        'volume': float(row[5]), 'confirm': str(row[8]),
    }
    if (item['ts'] < CUTOFF or item['ts'] > NOW or item['ts'] % interval
            or any(not math.isfinite(item[key]) or item[key] <= 0
                   for key in ('open', 'high', 'low', 'close'))
            or not math.isfinite(item['volume']) or item['volume'] < 0
            or item['confirm'] not in ('0', '1')):
        return None
    return item


def missing_intervals(completed, interval):
    missing = []
    for a, b in zip(completed, completed[1:]):
        if b['ts'] - a['ts'] > interval:
            missing.extend(range(a['ts'] + interval, b['ts'], interval))
    return missing


def collect_candles(existing, bar, interval, limit, max_pages):
    known = dedup(existing, CUTOFF, limit, prefer_confirmed=True)
    recent = fetch('/api/v5/market/candles', {'instId': 'BTC-USDT', 'bar': bar, 'limit': '100'})
    for row in recent:
        item = parse_candle(row, interval)
        if item is not None:
            known.append(item)
    known = dedup(known, CUTOFF, limit, prefer_confirmed=True)
    completed = [r for r in known if r['confirm'] == '1']
    gaps = missing_intervals(completed, interval)
    recent_window = 100 * interval
    needs_backfill = (not known or known[0]['ts'] > CUTOFF + 2 * interval
                      or any(ts < NOW - recent_window for ts in gaps))
    if needs_backfill:
        cursor = None
        for _ in range(max_pages):
            params = {'instId': 'BTC-USDT', 'bar': bar, 'limit': '100'}
            if cursor is not None:
                params['after'] = str(cursor)
            page = fetch('/api/v5/market/history-candles', params)
            if not page:
                break
            stamps = []
            for row in page:
                stamps.append(int(row[0]))
                item = parse_candle(row, interval)
                if item is not None:
                    known.append(item)
            oldest = min(stamps)
            if oldest < CUTOFF or (cursor is not None and oldest >= cursor):
                break
            cursor = oldest
            time.sleep(0.15)
    return dedup(known, CUTOFF, limit, prefer_confirmed=True)


old = {'version': 2, 'oi': [], 'candles': [], 'candles_15m': []}
if OUT.exists():
    try:
        loaded = json.loads(OUT.read_text(encoding='utf-8'))
        if not isinstance(loaded, dict):
            raise ValueError('History root must be an object')
        old.update(loaded)
        if any(not isinstance(old.get(key), list) for key in ('oi', 'candles', 'candles_15m')):
            raise ValueError('History arrays missing')
    except (OSError, ValueError) as exc:
        raise SystemExit(f'Existing history cannot be read; refusing overwrite: {exc}')

errors = []
oi_collected = False
candles_collected = False
candles_15m_collected = False

try:
    rows = fetch('/api/v5/public/open-interest', {'instType': 'SWAP', 'instId': 'BTC-USDT-SWAP'})
    if not rows:
        raise ValueError('Empty OI response')
    row = rows[0]
    ts, oi = int(row['ts']), float(row['oiCcy'])
    if not math.isfinite(oi) or oi <= 0 or abs(NOW - ts) >= 10 * 60_000:
        raise ValueError('OI timestamp/value invalid')
    old['oi'].append({'ts': ts, 'oiBtc': oi})
    oi_collected = True
except (RuntimeError, ValueError, KeyError, TypeError, IndexError) as exc:
    errors.append(f'OI: {exc}')

try:
    old['candles'] = collect_candles(old['candles'], '1H', HOUR, 2500, 30)
    candles_collected = True
except (RuntimeError, ValueError, KeyError, TypeError, IndexError) as exc:
    errors.append(f'1H candles: {exc}')

try:
    # 90 days * 96 bars/day = 8640 bars; 100 pages cover the initial backfill.
    old['candles_15m'] = collect_candles(old['candles_15m'], '15m', MIN15, 9000, 100)
    candles_15m_collected = True
except (RuntimeError, ValueError, KeyError, TypeError, IndexError) as exc:
    errors.append(f'15m candles: {exc}')

old['oi'] = dedup(old['oi'], CUTOFF, 15000)
old['candles'] = dedup(old['candles'], CUTOFF, 2500, prefer_confirmed=True)
old['candles_15m'] = dedup(old['candles_15m'], CUTOFF, 9000, prefer_confirmed=True)
completed = [r for r in old['candles'] if r['confirm'] == '1']
completed_15m = [r for r in old['candles_15m'] if r['confirm'] == '1']
missing = missing_intervals(completed, HOUR)
missing_15m = missing_intervals(completed_15m, MIN15)
last_closed_hour = (NOW // HOUR) * HOUR - HOUR
last_closed_15m = (NOW // MIN15) * MIN15 - MIN15
last_confirmed_ts = completed[-1]['ts'] if completed else None
last_confirmed_15m_ts = completed_15m[-1]['ts'] if completed_15m else None
closed_candle_lag_hours = ((last_closed_hour - last_confirmed_ts) // HOUR
                           if last_confirmed_ts is not None else None)
closed_15m_lag_bars = ((last_closed_15m - last_confirmed_15m_ts) // MIN15
                       if last_confirmed_15m_ts is not None else None)
recent_oi = [r for r in old['oi'] if r['ts'] >= NOW - 48 * HOUR]
oi_gaps = [b['ts'] - a['ts'] for a, b in zip(recent_oi, recent_oi[1:])]

old['version'] = 3
old['checked_at'] = datetime.now(timezone.utc).isoformat()
old['errors'] = errors
old['oi_latest_ts'] = old['oi'][-1]['ts'] if old['oi'] else None
old['candles_latest_ts'] = old['candles'][-1]['ts'] if old['candles'] else None
old['candles_15m_latest_ts'] = old['candles_15m'][-1]['ts'] if old['candles_15m'] else None
old['quality'] = {
    'oi_count': len(old['oi']),
    'candle_count': len(old['candles']),
    'completed_candle_count': len(completed),
    'missing_hourly_candles_between_first_and_last': len(missing),
    'missing_hourly_candle_ts_sample': missing[:24],
    'oi_first_ts': old['oi'][0]['ts'] if old['oi'] else None,
    'oi_last_ts': old['oi_latest_ts'],
    'candle_first_ts': old['candles'][0]['ts'] if old['candles'] else None,
    'candle_last_ts': old['candles_latest_ts'],
    'last_confirmed_candle_ts': last_confirmed_ts,
    'expected_last_closed_candle_ts': last_closed_hour,
    'closed_candle_lag_hours': closed_candle_lag_hours,
    'oi_max_gap_minutes_last_48h': round(max(oi_gaps) / 60000, 2) if oi_gaps else None,
    'oi_collection_succeeded': oi_collected,
    'candle_collection_succeeded': candles_collected,
    'candles_15m_count': len(old['candles_15m']),
    'completed_15m_count': len(completed_15m),
    'missing_15m_candles_between_first_and_last': len(missing_15m),
    'missing_15m_candle_ts_sample': missing_15m[:24],
    'last_confirmed_15m_ts': last_confirmed_15m_ts,
    'expected_last_closed_15m_ts': last_closed_15m,
    'closed_15m_lag_bars': closed_15m_lag_bars,
    'candles_15m_collection_succeeded': candles_15m_collected,
    'errors': errors,
}
tmp = OUT.with_suffix('.json.tmp')
tmp.write_text(json.dumps(old, ensure_ascii=False, separators=(',', ':')) + '\n', encoding='utf-8')
os.replace(tmp, OUT)
print('OI:', len(old['oi']), '1H:', len(old['candles']),
      '15m:', len(old['candles_15m']), 'Missing 1H:', len(missing),
      'Missing 15m:', len(missing_15m), '15m lag:', closed_15m_lag_bars,
      'Errors:', errors)
