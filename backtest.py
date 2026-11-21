#!/usr/bin/env python3
"""
BTC Institutional Demand PRO — 1H historical signal validation (research only).

The score logic follows the live panel:
- ETF and Exchange Netflow: daily series, lagged to the last closed UTC day.
- Open Interest: OKX BTC-USDT-SWAP hourly history; 6-hour change.
- Funding: last settled OKX funding rate available at signal time.
- Price/volume: OKX BTC-USDT hourly candles; EMA20/EMA50 and 20-hour volume average.

Signals are checked after the hourly candle closes. Entry = next hourly candle open.
Forward directional returns are measured after 24 / 72 / 168 hours (1 / 3 / 7 days).

No fees, spread, slippage, funding cashflows, stop/target or liquidation simulation.
"""
from __future__ import annotations

import os
import json
import time
import bisect
import statistics
from datetime import datetime, timezone, timedelta, date
from pathlib import Path
from typing import Any

import requests
from bs4 import BeautifulSoup

BASE = "https://www.okx.com"
FARSIDE_URL = "https://farside.co.uk/bitcoin-etf-flow-all-data/"
KOTE_URL = "https://kotecharts.com/api/v1/public/charts/exchange-netflow"
KOTE_KEY = os.getenv("KOTE_API_KEY", "").strip()
OUT_JSON = Path("backtest_report.json")
OUT_HTML = Path("backtest_report.html")
BACKTEST_DAYS = int(os.getenv("BACKTEST_DAYS", "55"))
MAX_HOURLY_POINTS = 1440  # OKX documents a latest-1,440-entry cap for this OI history endpoint.
HEADERS = {"User-Agent": "BTC-Institutional-Demand-Backtest/1.0"}

def get_json(url: str, params: dict | None = None, headers: dict | None = None) -> Any:
    response = requests.get(url, params=params, headers={**HEADERS, **(headers or {})}, timeout=30)
    response.raise_for_status()
    payload = response.json()
    if isinstance(payload, dict) and "code" in payload and str(payload.get("code")) != "0":
        raise RuntimeError(f"API error {payload.get('code')}: {payload.get('msg')}")
    if isinstance(payload, dict) and payload.get("success") is False:
        err = payload.get("error", {})
        raise RuntimeError(f"Kote API error: {err.get('code', '')} {err.get('message', err)}")
    return payload

def get_html(url: str) -> str:
    response = requests.get(url, headers=HEADERS, timeout=30)
    response.raise_for_status()
    return response.text

def parse_money(text: str) -> float | None:
    value = text.strip().replace("$", "").replace(",", "").replace("−", "-")
    if value in {"", "-", "—", "–", "N/A", "n/a"}:
        return None
    negative = value.startswith("(") and value.endswith(")")
    if negative:
        value = value[1:-1]
    try:
        number = float(value)
        return -number if negative else number
    except ValueError:
        return None

def to_utc_date(timestamp_ms: str | int | float) -> date:
    return datetime.fromtimestamp(int(timestamp_ms) / 1000, tz=timezone.utc).date()

def parse_farside_history() -> dict[date, float]:
    soup = BeautifulSoup(get_html(FARSIDE_URL), "html.parser")
    flows: dict[date, float] = {}
    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if not rows:
            continue
        header = [c.get_text(" ", strip=True).lower() for c in rows[0].find_all(["th", "td"])]
        if not any("date" in h for h in header) or not any("total" in h for h in header):
            continue
        for row in rows[1:]:
            cells = [c.get_text(" ", strip=True) for c in row.find_all(["td", "th"])]
            if len(cells) < 3:
                continue
            try:
                day = datetime.strptime(cells[0].strip(), "%d %b %Y").date()
            except ValueError:
                continue
            total = parse_money(cells[-1])
            populated = any(parse_money(v) is not None for v in cells[1:-1])
            if total is not None and populated:
                flows[day] = total
    if not flows:
        raise RuntimeError("Farside geçmiş ETF tablosu okunamadı.")
    return flows

def find_series_points(obj: Any) -> list[dict]:
    if isinstance(obj, dict):
        series = obj.get("series")
        if isinstance(series, list):
            return [x for x in series if isinstance(x, dict)]
        for key in ("data", "result", "points"):
            if key in obj:
                found = find_series_points(obj[key])
                if found:
                    return found
    elif isinstance(obj, list):
        if obj and all(isinstance(x, dict) for x in obj) and any("t" in x for x in obj):
            return obj
        for item in obj:
            found = find_series_points(item)
            if found:
                return found
    return []

def get_netflow_value(point: dict) -> float | None:
    for key in ("exNetBtc", "ex_net_btc", "netflow", "netFlow", "value"):
        if point.get(key) is not None:
            try:
                return float(point[key])
            except (TypeError, ValueError):
                pass
    for key, value in point.items():
        name = key.lower()
        if "net" in name and ("btc" in name or "flow" in name) and value is not None:
            try:
                return float(value)
            except (TypeError, ValueError):
                pass
    return None

def parse_date_label(value: Any) -> date:
    text = str(value)
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        return date.fromisoformat(text[:10])

def fetch_kote_history(start: date, end: date) -> dict[date, float]:
    if not KOTE_KEY:
        raise RuntimeError("KOTE_API_KEY GitHub Secret bulunamadı.")
    payload = get_json(
        KOTE_URL,
        params={"from": start.isoformat(), "to": end.isoformat(), "granularity": "day"},
        headers={"X-API-Key": KOTE_KEY},
    )
    points = find_series_points(payload.get("data", payload))
    result: dict[date, float] = {}
    for point in points:
        if point.get("partial") is True or point.get("t") is None:
            continue
        value = get_netflow_value(point)
        if value is not None:
            result[parse_date_label(point["t"])] = value
    if not result:
        raise RuntimeError("Kote API geçmiş Exchange Netflow serisini döndürmedi.")
    return result

def fetch_okx_hourly_candles(target_count: int) -> list[dict]:
    """Fetch completed 1H UTC spot candles, paging backwards."""
    url = f"{BASE}/api/v5/market/history-candles"
    collected: dict[int, dict] = {}
    after = None
    for _ in range(20):
        params = {"instId": "BTC-USDT", "bar": "1H", "limit": "100"}
        if after:
            params["after"] = str(after)
        payload = get_json(url, params=params)
        rows = payload.get("data", [])
        if not rows:
            break
        for row in rows:
            if len(row) < 9 or str(row[8]) != "1":
                continue
            ts = int(row[0])
            collected[ts] = {
                "ts": ts, "date": to_utc_date(ts),
                "open": float(row[1]), "high": float(row[2]),
                "low": float(row[3]), "close": float(row[4]),
                "volume": float(row[5]),
            }
        oldest = min(int(r[0]) for r in rows)
        if len(collected) >= target_count or after == oldest:
            break
        after = oldest
        time.sleep(0.15)
    return sorted(collected.values(), key=lambda x: x["ts"])[-target_count:]

def fetch_okx_hourly_oi(target_count: int) -> list[dict]:
    """Fetch the latest up to 1,440 hourly OI observations."""
    url = f"{BASE}/api/v5/rubik/stat/contracts/open-interest-history"
    collected: dict[int, dict] = {}
    end = None
    for _ in range(20):
        params = {"instId": "BTC-USDT-SWAP", "period": "1H", "limit": "100"}
        if end:
            params["end"] = str(end)
        payload = get_json(url, params=params)
        rows = payload.get("data", [])
        if not rows:
            break
        for row in rows:
            if len(row) >= 3:
                ts = int(row[0])
                collected[ts] = {"ts": ts, "oi_btc": float(row[2])}
        oldest = min(int(r[0]) for r in rows)
        if len(collected) >= target_count or len(collected) >= MAX_HOURLY_POINTS or end == oldest:
            break
        end = oldest
        time.sleep(0.15)
    return sorted(collected.values(), key=lambda x: x["ts"])

def fetch_okx_funding_history() -> list[dict]:
    """OKX documents up to 3 months; max 400 rows per request."""
    payload = get_json(
        f"{BASE}/api/v5/public/funding-rate-history",
        params={"instId": "BTC-USDT-SWAP", "limit": "400"},
    )
    events = []
    for row in payload.get("data", []):
        ts = row.get("fundingTime")
        # Use realizedRate only; a forecasted fundingRate would introduce look-ahead.
        rate = row.get("realizedRate")
        if ts is None or rate is None:
            continue
        events.append({"ts": int(ts), "rate_pct": float(rate) * 100.0})
    return sorted(events, key=lambda x: x["ts"])

def latest_daily_value(series: dict[date, float], day: date, max_age_days: int = 5) -> tuple[date, float] | None:
    eligible = [d for d in series if d <= day]
    if not eligible:
        return None
    latest = max(eligible)
    if (day - latest).days > max_age_days:
        return None
    return latest, series[latest]

def ema(values: list[float], period: int) -> float:
    alpha = 2.0 / (period + 1)
    current = values[0]
    for value in values[1:]:
        current = alpha * value + (1 - alpha) * current
    return current

def score_at(
    i: int,
    candles: list[dict],
    candle_ts: list[int],
    oi_rows: list[dict],
    oi_ts: list[int],
    funding_rows: list[dict],
    funding_ts: list[int],
    etf_flows: dict[date, float],
    netflows: dict[date, float],
) -> dict | None:
    candle = candles[i]
    close_ts = candle["ts"] + 3_600_000
    signal_day = to_utc_date(close_ts)
    flow_cutoff = signal_day - timedelta(days=1)

    etf_obs = latest_daily_value(etf_flows, flow_cutoff, max_age_days=5)
    if etf_obs is None:
        return None
    etf_value = etf_obs[1]
    etf_score = 20 if etf_value >= 100 else 10 if etf_value > 0 else -20 if etf_value <= -100 else -10 if etf_value < 0 else 0

    nf_dates = sorted(d for d in netflows if d <= flow_cutoff)
    if len(nf_dates) < 3 or (flow_cutoff - nf_dates[-1]).days > 4:
        return None
    nf_mean = sum(netflows[d] for d in nf_dates[-3:]) / 3.0
    netflow_score = 15 if nf_mean <= -500 else 7 if nf_mean < 0 else -15 if nf_mean >= 500 else -7 if nf_mean > 0 else 0

    # Map OI to latest observation available no later than this candle close and 6h earlier.
    oi_idx = bisect.bisect_right(oi_ts, close_ts) - 1
    oi_old_idx = bisect.bisect_right(oi_ts, close_ts - 6 * 3_600_000) - 1
    if oi_idx < 0 or oi_old_idx < 0:
        return None
    if close_ts - oi_ts[oi_idx] > 2 * 3_600_000:
        return None
    oi_change = (oi_rows[oi_idx]["oi_btc"] / oi_rows[oi_old_idx]["oi_btc"] - 1) * 100 if oi_rows[oi_old_idx]["oi_btc"] else 0.0

    if i < 6:
        return None
    price_change_6h = (candles[i]["close"] / candles[i - 6]["close"] - 1) * 100 if candles[i - 6]["close"] else 0.0
    oi_score = 20 if price_change_6h > 0 and oi_change > 0.5 else 8 if price_change_6h > 0 and abs(oi_change) <= 0.5 else -15 if price_change_6h < 0 and oi_change > 0.5 else 0

    # Last settled funding event timestamped at or before signal time.
    funding_idx = bisect.bisect_right(funding_ts, close_ts) - 1
    if funding_idx < 0 or close_ts - funding_ts[funding_idx] > 24 * 3_600_000:
        return None
    funding_value = funding_rows[funding_idx]["rate_pct"]
    if funding_value <= -0.05:
        funding_score = 5
    elif funding_value < -0.01:
        funding_score = 8
    elif abs(funding_value) <= 0.01:
        funding_score = 15
    elif funding_value >= 0.05:
        funding_score = -15
    else:
        funding_score = 0

    closes = [x["close"] for x in candles[max(0, i - 59):i + 1]]
    volumes = [x["volume"] for x in candles[max(0, i - 19):i + 1]]
    if len(closes) < 50 or len(volumes) < 20:
        return None
    ema20 = ema(closes[-40:], 20)
    ema50 = ema(closes[-60:], 50)
    volume_avg20 = sum(volumes) / 20.0
    if candle["close"] > ema20 and candle["close"] > ema50 and candle["volume"] > volume_avg20:
        price_score = 30
    elif candle["close"] > ema20:
        price_score = 12
    elif candle["close"] < ema20 and candle["close"] < ema50 and candle["volume"] > volume_avg20:
        price_score = -30
    else:
        price_score = 0

    total = etf_score + netflow_score + oi_score + funding_score + price_score
    return {
        "timestamp": close_ts, "signal_day": signal_day.isoformat(),
        "score": total, "parts": {
            "ETF": etf_score, "Exchange Netflow": netflow_score,
            "Open Interest": oi_score, "Funding": funding_score,
            "Price & Volume": price_score,
        },
        "inputs": {
            "ETF date": etf_obs[0].isoformat(), "ETF flow M USD": etf_value,
            "Netflow 3D avg BTC": nf_mean,
            "OI change pct 6H": oi_change,
            "Price change pct 6H": price_change_6h,
            "Funding pct": funding_value,
            "Close": candle["close"], "EMA20": ema20, "EMA50": ema50,
        }
    }

def forward_return(entry: float, exit_price: float, direction: str) -> float:
    if entry <= 0 or exit_price <= 0:
        return 0.0
    if direction == "LONG":
        return (exit_price / entry - 1.0) * 100.0
    return ((entry - exit_price) / entry) * 100.0

def summarize(signals: list[dict], horizon_hours: int, direction: str | None = None) -> dict:
    field = f"r{horizon_hours}"
    selected = [s for s in signals if (direction is None or s["direction"] == direction) and s.get(field) is not None]
    values = [float(s[field]) for s in selected]
    if not values:
        return {"trades": 0, "win_rate_pct": None, "avg_return_pct": None, "median_return_pct": None}
    return {
        "trades": len(values),
        "win_rate_pct": sum(1 for x in values if x > 0) / len(values) * 100.0,
        "avg_return_pct": statistics.mean(values),
        "median_return_pct": statistics.median(values),
    }

def sequential_non_overlapping(signals: list[dict], horizon_hours: int) -> list[dict]:
    """
    Keep only trades that do not overlap for the selected holding horizon.
    One hypothetical account, one open position at a time. Any new signal
    while a position is open is skipped, including a signal in the opposite direction.
    """
    accepted = []
    next_available_ts = 0
    for signal in sorted(signals, key=lambda x: x["signal_time"]):
        ts = int(datetime.fromisoformat(signal["signal_time"]).timestamp() * 1000)
        if ts < next_available_ts:
            continue
        accepted.append(signal)
        next_available_ts = ts + horizon_hours * 3_600_000
    return accepted

def html_report(report: dict) -> str:
    metrics = report["metrics"]
    def pct(value):
        return "—" if value is None else f"{value:+.2f}%"
    rows = []
    for horizon, label_h in ((24, "1 gün"), (72, "3 gün"), (168, "7 gün")):
        for label, key in (
            ("Tüm sinyaller (örtüşebilir)", "all"),
            ("LONG (örtüşebilir)", "long"),
            ("SHORT (örtüşebilir)", "short"),
            ("Tek pozisyon: tümü", "sequential_all"),
            ("Tek pozisyon: LONG", "sequential_long"),
            ("Tek pozisyon: SHORT", "sequential_short"),
        ):
            m = metrics[str(horizon)][key]
            win = "—" if m["win_rate_pct"] is None else f"{m['win_rate_pct']:.1f}%"
            rows.append(f"<tr><td>{label_h}</td><td>{label}</td><td>{m['trades']}</td><td>{win}</td><td>{pct(m['avg_return_pct'])}</td><td>{pct(m['median_return_pct'])}</td></tr>")
    signal_rows = []
    for s in report["signals"]:
        signal_rows.append(
            f"<tr><td>{s['signal_time']}</td><td>{s['direction']}</td><td>{s['score']:+}</td>"
            f"<td>{s['entry_time']}</td><td>${s['entry_price']:,.0f}</td>"
            f"<td>{pct(s['r24'])}</td><td>{pct(s['r72'])}</td><td>{pct(s['r168'])}</td></tr>"
        )
    caveats = "".join(f"<li>{x}</li>" for x in report["caveats"])
    return f"""<!doctype html><html lang="tr"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>BTC Demand — Tarihsel Sinyal Testi</title><style>
body{{margin:0;background:#0b1220;color:#e5e7eb;font:15px/1.55 system-ui,Arial}}main{{max-width:1150px;margin:auto;padding:22px}}
.card{{background:#151f31;border:1px solid #29384f;border-radius:12px;padding:18px;margin:14px 0}}
h1{{font-size:26px}}.muted{{color:#9ca3af}}table{{border-collapse:collapse;width:100%;font-size:14px}}td,th{{padding:9px;border-bottom:1px solid #2d3a50;text-align:left}}th{{color:#9ca3af}}
a{{color:#7dd3fc}}.num{{font-size:28px;font-weight:800}}li{{margin:7px 0}}.scroll{{overflow-x:auto}}
</style><main><p><a href="./">← Ana panele dön</a></p>
<h1>BTC Institutional Demand PRO — 1 Saatlik Tarihsel Sinyal Testi</h1>
<p class="muted">Oluşturulma: {report['generated_at']} · Test dönemi: {report['period']['from']} – {report['period']['to']}</p>
<section class="card"><div class="muted">Geçerli saatlik skor gözlemi</div><div class="num">{report['valid_score_hours']}</div>
<div class="muted">LONG: {report['signal_counts']['long']} · SHORT: {report['signal_counts']['short']} · Toplam: {report['signal_counts']['total']}</div></section>
<section class="card"><h2>Performans özeti</h2><div class="scroll"><table><thead><tr><th>Ufuk</th><th>Grup</th><th>Sinyal</th><th>Kazanma oranı</th><th>Ortalama yönsel getiri</th><th>Medyan yönsel getiri</th></tr></thead>
<tbody>{''.join(rows)}</tbody></table></div></section>
<section class="card"><h2>Sinyal listesi</h2><div class="scroll"><table><thead><tr><th>Sinyal zamanı (UTC)</th><th>Yön</th><th>Skor</th><th>Giriş zamanı (UTC)</th><th>Giriş fiyatı</th><th>1 gün</th><th>3 gün</th><th>7 gün</th></tr></thead>
<tbody>{''.join(signal_rows) if signal_rows else '<tr><td colspan="8">Bu test döneminde +60/-60 eşiğinde sinyal oluşmadı.</td></tr>'}</tbody></table></div></section>
<section class="card"><h2>Veri kapsaması</h2><p>{json.dumps(report['data_coverage'], ensure_ascii=False)}</p></section>
<section class="card"><h2>Yöntem ve sınırlamalar</h2><ul>{caveats}</ul></section>
<p class="muted">Araştırma amaçlıdır; yatırım tavsiyesi veya gerçek işlem performansı değildir.</p></main></html>"""

def main():
    if BACKTEST_DAYS < 20 or BACKTEST_DAYS > 60:
        raise RuntimeError("Bu ilk test için BACKTEST_DAYS 20–60 arasında olmalıdır. Varsayılan 55 günü kullanın.")
    now = datetime.now(timezone.utc)
    end_date = now.date() - timedelta(days=1)
    start_date = end_date - timedelta(days=BACKTEST_DAYS + 5)
    print(f"Fetching {BACKTEST_DAYS} days of 1H history, plus warm-up. Flow range: {start_date} to {end_date}")

    etf = parse_farside_history()
    netflow = fetch_kote_history(start_date, end_date)
    target_points = min(MAX_HOURLY_POINTS, BACKTEST_DAYS * 24)
    candles = fetch_okx_hourly_candles(target_points)
    oi_rows = fetch_okx_hourly_oi(target_points)
    funding_rows = fetch_okx_funding_history()

    if len(candles) < min(target_points // 2, 500):
        raise RuntimeError(f"OKX hourly candles insufficient: {len(candles)} records.")
    if len(oi_rows) < min(target_points // 2, 500):
        raise RuntimeError(f"OKX hourly OI history insufficient: {len(oi_rows)} records.")
    if len(funding_rows) < 20:
        raise RuntimeError("OKX settled funding history is insufficient.")

    candle_ts = [x["ts"] for x in candles]
    oi_ts = [x["ts"] for x in oi_rows]
    funding_ts = [x["ts"] for x in funding_rows]

    scored_rows = []
    for i in range(len(candles)):
        if i < 60:
            continue
        score = score_at(i, candles, candle_ts, oi_rows, oi_ts, funding_rows, funding_ts, etf, netflow)
        if score:
            scored_rows.append((i, score))

    signals = []
    previous = None
    previous_ts = None
    for i, scored in scored_rows:
        score = scored["score"]
        direction = None
        # A missing/invalid hourly score breaks continuity. The first score after
        # a gap only resets the baseline; it is NOT treated as a fresh crossing.
        contiguous = previous_ts is not None and scored["timestamp"] - previous_ts == 3_600_000
        if not contiguous or previous is None:
            previous = score
            previous_ts = scored["timestamp"]
            continue
        # Emit only on a real crossing from the immediately preceding valid hour.
        if score >= 60 and previous < 60:
            direction = "LONG"
        elif score <= -60 and previous > -60:
            direction = "SHORT"
        previous = score
        previous_ts = scored["timestamp"]
        if not direction:
            continue

        if i + 168 >= len(candles):
            continue
        entry_candle = candles[i + 1]
        row = {
            "signal_time": datetime.fromtimestamp(scored["timestamp"] / 1000, tz=timezone.utc).isoformat(),
            "direction": direction, "score": score,
            "entry_time": datetime.fromtimestamp((entry_candle["ts"]) / 1000, tz=timezone.utc).isoformat(),
            "entry_price": entry_candle["open"], "inputs": scored["inputs"],
        }
        for horizon in (24, 72, 168):
            exit_candle = candles[i + horizon]
            row[f"r{horizon}"] = round(forward_return(entry_candle["open"], exit_candle["close"], direction), 5)
        signals.append(row)

    metrics = {}
    sequential_counts = {}
    for horizon in (24, 72, 168):
        seq = sequential_non_overlapping(signals, horizon)
        sequential_counts[str(horizon)] = len(seq)
        metrics[str(horizon)] = {
            "all": summarize(signals, horizon),
            "long": summarize(signals, horizon, "LONG"),
            "short": summarize(signals, horizon, "SHORT"),
            "sequential_all": summarize(seq, horizon),
            "sequential_long": summarize(seq, horizon, "LONG"),
            "sequential_short": summarize(seq, horizon, "SHORT"),
        }
    tested = [x[1]["signal_day"] for x in scored_rows]
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "period": {"from": min(tested) if tested else None, "to": max(tested) if tested else None,
                   "requested_days": BACKTEST_DAYS},
        "valid_score_hours": len(scored_rows),
        "signal_counts": {
            "long": sum(1 for x in signals if x["direction"] == "LONG"),
            "short": sum(1 for x in signals if x["direction"] == "SHORT"),
            "total": len(signals),
        },
        "sequential_non_overlapping_trade_counts_by_horizon": sequential_counts,
        "metrics": metrics,
        "signals": signals,
        "data_coverage": {
            "okx_hourly_candles": len(candles), "okx_hourly_oi_points": len(oi_rows),
            "okx_funding_events": len(funding_rows), "farside_etf_days": len(etf),
            "kote_netflow_days": len(netflow),
        },
        "caveats": [
            "Bu, mevcut canlı skor mantığına yakın 1 saatlik tarihsel sinyal yönü testidir; otomatik emir veya pozisyon simülatörü değildir.",
            "Sinyal saatlik mum kapandıktan sonra hesaplanır; giriş bir sonraki saatlik mumun açılış fiyatıdır.",
            "ETF ve Exchange Netflow için sinyal anından önceki UTC gününe ait veriler kullanılır; look-ahead riskini azaltır.",
            "Open Interest ve Funding sadece OKX BTC-USDT-SWAP piyasasını temsil eder; tüm borsaların toplamı değildir.",
            "OKX sözleşme OI geçmişi en fazla son 1.440 veri noktasıyla sınırlıdır; 1 saatlik test yaklaşık 60 günle sınırlanır.",
            "OKX funding history endpoint'i dokümantasyona göre en fazla üç aylık geçmiş döndürür.",
            "Getiriler brüttür; komisyon, spread, slippage, fonlama nakit akışları, stop-loss, take-profit ve likidasyon etkileri dahil değildir.",
            "Ham 1/3/7 günlük ölçümler birbirleriyle örtüşebilir ve bağımsız değildir. Raporda ayrıca her ufuk için tek pozisyon/örtüşmeyen işlem özeti bulunur; bu bölüm, aynı anda yalnızca bir pozisyon açık varsayar.",
            "Veri boşluğundan sonra gelen ilk geçerli skor yeni eşik geçişi sayılmaz; gerçek geçiş için ardışık iki saatlik geçerli skor gerekir.",
            "Eşikler ve ağırlıklar deneysel başlangıç kurallarıdır; parametre optimizasyonu yapılmamıştır.",
            "Kote Charts yalnızca kişisel/ticari olmayan kullanım ve kaynak gösterme koşuluyla kullanılabilir: https://kotecharts.com/c/exchange-netflow",
            "ETF akışları Farside Investors günlük tablosundan alınır: https://farside.co.uk/bitcoin-etf-flow-all-data/",
        ],
        "method": "Live-like 1H score; +60 / -60 first threshold crossing; next-hour-open entry; 24/72/168-hour directional returns."
    }
    OUT_JSON.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    OUT_HTML.write_text(html_report(report), encoding="utf-8")
    print(json.dumps({
        "period": report["period"], "valid_score_hours": report["valid_score_hours"],
        "signal_counts": report["signal_counts"],
        "sequential_non_overlapping_trade_counts_by_horizon": report["sequential_non_overlapping_trade_counts_by_horizon"],
        "metrics": report["metrics"],
        "data_coverage": report["data_coverage"],
    }, ensure_ascii=False, indent=2))

if __name__ == "__main__":
    main()
