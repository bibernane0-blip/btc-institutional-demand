#!/usr/bin/env python3
"""Read-only V7.0 audit; no network calls or source-file modifications."""
import argparse
import json
import math
from datetime import datetime, timezone
from pathlib import Path

HOUR = 3_600_000

def iso(ts):
    return datetime.fromtimestamp(ts / 1000, timezone.utc).isoformat() if isinstance(ts, int) else None

def audit(path, now_ms=None):
    doc = json.loads(Path(path).read_text(encoding='utf-8'))
    if not isinstance(doc, dict):
        raise ValueError('History must be a JSON object')
    oi, candles = doc.get('oi'), doc.get('candles')
    if not isinstance(oi, list) or not isinstance(candles, list):
        raise ValueError('oi/candles must be arrays')
    problems = []
    def validate(rows, name):
        stamps = []
        for i, row in enumerate(rows):
            if not isinstance(row, dict) or type(row.get('ts')) is not int:
                problems.append(f'{name}[{i}]: invalid timestamp'); continue
            stamps.append(row['ts'])
            fields = ('oiBtc',) if name == 'oi' else ('open', 'high', 'low', 'close', 'volume')
            for field in fields:
                value = row.get(field)
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or (value <= 0 if field != 'volume' else value < 0):
                    problems.append(f'{name}[{i}]: invalid {field}')
            if name == 'candles':
                if row['ts'] % HOUR: problems.append(f'candles[{i}]: misaligned hour')
                if row.get('confirm') not in ('0', '1'): problems.append(f'candles[{i}]: invalid confirm')
                if all(isinstance(row.get(k), (int, float)) for k in ('open', 'high', 'low', 'close')):
                    if row['high'] < max(row['open'], row['close'], row['low']) or row['low'] > min(row['open'], row['close'], row['high']):
                        problems.append(f'candles[{i}]: inconsistent OHLC')
        if stamps != sorted(set(stamps)):
            problems.append(f'{name}: timestamps unsorted or duplicated')
        return stamps
    oi_ts = validate(oi, 'oi'); candle_ts = validate(candles, 'candles')
    completed = [c['ts'] for c in candles if isinstance(c, dict) and c.get('confirm') == '1' and type(c.get('ts')) is int]
    gaps = sum(max(0, (b-a)//HOUR-1) for a,b in zip(completed, completed[1:]))
    oi_intervals = [(b-a)/60000 for a,b in zip(oi_ts,oi_ts[1:])]
    now_ms = now_ms if now_ms is not None else int(datetime.now(timezone.utc).timestamp()*1000)
    return {'checked_at': iso(now_ms), 'oi_count': len(oi), 'oi_first': iso(oi_ts[0]) if oi_ts else None,
            'oi_last': iso(oi_ts[-1]) if oi_ts else None,
            'oi_span_hours': round((oi_ts[-1]-oi_ts[0])/HOUR, 2) if len(oi_ts)>1 else 0,
            'oi_max_gap_minutes': round(max(oi_intervals),2) if oi_intervals else None,
            'oi_latest_age_minutes': round((now_ms-oi_ts[-1])/60000,1) if oi_ts else None,
            'completed_candles': len(completed), 'missing_hours_between_completed_candles': gaps,
            'latest_completed_candle': iso(completed[-1]) if completed else None,
            'problems': problems, 'structurally_valid': not problems and gaps == 0}

if __name__ == '__main__':
    p=argparse.ArgumentParser(); p.add_argument('path', nargs='?', default='market_history.json'); p.add_argument('--output')
    args=p.parse_args(); result=audit(args.path)
    rendered=json.dumps(result, ensure_ascii=False, indent=2)
    if args.output: Path(args.output).write_text(rendered+'\n', encoding='utf-8')
    print(rendered)
    raise SystemExit(0 if result['structurally_valid'] else 1)
