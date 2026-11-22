#!/usr/bin/env python3
"""
BTC Institutional Demand PRO — institutional-flow snapshot updater.

The browser connects directly to OKX public WebSockets for live market inputs.
This scheduled job only updates slower flow inputs:
- Farside: latest completed daily US spot Bitcoin ETF flow.
- Kote Charts: latest 24 hours of exchange-netflow 5-minute buckets.

KOTE_API_KEY remains a GitHub Actions secret and is never sent to the browser.
"""
from __future__ import annotations
import os
import json
import math
from datetime import datetime, timezone, timedelta
from typing import Any

import requests
from bs4 import BeautifulSoup

OUT = "data.json"
FARSIDE_URL = "https://farside.co.uk/bitcoin-etf-flow-all-data/"
KOTE_URL = "https://kotecharts.com/api/v1/public/charts/exchange-netflow"
KOTE_KEY = os.getenv("KOTE_API_KEY", "").strip()
HEADERS = {"User-Agent": "BTC-Institutional-Demand-Personal-Dashboard/2.0"}

def utcnow() -> datetime:
    return datetime.now(timezone.utc)

def iso_z(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")

def get_json(url: str, params: dict | None = None, headers: dict | None = None) -> Any:
    response = requests.get(url, params=params, headers={**HEADERS, **(headers or {})}, timeout=30)
    response.raise_for_status()
    payload = response.json()
    if isinstance(payload, dict) and payload.get("success") is False:
        error = payload.get("error", {})
        raise RuntimeError(f"Kote API: {error.get('code', '')} {error.get('message', error)}")
    if isinstance(payload, dict) and "code" in payload and str(payload.get("code")) != "0":
        raise RuntimeError(f"API error {payload.get('code')}: {payload.get('msg')}")
    return payload

def get_html(url: str) -> str:
    response = requests.get(url, headers=HEADERS, timeout=30)
    response.raise_for_status()
    return response.text

def parse_money(cell: str) -> float | None:
    value = cell.strip().replace("$", "").replace(",", "").replace("−", "-")
    if value in {"", "-", "—", "–", "N/A", "n/a"}:
        return None
    negative = value.startswith("(") and value.endswith(")")
    if negative:
        value = value[1:-1]
    try:
        number = float(value)
        if not math.isfinite(number):
            return None
        return -number if negative else number
    except ValueError:
        return None

def fetch_farside_etf() -> dict:
    """Get latest complete daily total from Farside's public table."""
    soup = BeautifulSoup(get_html(FARSIDE_URL), "html.parser")
    latest = None
    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if not rows:
            continue
        header = [cell.get_text(" ", strip=True).lower() for cell in rows[0].find_all(["th", "td"])]
        if not any("date" in item for item in header) or not any("total" in item for item in header):
            continue
        for row in rows[1:]:
            cells = [cell.get_text(" ", strip=True) for cell in row.find_all(["td", "th"])]
            if len(cells) < 3:
                continue
            try:
                day = datetime.strptime(cells[0].strip(), "%d %b %Y").date()
            except ValueError:
                continue
            total = parse_money(cells[-1])
            has_fund_values = any(parse_money(value) is not None for value in cells[1:-1])
            if total is None or not has_fund_values:
                continue
            candidate = {"date": day.isoformat(), "flow_usd_m": total}
            if latest is None or candidate["date"] > latest["date"]:
                latest = candidate
    if latest is None:
        raise RuntimeError("Farside tablosundan tamamlanmış günlük ETF akışı okunamadı.")
    return {
        "ok": True, **latest,
        "source": "Farside Investors",
        "source_url": FARSIDE_URL,
        "frequency": "daily; new value when provider publishes a completed day",
        "fetched_at": iso_z(utcnow()),
    }

def find_points(obj: Any) -> list[dict]:
    """Find Kote's time series regardless of envelope nesting."""
    if isinstance(obj, dict):
        series = obj.get("series")
        if isinstance(series, list):
            return [item for item in series if isinstance(item, dict)]
        for key in ("data", "result", "points", "values"):
            if key in obj:
                found = find_points(obj[key])
                if found:
                    return found
    elif isinstance(obj, list):
        if obj and all(isinstance(item, dict) for item in obj) and any("t" in item for item in obj):
            return obj
        for item in obj:
            found = find_points(item)
            if found:
                return found
    return []

def parse_point_datetime(value: Any) -> datetime:
    if isinstance(value, (int, float)):
        raw = float(value)
        seconds = raw / 1000 if raw > 10_000_000_000 else raw
        return datetime.fromtimestamp(seconds, tz=timezone.utc)
    text = str(value).strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except ValueError:
        parsed_date = datetime.strptime(text[:10], "%Y-%m-%d")
        return parsed_date.replace(tzinfo=timezone.utc)

def netflow_value(point: dict) -> float | None:
    for key in ("exNetBtc", "ex_net_btc", "netflow", "netFlow", "value"):
        if point.get(key) is not None:
            try:
                return float(point[key])
            except (TypeError, ValueError):
                pass
    for key, value in point.items():
        lowered = key.lower()
        if "net" in lowered and ("btc" in lowered or "flow" in lowered) and value is not None:
            try:
                return float(value)
            except (TypeError, ValueError):
                pass
    return None

def fetch_kote_netflow() -> dict:
    """
    Request 5-minute exchange-netflow points for a trailing 24-hour window.
    Kote documents 5m buckets for the last 30 days. If the selected chart
    does not provide the requested resolution, this source is marked failed;
    no daily figure is mislabeled as intraday.
    """
    if not KOTE_KEY:
        raise RuntimeError("KOTE_API_KEY GitHub Secret bulunamadı.")
    now = utcnow()
    end = now.replace(minute=now.minute // 5 * 5, second=0, microsecond=0)
    start = end - timedelta(hours=24)
    payload = get_json(
        KOTE_URL,
        params={"from": iso_z(start), "to": iso_z(end), "granularity": "5m"},
        headers={"X-API-Key": KOTE_KEY},
    )
    points = find_points(payload.get("data", payload))
    buckets = {}
    conflicting = set()
    for point in points:
        if point.get("partial") is True or point.get("t") is None:
            continue
        value = netflow_value(point)
        if value is None or not math.isfinite(value):
            continue
        timestamp = parse_point_datetime(point["t"])
        if not start <= timestamp < end or (timestamp - start).total_seconds() % 300:
            continue
        if timestamp in buckets and buckets[timestamp] != value:
            conflicting.add(timestamp)
        buckets[timestamp] = value
    for timestamp in conflicting:
        del buckets[timestamp]

    if not buckets:
        raise RuntimeError("Kote Charts 5 dakikalık Exchange Netflow verisi döndürmedi.")
    latest = max(buckets)
    trailing_sum = sum(buckets.values())
    expected = 288
    coverage = min(100.0, len(buckets) / expected * 100.0)
    missing_buckets = [
        start + timedelta(minutes=5 * i)
        for i in range(expected)
        if start + timedelta(minutes=5 * i) not in buckets
    ]
    print(json.dumps({
        "netflow_expected": expected,
        "netflow_received": len(buckets),
        "netflow_missing_count": len(missing_buckets),
        "netflow_missing_first_10": [iso_z(ts) for ts in missing_buckets[:10]],
        "netflow_latest_bucket": iso_z(latest),
        "netflow_requested_end": iso_z(end),
    }, ensure_ascii=False))
    return {
        "ok": True,
        "trailing_24h_btc": trailing_sum,
        "latest_bucket_ts": iso_z(latest),
        "observations_24h": len(buckets),
        "expected_observations_24h": expected,
        "coverage_pct": round(coverage, 1),
        "granularity": "5m",
        "source": "Kote Charts",
        "source_url": "https://kotecharts.com/c/exchange-netflow",
        "frequency": "5-minute buckets; data updated by Kote when new processed blocks change the chart",
        "fetched_at": iso_z(now),
        "note": "Negatif değer net borsa çıkışını, pozitif değer net borsa girişini gösterir. Kote entity labels heuristics-based estimates.",
    }

def main() -> None:
    now = iso_z(utcnow())
    data = {
        "updated_at": now,
        "schema_version": 2,
        "sources": {},
        "notes": [
            "OKX live market data is streamed directly to the browser over public WebSocket; it is not refreshed by this file.",
            "ETF flow is daily; no intraday ETF flow is inferred.",
            "Kote Exchange Netflow uses 5-minute buckets over a trailing 24-hour window when the chart supports that resolution.",
            "Kote API key remains on GitHub Actions and is never exposed in data.json.",
        ],
    }
    try:
        data["sources"]["etf"] = fetch_farside_etf()
    except Exception as exc:
        data["sources"]["etf"] = {"ok": False, "source": "Farside Investors", "error": str(exc), "fetched_at": now}
    try:
        data["sources"]["netflow"] = fetch_kote_netflow()
    except Exception as exc:
        data["sources"]["netflow"] = {"ok": False, "source": "Kote Charts", "error": str(exc), "fetched_at": now}

    data["flow_analysis"] = {
        "score": None,
        "label": "VERİ EKSİK — KURUMSAL AKIŞ SKORU HESAPLANMADI",
        "components": {},
        "missing": [name for name in ("etf", "netflow") if not data["sources"].get(name, {}).get("ok")],
        "formula": "ETF ±50 + Exchange Netflow ±50; experimental weighting.",
    }
    etf = data["sources"].get("etf", {})
    netflow = data["sources"].get("netflow", {})
    if etf.get("ok") and netflow.get("ok"):
        checked_at = utcnow()
        etf_age = checked_at - datetime.fromisoformat(etf["date"]).replace(tzinfo=timezone.utc)
        netflow_age = checked_at - datetime.fromisoformat(netflow["latest_bucket_ts"])
        missing = data["flow_analysis"]["missing"]
        if not timedelta(0) <= etf_age <= timedelta(days=4):
            missing.append("ETF günlük verisi eski veya tarihi geçersiz")
        if not timedelta(0) <= netflow_age <= timedelta(minutes=180):
            missing.append("Netflow verisi eski veya zaman damgası geçersiz")
        if netflow["coverage_pct"] != 100 or netflow["granularity"] != "5m":
            missing.append("Netflow için 24 saatlik 5 dakika verisi eksik")

    if not data["flow_analysis"]["missing"]:
        etf_value = float(etf["flow_usd_m"])
        etf_score = 50 if etf_value >= 100 else 25 if etf_value > 0 else -50 if etf_value <= -100 else -25 if etf_value < 0 else 0
        nf_value = float(netflow["trailing_24h_btc"])
        netflow_score = 50 if nf_value <= -500 else 25 if nf_value < 0 else -50 if nf_value >= 500 else -25 if nf_value > 0 else 0
        total = etf_score + netflow_score
        if total >= 60: label = "GÜÇLÜ POZİTİF"
        elif total >= 30: label = "POZİTİF — TEYİT BEKLE"
        elif total <= -60: label = "GÜÇLÜ NEGATİF"
        elif total <= -30: label = "NEGATİF — TEYİT BEKLE"
        else: label = "NÖTR"
        data["flow_analysis"] = {
            "score": total,
            "label": label,
            "components": {
                "ETF": {"score": etf_score, "value": etf_value, "unit": "milyon USD", "date": etf["date"]},
                "Exchange Netflow": {"score": netflow_score, "value": nf_value, "unit": "BTC / son 24 saat", "coverage_pct": netflow["coverage_pct"]},
            },
            "missing": [],
            "formula": "ETF ±50 + Exchange Netflow ±50; experimental weighting.",
        }
    with open(OUT, "w", encoding="utf-8") as output:
        json.dump(data, output, ensure_ascii=False, indent=2)
    print(json.dumps({
        "updated_at": data["updated_at"],
        "etf_ok": etf.get("ok"),
        "netflow_ok": netflow.get("ok"),
        "flow_score": data["flow_analysis"]["score"],
        "netflow_coverage": netflow.get("coverage_pct"),
    }, ensure_ascii=False))

if __name__ == "__main__":
    main()
