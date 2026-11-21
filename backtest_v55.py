#!/usr/bin/env python3
"""V5.5: audit V5.4 window attribution and study ex-ante market regimes."""
import csv, json, math
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
ROOT=Path(__file__).resolve().parent
COST=0.0012

def iso(ms): return datetime.fromtimestamp(ms/1000,timezone.utc).isoformat()
def ema(values,n):
    a=2/(n+1); v=None; out=[]
    for x in values:
        v=x if v is None else a*x+(1-a)*v
        out.append(v)
    return out

def stats(rs):
    if not rs: return {'trades':0,'mean_net_pct':None,'win_rate_pct':None,'profit_factor':None}
    pos=sum(max(x,0) for x in rs); neg=-sum(min(x,0) for x in rs)
    return {'trades':len(rs),'mean_net_pct':round(100*sum(rs)/len(rs),4), 'win_rate_pct':round(100*sum(x>0 for x in rs)/len(rs),2), 'profit_factor':round(pos/neg,3) if neg else None}

def main():
    src=json.loads((ROOT/'market_history.json').read_text(encoding='utf-8'))
    candles=sorted((x for x in src['candles'] if str(x.get('confirm'))=='1'),key=lambda x:x['ts'])
    if len(candles)<400: raise SystemExit('Insufficient candles')
    ts=[x['ts'] for x in candles]
    if len(set(ts))!=len(ts) or any(b-a!=3600000 for a,b in zip(ts,ts[1:])): raise SystemExit('Missing/duplicate candles')
    closes=[x['close'] for x in candles]
    fast=ema(closes,24); slow=ema(closes,72)
    split=int(len(candles)*.6); size=len(candles)-split
    bounds=[split,split+size//3,split+2*size//3,len(candles)]
    # Reproduce V5.4 entry logic exactly; one portfolio per horizon.
    allrows=[]; results={}
    for h in (1,4,12,24):
        trades=[]; next_free=72
        for i in range(72,len(candles)-h):
            if i<next_free: continue
            av=sum(x['volume'] for x in candles[i-24:i])/24
            vr=candles[i]['volume']/av if av>0 else 0
            mom=closes[i]/closes[i-12]-1
            direction=1 if fast[i]>slow[i] and mom>0.004 else (-1 if fast[i]<slow[i] and mom<-.004 else 0)
            if not direction or vr<=1.2: continue
            entry=i+1; exit_i=entry+h-1
            if entry>=split and i>=split:
                # Regime is determined at signal close, using only current/past data.
                trailing=[math.log(closes[k]/closes[k-1]) for k in range(i-23,i+1)]
                volatility=math.sqrt(sum(x*x for x in trailing)/len(trailing))*math.sqrt(24)*100
                trend_gap=(fast[i]/slow[i]-1)*100
                regime='uptrend' if trend_gap>0.2 else ('downtrend' if trend_gap<-.2 else 'sideways')
                gross=direction*(candles[exit_i]['close']/candles[entry]['open']-1)
                baseline_short=candles[entry]['open']/candles[exit_i]['close']-1 # simple short gross approximation
                window=next((j+1 for j in range(3) if entry>=bounds[j] and exit_i<bounds[j+1]),None)
                crossing=(window is None)
                trades.append({'horizon_h':h,'direction':'LONG' if direction>0 else 'SHORT','signal_utc':iso(ts[i]),'entry_utc':iso(ts[entry]),'exit_utc':iso(ts[exit_i]),'regime':regime,'trailing_volatility_pct':round(volatility,4),'window':window or 'boundary_crossing','net_return_pct':round(100*(gross-COST),5),'same_entry_always_short_net_pct':round(100*(baseline_short-COST),5)})
            next_free=exit_i+1
        results[str(h)]={'overall':stats([r['net_return_pct']/100 for r in trades]),'windows':{},'regimes':{},'directions':{},'boundary_crossing_trades':sum(r['window']=='boundary_crossing' for r in trades)}
        for j in (1,2,3):
            subset=[r for r in trades if r['window']==j]
            results[str(h)]['windows'][str(j)]=stats([r['net_return_pct']/100 for r in subset])
        for key in ('uptrend','downtrend','sideways'):
            subset=[r for r in trades if r['regime']==key]
            results[str(h)]['regimes'][key]=stats([r['net_return_pct']/100 for r in subset])
        for key in ('LONG','SHORT'):
            subset=[r for r in trades if r['direction']==key]
            results[str(h)]['directions'][key]=stats([r['net_return_pct']/100 for r in subset])
        short=[r for r in trades if r['direction']=='SHORT']
        results[str(h)]['short_vs_same_entry_always_short']={'strategy':stats([r['net_return_pct']/100 for r in short]),'same_entry_short':stats([r['same_entry_always_short_net_pct']/100 for r in short]),'note':'Identical short entries imply identical returns; not an independent benchmark.'}
        allrows.extend(trades)
    report={'version':'5.5','method':'V5.4 rule reproduction; ex-ante EMA regime attribution; no optimization','data_first_utc':iso(ts[0]),'data_last_utc':iso(ts[-1]),'test_start_utc':iso(ts[split]),'confirmed_candles':len(candles),'round_trip_cost_pct':100*COST,'regime_definition':'At signal close: EMA24/EMA72 difference > +0.2% uptrend, < -0.2% downtrend, otherwise sideways','limitations':['Regime definitions are exploratory and were selected after reviewing prior results; not independent validation','Windows exclude trades crossing boundaries; boundary_crossing_trades reconciles counts','90-day sample and test set reused for research; no new out-of-sample evidence','No OI/funding/ETF/netflow, no liquidation/leverage or dynamic slippage','Volatility is trailing 24-hour RMS log return, informational only','Same-entry always-short comparison is tautological for SHORT trades; need a separately scheduled benchmark'], 'results':results}
    (ROOT/'backtest_v55_results.json').write_text(json.dumps(report,indent=2,ensure_ascii=False)+'\n',encoding='utf-8')
    fields=['horizon_h','direction','signal_utc','entry_utc','exit_utc','regime','trailing_volatility_pct','window','net_return_pct','same_entry_always_short_net_pct']
    with (ROOT/'backtest_v55_trades.csv').open('w',newline='',encoding='utf-8') as f:
        w=csv.DictWriter(f,fieldnames=fields);w.writeheader();w.writerows(allrows)
    print('V5.5 completed. Test trades:',len(allrows))
    for h in (1,4,12,24):
        x=results[str(h)]; print(h,'hours:',x['overall']['trades'],'trades;',x['boundary_crossing_trades'],'cross window boundaries')
if __name__=='__main__': main()
