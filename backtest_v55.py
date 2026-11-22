#!/usr/bin/env python3
"""
BTC Institutional Demand PRO
V5.5 — Market Regime Audit (Corrected)

Corrections:
- Accurate trade exit timestamps
- Consistent SHORT return calculations
- Candle data validation
- Trade duration assertions
- Existing strategy rules preserved

This is a research backtest, not a live trading system.
"""

import csv
import json
import math

from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parent

COST = 0.0012
HOUR_MS = 3_600_000

HORIZONS = (1, 4, 12, 24)


def iso(ms):
    """Convert Unix milliseconds to UTC ISO timestamp."""
    return datetime.fromtimestamp(
        ms / 1000,
        timezone.utc
    ).isoformat()


def ema(values, n):
    """Calculate exponential moving average."""
    alpha = 2 / (n + 1)

    value = None
    output = []

    for x in values:
        value = (
            x if value is None
            else alpha * x + (1 - alpha) * value
        )

        output.append(value)

    return output


def stats(returns):
    """Calculate basic strategy statistics."""

    if not returns:
        return {
            "trades": 0,
            "mean_net_pct": None,
            "win_rate_pct": None,
            "profit_factor": None
        }

    positive = sum(max(x, 0) for x in returns)
    negative = -sum(min(x, 0) for x in returns)

    return {
        "trades": len(returns),

        "mean_net_pct": round(
            100 * sum(returns) / len(returns),
            4
        ),

        "win_rate_pct": round(
            100 * sum(x > 0 for x in returns) / len(returns),
            2
        ),

        "profit_factor": (
            round(positive / negative, 3)
            if negative > 0
            else None
        )
    }


def validate_candles(candles):
    """
    Validate confirmed hourly candles.

    Checks:
    - Minimum sample size
    - Duplicate timestamps
    - Missing hourly candles
    - Invalid OHLC prices
    - Invalid volumes
    """

    if len(candles) < 400:
        raise SystemExit(
            "ERROR: Insufficient confirmed candles."
        )

    timestamps = [x["ts"] for x in candles]

    if len(set(timestamps)) != len(timestamps):
        raise SystemExit(
            "ERROR: Duplicate candle timestamps."
        )

    for previous, current in zip(
        timestamps,
        timestamps[1:]
    ):
        if current - previous != HOUR_MS:
            raise SystemExit(
                "ERROR: Missing or irregular hourly candles."
            )

    for index, candle in enumerate(candles):

        for field in (
            "open",
            "high",
            "low",
            "close"
        ):
            value = candle[field]

            if not math.isfinite(value) or value <= 0:
                raise SystemExit(
                    f"ERROR: Invalid {field} "
                    f"at candle {index}."
                )

        volume = candle["volume"]

        if not math.isfinite(volume) or volume < 0:
            raise SystemExit(
                f"ERROR: Invalid volume at candle {index}."
            )

        if candle["high"] < max(
            candle["open"],
            candle["close"],
            candle["low"]
        ):
            raise SystemExit(
                f"ERROR: Invalid high at candle {index}."
            )

        if candle["low"] > min(
            candle["open"],
            candle["close"],
            candle["high"]
        ):
            raise SystemExit(
                f"ERROR: Invalid low at candle {index}."
            )

    print(
        f"DATA VALIDATION PASSED: "
        f"{len(candles)} confirmed hourly candles."
    )


def load_candles():
    """Load and validate market history."""

    path = ROOT / "market_history.json"

    if not path.exists():
        raise SystemExit(
            "ERROR: market_history.json not found."
        )

    source = json.loads(
        path.read_text(encoding="utf-8")
    )

    candles = sorted(
        (
            x for x in source["candles"]
            if str(x.get("confirm")) == "1"
        ),
        key=lambda x: int(x["ts"])
    )

    normalized = []

    for candle in candles:

        normalized.append({
            "ts": int(candle["ts"]),
            "open": float(candle["open"]),
            "high": float(candle["high"]),
            "low": float(candle["low"]),
            "close": float(candle["close"]),
            "volume": float(candle["volume"]),
            "confirm": "1"
        })

    validate_candles(normalized)

    return normalized


def classify_regime(fast, slow):
    """
    Classify regime using information available
    at signal close.
    """

    trend_gap = (
        fast / slow - 1
    ) * 100

    if trend_gap > 0.2:
        return "uptrend"

    if trend_gap < -0.2:
        return "downtrend"

    return "sideways"


def calculate_volatility(closes, i):
    """Trailing 24-hour RMS log-return volatility."""

    returns = [
        math.log(
            closes[k] / closes[k - 1]
        )
        for k in range(i - 23, i + 1)
    ]

    rms = math.sqrt(
        sum(x * x for x in returns) / len(returns)
    )

    return rms * math.sqrt(24) * 100


def calculate_trade_return(
    direction,
    entry_price,
    exit_price
):
    """Calculate simple price return net of round-trip cost."""

    gross = direction * (
        exit_price / entry_price - 1
    )

    return gross - COST


def calculate_short_return(
    entry_price,
    exit_price
):
    """Calculate simple SHORT return net of cost."""

    gross = 1 - (
        exit_price / entry_price
    )

    return gross - COST


def main():

    candles = load_candles()

    timestamps = [
        x["ts"] for x in candles
    ]

    closes = [
        x["close"] for x in candles
    ]

    fast = ema(closes, 24)
    slow = ema(closes, 72)

    split = int(len(candles) * 0.6)

    test_size = len(candles) - split

    bounds = [
        split,
        split + test_size // 3,
        split + 2 * test_size // 3,
        len(candles)
    ]

    all_rows = []
    results = {}

    for horizon in HORIZONS:

        trades = []

        next_free = 72

        for i in range(
            72,
            len(candles) - horizon
        ):

            if i < next_free:
                continue

            average_volume = sum(
                x["volume"]
                for x in candles[i - 24:i]
            ) / 24

            volume_ratio = (
                candles[i]["volume"] / average_volume
                if average_volume > 0
                else 0
            )

            momentum = (
                closes[i] / closes[i - 12] - 1
            )

            direction = 0

            if (
                fast[i] > slow[i]
                and momentum > 0.004
            ):
                direction = 1

            elif (
                fast[i] < slow[i]
                and momentum < -0.004
            ):
                direction = -1

            if direction == 0:
                continue

            if volume_ratio <= 1.2:
                continue

            # Entry at next candle open.
            entry_index = i + 1

            # Exit at close of final holding candle.
            exit_index = (
                entry_index + horizon - 1
            )

            if exit_index >= len(candles):
                continue

            entry_timestamp = timestamps[
                entry_index
            ]

            # Corrected: candle close timestamp.
            exit_timestamp = (
                timestamps[exit_index] + HOUR_MS
            )

            # Verify actual holding period.
            expected_duration = (
                horizon * HOUR_MS
            )

            actual_duration = (
                exit_timestamp - entry_timestamp
            )

            if actual_duration != expected_duration:
                raise RuntimeError(
                    "Trade duration mismatch: "
                    f"horizon={horizon}, "
                    f"actual_ms={actual_duration}"
                )

            if (
                entry_index >= split
                and i >= split
            ):

                entry_price = candles[
                    entry_index
                ]["open"]

                exit_price = candles[
                    exit_index
                ]["close"]

                regime = classify_regime(
                    fast[i],
                    slow[i]
                )

                volatility = calculate_volatility(
                    closes,
                    i
                )

                net_return = calculate_trade_return(
                    direction,
                    entry_price,
                    exit_price
                )

                baseline_short = (
                    calculate_short_return(
                        entry_price,
                        exit_price
                    )
                )

                window = next(
                    (
                        j + 1
                        for j in range(3)
                        if (
                            entry_index >= bounds[j]
                            and exit_index < bounds[j + 1]
                        )
                    ),
                    None
                )

                crossing = (
                    window is None
                )

                trade = {
                    "horizon_h": horizon,

                    "direction": (
                        "LONG"
                        if direction > 0
                        else "SHORT"
                    ),

                    "signal_utc": iso(
                        timestamps[i]
                    ),

                    "entry_utc": iso(
                        entry_timestamp
                    ),

                    "exit_utc": iso(
                        exit_timestamp
                    ),

                    "regime": regime,

                    "trailing_volatility_pct": round(
                        volatility,
                        4
                    ),

                    "window": (
                        window
                        if not crossing
                        else "boundary_crossing"
                    ),

                    "net_return_pct": round(
                        100 * net_return,
                        5
                    ),

                    "same_entry_always_short_net_pct": round(
                        100 * baseline_short,
                        5
                    )
                }

                trades.append(trade)

            # Preserve V5.5 non-overlapping trade logic.
            next_free = exit_index + 1

        # Overall results
        results[str(horizon)] = {
            "overall": stats([
                row["net_return_pct"] / 100
                for row in trades
            ]),

            "windows": {},

            "regimes": {},

            "directions": {},

            "boundary_crossing_trades": sum(
                row["window"] == "boundary_crossing"
                for row in trades
            )
        }

        # Window statistics
        for j in (1, 2, 3):

            subset = [
                row for row in trades
                if row["window"] == j
            ]

            results[str(horizon)]["windows"][
                str(j)
            ] = stats([
                row["net_return_pct"] / 100
                for row in subset
            ])

        # Market regime statistics
        for regime in (
            "uptrend",
            "downtrend",
            "sideways"
        ):

            subset = [
                row for row in trades
                if row["regime"] == regime
            ]

            results[str(horizon)]["regimes"][
                regime
            ] = stats([
                row["net_return_pct"] / 100
                for row in subset
            ])

        # LONG / SHORT statistics
        for direction in (
            "LONG",
            "SHORT"
        ):

            subset = [
                row for row in trades
                if row["direction"] == direction
            ]

            results[str(horizon)]["directions"][
                direction
            ] = stats([
                row["net_return_pct"] / 100
                for row in subset
            ])

        # Same-entry SHORT consistency check
        short_trades = [
            row for row in trades
            if row["direction"] == "SHORT"
        ]

        for row in short_trades:

            difference = abs(
                row["net_return_pct"]
                - row[
                    "same_entry_always_short_net_pct"
                ]
            )

            if difference > 0.00001:
                raise RuntimeError(
                    "SHORT return consistency check failed."
                )

        results[str(horizon)][
            "short_vs_same_entry_always_short"
        ] = {

            "strategy": stats([
                row["net_return_pct"] / 100
                for row in short_trades
            ]),

            "same_entry_short": stats([
                row[
                    "same_entry_always_short_net_pct"
                ] / 100
                for row in short_trades
            ]),

            "note": (
                "Same-entry SHORT returns must match. "
                "This is a consistency check, "
                "not an independent benchmark."
            )
        }

        all_rows.extend(trades)

    report = {
        "version": "5.5-corrected",

        "method": (
            "V5.4 rule reproduction; "
            "ex-ante EMA regime attribution; "
            "corrected exit timestamps and "
            "SHORT return consistency checks; "
            "no optimization"
        ),

        "data_first_utc": iso(
            timestamps[0]
        ),

        "data_last_utc": iso(
            timestamps[-1]
        ),

        "test_start_utc": iso(
            timestamps[split]
        ),

        "confirmed_candles": len(
            candles
        ),

        "round_trip_cost_pct": (
            100 * COST
        ),

        "regime_definition": (
            "At signal close: "
            "EMA24/EMA72 difference > +0.2% uptrend, "
            "< -0.2% downtrend, "
            "otherwise sideways"
        ),

        "limitations": [
            "Regime definitions are exploratory.",
            "The historical test set has been reused.",
            "This is not independent out-of-sample validation.",
            "Boundary-crossing trades are reported separately.",
            "No OI, funding, ETF flow or liquidation modeling.",
            "Fixed trading costs do not model dynamic slippage.",
            "The same-entry SHORT comparison is not independent.",
            "No leverage or liquidation risk modeling."
        ],

        "results": results
    }

    # Save JSON report
    json_path = (
        ROOT / "backtest_v55_results.json"
    )

    json_path.write_text(
        json.dumps(
            report,
            indent=2,
            ensure_ascii=False
        ) + "\n",
        encoding="utf-8"
    )

    # Save CSV report
    csv_path = (
        ROOT / "backtest_v55_trades.csv"
    )

    fields = [
        "horizon_h",
        "direction",
        "signal_utc",
        "entry_utc",
        "exit_utc",
        "regime",
        "trailing_volatility_pct",
        "window",
        "net_return_pct",
        "same_entry_always_short_net_pct"
    ]

    with csv_path.open(
        "w",
        newline="",
        encoding="utf-8"
    ) as file:

        writer = csv.DictWriter(
            file,
            fieldnames=fields
        )

        writer.writeheader()
        writer.writerows(all_rows)

    print()
    print("====================================")
    print("BTC Institutional Demand PRO")
    print("V5.5 CORRECTED — COMPLETED")
    print("====================================")

    print(
        "Confirmed candles:",
        len(candles)
    )

    print(
        "Test start:",
        iso(timestamps[split])
    )

    print(
        "Total test trades:",
        len(all_rows)
    )

    for horizon in HORIZONS:

        result = results[str(horizon)]

        print(
            f"{horizon}h:",
            result["overall"]["trades"],
            "trades;",
            result["boundary_crossing_trades"],
            "boundary crossings"
        )

    print()
    print("AUDIT CHECKS PASSED")
    print("- Candle integrity")
    print("- Trade holding durations")
    print("- SHORT return consistency")
    print()


if __name__ == "__main__":
    main()
