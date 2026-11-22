#!/usr/bin/env python3
"""V7.19 offline replay of V7.16.1 paper strategy. No network, no live orders.
Uses the uploaded signal_tracker.py functions; does not alter live files.
"""
import bisect, csv, importlib.util, json, math
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT=Path(__file__).resolve().parent
H=3600000; Q=900000

def load(name):
    p=ROOT/name
    if not p.exists(): raise SystemExit(f'Missing {name}')
    return json.loads(p.read_text(encoding='utf-8'))

def aggregate(rows, duration):
    groups={}
    for c in rows:
        k=c['ts']//duration*duration
        if k not in groups:
            groups[k]=dict(ts=k,open=c['open'],high=c['high'],low=c['low'],close=c['close'],volume=0,confirm='1')
        x=groups[k];x['high']=max(x['high'],c['high']);x['low']=min(x['low'],c['low']);x['close']=c['close'];x['volume']+=c.get('volume',0)
    return sorted(groups.values(),key=lambda x:x['ts'])

def main():
    h=load('market_history.json');oi=load('historical_oi.json');fund=load('historical_funding.json')
    if oi.get('instrument')!='BTC-USDT-SWAP' or oi.get('unit')!='BTC (oiCcy)':raise SystemExit('OI instrument/unit mismatch')
    if fund.get('instId')!='BTC-USDT-SWAP':raise SystemExit('Funding instrument mismatch')
    spec=importlib.util.spec_from_file_location('tracker',ROOT/'signal_tracker.py')
    if spec is None or spec.loader is None:raise SystemExit('Missing signal_tracker.py')
    tr=importlib.util.module_from_spec(spec);spec.loader.exec_module(tr)
    hourly=sorted((x for x in h['candles'] if x.get('confirm')=='1'),key=lambda x:x['ts'])
    quarter=sorted((x for x in h['candles_15m'] if x.get('confirm')=='1'),key=lambda x:x['ts'])
    oirows=sorted(oi['records'],key=lambda x:x['ts']);fr=sorted(fund['rows'],key=lambda x:x['ts'])
    if len(oirows)<100 or not fr:raise SystemExit('OI/funding history insufficient')
    oits=[x['ts'] for x in oirows];fts=[x['ts'] for x in fr]
    ht=[x['ts'] for x in hourly];qt=[x['ts'] for x in quarter]
    bars={'1D':aggregate(hourly,24*H),'4H':aggregate(hourly,4*H),'1H':hourly,'15m':quarter}
    bt={k:[x['ts'] for x in v] for k,v in bars.items()}
    start=max(oits[0]+8*H,fts[0],ht[0]+80*H,qt[0]+100*Q)
    end=min(oits[-1],qt[-1]+Q)
    checks=Counter();trades=[];open_trade=None;signals=[]
    # Only evaluate when 15m candle has closed. Snapshot all inputs as-of that moment.
    for i,c in enumerate(quarter):
        now=c['ts']+Q
        if now<start or now>end:continue
        # Update an existing position using this completed candle, before evaluating a new signal.
        if open_trade and c['ts']>=open_trade['opened_at']:
            t=open_trade;long=t['side']=='LONG';sl=c['low']<=t['stop'] if long else c['high']>=t['stop'];tp=c['high']>=t['tp2'] if long else c['low']<=t['tp2']
            if c['high']>=t['tp1'] if long else c['low']<=t['tp1']:t['tp1_touched']=True
            if sl or tp or now-t['opened_at']>=72*H:
                t['status']='STOP' if sl else 'TP2' if tp else 'TIMEOUT'
                t['exit']=t['stop'] if sl else t['tp2'] if tp else c['close'];t['closed_at']=now
                risk=abs(t['entry']-t['stop']);direction=1 if long else -1
                gross=direction*(t['exit']-t['entry'])/risk
                hours=(now-t['opened_at'])/H
                costs=((t['entry']+t['exit'])*(tr.COST['fee_per_side']+tr.COST['slippage_per_side'])+t['entry']*tr.COST['funding_per_8h']*hours/8)/risk
                t['gross_r']=round(gross,5);t['net_r']=round(gross-costs,5)
                open_trade=None
        # A previously confirmed signal enters at next 15m candle OPEN.
        if signals and signals[-1].get('pending') and c['ts']==signals[-1]['signal_close']:
            plan=signals[-1];plan.pop('pending');entry=c['open'];long=plan['side']=='LONG';risk=abs(entry-plan['stop'])
            if ((entry>plan['stop']) if long else (entry<plan['stop'])) and .001<=risk/entry<=.03 and open_trade is None:
                plan.update(entry=entry,opened_at=c['ts'],tp1=entry+(1 if long else -1)*1.5*risk,tp2=entry+(1 if long else -1)*2.5*risk,status='OPEN',tp1_touched=False)
                trades.append(plan);open_trade=plan
            else:checks['invalid_or_overlap_entry']+=1
        if open_trade is not None:checks['position_open']+=1;continue
        # Use only confirmed 1H closes. The live score uses current ticker price; replay uses 15m close proxy.
        hi=bisect.bisect_right(ht,now-H);historical_hourly=hourly[max(0,hi-2500):hi]
        if len(historical_hourly)<80 or historical_hourly[-1]['ts']!=(now//H)*H-H:
            checks['hourly_unavailable']+=1;continue
        oi_i=bisect.bisect_right(oits,now)-1
        if oi_i<0 or now-oits[oi_i]>2*H:checks['oi_stale']+=1;continue
        oi_now=oirows[oi_i];base_i=bisect.bisect_right(oits,oi_now['ts']-6*H)-1
        if base_i<0 or oi_now['ts']-6*H-oits[base_i]>H:checks['oi_baseline_missing']+=1;continue
        fi=bisect.bisect_right(fts,now)-1
        if fi<0 or now-fts[fi]>12*H:checks['funding_stale']+=1;continue
        # Replay the exact score arithmetic; live NOW/ticker references cannot be reproduced exactly.
        closes=[x['close'] for x in historical_hourly];e20=tr.ema(closes[-40:],20);e50=tr.ema(closes[-80:],50)
        price=c['close'];avg=sum(x['volume'] for x in historical_hourly[-21:-1])/20
        ps=50 if price>e20 and price>e50 and historical_hourly[-1]['volume']>avg else 20 if price>e20 else -50 if price<e20 and price<e50 and historical_hourly[-1]['volume']>avg else 0
        ref_ts=((now-6*H)//H)*H-H;ref=next((x for x in historical_hourly if x['ts']==ref_ts),None)
        if ref is None:checks['price_baseline_missing']+=1;continue
        pc=(price/ref['close']-1)*100;oc=(oi_now['oiBtc']/oirows[base_i]['oiBtc']-1)*100
        oscore=30 if pc>0 and oc>.5 else 12 if pc>0 and abs(oc)<=.5 else -23 if pc<0 and oc>.5 else 0
        funding_pct=fr[fi]['rate']*100
        fs=7 if funding_pct<=-.05 else 12 if funding_pct<-.01 else 20 if abs(funding_pct)<=.01 else -20 if funding_pct>=.05 else 0
        score=max(-100,min(100,ps+oscore+fs))
        if -30<score<30:checks['score_neutral']+=1;continue
        side='LONG' if score>=30 else 'SHORT'
        # Supply same 100-candle windows and same NOW to original tracker helpers.
        tr.NOW=now
        slices={}
        for key,duration in tr.DUR.items():
            end_i=bisect.bisect_right(bt[key],now-duration)
            slices[key]=bars[key][max(0,end_i-100):end_i]
        trends={key:tr.structure(slices[key],key) for key in tr.DUR}
        regimes={key:tr.regime(slices[key],key) for key in tr.DUR}
        if any(v is None for v in trends.values()) or any(v is None for v in regimes.values()):checks['structure_regime_unavailable']+=1;continue
        pairs=[(a,b) for a,b in [('1D','4H'),('4H','1H'),('1H','15m')] if trends[a]==side and trends[b]==side]
        if not pairs:checks['no_adjacent_pair']+=1;continue
        if regimes['1D']['volatility']=='HIGH':checks['daily_volatility_high']+=1;continue
        plan=tr.setup(slices['15m'],regimes['15m']['atr_pct'],side)
        if plan is None:checks['ema_setup_absent']+=1;continue
        if i+1>=len(quarter) or quarter[i+1]['ts']!=now:checks['next_candle_missing']+=1;continue
        plan.update(score=score,pair=pairs[0],pending=True)
        signals.append(plan);checks['signal']+=1
    complete=[t for t in trades if t.get('net_r') is not None]
    wins=sum(t['net_r']>0 for t in complete)
    result={'version':'V7.19','mode':'historical replay; not exact live tick replay','instrument':'BTC-USDT-SWAP','period_start_utc':datetime.fromtimestamp(start/1000,timezone.utc).isoformat(),'period_end_utc':datetime.fromtimestamp(end/1000,timezone.utc).isoformat(),'score_price_proxy':'last closed 15m candle; live uses BTC-USDT ticker','funding_proxy':'last realized funding rate; live uses current predicted fundingRate','oi_proxy':'latest hourly OI observation; live uses current snapshot','daily_warmup':'65+ full UTC daily bars required; 90d input limits eligible dates','checks':dict(checks),'trades_total':len(trades),'closed_trades':len(complete),'wins_net':wins,'win_rate_net':round(wins/len(complete),4) if complete else None,'sum_net_r':round(sum(t['net_r'] for t in complete),4),'trades':trades}
    (ROOT/'backtest_v7_19_results.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    with (ROOT/'backtest_v7_19_trades.csv').open('w',newline='',encoding='utf-8') as f:
        fields=['side','signal_close','score','entry','stop','tp1','tp2','opened_at','closed_at','exit','status','gross_r','net_r','tp1_touched']
        w=csv.DictWriter(f,fieldnames=fields,extrasaction='ignore');w.writeheader();w.writerows(trades)
    print('V7.19',{'trades':len(trades),'closed':len(complete),'sum_net_r':result['sum_net_r'],'checks':dict(checks)})

if __name__=='__main__':main()
