#!/usr/bin/env python3
"""V5.2: collect public OKX BTC candles and OI; preserve verified history."""
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
DAY = 24 * HOUR
CUTOFF = NOW - 90 * DAY
SESSION = requests.Session()
SESSION.headers.update({'User-Agent': 'BTC-Demand-History/5.2'})


def fetch(path, params, retries=3):
    for attempt in range(retries):
        try:
            r = SESSION.get(BASE + path, params=params, timeout=25)
            r.raise_for_status()
            payload = r.json()
            if str(payload.get('code')) != '0' or not isinstance(payload.get('data'), list):
                raise ValueError(f"OKX response: {payload.get('msg', 'invalid data')}")
            return payload['data']
        except (requests.RequestException, ValueError) as exc:
            if attempt == retries - 1:
                raise RuntimeError(f'{path}: {exc}') from exc
            time.sleep(2 ** attempt)


def dedup(rows, cutoff, limit):
    valid = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        ts = row.get('ts')
        if type(ts) is not int or not cutoff <= ts <= NOW + 60_000:
            continue
        valid[ts] = row
    return sorted(valid.values(), key=lambda x: x['ts'])[-limit:]


old = {'version': 2, 'oi': [], 'candles': [], 'errors': []}
if OUT.exists():
    try:
        loaded = json.loads(OUT.read_text(encoding='utf-8'))
        if isinstance(loaded, dict):
            old.update(loaded)
    except (OSError, ValueError) as exc:
        raise SystemExit(f'Existing history cannot be read; refusing overwrite: {exc}')

errors = []

# OI public endpoint supplies a current snapshot, not a historical backfill.
try:
    rows = fetch('/api/v5/public/open-interest', {'instType': 'SWAP', 'instId': 'BTC-USDT-SWAP'})
    if not rows:
        raise ValueError('Empty OI response')
    row = rows[0]
    ts, oi = int(row['ts']), float(row['oiCcy'])
    if not math.isfinite(oi) or oi <= 0 or abs(NOW - ts) >= 10 * 60_000:
        raise ValueError('OI timestamp/value invalid')
    old['oi'].append({'ts': ts, 'oiBtc': oi})
except (RuntimeError, ValueError, KeyError, TypeError, IndexError) as exc:
    errors.append(f'OI: {exc}')

# OKX historical candles pagination: `after` requests candles older than the supplied ts.
# Each page contains up to 100 hourly candles. A complete 90-day backfill can take ~22 calls.
try:
    known = dedup(old.get('candles', []), CUTOFF, 2500)
    completed_ts = {x['ts'] for x in known if x.get('confirm') == '1'}
    cursor = None
    for page in range(30):
        params = {'instId': 'BTC-USDT', 'bar': '1H', 'limit': '100'}
        if cursor is not None:
            params['after'] = str(cursor)
        rows = fetch('/api/v5/market/history-candles', params)
        if not rows:
            break
        page_timestamps = []
        for row in rows:
            ts = int(row[0])
            page_timestamps.append(ts)
            if ts < CUTOFF or ts > NOW or ts in completed_ts:
                continue
            item = {
                'ts': ts, 'open': float(row[1]), 'high': float(row[2]),
                'low': float(row[3]), 'close': float(row[4]),
                'volume': float(row[5]), 'confirm': str(row[8]),
            }
            if (any(not math.isfinite(item[key]) or item[key] <= 0
                    for key in ('open', 'high', 'low', 'close'))
                    or not math.isfinite(item['volume']) or item['volume'] < 0
                    or item['confirm'] not in {'0', '1'}):
                continue
            known.append(item)
            if item['confirm'] == '1':
                completed_ts.add(ts)
        oldest = min(page_timestamps)
        if oldest < CUTOFF or (cursor is not None and oldest >= cursor):
            break
        cursor = oldest
        time.sleep(0.15)
    old['candles'] = known
except (RuntimeError, ValueError, KeyError, TypeError, IndexError) as exc:
    errors.append(f'Candles: {exc}')

old['oi'] = dedup(old.get('oi', []), CUTOFF, 15000)
old['candles'] = dedup(old.get('candles', []), CUTOFF, 2500)
# Use only completed candles for continuity; a forming candle is not an absence.
completed = [x for x in old['candles'] if x.get('confirm') == '1']
missing = []
for a, b in zip(completed, completed[1:]):
    if b['ts'] - a['ts'] > HOUR:
        missing.extend(range(a['ts'] + HOUR, b['ts'], HOUR))

old['version'] = 2
old['checked_at'] = datetime.now(timezone.utc).isoformat()
old['errors'] = errors
old['oi_latest_ts'] = old['oi'][-1]['ts'] if old['oi'] else None
old['candles_latest_ts'] = old['candles'][-1]['ts'] if old['candles'] else None
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
    'errors': errors,
}
# No partial write; atomically replace only after successful serialization.
tmp = OUT.with_suffix('.json.tmp')
tmp.write_text(json.dumps(old, ensure_ascii=False, separators=(',', ':')) + '\n', encoding='utf-8')
os.replace(tmp, OUT)
print('OI:', len(old['oi']), 'Candles:', len(old['candles']), 'Missing hours:', len(missing), 'Errors:', errors)
