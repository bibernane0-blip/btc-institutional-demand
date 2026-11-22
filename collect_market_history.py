#!/usr/bin/env python3
"""V6.1.2: incremental OKX BTC candles and OI with explicit freshness diagnostics."""
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
SESSION.headers.update({'User-Agent': 'BTC-Demand-History/6.1.2'})


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


def parse_candle(row):
    item = {
        'ts': int(row[0]), 'open': float(row[1]), 'high': float(row[2]),
        'low': float(row[3]), 'close': float(row[4]),
        'volume': float(row[5]), 'confirm': str(row[8]),
    }
    if (item['ts'] < CUTOFF or item['ts'] > NOW or item['ts'] % HOUR
            or any(not math.isfinite(item[key]) or item[key] <= 0
                   for key in ('open', 'high', 'low', 'close'))
            or not math.isfinite(item['volume']) or item['volume'] < 0
            or item['confirm'] not in ('0', '1')):
        return None
    return item


def missing_hours(completed):
    missing = []
    for a, b in zip(completed, completed[1:]):
        if b['ts'] - a['ts'] > HOUR:
            missing.extend(range(a['ts'] + HOUR, b['ts'], HOUR))
    return missing


old = {'version': 2, 'oi': [], 'candles': []}
if OUT.exists():
    try:
        loaded = json.loads(OUT.read_text(encoding='utf-8'))
        if not isinstance(loaded, dict):
            raise ValueError('History root must be an object')
        old.update(loaded)
        if not isinstance(old.get('oi'), list) or not isinstance(old.get('candles'), list):
            raise ValueError('History arrays missing')
    except (OSError, ValueError) as exc:
        raise SystemExit(f'Existing history cannot be read; refusing overwrite: {exc}')

errors = []
oi_collected = False
candles_collected = False

try:
    rows = fetch('/api/v5/public/open-interest', {'instType': 'SWAP', 'instId': 'BTC-USDT-SWAP'})
    if not rows:
        raise ValueError('Empty OI response')
