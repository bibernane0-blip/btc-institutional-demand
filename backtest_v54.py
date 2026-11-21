#!/usr/bin/env python3
"""V5.4 robustness research. Separate portfolios per horizon; no live signals."""
import csv, json, math
from pathlib import Path
from datetime import datetime, timezone
ROOT=Path(__file__).resolve().parent
HORIZONS=(1,4,12,24)
COSTS=(0.0012,0.0025,0.005)

def iso(ts): return datetime.fromtimestamp(ts/1000,timezone.utc).isoformat()
def ema(vals,n):
    a=2/(n+1); out=[]; v=None
    for x in vals:
        v=x if v is None else a*x+(1-a)*v
        out.append(v)
    return out

def metrics(trades,cost):
    rs=[t['gross_return']-cost for t in trades]
    if not rs: return {'trades':0,'mean_net_pct':None,'win_rate_pct':None,'profit_factor':None,'compounded_pct':None,'max_drawdown_pct':None}
    wins=sum(x>0 for x in rs); gain=sum(x for x in rs if x>0); loss=-sum(x for x in rs if x<0)
    eq=peak=1.; dd=0.
    for r in rs:
        eq*=1+r; peak=max(peak,eq); dd=max(dd,1-eq/peak)
    return {'trades':len(rs),'mean_net_pct':round(100*sum(rs)/len(rs),4),'win_rate_pct':round(100*wins/len(rs),2), 'profit_factor':round(gain/loss,3) if loss else None,'compounded_pct':round(100*(eq-1),3),'max_drawdown_pct':round(100*dd,3)}

def simulate(c,fast,slow,h,volume_filter):
    closes=[x['close'] for x in c]; vols=[x['volume'] for x in c]; trades=[]; next_free=72
    for i in range(72,len(c)-h):
        if i<next_free: continue
        avg=sum(vols[i-24:i])/24
        vr=vols[i]/avg if avg>0 else 0
        mom=closes[i]/closes[i-12]-1
        direction=1 if fast[i]>slow[i] and mom>0.004 else (-1 if fast[i]<slow[i] and mom<-.004 else 0)
        if not direction or (volume_filter and vr<=1.2): continue
        entry_i=i+1; exit_i=entry_i+h-1
        entry=c[entry_i]['open']; exit_price=c[exit_i]['close']
        trades.append({'horizon_h':h,'filter':'volume_on' if volume_filter else 'volume_off', 'direction':'LONG' if direction==1 else 'SHORT','signal_utc':iso(c[i]['ts']),'entry_utc':iso(c[entry_i]['ts']),'exit_utc':iso(c[exit_i]['ts']),'entry_index':entry_i,'exit_index':exit_i,'entry_price':entry,'exit_price':exit_price,'gross_return':direction*(exit_price/entry-1)})
        next_free=exit_i+1
    return trades

def main():
    source=json.loads((ROOT/'market_history.json').read_text(encoding='utf-8'))
    c=sorted((x for x in source['candles'] if str(x.get('confirm'))=='1'),key=lambda x:x['ts'])
    if len(c)<400: raise SystemExit('Need >=400 confirmed candles')
    if len({x['ts'] for x in c})!=len(c): raise SystemExit('Duplicate timestamps')
    for a,b in zip(c,c[1:]):
        if b['ts']-a['ts']!=3600000: raise SystemExit('Missing candle at '+iso(a['ts']))
    for x in c:
        if not (0<x['low']<=min(x['open'],x['close'])<=max(x['open'],x['close'])<=x['high'] and x['volume']>=0): raise SystemExit('Invalid candle')
    close=[x['close'] for x in c]; fast=ema(close,24); slow=ema(close,72)
    split=int(len(c)*.6); test_len=len(c)-split
    # Strict split: train exits before split; test signals and entries must occur after split.
    # Three non-overlapping chronological test windows, exit contained in window.
    bounds=[split,split+test_len//3,split+2*test_len//3,len(c)]
    report={'version':'5.4','method':'price-volume robustness research; NOT institutional-demand strategy','confirmed_candles':len(c),'data_first_utc':iso(c[0]['ts']),'data_last_utc':iso(c[-1]['ts']),'test_first_utc':iso(c[split]['ts']),'costs_round_trip_pct':[100*x for x in COSTS], 'limitations':['No OI/funding/ETF/netflow inputs','Overlapping trades across different horizons; do not combine portfolio returns','No stop-loss, leverage, liquidation, or dynamic order-book slippage','Walk-forward windows are chronological stability checks, NOT parameter optimization','Volume ablation changes trade timing and number; not paired causal estimate','Compounded returns assume full reinvestment on each nonoverlapping trade within a horizon','Buy-and-hold benchmark is unlevered spot and not directly comparable to short exposure'], 'results':{}}
    rows=[]
    for h in HORIZONS:
        report['results'][str(h)]={}
        for vf in (True,False):
            trades=simulate(c,fast,slow,h,vf); rows+=trades
            train=[t for t in trades if t['exit_index']<split]
            test=[t for t in trades if t['entry_index']>=split and t['entry_index']-1>=split]
            # trades crossing split intentionally excluded
            key='volume_on' if vf else 'volume_off'
            d={'train':metrics(train,COSTS[0]),'test':metrics(test,COSTS[0]), 'test_long':metrics([t for t in test if t['direction']=='LONG'],COSTS[0]),'test_short':metrics([t for t in test if t['direction']=='SHORT'],COSTS[0]), 'cost_sensitivity':{str(round(100*cost,3))+'pct':metrics(test,cost) for cost in COSTS},'test_windows':[]}
            for j in range(3):
                lo,hi=bounds[j:j+2]
                window=[t for t in test if t['entry_index']>=lo and t['exit_index']<hi]
                d['test_windows'].append({'first_utc':iso(c[lo]['ts']),'last_utc':iso(c[hi-1]['ts']),'stats':metrics(window,COSTS[0])})
            report['results'][str(h)][key]=d
    # Same test window, buy at first test candle open, sell at last confirmed close.
    bh=c[-1]['close']/c[split]['open']-1
    report['buy_hold_test_pct']=round(100*bh,3)
    (ROOT/'backtest_v54_results.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    fields=['horizon_h','filter','direction','signal_utc','entry_utc','exit_utc','entry_index','exit_index','entry_price','exit_price','gross_return']
    with (ROOT/'backtest_v54_trades.csv').open('w',newline='',encoding='utf-8') as f:
        w=csv.DictWriter(f,fieldnames=fields); w.writeheader();w.writerows(rows)
    print('V5.4 complete:',len(c),'confirmed candles;',len(rows),'scenario trade records; buy-hold test %:',report['buy_hold_test_pct'])
    for h in HORIZONS:
        d=report['results'][str(h)]['volume_on']['test']
        print(f'{h}h volume-on test: {d["trades"]} trades, mean net {d["mean_net_pct"]}%')

if __name__=='__main__': main()
