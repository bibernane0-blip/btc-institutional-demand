#!/usr/bin/env python3
"""Collect exchange-native BTC-USDT-SWAP historical OI, without synthetic fills."""
import json, time, datetime as dt, pathlib
import requests

BASE='https://www.okx.com/api/v5/rubik/stat/contracts/open-interest-history'
OUT=pathlib.Path('historical_oi.json')
INST='BTC-USDT-SWAP'
PERIOD='1H'
TARGET_DAYS=90

def utc(ms):
    return dt.datetime.fromtimestamp(ms/1000, dt.timezone.utc).isoformat()

def main():
    cutoff=int((dt.datetime.now(dt.timezone.utc)-dt.timedelta(days=TARGET_DAYS)).timestamp()*1000)
    rows={}
    cursor=None
    errors=[]
    session=requests.Session()
    for page in range(20):
        params={'instId':INST,'period':PERIOD,'limit':'100'}
        if cursor is not None: params['end']=str(cursor)
        try:
            response=session.get(BASE,params=params,timeout=25)
            response.raise_for_status()
            body=response.json()
            if body.get('code')!='0': raise RuntimeError(str(body))
            data=body.get('data') or []
        except Exception as exc:
            errors.append(f'page {page+1}: {exc}')
            break
        if not data: break
        for item in data:
            try:
                ts=int(item[0]); oi_btc=float(item[2]); oi_usd=float(item[3])
                if ts>=cutoff and oi_btc>=0:
                    rows[ts]={'ts':ts,'oiBtc':oi_btc,'oiUsd':oi_usd}
            except (IndexError,ValueError,TypeError) as exc:
                errors.append(f'invalid row: {exc}')
        oldest=min(int(x[0]) for x in data)
        if cursor is not None and oldest>=cursor: break
        cursor=oldest
        if oldest<cutoff or len(data)<100: break
        time.sleep(.25)
    values=[rows[k] for k in sorted(rows)]
    # Repeated runs merge previously observed historical rows; never interpolate.
    if OUT.exists():
        try:
            old=json.loads(OUT.read_text())
            if old.get('instrument')==INST and old.get('period')==PERIOD:
                for x in old.get('records',[]):
                    if int(x['ts'])>=cutoff: rows[int(x['ts'])]=x
                values=[rows[k] for k in sorted(rows)]
        except (ValueError,KeyError,TypeError):
            pass
    payload={'version':1,'instrument':INST,'period':PERIOD,'unit':'BTC (oiCcy)',
             'source':'OKX /api/v5/rubik/stat/contracts/open-interest-history',
             'updated_at':dt.datetime.now(dt.timezone.utc).isoformat(),
             'count':len(values),'first_utc':utc(values[0]['ts']) if values else None,
             'last_utc':utc(values[-1]['ts']) if values else None,
             'span_days':round((values[-1]['ts']-values[0]['ts'])/86400000,2) if len(values)>1 else 0,
             'errors':errors,'records':values}
    OUT.write_text(json.dumps(payload,separators=(',',':'),ensure_ascii=False)+'\n')
    print(json.dumps({k:v for k,v in payload.items() if k!='records'},indent=2))
    if not values: raise SystemExit('No OI data retrieved; inspect errors')

if __name__=='__main__': main()
