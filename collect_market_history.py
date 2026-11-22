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
    row = rows[0]
    ts, oi = int(row['ts']), float(row['oiCcy'])
    if not math.isfinite(oi) or oi <= 0 or abs(NOW - ts) >= 10 * 60_000:
        raise ValueError('OI timestamp/value invalid')
    old['oi'].append({'ts': ts, 'oiBtc': oi})
    oi_collected = True
except (RuntimeError, ValueError, KeyError, TypeError, IndexError) as exc:
    errors.append(f'OI: {exc}')

# Fetch recent candles on every run: history-candles may lag the latest closed bar.
# Keep the 90-day backfill only for first initialization or genuine gaps.
try:
    known = dedup(old['candles'], CUTOFF, 2500, prefer_confirmed=True)
    recent = fetch('/api/v5/market/candles', {'instId': 'BTC-USDT', 'bar': '1H', 'limit': '100'})
    for row in recent:
        item = parse_candle(row)
        if item is not None:
            known.append(item)
    known = dedup(known, CUTOFF, 2500, prefer_confirmed=True)
    completed = [item for item in known if item['confirm'] == '1']
    gaps = missing_hours(completed)
    # Backfill initial history, or gaps beyond the most recent 100-hour window.
    if not known or (known[0]['ts'] > CUTOFF + 2 * HOUR) or any(ts < NOW - 100 * HOUR for ts in gaps):
        cursor = None
        for _ in range(30):
            params = {'instId': 'BTC-USDT', 'bar': '1H', 'limit': '100'}
            if cursor is not None:
                params['after'] = str(cursor)
            page = fetch('/api/v5/market/history-candles', params)
            if not page:
                break
            stamps = []
            for row in page:
                ts = int(row[0])
                stamps.append(ts)
                item = parse_candle(row)
                if item is not None:
                    known.append(item)
            oldest = min(stamps)
            if oldest < CUTOFF or (cursor is not None and oldest >= cursor):
                break
            cursor = oldest
            time.sleep(0.15)
    old['candles'] = dedup(known, CUTOFF, 2500, prefer_confirmed=True)
    candles_collected = True
except (RuntimeError, ValueError, KeyError, TypeError, IndexError) as exc:
    errors.append(f'Candles: {exc}')

old['oi'] = dedup(old['oi'], CUTOFF, 15000)
old['candles'] = dedup(old['candles'], CUTOFF, 2500, prefer_confirmed=True)
completed = [item for item in old['candles'] if item['confirm'] == '1']
missing = missing_hours(completed)
last_closed_hour = (NOW // HOUR) * HOUR - HOUR
last_confirmed_ts = completed[-1]['ts'] if completed else None
closed_candle_lag_hours = (last_closed_hour - last_confirmed_ts) // HOUR if last_confirmed_ts is not None else None
recent_oi = [item for item in old['oi'] if item['ts'] >= NOW - 48 * HOUR]
oi_gaps = [b['ts'] - a['ts'] for a, b in zip(recent_oi, recent_oi[1:])]

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
    'last_confirmed_candle_ts': last_confirmed_ts,
    'expected_last_closed_candle_ts': last_closed_hour,
    'closed_candle_lag_hours': closed_candle_lag_hours,
    'oi_max_gap_minutes_last_48h': round(max(oi_gaps) / 60000, 2) if oi_gaps else None,
    'oi_collection_succeeded': oi_collected,
    'candle_collection_succeeded': candles_collected,
    'errors': errors,
}
tmp = OUT.with_suffix('.json.tmp')
tmp.write_text(json.dumps(old, ensure_ascii=False, separators=(',', ':')) + '\n', encoding='utf-8')
os.replace(tmp, OUT)
print('OI:', len(old['oi']), 'Candles:', len(old['candles']),
      'Missing hours:', len(missing), 'Closed candle lag:', closed_candle_lag_hours,
      'OI max gap (min):', old['quality']['oi_max_gap_minutes_last_48h'], 'Errors:', errors)
