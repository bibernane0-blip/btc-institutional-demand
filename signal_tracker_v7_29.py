#!/usr/bin/env python3
"""V7.29 manual-only BTC watchlist; no exchange orders. Requires requests and signal_tracker.py."""
import json, math, os, time
from pathlib import Path
import requests
import signal_tracker as legacy
from backtest_v7_21 import adx14

ROOT=Path(__file__).resolve().parent
OUT=ROOT/'manual_signals_v7_28.json'
DUR=legacy.DUR
MAP={'1D':('4H','1H','15m'),'4H':('1H','15m'),'1H':('15m',)}
LIMIT_BARS={"15m":4,"1H":16,"4H":16}  # Expiry is anchored to the original signal candle close.
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
                estimated_tp2=round(entry+d*3.0*risk,2),
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

def trend_at(cs, tf, at):
    """Historical trend using only candles closed by `at` (no lookahead)."""
    rows=latest_closed_at(cs,tf,at)
    if len(rows)<35:return None
    if any(y['ts']-x['ts']!=DUR[tf] for x,y in zip(rows[-35:],rows[-34:])):return None
    highs=[];lows=[]
    for i in range(2,len(rows)-2):
        if all(j==i or rows[i]['high']>rows[j]['high'] for j in range(i-2,i+3)):highs.append(rows[i]['high'])
        if all(j==i or rows[i]['low']<rows[j]['low'] for j in range(i-2,i+3)):lows.append(rows[i]['low'])
    if len(highs)<2 or len(lows)<2:return 'YATAY'
    if highs[-1]>highs[-2] and lows[-1]>lows[-2]:return 'LONG'
    if highs[-1]<highs[-2] and lows[-1]<lows[-2]:return 'SHORT'
    return 'YATAY'

def validate_and_reprice(t,at):
    """Validate at a completed entry-TF candle boundary, before the next 15m fill bar."""
    tf=t['entry_timeframe'];rows=latest_closed_at(BARS[tf],tf,at)
    if len(rows)<210 or any(y['ts']-x['ts']!=DUR[tf] for x,y in zip(rows[-210:],rows[-209:])):
        t.update(status='INVALIDATED_DATA',invalidated_at=at);return
    ix=indicators(rows);d=1 if t['side']=='LONG' else -1
    valid=(ix['adx'] is not None and ix['adx']>=25 and
           ((ix['ema50']>ix['ema200']) if d==1 else (ix['ema50']<ix['ema200'])) and
           ((rows[-1]['close']>ix['ema50']) if d==1 else (rows[-1]['close']<ix['ema50'])))
    sources=t.get('trend_sources',[])
    if valid:valid=bool(sources) and any(trend_at(BARS[src],src,at)==t['side'] for src in sources)
    if not valid:
        t.update(status='INVALIDATED',invalidated_at=at,invalidated_reason='ADX / EMA50-EMA200 / close-EMA50 / original parent trend')
        return
    price=round(ix['ema20'],2);atr_est=atr10_mean(rows)
    if atr_est is None:t.update(status='INVALIDATED_DATA',invalidated_at=at);return
    t.update(limit_price=price,**levels(price,t['side'],atr_est),
             last_repriced_candle_close=at,estimate_updated_at=at,
             adx=round(ix['adx'],2),ema20=round(ix['ema20'],2),
             ema50=round(ix['ema50'],2),ema200=round(ix['ema200'],2))
    t.setdefault('limit_updates',[]).append({'candle_close':at,'limit_price':price})
    t['limit_updates']=t['limit_updates'][-30:]

def check_waiting(db,cs,now):
    """Replay closed 15m bars chronologically; recheck and reprice at entry-TF boundaries."""
    for t in db['signals']:
        if t['status']!='WAITING':continue
        tf=t['entry_timeframe'];d=1 if t['side']=='LONG' else -1
        cursor=t.get('last_checked_ts',t['created_at'])
        for c in cs:
            start=c['ts'];end=start+DUR['15m']
            if end>now or start<cursor or start<t['created_at']:continue
            if start>=t['expires_at']:
                t.update(status='EXPIRED',closed_at=t['expires_at']);break
            if start>cursor and start-cursor>DUR['15m']*2:
                t.update(status='DATA_GAP',closed_at=start);break
            # The EMA20 update and validity check apply at the START of a new 15m candle,
            # using the most recently completed entry-TF candle only.
            last_boundary=(start//DUR[tf])*DUR[tf]
            if last_boundary>t.get('last_repriced_candle_close',t['signal_candle_close']):
                validate_and_reprice(t,last_boundary)
                if t['status']!='WAITING':break
            t['last_checked_ts']=end
            hit=(c['low']<=t['limit_price'] if d==1 else c['high']>=t['limit_price'])
            if not hit:continue
            history=latest_closed_at(BARS[tf],tf,start);atr=atr14(history)
            if atr is None:t.update(status='ATR_MISSING',closed_at=start);break
            risk=2.5*atr
            t.update(status='PAPER_OPEN',filled_at=start,entry=t['limit_price'],atr_at_fill=atr,
                     stop=t['limit_price']-d*risk,tp1=t['limit_price']+d*1.5*risk,
                     tp2=t['limit_price']+d*3.0*risk,tp1_touched=False,
                     last_checked_ts=start-DUR['15m'])
            break
        if t['status']=='WAITING' and now>=t['expires_at']:
            t.update(status='EXPIRED',closed_at=t['expires_at'])

def check_exits(db,cs,now):
    """Paper positions only. Ambiguous OHLC candles assume STOP before TP2."""
    for t in db['signals']:
        if t['status']!='PAPER_OPEN':continue
        d=1 if t['side']=='LONG' else -1
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
    db=json.loads(OUT.read_text()) if OUT.exists() else {'version':'7.29','signals':[]}
    db.setdefault('signals',[])
    # Migration: previously queued EMA50 limits are cancelled, not silently rewritten.
    for t in db['signals']:
        if t['status']=='WAITING' and t.get('strategy_version')!='7.29':
            t.update(status='CANCELLED_STRATEGY_CHANGE',closed_at=now)
    # Historical replay handles validation, EMA20 repricing and fill in chronological order.
    check_waiting(db,clean['15m'],now)
    check_exits(db,clean['15m'],now)
    for source,entries in MAP.items():
        side=trends[source]
        if side not in ('LONG','SHORT'):continue
        for tf in entries:
            ix=ind[tf];last=clean[tf][-1];d=1 if side=='LONG' else -1
            if ix['adx'] is None or ix['adx']<25:continue
            if not ((ix['ema50']>ix['ema200']) if d==1 else (ix['ema50']<ix['ema200'])):continue
            # EMA20 limit, while EMA50 remains the trend-side filter.
            if not ((last['close']>ix['ema50']) if d==1 else (last['close']<ix['ema50'])):continue
            target=ix['ema20']
            atr_est=atr10_mean(clean[tf])
            if atr_est is None:continue
            # Same direction may be signaled again on a later closed bar.
            # Dedupe ONLY identical direction/entry-timeframe/bar; no cooldown or one-open restriction.
            ident=f'{side}-{tf}-{last["ts"]+DUR[tf]}'
            if any(s['id']==ident for s in db['signals']):continue
            sources=[s for s in MAP if tf in MAP[s] and trends[s]==side]
            db['signals'].append(dict(id=ident,side=side,trend_sources=sources,entry_timeframe=tf,
                created_at=now,signal_candle_close=last['ts']+DUR[tf],
                expires_at=last['ts']+DUR[tf]+LIMIT_BARS[tf]*DUR[tf],
                strategy_version='7.29',limit_valid_bars=LIMIT_BARS[tf],
                limit_price=round(target,2),**levels(round(target,2),side,atr_est),
                estimate_updated_at=last['ts']+DUR[tf],
                last_repriced_candle_close=last['ts']+DUR[tf],
                last_checked_ts=now,
                adx=round(ix['adx'],2),ema20=round(ix['ema20'],2),
                ema50=round(ix['ema50'],2),ema200=round(ix['ema200'],2),status='WAITING',
                note='MANUAL ONLY. Limit price is a watch level, not an exchange order.'))
    db['signals']=db['signals'][-1000:]
    db.update(version='7.29',updated_at=now,trend=trends,settings={'stop_atr':2.5,'tp1_r':1.5,'tp2_r':3.0,
      'tp1_partial':False,'max_hold_hours':240,'limit_valid_bars':LIMIT_BARS,'cooldown_hours':0,
      'allow_repeated_same_side':True,'score_gate':'disabled for independent opposing trends',
      'paper_only':True,'atr_at_fill':'last fully closed entry-timeframe bar',
      'estimate_atr':'mean of ATR14 values on last 10 fully closed entry-timeframe bars',
      'reprice':'EMA20 on every closed entry-timeframe candle',
      'pending_validation':'ADX14>=25, EMA50/EMA200 direction, close relative to EMA50, at least one ORIGINAL parent trend source',
      'expiry_reset_on_reprice':False})
    tmp=OUT.with_suffix('.tmp');tmp.write_text(json.dumps(db,ensure_ascii=False,indent=2));os.replace(tmp,OUT)
    print('V7.29',len(db['signals']),'signals')
if __name__=='__main__':main()
