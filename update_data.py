#!/usr/bin/env python3
"""
BTC Institutional Demand PRO — Free data collector.
Sources:
- Binance public REST endpoints (market data; no key)
- Farside Investors public BTC ETF flow table (daily; HTML extraction)
- Kote Charts API (daily exchange netflow; free API key; non-commercial use with attribution)
"""
from __future__ import annotations
import os, re, json, time
from datetime import datetime, timezone, date
from typing import Any
import requests
from bs4 import BeautifulSoup

OUT = "data.json"
BINANCE_SPOT = "https://api.binance.com"
BINANCE_FUT = "https://fapi.binance.com"
FARSIDE_URL = "https://farside.co.uk/bitcoin-etf-flow-all-data/"
KOTE_URL = "https://kotecharts.com/api/v1/public/charts/exchange-netflow"
KOTE_KEY = os.getenv("KOTE_API_KEY", "").strip()
HEADERS = {"User-Agent": "BTC-Institutional-Demand-Personal-Dashboard/1.0"}

def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()

def get_json(url: str, params: dict | None = None, headers: dict | None = None) -> Any:
    r = requests.get(url, params=params, headers={**HEADERS, **(headers or {})}, timeout=25)
    r.raise_for_status()
    return r.json()

def get_html(url: str) -> str:
    r = requests.get(url, headers=HEADERS, timeout=25)
    r.raise_for_status()
    return r.text

def parse_money(cell: str) -> float | None:
    s = cell.strip().replace("$", "").replace(",", "").replace("−", "-")
    if s in {"", "-", "—", "–", "N/A", "n/a"}:
        return None
    neg = s.startswith("(") and s.endswith(")")
    if neg:
        s = s[1:-1]
    try:
        x = float(s)
        return -x if neg else x
    except ValueError:
        return None

def fetch_farside_etf() -> dict:
    """Extract latest complete daily total from Farside's public all-data table."""
    html = get_html(FARSIDE_URL)
    soup = BeautifulSoup(html, "html.parser")
    best = None
    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if not rows:
            continue
        headers = [c.get_text(" ", strip=True).lower() for c in rows[0].find_all(["th", "td"])]
        if not any("date" in h for h in headers) or not any("total" in h for h in headers):
            continue
        for row in rows[1:]:
            cells = [c.get_text(" ", strip=True) for c in row.find_all(["td", "th"])]
            if len(cells) < 3:
                continue
            try:
                dt = datetime.strptime(cells[0].strip(), "%d %b %Y").date()
            except ValueError:
                continue
            total = parse_money(cells[-1])
            # Skip placeholder rows (all dashes) and summary rows.
            any_fund_value = any(parse_money(v) is not None for v in cells[1:-1])
            if total is None or not any_fund_value:
                continue
            candidate = {"date": dt.isoformat(), "flow_usd_m": total}
            if best is None or candidate["date"] > best["date"]:
                best = candidate
    if best is None:
        raise RuntimeError("Farside tablosundan tamamlanmış günlük ETF akışı okunamadı.")
    return {"ok": True, **best, "source": "Farside Investors",
            "source_url": FARSIDE_URL, "frequency": "daily", "fetched_at": utcnow()}

def _find_points(obj: Any) -> list[dict]:
    """Find a time-series list in flexible Kote response envelopes."""
    if isinstance(obj, list):
        if obj and all(isinstance(x, dict) for x in obj):
            # A points array normally contains t plus one or more metric fields.
            if any(("t" in x or "date" in x or "timestamp" in x) for x in obj):
                return obj
        for item in obj:
            found = _find_points(item)
            if found:
                return found
    elif isinstance(obj, dict):
        for key in ("data", "points", "series", "values", "result"):
            if key in obj:
                found = _find_points(obj[key])
                if found:
                    return found
        for value in obj.values():
            found = _find_points(value)
            if found:
                return found
    return []

def _point_date(p: dict) -> str:
    v = p.get("t", p.get("date", p.get("timestamp", "")))
    if isinstance(v, (int, float)):
        # Accept seconds or milliseconds.
        ts = float(v) / (1000 if v > 10_000_000_000 else 1)
        return datetime.fromtimestamp(ts, tz=timezone.utc).date().isoformat()
    s = str(v)
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        return s[:10]

def _netflow_value(p: dict) -> float | None:
    preferred = ["exNetBtc", "ex_net_btc", "netflow", "netFlow", "value"]
    for k in preferred:
        if k in p and p[k] is not None:
            try: return float(p[k])
            except (TypeError, ValueError): pass
    for k, v in p.items():
        lk = k.lower()
        if "net" in lk and ("btc" in lk or "flow" in lk) and v is not None:
            try: return float(v)
            except (TypeError, ValueError): pass
    return None

def fetch_kote_netflow() -> dict:
    if not KOTE_KEY:
        raise RuntimeError("GitHub Actions secret KOTE_API_KEY tanımlı değil.")
    payload = get_json(KOTE_URL, params={"granularity": "day"}, headers={"X-API-Key": KOTE_KEY})
    if payload.get("success") is False:
        raise RuntimeError(str(payload.get("error", "Kote API isteği başarısız.")))
    points = _find_points(payload.get("data", payload))
    normalized = []
    for p in points:
        d = _point_date(p)
        v = _netflow_value(p)
        if d and v is not None:
            normalized.append({"date": d, "netflow_btc": v})
    normalized.sort(key=lambda x: x["date"])
    if not normalized:
        raise RuntimeError("Kote yanıtında netflow serisi bulunamadı; API alan adı değişmiş olabilir.")
    latest = normalized[-1]
    recent = [x for x in normalized if (date.fromisoformat(latest["date"]) - date.fromisoformat(x["date"])).days <= 2]
    mean3 = sum(x["netflow_btc"] for x in recent) / len(recent)
    return {"ok": True, **latest, "mean_3d_btc": mean3, "observations_3d": len(recent),
            "source": "Kote Charts", "source_url": "https://kotecharts.com/c/exchange-netflow",
            "frequency": "daily", "fetched_at": utcnow(),
            "note": "Etiketlenmiş borsa cüzdanlarına dayalı tahmini on-chain akış; tek günlük transferler yanıltıcı olabilir."}

def fetch_binance() -> dict:
    # 1h spot klines for technical context
    klines = get_json(f"{BINANCE_SPOT}/api/v3/klines",
                      params={"symbol": "BTCUSDT", "interval": "1h", "limit": 60})
    closes = [float(k[4]) for k in klines]
    volumes = [float(k[5]) for k in klines]
    if len(closes) < 50:
        raise RuntimeError("Binance fiyat geçmişi yetersiz.")

    def ema(values: list[float], period: int) -> float:
        alpha = 2 / (period + 1)
        current = values[0]
        for v in values[1:]:
            current = alpha * v + (1 - alpha) * current
        return current

    ema20 = ema(closes[-40:], 20)
    ema50 = ema(closes[-60:], 50)
    vol_avg20 = sum(volumes[-20:]) / 20
    price_6h_pct = ((closes[-1] / closes[-7]) - 1) * 100 if closes[-7] else 0.0

    premium = get_json(f"{BINANCE_FUT}/fapi/v1/premiumIndex", params={"symbol": "BTCUSDT"})
    oi_hist = get_json(f"{BINANCE_FUT}/futures/data/openInterestHist",
                       params={"symbol": "BTCUSDT", "period": "1h", "limit": 7})
    if len(oi_hist) >= 2 and float(oi_hist[0]["sumOpenInterest"]) != 0:
        oi_change_6h = (float(oi_hist[-1]["sumOpenInterest"]) /
                        float(oi_hist[0]["sumOpenInterest"]) - 1) * 100
    else:
        oi_change_6h = None

    funding_pct = float(premium["lastFundingRate"]) * 100
    current_oi = get_json(f"{BINANCE_FUT}/fapi/v1/openInterest", params={"symbol": "BTCUSDT"})
    return {
        "ok": True, "source": "Binance public market data",
        "source_url": "https://developers.binance.com/docs/derivatives/usds-margined-futures/market-data/rest-api",
        "fetched_at": utcnow(), "frequency": "near-real-time",
        "price_usd": closes[-1], "ema20": ema20, "ema50": ema50,
        "volume": volumes[-1], "volume_avg20": vol_avg20,
        "price_change_6h_pct": price_6h_pct,
        "open_interest_btc": float(current_oi["openInterest"]),
        "oi_change_6h_pct": oi_change_6h,
        "funding_rate_pct": funding_pct,
    }

def score_components(etf: dict | None, netflow: dict | None, market: dict | None) -> dict:
    parts: dict[str, dict] = {}
    missing = []
    if not etf or not etf.get("ok"):
        missing.append("ETF")
    else:
        f = float(etf["flow_usd_m"])
        parts["ETF"] = {"score": 20 if f >= 100 else 10 if f > 0 else -20 if f <= -100 else -10 if f < 0 else 0,
                        "weight": 20, "value": f, "unit": "milyon USD", "date": etf["date"]}
    if not netflow or not netflow.get("ok"):
        missing.append("Exchange Netflow")
    else:
        f = float(netflow["mean_3d_btc"])
        parts["Exchange Netflow"] = {"score": 15 if f <= -500 else 7 if f < 0 else -15 if f >= 500 else -7 if f > 0 else 0,
                                     "weight": 15, "value": f, "unit": "BTC / 3 günlük ortalama", "date": netflow["date"]}
    if not market or not market.get("ok") or market.get("oi_change_6h_pct") is None:
        missing.append("Open Interest / Market")
    else:
        oi = float(market["oi_change_6h_pct"])
        pc = float(market["price_change_6h_pct"])
        oi_score = 20 if pc > 0 and oi > 0.5 else 8 if pc > 0 and abs(oi) <= 0.5 else -15 if pc < 0 and oi > 0.5 else 0
        parts["Open Interest"] = {"score": oi_score, "weight": 20,
                                  "value": oi, "unit": "% / 6 saat"}
        fr = float(market["funding_rate_pct"])
        if fr <= -0.05: fs = 5
        elif fr < -0.01: fs = 8
        elif abs(fr) <= 0.01: fs = 15
        elif fr >= 0.05: fs = -15
        else: fs = 0
        parts["Funding"] = {"score": fs, "weight": 15, "value": fr, "unit": "%"}
        price = float(market["price_usd"])
        if price > float(market["ema20"]) and price > float(market["ema50"]) and float(market["volume"]) > float(market["volume_avg20"]):
            ps = 30
        elif price > float(market["ema20"]):
            ps = 12
        elif price < float(market["ema20"]) and price < float(market["ema50"]) and float(market["volume"]) > float(market["volume_avg20"]):
            ps = -30
        else:
            ps = 0
        parts["Price & Volume"] = {"score": ps, "weight": 30, "value": price, "unit": "USD"}
    # Never manufacture a complete score from absent data.
    total = sum(x["score"] for x in parts.values()) if not missing and len(parts) == 5 else None
    if total is None:
        label = "VERİ EKSİK — SİNYAL ÜRETİLMEZ"
    elif total >= 60: label = "GÜÇLÜ POZİTİF — LONG FIRSATI ARA"
    elif total >= 30: label = "POZİTİF — TEYİT BEKLE"
    elif total <= -60: label = "GÜÇLÜ NEGATİF — SHORT FIRSATI ARA"
    elif total <= -30: label = "NEGATİF — TEYİT BEKLE"
    else: label = "NÖTR — BEKLE"
    return {"score": total, "label": label, "components": parts, "missing": missing,
            "range": "-100 ile +100", "method": "Prototip ağırlıklandırma; geriye dönük test edilmemiştir."}

def main():
    data = {"updated_at": utcnow(), "sources": {}, "analysis": None,
            "disclaimer": "Eğitim/araştırma amaçlı karar destek aracıdır; otomatik emir göndermez ve yatırım tavsiyesi değildir."}
    try:
        data["sources"]["market"] = fetch_binance()
    except Exception as e:
        data["sources"]["market"] = {"ok": False, "error": str(e), "source": "Binance"}
    try:
        data["sources"]["etf"] = fetch_farside_etf()
    except Exception as e:
        data["sources"]["etf"] = {"ok": False, "error": str(e), "source": "Farside Investors"}
    try:
        data["sources"]["netflow"] = fetch_kote_netflow()
    except Exception as e:
        data["sources"]["netflow"] = {"ok": False, "error": str(e), "source": "Kote Charts"}
    data["analysis"] = score_components(data["sources"].get("etf"), data["sources"].get("netflow"),
                                        data["sources"].get("market"))
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print("Updated", OUT, "score=", data["analysis"]["score"], "missing=", data["analysis"]["missing"])

if __name__ == "__main__":
    main()
