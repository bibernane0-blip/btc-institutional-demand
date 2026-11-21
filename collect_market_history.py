#!/usr/bin/env python3
"""V5.1: collect public OKX OI and 1H candles; preserve history on errors."""
import json
from pathlib import Path
from datetime import datetime, timezone
import requests

BASE='https://www.okx.com'
OUT=Path('market_history.json')
now=int(datetime.now(timezone.utc).timestamp()*1000)
old={'version':1,'oi':[],'candles':[],'errors':[]}
if OUT.exists():
    try:
        candidate=json.loads(OUT.read_text(encoding='utf-8'))
        if isinstance(candidate,dict): old.update(candidate)
    except (ValueError,OSError): pass
errors=[]

def get(path,params):
    response=requests.get(BASE+path,params=params,timeout=25,headers={'User-Agent':'BTC-Demand-V5.1/1.0'})
    response.raise_for_status()
    payload=response.json()
    if str(payload.get('code'))!='0' or not isinstance(payload.get('data'),list) or not payload['data']:
        raise ValueError(f'OKX invalid response {path}: {payload.get("msg")}')
    return payload['data']

try:
    row=get('/api/v5/public/open-interest',{'instType':'SWAP','instId':'BTC-USDT-SWAP'})[0]
    ts=int(row['ts']); oi=float(row['oiCcy'])
    if 0 < oi and abs(now-ts)<10*60*1000:
        old['oi'].append({'ts':ts,'oiBtc':oi})
    else: raise ValueError('OI timestamp/value invalid')
except (requests.RequestException,ValueError,KeyError,TypeError) as exc:
    errors.append('OI: '+str(exc))

try:
    rows=get('/api/v5/market/candles',{'instId':'BTC-USDT','bar':'1H','limit':'100'})
    for row in rows:
        item={'ts':int(row[0]),'open':float(row[1]),'high':float(row[2]),'low':float(row[3]),'close':float(row[4]),'volume':float(row[5]),'confirm':str(row[8])}
        if item['ts'] <= now and item['close']>0: old['candles'].append(item)
except (requests.RequestException,ValueError,KeyError,TypeError,IndexError) as exc:
    errors.append('Candles: '+str(exc))

# Deduplicate by source timestamp, retain bounded history.
def dedup(items,days):
    cutoff=now-days*86400000
    return sorted({int(x['ts']):x for x in items if isinstance(x,dict) and isinstance(x.get('ts'),int) and cutoff<=x['ts']<=now+60000}.values(),key=lambda x:x['ts'])

old['oi']=dedup(old.get('oi',[]),30)[-10000:]
old['candles']=dedup(old.get('candles',[]),90)[-2200:]
old['version']=1
old['checked_at']=datetime.now(timezone.utc).isoformat()
old['errors']=errors
old['oi_latest_ts']=old['oi'][-1]['ts'] if old['oi'] else None
old['candles_latest_ts']=old['candles'][-1]['ts'] if old['candles'] else None
OUT.write_text(json.dumps(old,ensure_ascii=False,separators=(',',':'))+'\n',encoding='utf-8')
print('OI records:',len(old['oi']),'Candle records:',len(old['candles']),'Errors:',errors)
