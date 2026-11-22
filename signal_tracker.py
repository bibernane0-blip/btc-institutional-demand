#!/usr/bin/env python3
"""V7.16 experimental server-side paper signal tracker. No order execution."""
import json, math, os, time
from pathlib import Path
import requests

BASE='https://www.okx.com'; HISTORY=Path('market_history.json'); OUT=Path('signal_history.json')
H=3600000; M=60000; NOW=int(time.time()*1000)
S=requests.Session(); S.headers.update({'User-Agent':'BTC-Demand-SignalTracker/7.16'})
DUR={'1D':24*H,'4H':4*H,'1H':H,'15m':15*M}
BAR={'1D':'1Dutc','4H':'4H','1H':'1H','15m':'15m'}
LAG={'1D':48*H,'4H':8*H,'1H':2*H,'15m':30*M}
COST={'fee_per_side':.0005,'slippage_per_side':.0002,'funding_per_8h':.0001}

def api(path,params):
    for attempt in range(3):
        try:
            r=S.get(BASE+path,params=params,timeout=20);r.raise_for_status();v=r.json()
            if str(v.get('code'))!='0' or not isinstance(v.get('data'),list):raise ValueError(str(v.get('msg')))
            return v['data']
        except (requests.RequestException,ValueError):
            if attempt==2:raise
            time.sleep(2**attempt)

def candle(row):
    return dict(ts=int(row[0]),open=float(row[1]),high=float(row[2]),low=float(row[3]),close=float(row[4]),volume=float(row[5]),confirm=str(row[8]))

def clean(rows,duration,limit=100):
    a={}
    for c in rows:
        if c.get('confirm')!='1' or c['ts']+duration>NOW:continue
        if not all(math.isfinite(c[k]) and c[k]>0 for k in ('open','high','low','close')):continue
        a[c['ts']]=c
    return sorted(a.values(),key=lambda x:x['ts'])[-limit:]

def contiguous(cs,duration,n):
    return len(cs)>=n and all(b['ts']-a['ts']==duration for a,b in zip(cs[-n:],cs[-n+1:]))

def ema(values,n):
    v=values[0];k=2/(n+1)
    for x in values[1:]:v=x*k+v*(1-k)
    return v

def structure(cs,key):
    d=DUR[key];cs=clean(cs,d)
    if len(cs)<35 or not contiguous(cs,d,35) or NOW-(cs[-1]['ts']+d)>LAG[key]:return None
    hi=[];lo=[]
    for i in range(2,len(cs)-2):
        if all(j==i or cs[i]['high']>cs[j]['high'] for j in range(i-2,i+3)):hi.append(cs[i]['high'])
        if all(j==i or cs[i]['low']<cs[j]['low'] for j in range(i-2,i+3)):lo.append(cs[i]['low'])
    if min(len(hi),len(lo))<2:return 'YATAY'
    if hi[-1]>hi[-2] and lo[-1]>lo[-2]:return 'LONG'
    if hi[-1]<hi[-2] and lo[-1]<lo[-2]:return 'SHORT'
    return 'YATAY'

def regime(cs,key):
    d=DUR[key];cs=clean(cs,d)
    if len(cs)<65 or not contiguous(cs,d,len(cs)) or NOW-(cs[-1]['ts']+d)>LAG[key]:return None
    tr=[];plus=[];minus=[]
    for p,c in zip(cs,cs[1:]):
        up=c['high']-p['high'];down=p['low']-c['low']
        tr.append(max(c['high']-c['low'],abs(c['high']-p['close']),abs(c['low']-p['close'])))
        plus.append(up if up>down and up>0 else 0)
        minus.append(down if down>up and down>0 else 0)
    n=14;st=sum(tr[:n]);sp=sum(plus[:n]);sm=sum(minus[:n]);dx=[];atrs=[]
    def rec(i):
        p=100*sp/st if st else 0;m=100*sm/st if st else 0
        dx.append(100*abs(p-m)/(p+m) if p+m else 0)
        atrs.append(100*(st/n)/cs[i+1]['close'])
    rec(n-1)
    for i in range(n,len(tr)):
        st=st-st/n+tr[i];sp=sp-sp/n+plus[i];sm=sm-sm/n+minus[i];rec(i)
    adx=sum(dx[:n])/n
    for x in dx[n:]:adx=(adx*(n-1)+x)/n
    ref=sorted(atrs[-41:-1]);median=(ref[19]+ref[20])/2 if len(ref)>=40 else 0
    if not median:return None
    return {'atr_pct':atrs[-1],'volatility':'HIGH' if atrs[-1]/median>=1.5 else 'OK'}

def score(history,cs,price,oi,oi_ts,funding_pct):
    hourly=clean(cs,H,2500)
    if not contiguous(hourly,H,80) or hourly[-1]['ts']!=(NOW//H)*H-H:return None,'Hourly data stale'
    closes=[c['close'] for c in hourly];e20=ema(closes[-40:],20);e50=ema(closes[-80:],50)
    avg=sum(c['volume'] for c in hourly[-21:-1])/20
    ps=50 if price>e20 and price>e50 and hourly[-1]['volume']>avg else 20 if price>e20 else -50 if price<e20 and price<e50 and hourly[-1]['volume']>avg else 0
    target=(NOW-6*H)//H*H-H
    base=next((c for c in hourly if c['ts']==target),None)
    if base is None:return None,'6h price reference missing'
    pchange=(price/base['close']-1)*100
    bt=oi_ts-6*H
    candidates=[x for x in history.get('oi',[]) if isinstance(x.get('ts'),int) and bt-60*M<=x['ts']<=bt and x.get('oiBtc',0)>0]
    if not candidates:return None,'6h OI reference missing'
    baseline=max(candidates,key=lambda x:x['ts'])
    ochange=(oi/baseline['oiBtc']-1)*100
    oi_score=30 if pchange>0 and ochange>.5 else 12 if pchange>0 and abs(ochange)<=.5 else -23 if pchange<0 and ochange>.5 else 0
    funding_score=7 if funding_pct<=-.05 else 12 if funding_pct<-.01 else 20 if abs(funding_pct)<=.01 else -20 if funding_pct>=.05 else 0
    return max(-100,min(100,ps+oi_score+funding_score)),None

def setup(cs,atr_pct,side):
    cs=clean(cs,15*M,45)
    if len(cs)<35 or not contiguous(cs,15*M,len(cs)) or NOW-(cs[-1]['ts']+15*M)>30*M:return None
    values=[c['close'] for c in cs];e=[];v=values[0];k=2/21
    for close in values:
        v=close*k+v*(1-k) if e else close;e.append(v)
    confirm=cs[-2];breakout=cs[-1];islong=side=='LONG'
    if not (confirm['low']<=e[-2]<=confirm['high']):return None
    if not (confirm['close']>e[-2] if islong else confirm['close']<e[-2]):return None
    if not (breakout['high']>confirm['high'] and breakout['close']>confirm['high'] if islong else breakout['low']<confirm['low'] and breakout['close']<confirm['low']):return None
    entry=breakout['close'];atr=entry*atr_pct/100
    swing=min(c['low'] for c in cs[-10:-1]) if islong else max(c['high'] for c in cs[-10:-1])
    stop=swing-atr if islong else swing+atr;risk=abs(entry-stop)
    if not ((stop<entry) if islong else (stop>entry)) or not .001<=risk/entry<=.03:return None
    direction=1 if islong else -1
    return {'side':side,'signal_close':breakout['ts']+15*M,'reference_entry':entry,'stop':stop,'tp1':entry+direction*1.5*risk,'tp2':entry+direction*2.5*risk,'risk_pct':100*risk/entry}

def update_open(trades,cs):
    closed=clean(cs,15*M,9000)
    for t in trades:
        if t['status']!='OPEN':continue
        bars=[c for c in closed if c['ts']>=t['signal_close'] and c['ts']>t['last_candle_ts']]
        for c in bars:
            if t['last_candle_ts'] and c['ts']-t['last_candle_ts']>15*M:
                t['status']='DATA_GAP';t['reason']='Missing 15m candle; no inferred exit';break
            t['last_candle_ts']=c['ts'];islong=t['side']=='LONG'
            sl=c['low']<=t['stop'] if islong else c['high']>=t['stop']
            tp=c['high']>=t['tp2'] if islong else c['low']<=t['tp2']
            if c['high']>=t['tp1'] if islong else c['low']<=t['tp1']:t['tp1_touched']=True
            if sl or tp or c['ts']+15*M-t['opened_at']>=72*H:
                status='STOP' if sl else 'TP2' if tp else 'TIMEOUT'
                exit_price=t['stop'] if sl else t['tp2'] if tp else c['close']
                risk=abs(t['entry']-t['stop']);gross=(1 if islong else -1)*(exit_price-t['entry'])/risk
                hours=max(0,c['ts']+15*M-t['opened_at'])/H
                costs=((t['entry']+exit_price)*(COST['fee_per_side']+COST['slippage_per_side'])+t['entry']*COST['funding_per_8h']*hours/8)/risk
                t.update(status=status,exit=exit_price,closed_at=c['ts']+15*M,gross_r=round(gross,6),net_r=round(gross-costs,6));break

def main():
    history=json.loads(HISTORY.read_text());db=json.loads(OUT.read_text()) if OUT.exists() else {'version':1,'trades':[],'checks':[]}
    trades=db.setdefault('trades',[]);checks=db.setdefault('checks',[])
    cs15=history.get('candles_15m',[])
    if not cs15:raise ValueError('15m history missing')
    update_open(trades,cs15)
    result={'ts':NOW,'status':'WAIT'}
    try:
        ticker=api('/api/v5/market/ticker',{'instId':'BTC-USDT'})[0]
        oi_row=api('/api/v5/public/open-interest',{'instType':'SWAP','instId':'BTC-USDT-SWAP'})[0]
        funding_row=api('/api/v5/public/funding-rate',{'instId':'BTC-USDT-SWAP'})[0]
        price=float(ticker['last']);price_ts=int(ticker['ts']);oi=float(oi_row['oiCcy']);oi_ts=int(oi_row['ts'])
        funding_pct=float(funding_row['fundingRate'])*100
        if NOW-price_ts>30000 or NOW-oi_ts>90000 or not all(math.isfinite(x) for x in (price,oi,funding_pct)):
            raise ValueError('Live price/OI stale or invalid')
        bars={}
        for key in ('1D','4H','1H','15m'):
            bars[key]=[candle(x) for x in api('/api/v5/market/candles',{'instId':'BTC-USDT','bar':BAR[key],'limit':'100'})]
        s,error=score(history,history.get('candles',[]),price,oi,oi_ts,funding_pct)
        result['score']=s
        if error:result['reason']=error
        elif s is None or -30<s<30:result['reason']='Score neutral'
        else:
            side='LONG' if s>=30 else 'SHORT'
            trends={k:structure(bars[k],k) for k in DUR}
            regimes={k:regime(bars[k],k) for k in DUR}
            result['trends']=trends
            if any(v is None for v in trends.values()) or any(v is None for v in regimes.values()):result['reason']='Structure/regime unavailable'
            elif regimes['1D']['volatility']=='HIGH':result['reason']='Daily volatility high'
            else:
                pairs=[(a,b) for a,b in [('1D','4H'),('4H','1H'),('1H','15m')] if trends[a]==side and trends[b]==side]
                if not pairs:result['reason']='No adjacent timeframe agreement'
                else:
                    plan=setup(bars['15m'],regimes['15m']['atr_pct'],side)
                    if plan is None:result['reason']='15m EMA20 setup absent'
                    else:
                        result['status']='SIGNAL';result['pair']=pairs[0]
                        # Signal confirmed on completed candle, not a real fill. Use next bar OPEN only when observed.
                        ident=f"{side}-{plan['signal_close']}"
                        if not any(t['id']==ident for t in trades):
                            trades.append(dict(id=ident,**plan,status='PENDING',created_at=NOW))
        # Pending signals: enter only at observed first subsequent closed 15m bar OPEN.
        for t in trades:
            if t['status']!='PENDING':continue
            future=sorted((c for c in cs15 if c.get('confirm')=='1' and c['ts']>=t['signal_close']),key=lambda x:x['ts'])
            if not future:continue
            first=future[0]
            if first['ts']!=t['signal_close'] or t['created_at']>=first['ts']+15*M:
                t.update(status='MISSED_ENTRY',reason='Next-bar open not observable at signal check');continue
            entry=first['open'];risk=abs(entry-t['stop']);islong=t['side']=='LONG'
            if not (entry>t['stop'] if islong else entry<t['stop']) or not .001<=risk/entry<=.03:
                t.update(status='INVALID_ENTRY',reason='Open price invalidates risk');continue
            t.update(status='OPEN',entry=entry,opened_at=first['ts'],last_candle_ts=first['ts']-15*M,tp1=entry+(1 if islong else -1)*1.5*risk,tp2=entry+(1 if islong else -1)*2.5*risk,tp1_touched=False)
        update_open(trades,cs15)
    except (requests.RequestException,ValueError,KeyError,IndexError,TypeError) as e:
        result.update(status='ERROR',reason=str(e))
    checks.append(result);db['checks']=checks[-200:];db['trades']=trades[-500:];db['updated_at']=NOW
    tmp=OUT.with_suffix('.json.tmp');tmp.write_text(json.dumps(db,ensure_ascii=False,separators=(',',':'))+'\n');os.replace(tmp,OUT)
    print('V7.16:',result,'trades:',len(trades))

if __name__=='__main__':main()
