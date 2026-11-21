#!/usr/bin/env python3
"""V5.3 price/volume research baseline. Not the institutional-demand strategy."""
import csv
import json
import math
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SOURCE = ROOT / 'market_history.json'
OUT = ROOT / 'backtest_v53_results.json'
TRADES = ROOT / 'backtest_v53_trades.csv'
HORIZONS = (1, 4, 12, 24)
ROUND_TRIP_COST = 0.0012  # 0.12% total fee/slippage assumption
WARMUP = 72


def utc(ts):
    return datetime.fromtimestamp(ts / 1000, tz=timezone.utc).isoformat()


def ema(values, period):
    alpha = 2 / (period + 1)
    out = []
    previous = None
    for value in values:
        previous = value if previous is None else alpha * value + (1 - alpha) * previous
        out.append(previous)
    return out


def stats(trades):
    if not trades:
        return {'trades': 0, 'win_rate_pct': None, 'mean_net_pct': None,
                'median_net_pct': None, 'total_compounded_pct': None, 'profit_factor': None}
    returns = sorted(t['net_return'] for t in trades)
    n = len(returns)
    wins = sum(r > 0 for r in returns)
    gross_gain = sum(r for r in returns if r > 0)
    gross_loss = -sum(r for r in returns if r < 0)
    compounded = math.prod(1 + r for r in returns) - 1
    return {'trades': n, 'win_rate_pct': round(100 * wins / n, 2),
            'mean_net_pct': round(100 * sum(returns) / n, 3),
            'median_net_pct': round(100 * ((returns[(n-1)//2] + returns[n//2])/2), 3),
            'total_compounded_pct': round(100 * compounded, 3),
            'profit_factor': round(gross_gain / gross_loss, 3) if gross_loss else None}


def main():
    data = json.loads(SOURCE.read_text(encoding='utf-8'))
    raw = data.get('candles', [])
    candles = sorted((r for r in raw if str(r.get('confirm')) == '1'), key=lambda r: r['ts'])
    if len(candles) < 400:
        raise SystemExit('Insufficient confirmed candles (minimum 400).')
    if len({r['ts'] for r in candles}) != len(candles):
        raise SystemExit('Duplicate confirmed candle timestamps.')
    for a, b in zip(candles, candles[1:]):
        if b['ts'] - a['ts'] != 3600000:
            raise SystemExit('Missing hourly candle: ' + utc(a['ts']))
    for r in candles:
        if not (0 < r['low'] <= min(r['open'], r['close']) <= max(r['open'], r['close']) <= r['high'] and r['volume'] >= 0):
            raise SystemExit('Invalid OHLCV at ' + utc(r['ts']))
    closes = [r['close'] for r in candles]
    volumes = [r['volume'] for r in candles]
    fast, slow = ema(closes, 24), ema(closes, 72)
    split = int(len(candles) * 0.6)
    rows = []
    results = {}
    for horizon in HORIZONS:
        # Independent portfolio per horizon; at most one position open per portfolio.
        trades = []
        next_free = WARMUP
        for i in range(WARMUP, len(candles) - horizon):
            if i < next_free:
                continue
            # Signal formed after close of bar i. Enter at bar i+1 open.
            avg_volume = sum(volumes[i-24:i]) / 24
            volume_ratio = volumes[i] / avg_volume if avg_volume > 0 else 0
            momentum_12h = closes[i] / closes[i-12] - 1
            direction = 0
            if fast[i] > slow[i] and momentum_12h > 0.004 and volume_ratio > 1.2:
                direction = 1
            elif fast[i] < slow[i] and momentum_12h < -0.004 and volume_ratio > 1.2:
                direction = -1
            if direction == 0:
                continue
            entry_index = i + 1
            exit_index = entry_index + horizon - 1
            # Entry at next candle open, exit at close after horizon candles.
            entry = candles[entry_index]['open']
            exit_price = candles[exit_index]['close']
            gross = direction * (exit_price / entry - 1)
            net = gross - ROUND_TRIP_COST
            trade = {'horizon_h': horizon, 'signal_utc': utc(candles[i]['ts']),
                     'entry_utc': utc(candles[entry_index]['ts']),
                     'exit_utc': utc(candles[exit_index]['ts']),
                     'direction': 'LONG' if direction > 0 else 'SHORT',
                     'entry_price': entry, 'exit_price': exit_price,
                     'gross_return': gross, 'net_return': net,
                     'sample': 'train' if entry_index < split else 'test'}
            trades.append(trade)
            rows.append(trade)
            next_free = exit_index + 1
        results[str(horizon)] = {
            'train': stats([t for t in trades if t['sample'] == 'train']),
            'test': stats([t for t in trades if t['sample'] == 'test']),
            'test_long': stats([t for t in trades if t['sample'] == 'test' and t['direction'] == 'LONG']),
            'test_short': stats([t for t in trades if t['sample'] == 'test' and t['direction'] == 'SHORT'])
        }
    report = {'version': '5.3', 'method': 'price-volume baseline; NOT institutional-demand strategy',
              'data_first_utc': utc(candles[0]['ts']), 'data_last_utc': utc(candles[-1]['ts']),
              'confirmed_candles': len(candles), 'split_index': split,
              'test_first_utc': utc(candles[split]['ts']),
              'round_trip_cost_pct': ROUND_TRIP_COST * 100,
              'rules': 'EMA24 vs EMA72, 12h momentum >+0.4% or <-0.4%, current volume >1.2x prior 24h mean; signal on close, next open entry; exit at close after H bars; one trade at a time per horizon',
              'limitations': ['No funding, OI, ETF or netflow signals',
                              'Fixed cost approximation; no spread/orderbook modeling',
                              'Fixed horizons; no stop-loss or liquidation modeling',
                              'Small test samples can be unreliable',
                              'Split by entry time; training trades can exit after split; separate portfolio accounting'],
              'results': results}
    OUT.write_text(json.dumps(report, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    with TRADES.open('w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=['horizon_h','signal_utc','entry_utc','exit_utc','direction','entry_price','exit_price','gross_return','net_return','sample'])
        writer.writeheader()
        writer.writerows(rows)
    print('Confirmed candles:', len(candles), '| Train/test split:', utc(candles[split]['ts']))
    for h, r in results.items():
        print(f"{h}h | TRAIN {r['train']['trades']} trades, mean {r['train']['mean_net_pct']}% | TEST {r['test']['trades']} trades, win {r['test']['win_rate_pct']}%, mean {r['test']['mean_net_pct']}%")
    print('Saved:', OUT.name, TRADES.name)


if __name__ == '__main__':
    main()
