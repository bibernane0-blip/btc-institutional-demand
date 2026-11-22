#!/usr/bin/env python3
"""V7.28 manual-only BTC watchlist; no exchange orders. Requires requests and signal_tracker.py."""
import json, math, os, time
from pathlib import Path
import requests
import signal_tracker as legacy
from backtest_v7_21 import adx14

ROOT=Path(__file__).resolve().parent
OUT=ROOT/'manual_signals_v7_28.json'
DUR=legacy.DUR
MAP={'1D':('4H','1H','15m'),'4H':('1H','15m'),'1H':('15m',)}
MAX_AGE_MS=4*3600000  # Limit order expires 4 hours after creation.
HOLD_MS=240*3600000
SESSION=requests.Session()
SESSION.headers['User-Agent']='BTC-Manual-Watchlist-V7.28'

def api(bar,limit=300):
    r=SESSION.get('https://www.okx.com/api/v5/market/candles',params={'instId':'BTC-USDT','bar':bar,'limit':str(limit)},timeout=25)
    r.raise_for_status();body=r.json()
    if body.get('code')!='0':raise RuntimeError(str(body.get('msg')))
    return sorted([legacy.candle(row) for row in body['data'] if str(row[8])=='1'],key=lambda x:x['ts'])

def ema_series(values,n):
    alpha=2/(n+1);v=values[0];out=[]
    for x in values:
        v=alpha*x+(1-alpha)*v;out.append(v)
    return out

def atr14(cs):
    if len(cs)<40:return None
    tr=[max(b['high']-b['low'],abs(b['high']-a['close']),abs(b['low']-a['close'])) for a,b in zip(cs,cs[1:])]
    value=sum(tr[:14])/14
    for x in tr[14:]:value=(value*13+x)/14
    return value

def atr10_mean(cs):
    """Average of ATR(14) at each of the last 10 fully closed entry-TF bars."""
    if len(cs)<40:return None
    vals=[]
    for end in range(len(cs)-9,len(cs)+1):
        a=atr14(cs[:end])
        if a is None:return None
        vals.append(a)
    return sum(vals)/len(vals)

def levels(entry,side,atr):
    d=1 if side=='LONG' else -1
    risk=2.5*atr
    return dict(estimated_stop=round(entry-d*risk,2),
                estimated_tp1=round(entry+d*1.5*risk,2),
                estimated_tp2=round(entry+d*2.5*risk,2),
                estimated_atr=round(atr,4))

def latest_closed(cs,tf,now):
    d=DUR[tf]
    rows=[c for c in cs if c['ts']+d<=now]
    if len(rows)<210:return []
    if any(b['ts']-a['ts']!=d for a,b in zip(rows[-210:],rows[-209:])):return []
    if now-(rows[-1]['ts']+d)>2*d:return []
    return rows

def indicators(cs):
    closes=[c['close'] for c in cs]
    e20=ema_series(closes,20)[-1];e50=ema_series(closes,50)[-1];e200=ema_series(closes,200)[-1]
    return dict(ema20=e20,ema50=e50,ema200=e200,adx=adx14(cs),atr=atr14(cs))

def check_exits(db,cs,now):
    """Illustrative paper positions only; OHLC ambiguity uses stop-first."""
    for t in db['signals']:
        if t['status'] not in ('WAITING','PAPER_OPEN'):continue
        d=1 if t['side']=='LONG' else -1
        if t['status']=='WAITING':
            for c in cs:
                if c['ts']<t.get('last_checked_ts',t['created_at']-DUR['15m']) or c['ts']>=t['expires_at'] or c['ts']+DUR['15m']>now:continue
                t['last_checked_ts']=c['ts']+1
                hit=(c['low']<=t['limit_price'] if d==1 else c['high']>=t['limit_price'])
                if not hit:continue
                # ATR from the entry timeframe, using bars closed before the fill candle.
                tf=t['entry_timeframe'];history=latest_closed_at(BARS[tf],tf,c['ts'])
                atr=atr14(history)
                if atr is None:
                    t['status']='ATR_MISSING';break
                risk=2.5*atr
                if not .001<=risk/t['limit_price']<=.03:
                    t['status']='RISK_REJECT';break
                t.update(status='PAPER_OPEN',filled_at=c['ts'],entry=t['limit_price'],atr_at_fill=atr,
                         stop=t['limit_price']-d*risk,tp1=t['limit_price']+d*1.5*risk,
                         tp2=t['limit_price']+d*2.5*risk,tp1_touched=False,last_checked_ts=c['ts']-DUR['15m'])
                break
            if t['status']=='WAITING' and now>=t['expires_at']:t['status']='EXPIRED'
        if t['status']!='PAPER_OPEN':continue
        for c in cs:
            if c['ts']<=t['last_checked_ts'] or c['ts']<t['filled_at'] or c['ts']+DUR['15m']>now:continue
            if t['last_checked_ts']>=t['filled_at'] and c['ts']-t['last_checked_ts']>DUR['15m']:
                t['status']='DATA_GAP';break
            t['last_checked_ts']=c['ts']
            if (c['high']>=t['tp1'] if d==1 else c['low']<=t['tp1']):t['tp1_touched']=True
            stop=(c['low']<=t['stop'] if d==1 else c['high']>=t['stop'])
            tp=(c['high']>=t['tp2'] if d==1 else c['low']<=t['tp2'])
            if stop or tp or c['ts']+DUR['15m']-t['filled_at']>=HOLD_MS:
                t['status']='STOP' if stop else 'TP2' if tp else 'TIMEOUT'
                t['closed_at']=c['ts']+DUR['15m'];t['exit']=t['stop'] if stop else t['tp2'] if tp else c['close']
                break

def latest_closed_at(cs,tf,at):
    d=DUR[tf]
    return [c for c in cs if c['ts']+d<=at][-250:]

def main():
    global BARS
    now=int(time.time()*1000)
    # For EMA200 on 1D, 300 daily candles are required.
    BARS={tf:api(bar) for tf,bar in legacy.BAR.items()}
    clean={tf:latest_closed(BARS[tf],tf,now) for tf in DUR}
    if any(not v for v in clean.values()):raise RuntimeError('Insufficient/stale candles; no signals issued')
    ind={tf:indicators(v) for tf,v in clean.items()}
    trends={tf:legacy.structure(clean[tf],tf) for tf in ('1D','4H','1H')}
    db=json.loads(OUT.read_text()) if OUT.exists() else {'version':'7.28','signals':[]}
    db.setdefault('signals',[])
    # Note: OHLC 15m paper fill is NOT an executable limit-order confirmation.
    check_exits(db,clean['15m'],now)
    # Reprice only after checking all confirmed 15m fill candles against previous limit.
    for t in db['signals']:
        if t['status']!='WAITING':continue
        tf=t['entry_timeframe'];rows=clean[tf];last=rows[-1]
        close_at=last['ts']+DUR[tf]
        if close_at<=t.get('last_repriced_candle_close',t.get('signal_candle_close',0)):continue
        ix=ind[tf];price=round(ix['ema50'],2)
        atr_est=atr10_mean(rows)
        if atr_est is None:continue
        t.update(limit_price=price,**levels(price,t['side'],atr_est),
                 last_repriced_candle_close=close_at,
                 estimate_updated_at=close_at,
                 ema20=round(ix['ema20'],2),ema50=round(ix['ema50'],2))
        t.setdefault('limit_updates',[]).append({'candle_close':close_at,'limit_price':price})
        t['limit_updates']=t['limit_updates'][-30:]
    for source,entries in MAP.items():
        side=trends[source]
        if side not in ('LONG','SHORT'):continue
        for tf in entries:
            ix=ind[tf];last=clean[tf][-1];d=1 if side=='LONG' else -1
            if ix['adx'] is None or ix['adx']<25:continue
            if not ((ix['ema50']>ix['ema200']) if d==1 else (ix['ema50']<ix['ema200'])):continue
            # B: Limit at EMA50. Only when price is on the favorable side of target.
            if not ((last['close']>ix['ema50']) if d==1 else (last['close']<ix['ema50'])):continue
            target=ix['ema50']
            atr_est=atr10_mean(clean[tf])
            if atr_est is None:continue
            # Same direction may be signaled again on a later closed bar.
            # Dedupe ONLY identical direction/entry-timeframe/bar; no cooldown or one-open restriction.
            ident=f'{side}-{tf}-{last["ts"]+DUR[tf]}'
            if any(s['id']==ident for s in db['signals']):continue
            sources=[s for s in MAP if tf in MAP[s] and trends[s]==side]
            db['signals'].append(dict(id=ident,side=side,trend_sources=sources,entry_timeframe=tf,
                created_at=now,signal_candle_close=last['ts']+DUR[tf],expires_at=now+MAX_AGE_MS,
                limit_price=round(target,2),**levels(round(target,2),side,atr_est),
                estimate_updated_at=last['ts']+DUR[tf],
                last_repriced_candle_close=last['ts']+DUR[tf],
                last_checked_ts=now-DUR['15m'],
                adx=round(ix['adx'],2),ema20=round(ix['ema20'],2),
                ema50=round(ix['ema50'],2),ema200=round(ix['ema200'],2),status='WAITING',
                note='MANUAL ONLY. Limit price is a watch level, not an exchange order.'))
    db['signals']=db['signals'][-1000:]
    db.update(updated_at=now,trend=trends,settings={'stop_atr':2.5,'tp1_r':1.5,'tp2_r':2.5,
      'tp1_partial':False,'max_hold_hours':240,'limit_valid_hours':4,'cooldown_hours':0,
      'allow_repeated_same_side':True,'score_gate':'disabled for independent opposing trends',
      'paper_only':True,'atr_at_fill':'last fully closed entry-timeframe bar',
      'estimate_atr':'mean of ATR14 values on last 10 fully closed entry-timeframe bars',
      'reprice':'EMA50 on every closed entry-timeframe candle'})
    tmp=OUT.with_suffix('.tmp');tmp.write_text(json.dumps(db,ensure_ascii=False,indent=2));os.replace(tmp,OUT)
    print('V7.28',len(db['signals']),'signals')
if __name__=='__main__':main()
