#!/usr/bin/env python3
"""V7.20 independent higher-timeframe trend replay. Paper backtest only."""
import bisect, csv, importlib.util, json, math, sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
ROOT=Path(__file__).resolve().parent
H=3600000; Q=900000
DUR={'1D':24*H,'4H':4*H,'1H':H,'15m':Q}
MAP={'1D':['4H','1H','15m'],'4H':['1H','15m'],'1H':['15m']}

def load(name):
    p=ROOT/name
    if not p.exists():raise SystemExit('Missing '+name)
    return json.loads(p.read_text(encoding='utf-8'))

def aggregate(rows,d):
    groups={}
    for c in rows:
        k=c['ts']//d*d
        if k not in groups:groups[k]=dict(ts=k,open=c['open'],high=c['high'],low=c['low'],close=c['close'],volume=0,confirm='1')
        g=groups[k];g['high']=max(g['high'],c['high']);g['low']=min(g['low'],c['low']);g['close']=c['close'];g['volume']+=c.get('volume',0)
    return sorted(groups.values(),key=lambda x:x['ts'])

def ema_setup(tr,cs,side):
    """Same EMA20 touch, close confirmation, and following-candle breakout as tracker.setup.
    Stop is deliberately not calculated here; V7.20 uses entry open +/- 2.5 ATR.
    """
    if len(cs)<35 or not tr.contiguous(cs,DUR[cs[0]['_tf']],len(cs)):return False
    v=cs[0]['close'];ema=[];k=2/21
    for c in cs:
        v=c['close']*k+v*(1-k) if ema else c['close'];ema.append(v)
    a,b=cs[-2:];long=side=='LONG'
    return (a['low']<=ema[-2]<=a['high'] and
            (a['close']>ema[-2] if long else a['close']<ema[-2]) and
            (b['high']>a['high'] and b['close']>a['high'] if long else b['low']<a['low'] and b['close']<a['low']))

def adx14(cs):
    if len(cs)<40:return None
    n=14;tr=[];pd=[];md=[]
    for a,b in zip(cs,cs[1:]):
        up=b['high']-a['high'];down=a['low']-b['low']
        tr.append(max(b['high']-b['low'],abs(b['high']-a['close']),abs(b['low']-a['close'])))
        pd.append(up if up>down and up>0 else 0)
        md.append(down if down>up and down>0 else 0)
    st=sum(tr[:n]);sp=sum(pd[:n]);sm=sum(md[:n]);dx=[]
    for i in range(n-1,len(tr)):
        if i>=n:
            st=st-st/n+tr[i];sp=sp-sp/n+pd[i];sm=sm-sm/n+md[i]
        plus=100*sp/st if st else 0;minus=100*sm/st if st else 0
        dx.append(100*abs(plus-minus)/(plus+minus) if plus+minus else 0)
    if len(dx)<n:return None
    value=sum(dx[:n])/n
    for x in dx[n:]:value=(value*(n-1)+x)/n
    return value

def main():
    variant=sys.argv[1] if len(sys.argv)>1 else 'adx'
    if variant not in ('adx','adx_ema','adx_ema_cooldown'):raise SystemExit('Invalid variant')
    h=load('market_history.json');oi=load('historical_oi.json');fund=load('historical_funding.json')
    if oi.get('instrument')!='BTC-USDT-SWAP' or oi.get('unit')!='BTC (oiCcy)':raise SystemExit('OI mismatch')
    if fund.get('instId')!='BTC-USDT-SWAP':raise SystemExit('Funding mismatch')
    spec=importlib.util.spec_from_file_location('tracker',ROOT/'signal_tracker.py')
    if not spec or not spec.loader:raise SystemExit('Missing signal_tracker.py')
    tr=importlib.util.module_from_spec(spec);spec.loader.exec_module(tr)
    hourly=sorted((x for x in h['candles'] if x.get('confirm')=='1'),key=lambda x:x['ts'])
    quarter=sorted((x for x in h['candles_15m'] if x.get('confirm')=='1'),key=lambda x:x['ts'])
    oirows=sorted(oi['records'],key=lambda x:x['ts']);fr=sorted(fund['rows'],key=lambda x:x['ts'])
    if len(oirows)<100 or not fr:raise SystemExit('Insufficient OI/funding')
    oits=[x['ts'] for x in oirows];fts=[x['ts'] for x in fr];ht=[x['ts'] for x in hourly]
    bars={'1D':aggregate(hourly,24*H),'4H':aggregate(hourly,4*H),'1H':hourly,'15m':quarter}
    bt={k:[x['ts'] for x in v] for k,v in bars.items()}
    start=max(oits[0]+8*H,fts[0],ht[0]+80*H,quarter[0]['ts']+100*Q)
    end=min(oits[-1],quarter[-1]['ts']+Q)
    checks=Counter();trades=[];pending=[];active=[];ids=set();last_entry={}
    for c in quarter:
        now=c['ts']+Q
        if now<start or now>end:continue
        # Enter at next 15m open, including 1H/4H setups; each independent signal gets its own position.
        remaining=[]
        for p in pending:
            if p['signal_close']!=c['ts']:
                if p['signal_close']>c['ts']:remaining.append(p)
                else:checks['missed_entry']+=1
                continue
            entry=c['open'];long=p['side']=='LONG';distance=2.5*p['atr_absolute']
            stop=entry-distance if long else entry+distance
            risk=abs(entry-stop)
            if not (.001<=risk/entry<=.03):checks['risk_out_of_bounds']+=1;continue
            p.update(entry=entry,stop=stop,tp1=entry+(1 if long else -1)*1.5*risk,
                     tp2=entry+(1 if long else -1)*2.5*risk,opened_at=c['ts'],status='OPEN',tp1_touched=False)
            active.append(p);trades.append(p);checks['entered']+=1
            last_entry[(p['side'],p['entry_timeframe'])]=c['ts']
        pending=remaining
        # Stops take precedence if stop and target hit in the same candle (conservative).
        survivors=[]
        for t in active:
            long=t['side']=='LONG';sl=c['low']<=t['stop'] if long else c['high']>=t['stop']
            tp=c['high']>=t['tp2'] if long else c['low']<=t['tp2']
            if (c['high']>=t['tp1'] if long else c['low']<=t['tp1']):t['tp1_touched']=True
            if sl or tp or now-t['opened_at']>=72*H:
                t['status']='STOP' if sl else 'TP2' if tp else 'TIMEOUT'
                t['exit']=t['stop'] if sl else t['tp2'] if tp else c['close'];t['closed_at']=now
                risk=abs(t['entry']-t['stop']);direction=1 if long else -1
                gross=direction*(t['exit']-t['entry'])/risk
                hours=(now-t['opened_at'])/H
                costs=((t['entry']+t['exit'])*(tr.COST['fee_per_side']+tr.COST['slippage_per_side'])+t['entry']*tr.COST['funding_per_8h']*hours/8)/risk
                t['gross_r']=round(gross,5);t['net_r']=round(gross-costs,5)
            else:survivors.append(t)
        active=survivors
        # Reconstruct live score using last completed 1H and 15m close as ticker proxy.
        hi=bisect.bisect_right(ht,now-H);hs=hourly[max(0,hi-2500):hi]
        if len(hs)<80 or hs[-1]['ts']!=(now//H)*H-H:checks['hourly_unavailable']+=1;continue
        oi_i=bisect.bisect_right(oits,now)-1
        if oi_i<0 or now-oits[oi_i]>2*H:checks['oi_stale']+=1;continue
        oi_now=oirows[oi_i];base_i=bisect.bisect_right(oits,oi_now['ts']-6*H)-1
        if base_i<0 or oi_now['ts']-6*H-oits[base_i]>H:checks['oi_baseline_missing']+=1;continue
        fi=bisect.bisect_right(fts,now)-1
        if fi<0 or now-fts[fi]>12*H:checks['funding_stale']+=1;continue
        closes=[x['close'] for x in hs];e20=tr.ema(closes[-40:],20);e50=tr.ema(closes[-80:],50)
        price=c['close'];avg=sum(x['volume'] for x in hs[-21:-1])/20
        ps=50 if price>e20 and price>e50 and hs[-1]['volume']>avg else 20 if price>e20 else -50 if price<e20 and price<e50 and hs[-1]['volume']>avg else 0
        ref_ts=((now-6*H)//H)*H-H;ref=next((x for x in hs if x['ts']==ref_ts),None)
        if ref is None:checks['price_baseline_missing']+=1;continue
        pc=(price/ref['close']-1)*100;oc=(oi_now['oiBtc']/oirows[base_i]['oiBtc']-1)*100
        oscore=30 if pc>0 and oc>.5 else 12 if pc>0 and abs(oc)<=.5 else -23 if pc<0 and oc>.5 else 0
        funding_pct=fr[fi]['rate']*100
        fs=7 if funding_pct<=-.05 else 12 if funding_pct<-.01 else 20 if abs(funding_pct)<=.01 else -20 if funding_pct>=.05 else 0
        score=max(-100,min(100,ps+oscore+fs))
        if -30<score<30:checks['score_neutral']+=1;continue
        score_side='LONG' if score>=30 else 'SHORT'
        tr.NOW=now
        slices={}
        for key,d in DUR.items():
            idx=bisect.bisect_right(bt[key],now-d)
            slices[key]=bars[key][max(0,idx-260):idx]
        for parent,children in MAP.items():
            direction=tr.structure(slices[parent],parent)
            if direction not in ('LONG','SHORT'):checks['parent_flat_or_unavailable']+=1;continue
            # Score filter is retained. Opposite trades are possible at different times,
            # but simultaneous opposite entries cannot pass one global directional score.
            if direction!=score_side:checks['score_direction_mismatch']+=1;continue
            for tf in children:
                d=DUR[tf]
                if now%d:continue  # Only evaluate newly completed entry-timeframe candles.
                segment=slices[tf]
                if len(segment)<65 or not tr.contiguous(segment,d,len(segment)):
                    checks['entry_data_unavailable']+=1;continue
                segment=segment[-100:] if len(segment)>100 else segment
                reg=tr.regime(segment,tf)
                if reg is None:checks['atr_unavailable']+=1;continue
                strength=adx14(segment)
                if strength is None or strength<25:
                    checks['adx_below_25']+=1;continue
                if variant!='adx':
                    full=slices[tf]
                    if len(full)<200:
                        checks['ema200_unavailable']+=1;continue
                    prices=[x['close'] for x in full]
                    e50=tr.ema(prices,50);e200=tr.ema(prices,200)
                    if not (e50>e200 if direction=='LONG' else e50<e200):
                        checks['ema50_200_mismatch']+=1;continue
                if variant=='adx_ema_cooldown' and now-last_entry.get((direction,tf),-10**18)<12*H:
                    checks['cooldown_12h']+=1;continue
                annotated=[dict(x,_tf=tf) for x in segment[-45:]]
                if not ema_setup(tr,annotated,direction):checks['ema_setup_absent']+=1;continue
                signal_id=f'{direction}-{tf}-{now}'  # Dedup same direction + entry timeframe across parents.
                if signal_id in ids:checks['duplicate_signal']+=1;continue
                ids.add(signal_id)
                atr=segment[-1]['close']*reg['atr_pct']/100
                pending.append(dict(id=signal_id,side=direction,trend_source=parent,entry_timeframe=tf,
                                    signal_close=now,score=score,atr_absolute=atr,atr_period=14,status='PENDING'))
                checks['signal']+=1
    completed=[t for t in trades if 'net_r' in t]
    wins=sum(t['net_r']>0 for t in completed)
    result={'version':'V7.21-'+variant,'filter_variant':variant,'adx14_min':25,'ema_filter':variant!='adx','cooldown_hours':12 if variant=='adx_ema_cooldown' else 0,'mode':'approximate historical replay, NOT exact live tick replay',
            'instrument':'BTC-USDT-SWAP','period_start_utc':datetime.fromtimestamp(start/1000,timezone.utc).isoformat(),
            'period_end_utc':datetime.fromtimestamp(end/1000,timezone.utc).isoformat(),
            'trend_to_entry_map':MAP,'independent_positions':True,
            'score_gate':'original score >=30 LONG, <=-30 SHORT; evaluated per parent direction',
            'deduplication':'same direction and entry timeframe on same candle; first parent wins (1D > 4H > 1H)',
            'stop_rule':'2.5 x ATR(14) of entry timeframe from NEXT 15m candle OPEN',
            'entry_rule':'EMA20 touch/confirmation and next-candle breakout on entry timeframe',
            'tp1':'1.5R tracked, not partial exit','tp2':'2.5R full exit',
            'risk_bounds':'0.1%-3% entry-to-stop',
            'data_caveats':['15m close used as live price proxy','latest historical OI hourly snapshot',
                            'last realized funding used in place of predicted live funding',
                            '1D and 4H bars aggregated from BTC-USDT 1H bars',
                            'OHLC stop precedence when stop and TP both touched','no intrabar fill precision'],
            'checks':dict(checks),'trades_total':len(trades),'closed_trades':len(completed),
            'open_trades_at_end':len(active),'wins_net':wins,
            'win_rate_net':round(wins/len(completed),4) if completed else None,
            'sum_net_r':round(sum(t['net_r'] for t in completed),4),'trades':trades}
    (ROOT/f'backtest_v7_21_{variant}_results.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    fields=['id','side','trend_source','entry_timeframe','signal_close','score','entry','stop','tp1','tp2','opened_at','closed_at','exit','status','gross_r','net_r','tp1_touched']
    with (ROOT/f'backtest_v7_21_{variant}_trades.csv').open('w',newline='',encoding='utf-8') as f:
        w=csv.DictWriter(f,fieldnames=fields,extrasaction='ignore');w.writeheader();w.writerows(trades)
    print('V7.21 '+variant,{'trades':len(trades),'closed':len(completed),'net_r':result['sum_net_r'],'checks':dict(checks)})
if __name__=='__main__':main()
