"""
Analyze saved bot signals and summarize which trade segments help or hurt PnL.

Usage:
    python analyze_signals.py
    python analyze_signals.py --file signals.json
    python analyze_signals.py --top 8
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from datetime import datetime, timezone
from itertools import product
from pathlib import Path


DEFAULT_FILE = Path(__file__).with_name("signals.json")


def load_signals(path: Path) -> list[dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError("signals file must contain a JSON list")
    return data


def avg(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def fmt_money(value: float) -> str:
    return f"${value:+.2f}"


def fmt_pct(value: float) -> str:
    return f"{value:.1f}%"


def parse_ts(value: str) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def bucket_confidence(confidence: float) -> str:
    pct = confidence * 100
    if pct < 5:
        return "<5%"
    if pct < 10:
        return "5-9%"
    if pct < 20:
        return "10-19%"
    if pct < 30:
        return "20-29%"
    if pct < 40:
        return "30-39%"
    if pct < 50:
        return "40-49%"
    if pct < 60:
        return "50-59%"
    if pct < 70:
        return "60-69%"
    return ">=70%"


def bucket_delta(delta_pct: float) -> str:
    if delta_pct < 0.005:
        return "<0.005%"
    if delta_pct < 0.010:
        return "0.005-0.009%"
    if delta_pct < 0.020:
        return "0.010-0.019%"
    if delta_pct < 0.030:
        return "0.020-0.029%"
    if delta_pct < 0.050:
        return "0.030-0.049%"
    if delta_pct < 0.10:
        return "0.050-0.099%"
    if delta_pct < 0.15:
        return "0.10-0.15%"
    if delta_pct < 0.20:
        return "0.15-0.20%"
    if delta_pct < 0.30:
        return "0.20-0.30%"
    if delta_pct < 0.50:
        return "0.30-0.50%"
    return ">=0.50%"


def bucket_time_left(time_left: float) -> str:
    if time_left < 10:
        return "<10s"
    if time_left < 15:
        return "10-14s"
    if time_left < 20:
        return "15-19s"
    if time_left < 30:
        return "20-29s"
    if time_left < 40:
        return "30-39s"
    return ">=40s"


def bucket_pm(pm_price: float) -> str:
    if pm_price < 0.55:
        return "<0.55"
    if pm_price < 0.58:
        return "0.55-0.579"
    if pm_price < 0.60:
        return "0.58-0.599"
    if pm_price < 0.62:
        return "0.60-0.619"
    if pm_price < 0.64:
        return "0.62-0.639"
    if pm_price < 0.67:
        return "0.64-0.669"
    if pm_price < 0.70:
        return "0.67-0.699"
    if pm_price < 0.80:
        return "0.70-0.799"
    if pm_price < 0.90:
        return "0.80-0.899"
    if pm_price < 0.94:
        return "0.90-0.939"
    if pm_price < 0.95:
        return "0.94-0.949"
    if pm_price < 0.96:
        return "0.95-0.959"
    if pm_price < 0.97:
        return "0.96-0.969"
    if pm_price < 0.98:
        return "0.97-0.979"
    return ">=0.98"


def bucket_expected_roi(signal: dict) -> str:
    amount = float(signal.get("amount", 0) or 0)
    pnl_expected = float(signal.get("pnl_expected", 0) or 0)
    if amount <= 0:
        return "unknown"

    roi = pnl_expected / amount * 100
    if roi < 2:
        return "<2%"
    if roi < 4:
        return "2-3.9%"
    if roi < 6:
        return "4-5.9%"
    if roi < 8:
        return "6-7.9%"
    return ">=8%"


def edge_proxy(signal: dict) -> float:
    confidence = float(signal.get("confidence", 0) or 0)
    pm = float(signal.get("pm", 0) or 0)
    return (confidence - pm) * 100


def model_edge(signal: dict) -> float | None:
    if signal.get("edge") is not None:
        return float(signal.get("edge", 0) or 0) * 100

    model_prob = signal.get("model_prob")
    market_prob = signal.get("market_prob")
    if model_prob is not None and market_prob is not None:
        return (float(model_prob or 0) - float(market_prob or 0)) * 100

    return None


def effective_edge(signal: dict) -> float:
    edge = model_edge(signal)
    if edge is not None:
        return edge
    return edge_proxy(signal)


def bucket_edge_proxy(signal: dict) -> str:
    edge = effective_edge(signal)
    if edge < -60:
        return "<-60pp"
    if edge < -40:
        return "-60..-40pp"
    if edge < -20:
        return "-40..-20pp"
    if edge < -10:
        return "-20..-10pp"
    if edge < 0:
        return "-10..0pp"
    if edge < 5:
        return "0..5pp"
    if edge < 10:
        return "5..10pp"
    return ">=10pp"


def bucket_indicator_confirm(signal: dict) -> str:
    confirm = float(signal.get("indicator_confirm", 0) or 0)
    if confirm < -0.50:
        return "<-0.50"
    if confirm < -0.20:
        return "-0.50..-0.20"
    if confirm < 0.0:
        return "-0.20..0.00"
    if confirm < 0.10:
        return "0.00..0.09"
    if confirm < 0.25:
        return "0.10..0.24"
    if confirm < 0.50:
        return "0.25..0.49"
    return ">=0.50"


def bucket_hour(signal: dict) -> str:
    dt = parse_ts(str(signal.get("timestamp", "")))
    return dt.strftime("%H:00 UTC") if dt else "unknown"


def bucket_weekday(signal: dict) -> str:
    dt = parse_ts(str(signal.get("timestamp", "")))
    return dt.strftime("%a") if dt else "unknown"


def summarize_trades(trades: list[dict], key_fn) -> list[dict]:
    groups: dict[str, list[dict]] = defaultdict(list)
    for trade in trades:
        groups[key_fn(trade)].append(trade)

    rows = []
    for key, items in groups.items():
        pnls = [float(item.get("realized_pnl", 0)) for item in items]
        amounts = [float(item.get("amount", 0) or 0) for item in items]
        expected_pnls = [float(item.get("pnl_expected", 0) or 0) for item in items]
        wins = sum(1 for item in items if item.get("won") is True)
        losses = sum(1 for item in items if item.get("won") is False)
        count = len(items)
        total_pnl = sum(pnls)
        total_amount = sum(amounts)
        total_expected = sum(expected_pnls)
        avg_pnl = total_pnl / count if count else 0.0
        win_rate = (wins / (wins + losses) * 100) if (wins + losses) else 0.0
        roi = (total_pnl / total_amount * 100) if total_amount else 0.0
        expected_roi = (total_expected / total_amount * 100) if total_amount else 0.0
        avg_conf = avg([float(item.get("confidence", 0)) * 100 for item in items])
        avg_delta = avg([float(item.get("delta", 0)) for item in items])
        avg_pm = avg([float(item.get("pm", 0)) for item in items])
        avg_time = avg([float(item.get("time_left", 0)) for item in items])

        rows.append({
            "key": key,
            "count": count,
            "wins": wins,
            "losses": losses,
            "win_rate": win_rate,
            "total_pnl": total_pnl,
            "total_expected": total_expected,
            "total_amount": total_amount,
            "avg_pnl": avg_pnl,
            "roi": roi,
            "expected_roi": expected_roi,
            "avg_conf": avg_conf,
            "avg_delta": avg_delta,
            "avg_pm": avg_pm,
            "avg_time": avg_time,
        })

    rows.sort(key=lambda row: (row["total_pnl"], row["avg_pnl"], -row["count"]))
    return rows


def print_table(title: str, rows: list[dict], top: int) -> None:
    print(f"\n=== {title} ===")
    if not rows:
        print("No data")
        return

    losers = rows[:top]
    winners = list(reversed(rows[-top:]))

    print("Worst segments:")
    for row in losers:
        print(
            f"  {row['key']:<12} trades={row['count']:<4} win_rate={fmt_pct(row['win_rate']):<7} "
            f"total={fmt_money(row['total_pnl']):<10} roi={fmt_pct(row['roi']):<7} "
            f"exp={fmt_pct(row['expected_roi']):<7} conf={row['avg_conf']:.1f}% "
            f"delta={row['avg_delta']:.3f}% pm={row['avg_pm']:.3f} t={row['avg_time']:.1f}s"
        )

    print("Best segments:")
    for row in winners:
        print(
            f"  {row['key']:<12} trades={row['count']:<4} win_rate={fmt_pct(row['win_rate']):<7} "
            f"total={fmt_money(row['total_pnl']):<10} roi={fmt_pct(row['roi']):<7} "
            f"exp={fmt_pct(row['expected_roi']):<7} conf={row['avg_conf']:.1f}% "
            f"delta={row['avg_delta']:.3f}% pm={row['avg_pm']:.3f} t={row['avg_time']:.1f}s"
        )


def filter_rows(rows: list[dict], min_trades: int) -> list[dict]:
    return [row for row in rows if row["count"] >= min_trades]


def _metric_avg(trades: list[dict], key: str, scale: float = 1.0) -> float:
    return avg([float(trade.get(key, 0) or 0) * scale for trade in trades])


def print_win_loss_profile(settled: list[dict], min_bucket_trades: int) -> None:
    print("\n=== LIVE TRADE BREAKDOWN ===")
    if not settled:
        print("No settled entered trades yet.")
        return

    winners = [trade for trade in settled if trade.get("won") is True]
    losers = [trade for trade in settled if trade.get("won") is False]

    def print_side(label: str, trades: list[dict]) -> None:
        total_pnl = sum(float(trade.get("realized_pnl", 0) or 0) for trade in trades)
        total_amount = sum(float(trade.get("amount", 0) or 0) for trade in trades)
        roi = (total_pnl / total_amount * 100) if total_amount else 0.0
        print(
            f"{label:<7} trades={len(trades):<3} total={fmt_money(total_pnl):<10} roi={fmt_pct(roi):<7} "
            f"conf={_metric_avg(trades, 'confidence', 100):.1f}% delta={_metric_avg(trades, 'delta'):.3f}% "
            f"pm={_metric_avg(trades, 'pm'):.3f} time={_metric_avg(trades, 'time_left'):.1f}s"
        )

    print_side("Winners", winners)
    print_side("Losers", losers)

    dimensions = [
        ("Confidence", lambda trade: bucket_confidence(float(trade.get("confidence", 0) or 0))),
        ("Delta", lambda trade: bucket_delta(float(trade.get("delta", 0) or 0))),
        ("PM price", lambda trade: bucket_pm(float(trade.get("pm", 0) or 0))),
        ("Time left", lambda trade: bucket_time_left(float(trade.get("time_left", 0) or 0))),
    ]

    for title, key_fn in dimensions:
        print(f"\n{title} buckets:")
        rows = filter_rows(summarize_trades(settled, key_fn), min_bucket_trades)
        if not rows:
            print(f"  No buckets with >= {min_bucket_trades} settled trades")
            continue

        for row in rows:
            winner_share = (row["wins"] / len(winners) * 100) if winners else 0.0
            loser_share = (row["losses"] / len(losers) * 100) if losers else 0.0
            edge = row["wins"] - row["losses"]
            print(
                f"  {row['key']:<12} wins={row['wins']:<3} losses={row['losses']:<3} "
                f"win_rate={fmt_pct(row['win_rate']):<7} pnl={fmt_money(row['total_pnl']):<10} "
                f"winner_mix={fmt_pct(winner_share):<7} loser_mix={fmt_pct(loser_share):<7} edge={edge:+d}"
            )


def print_confidence_diagnostics(settled: list[dict]) -> None:
    print("\n=== CONFIDENCE DIAGNOSTICS ===")
    values = [float(trade.get("confidence", 0) or 0) for trade in settled]
    if not values:
        print("No settled entries with confidence values.")
        return

    print(
        f"raw min={min(values):.4f} avg={avg(values):.4f} max={max(values):.4f} "
        f"| shown as {(avg(values) * 100):.1f}%"
    )
    if max(values) <= 0.2:
        print("Confidence values are clustered very low (<= 0.20 raw); do not tighten this filter yet without more data.")
    elif max(values) <= 1.0:
        print("Confidence appears to be stored as a 0..1 ratio.")
    else:
        print("Confidence appears to be stored on a scale larger than 0..1; verify signal generation logic.")


def filter_recent_signals(signals: list[dict], hours: float) -> list[dict]:
    if hours <= 0:
        return signals

    latest_ts = max((parse_ts(str(signal.get("timestamp", ""))) for signal in signals), default=None)
    if latest_ts is None:
        return []

    cutoff = latest_ts.timestamp() - (hours * 3600)
    recent = []
    for signal in signals:
        dt = parse_ts(str(signal.get("timestamp", "")))
        if dt and dt.timestamp() >= cutoff:
            recent.append(signal)
    return recent


def print_recent_skip_report(signals: list[dict], hours: float) -> None:
    recent = filter_recent_signals(signals, hours)
    print(f"\n=== RECENT SIGNAL FLOW ({hours:g}h) ===")
    if not recent:
        print("No recent signals in that time window.")
        return

    entered = [signal for signal in recent if signal.get("entered")]
    skipped = [signal for signal in recent if not signal.get("entered")]
    settled = [signal for signal in entered if signal.get("realized_pnl") is not None]

    print(f"signals: {len(recent)} | skipped: {len(skipped)} | entries: {len(entered)} | settled: {len(settled)}")

    skip_reasons: dict[str, int] = defaultdict(int)
    for signal in skipped:
        skip_reasons[str(signal.get("reason", "other"))] += 1

    print("Top skip reasons:")
    if not skip_reasons:
        print("  No skipped signals in this window")
    else:
        for reason, count in sorted(skip_reasons.items(), key=lambda item: item[1], reverse=True)[:12]:
            share = count / len(skipped) * 100 if skipped else 0.0
            print(f"  {count:<4} {fmt_pct(share):<7} {reason}")


def print_edge_proxy_report(signals: list[dict], min_trades: int) -> None:
    print("\n=== EDGE REPORT ===")
    if any(signal.get("edge") is not None for signal in signals):
        print("edge = model_prob - market_prob (percentage points).")
        print("Model probability uses the live PM price as a baseline prior plus a small signal-strength adjustment.")
    else:
        print("edge_proxy = confidence - PM price (percentage points).")
        print("Use as a diagnostic only: current confidence is signal strength, not calibrated win probability.")

    entered = [signal for signal in signals if signal.get("entered")]
    settled = [signal for signal in entered if signal.get("realized_pnl") is not None]
    if not settled:
        print("No settled entered trades to analyze.")
        return

    rows = filter_rows(summarize_trades(settled, bucket_edge_proxy), min_trades)
    if not rows:
        print(f"No edge buckets with >= {min_trades} settled trades")
        return

    for row in rows:
        matching = [signal for signal in settled if bucket_edge_proxy(signal) == row["key"]]
        avg_edge = avg([effective_edge(signal) for signal in matching])
        avg_model_prob = avg([float(signal.get("model_prob", 0) or 0) * 100 for signal in matching if signal.get("model_prob") is not None])
        avg_market_prob = avg([float(signal.get("market_prob", signal.get("pm", 0)) or 0) * 100 for signal in matching])
        print(
            f"  {row['key']:<12} trades={row['count']:<3} win_rate={fmt_pct(row['win_rate']):<7} "
            f"pnl={fmt_money(row['total_pnl']):<10} avg_edge={avg_edge:+.1f}pp "
            f"model={avg_model_prob:.1f}% market={avg_market_prob:.1f}%"
        )


def parse_grid(raw: str, cast):
    values = []
    for chunk in raw.split(","):
        item = chunk.strip()
        if not item:
            continue
        values.append(cast(item))
    if not values:
        raise ValueError(f"empty grid: {raw}")
    return values


def infer_pnl_if_entered(signal: dict, default_amount: float = 10.0) -> float | None:
    if signal.get("pnl_if_entered") is not None:
        return float(signal.get("pnl_if_entered", 0))

    won = signal.get("won")
    if won is None:
        return None

    amount = float(signal.get("amount", default_amount) or default_amount)
    pm = float(signal.get("pm", 0) or 0)
    if bool(won):
        payout = amount / pm if pm > 0 else amount
        return payout - amount
    return -amount


def eligible_by_filters(
    signal: dict,
    min_confidence: float,
    delta_skip: float,
    price_min_btc: float,
    price_min_eth: float,
    price_max: float,
    entry_min: int,
    entry_max: int,
) -> bool:
    coin = str(signal.get("coin", ""))
    pm = float(signal.get("pm", 0) or 0)
    conf = float(signal.get("confidence", 0) or 0)
    delta_pct = float(signal.get("delta", 0) or 0)
    time_left = float(signal.get("time_left", 0) or 0)

    if coin == "BTC":
        if pm < price_min_btc:
            return False
    elif coin == "ETH":
        if pm < price_min_eth:
            return False
    else:
        return False

    if pm > price_max:
        return False
    if conf < min_confidence:
        return False
    if delta_pct < delta_skip * 100:
        return False
    if not (entry_min <= time_left <= entry_max):
        return False
    return True


def run_grid_search(signals: list[dict], args) -> int:
    conf_grid = parse_grid(args.conf_grid, float)
    delta_grid = parse_grid(args.delta_grid, float)
    btc_grid = parse_grid(args.price_min_btc_grid, float)
    eth_grid = parse_grid(args.price_min_eth_grid, float)
    pmax_grid = parse_grid(args.price_max_grid, float)
    emin_grid = parse_grid(args.entry_min_grid, int)
    emax_grid = parse_grid(args.entry_max_grid, int)

    resolved_signals = [s for s in signals if s.get("won") is not None]
    if not resolved_signals:
        print("No resolved signals with winner info found.")
        print("Run updated bot longer so signals include won/pnl_if_entered for skipped entries.")
        return 1

    rows = []
    tested = 0

    for conf, delta_skip, pmin_btc, pmin_eth, pmax, emin, emax in product(
        conf_grid, delta_grid, btc_grid, eth_grid, pmax_grid, emin_grid, emax_grid
    ):
        if emin >= emax:
            continue
        if pmin_btc > pmax or pmin_eth > pmax:
            continue

        tested += 1
        selected = []
        for signal in resolved_signals:
            pm = float(signal.get("pm", 0) or 0)
            if pm < args.pm_floor:
                continue
            if pm > args.pm_ceiling:
                continue

            if not eligible_by_filters(
                signal,
                min_confidence=conf,
                delta_skip=delta_skip,
                price_min_btc=pmin_btc,
                price_min_eth=pmin_eth,
                price_max=pmax,
                entry_min=emin,
                entry_max=emax,
            ):
                continue

            pnl = infer_pnl_if_entered(signal, default_amount=args.default_amount)
            if pnl is None:
                continue

            amount = float(signal.get("amount", args.default_amount) or args.default_amount)
            selected.append((pnl, amount, bool(signal.get("won")), pm))

        trades = len(selected)
        if trades < args.min_sim_trades:
            continue

        total_pnl = sum(p for p, _, _, _ in selected)
        total_amount = sum(a for _, a, _, _ in selected)
        wins = sum(1 for _, _, won, _ in selected if won)
        win_rate = (wins / trades) * 100 if trades else 0.0
        roi = (total_pnl / total_amount) * 100 if total_amount else 0.0
        avg_pm = sum(pm for _, _, _, pm in selected) / trades if trades else 0.0

        rows.append(
            {
                "trades": trades,
                "wins": wins,
                "win_rate": win_rate,
                "total_pnl": total_pnl,
                "roi": roi,
                "avg_pm": avg_pm,
                "conf": conf,
                "delta_skip": delta_skip,
                "price_min_btc": pmin_btc,
                "price_min_eth": pmin_eth,
                "price_max": pmax,
                "entry_min": emin,
                "entry_max": emax,
            }
        )

    rows.sort(
        key=lambda r: (
            r["roi"],
            r["total_pnl"],
            r["win_rate"],
            -abs(r["avg_pm"] - 0.5),
            r["trades"],
        ),
        reverse=True,
    )

    print("=== GRID SEARCH (counterfactual on resolved signals) ===")
    print(f"resolved signals: {len(resolved_signals)}")
    print(f"configs tested:   {tested}")
    print(f"configs kept:     {len(rows)} (min trades = {args.min_sim_trades})")
    print(f"pm floor/ceiling: {args.pm_floor:.3f} .. {args.pm_ceiling:.3f}")
    if not rows:
        print("No configs satisfied min trade count. Reduce --min-sim-trades or widen grids.")
        return 0

    print("\nTop configs:")
    for i, row in enumerate(rows[: args.top_configs], start=1):
        print(
            f"{i:>2}. trades={row['trades']:<4} win_rate={fmt_pct(row['win_rate']):<7} "
            f"roi={fmt_pct(row['roi']):<7} total={fmt_money(row['total_pnl']):<10} avg_pm={row['avg_pm']:.3f} | "
            f"conf>={row['conf']:.2f} delta>={row['delta_skip']:.4f} "
            f"btc>={row['price_min_btc']:.2f} eth>={row['price_min_eth']:.2f} max<={row['price_max']:.2f} "
            f"time={row['entry_min']}-{row['entry_max']}s"
        )

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Analyze bot signals.json history")
    parser.add_argument("--file", default=str(DEFAULT_FILE), help="Path to signals.json")
    parser.add_argument("--top", type=int, default=6, help="Number of best/worst rows to show")
    parser.add_argument("--min-trades", type=int, default=3, help="Hide segment rows with fewer than this many settled trades")
    parser.add_argument("--recent-hours", type=float, default=6.0, help="Recent time window for skip-flow diagnostics")
    parser.add_argument("--optimize", action="store_true", help="Run offline filter grid-search using resolved signals")
    parser.add_argument("--top-configs", type=int, default=10, help="Number of best configs to print in optimize mode")
    parser.add_argument("--min-sim-trades", type=int, default=30, help="Minimum simulated trade count per config in optimize mode")
    parser.add_argument("--default-amount", type=float, default=10.0, help="Fallback amount for signals without amount field")
    parser.add_argument("--pm-floor", type=float, default=0.10, help="Ignore signals with PM price below this floor in optimize mode")
    parser.add_argument("--pm-ceiling", type=float, default=0.99, help="Ignore signals with PM price above this ceiling in optimize mode")
    parser.add_argument("--conf-grid", default="0.45,0.50,0.55,0.60", help="Comma-separated confidence thresholds")
    parser.add_argument("--delta-grid", default="0.0008,0.0010,0.0012,0.0015", help="Comma-separated delta_skip thresholds")
    parser.add_argument("--price-min-btc-grid", default="0.82,0.86,0.90,0.94", help="Comma-separated BTC min prices")
    parser.add_argument("--price-min-eth-grid", default="0.80,0.84,0.88,0.92", help="Comma-separated ETH min prices")
    parser.add_argument("--price-max-grid", default="0.94,0.95,0.96,0.97", help="Comma-separated max prices")
    parser.add_argument("--entry-min-grid", default="10,12,15", help="Comma-separated entry_min values")
    parser.add_argument("--entry-max-grid", default="25,30,35", help="Comma-separated entry_max values")
    args = parser.parse_args()

    path = Path(args.file)
    if not path.exists():
        print(f"Signals file not found: {path}")
        print("Tip: copy signals.json from your VPS into the repo root or pass --file /path/to/signals.json")
        return 1

    signals = load_signals(path)

    if args.optimize:
        return run_grid_search(signals, args)

    entered = [s for s in signals if s.get("entered")]
    settled = [s for s in entered if s.get("realized_pnl") is not None]
    pending = [s for s in entered if s.get("realized_pnl") is None]
    skipped = [s for s in signals if not s.get("entered")]

    wins = sum(1 for s in settled if s.get("won") is True)
    losses = sum(1 for s in settled if s.get("won") is False)
    total_pnl = sum(float(s.get("realized_pnl", 0)) for s in settled)
    total_expected = sum(float(s.get("pnl_expected", 0) or 0) for s in settled)
    total_amount = sum(float(s.get("amount", 0) or 0) for s in settled)
    avg_pm = avg([float(s.get("pm", 0)) for s in settled])
    avg_conf = avg([float(s.get("confidence", 0)) * 100 for s in settled])
    avg_delta = avg([float(s.get("delta", 0)) for s in settled])
    avg_time = avg([float(s.get("time_left", 0)) for s in settled])
    avg_model_prob = avg([float(s.get("model_prob", 0) or 0) * 100 for s in settled if s.get("model_prob") is not None])
    avg_edge_pp = avg([effective_edge(s) for s in settled])
    avg_indicator_confirm = avg([float(s.get("indicator_confirm", 0) or 0) for s in settled if s.get("indicator_confirm") is not None])
    win_rate = (wins / (wins + losses) * 100) if (wins + losses) else 0.0
    roi = (total_pnl / total_amount * 100) if total_amount else 0.0
    expected_roi = (total_expected / total_amount * 100) if total_amount else 0.0

    print("=== OVERVIEW ===")
    print(f"signals:   {len(signals)}")
    print(f"skipped:   {len(skipped)}")
    print(f"entries:   {len(entered)}")
    print(f"settled:   {len(settled)}")
    print(f"pending:   {len(pending)}")
    print(f"wins:      {wins}")
    print(f"losses:    {losses}")
    print(f"win rate:  {fmt_pct(win_rate)}")
    print(f"total pnl: {fmt_money(total_pnl)}")
    print(f"real roi:  {fmt_pct(roi)}")
    print(f"exp roi:   {fmt_pct(expected_roi)}")
    print(f"avg conf:  {avg_conf:.1f}%")
    print(f"avg delta: {avg_delta:.3f}%")
    print(f"avg pm:    {avg_pm:.3f}")
    print(f"avg time:  {avg_time:.1f}s")
    if any(s.get("model_prob") is not None for s in settled):
        print(f"avg model: {avg_model_prob:.1f}%")
        print(f"avg edge:  {avg_edge_pp:+.1f}pp")
    if any(s.get("indicator_confirm") is not None for s in settled):
        print(f"avg 1m:    {avg_indicator_confirm:+.2f}")

    print_table("By coin", filter_rows(summarize_trades(settled, lambda s: str(s.get("coin", "?"))), args.min_trades), args.top)
    print_table("By confidence", filter_rows(summarize_trades(settled, lambda s: bucket_confidence(float(s.get("confidence", 0)))), args.min_trades), args.top)
    print_table("By delta", filter_rows(summarize_trades(settled, lambda s: bucket_delta(float(s.get("delta", 0)))), args.min_trades), args.top)
    print_table("By 1m confirm", filter_rows(summarize_trades(settled, bucket_indicator_confirm), args.min_trades), args.top)
    print_table("By time left", filter_rows(summarize_trades(settled, lambda s: bucket_time_left(float(s.get("time_left", 0)))), args.min_trades), args.top)
    print_table("By PM price", filter_rows(summarize_trades(settled, lambda s: bucket_pm(float(s.get("pm", 0)))), args.min_trades), args.top)
    print_table("By expected ROI", filter_rows(summarize_trades(settled, bucket_expected_roi), args.min_trades), args.top)
    print_table("By UTC hour", filter_rows(summarize_trades(settled, bucket_hour), args.min_trades), args.top)
    print_table("By weekday", filter_rows(summarize_trades(settled, bucket_weekday), args.min_trades), args.top)
    print_win_loss_profile(settled, args.min_trades)
    print_confidence_diagnostics(settled)
    print_edge_proxy_report(signals, args.min_trades)
    print_indicator_confirm_report(signals, args.min_trades)
    print_recent_skip_report(signals, args.recent_hours)

    print("\n=== SKIP REASONS ===")
    skip_reasons: dict[str, int] = defaultdict(int)
    for signal in skipped:
        skip_reasons[str(signal.get("reason", "other"))] += 1
    for reason, count in sorted(skip_reasons.items(), key=lambda item: item[1], reverse=True)[:15]:
        print(f"  {count:<5} {reason}")

    print("\n=== QUICK TAKEAWAYS ===")
    confidence_rows = filter_rows(summarize_trades(settled, lambda s: bucket_confidence(float(s.get("confidence", 0)))), args.min_trades)
    delta_rows = filter_rows(summarize_trades(settled, lambda s: bucket_delta(float(s.get("delta", 0)))), args.min_trades)
    confirm_rows = filter_rows(summarize_trades(settled, bucket_indicator_confirm), args.min_trades)
    time_rows = filter_rows(summarize_trades(settled, lambda s: bucket_time_left(float(s.get("time_left", 0)))), args.min_trades)
    pm_rows = filter_rows(summarize_trades(settled, lambda s: bucket_pm(float(s.get("pm", 0)))), args.min_trades)
    roi_rows = filter_rows(summarize_trades(settled, bucket_expected_roi), args.min_trades)
    hour_rows = filter_rows(summarize_trades(settled, bucket_hour), args.min_trades)

    if confidence_rows:
        print(f"  weakest confidence bucket: {confidence_rows[0]['key']} {fmt_money(confidence_rows[0]['total_pnl'])}")
        print(f"  strongest confidence bucket: {confidence_rows[-1]['key']} {fmt_money(confidence_rows[-1]['total_pnl'])}")
    if delta_rows:
        print(f"  weakest delta bucket: {delta_rows[0]['key']} {fmt_money(delta_rows[0]['total_pnl'])}")
        print(f"  strongest delta bucket: {delta_rows[-1]['key']} {fmt_money(delta_rows[-1]['total_pnl'])}")
    if confirm_rows:
        print(f"  weakest 1m confirm bucket: {confirm_rows[0]['key']} {fmt_money(confirm_rows[0]['total_pnl'])}")
        print(f"  strongest 1m confirm bucket: {confirm_rows[-1]['key']} {fmt_money(confirm_rows[-1]['total_pnl'])}")
    if time_rows:
        print(f"  worst timing bucket: {time_rows[0]['key']} {fmt_money(time_rows[0]['total_pnl'])}")
        print(f"  best timing bucket: {time_rows[-1]['key']} {fmt_money(time_rows[-1]['total_pnl'])}")
    if pm_rows:
        print(f"  worst PM bucket: {pm_rows[0]['key']} {fmt_money(pm_rows[0]['total_pnl'])}")
        print(f"  best PM bucket: {pm_rows[-1]['key']} {fmt_money(pm_rows[-1]['total_pnl'])}")
    if roi_rows:
        print(f"  weakest expected ROI bucket: {roi_rows[0]['key']} {fmt_money(roi_rows[0]['total_pnl'])}")
        print(f"  strongest expected ROI bucket: {roi_rows[-1]['key']} {fmt_money(roi_rows[-1]['total_pnl'])}")
    if hour_rows:
        print(f"  worst UTC hour: {hour_rows[0]['key']} {fmt_money(hour_rows[0]['total_pnl'])}")
        print(f"  best UTC hour: {hour_rows[-1]['key']} {fmt_money(hour_rows[-1]['total_pnl'])}")

    print("\n=== NEXT ACTIONS ===")
    if len(settled) < 30:
        print("  Need more settled trades before tuning thresholds aggressively (<30 settled trades).")
    elif total_pnl <= 0:
        print("  Strategy is not yet profitable on settled trades; tighten filters before moving to paper/live.")
    else:
        print("  Settled trades are positive overall; compare best/worst buckets before changing thresholds.")

    if expected_roi > 0 and roi < 0:
        print("  Expected edge is positive but realized edge is negative: suspect slippage, noisy filters, or small sample size.")
    if win_rate < 55 and settled:
        print("  Win rate is weak for high-price PM entries; review PM price and confidence thresholds first.")
    if pending:
        print(f"  There are {len(pending)} pending entries; rerun later after those markets settle.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
