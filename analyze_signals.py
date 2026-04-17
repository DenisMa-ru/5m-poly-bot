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
    if pct < 40:
        return "<40%"
    if pct < 50:
        return "40-49%"
    if pct < 60:
        return "50-59%"
    if pct < 70:
        return "60-69%"
    return ">=70%"


def bucket_delta(delta_pct: float) -> str:
    if delta_pct < 0.10:
        return "<0.10%"
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
    if pm_price < 0.94:
        return "<0.94"
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


def main() -> int:
    parser = argparse.ArgumentParser(description="Analyze bot signals.json history")
    parser.add_argument("--file", default=str(DEFAULT_FILE), help="Path to signals.json")
    parser.add_argument("--top", type=int, default=6, help="Number of best/worst rows to show")
    parser.add_argument("--min-trades", type=int, default=3, help="Hide segment rows with fewer than this many settled trades")
    args = parser.parse_args()

    path = Path(args.file)
    if not path.exists():
        print(f"Signals file not found: {path}")
        print("Tip: copy signals.json from your VPS into the repo root or pass --file /path/to/signals.json")
        return 1

    signals = load_signals(path)
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

    print_table("By coin", filter_rows(summarize_trades(settled, lambda s: str(s.get("coin", "?"))), args.min_trades), args.top)
    print_table("By confidence", filter_rows(summarize_trades(settled, lambda s: bucket_confidence(float(s.get("confidence", 0)))), args.min_trades), args.top)
    print_table("By delta", filter_rows(summarize_trades(settled, lambda s: bucket_delta(float(s.get("delta", 0)))), args.min_trades), args.top)
    print_table("By time left", filter_rows(summarize_trades(settled, lambda s: bucket_time_left(float(s.get("time_left", 0)))), args.min_trades), args.top)
    print_table("By PM price", filter_rows(summarize_trades(settled, lambda s: bucket_pm(float(s.get("pm", 0)))), args.min_trades), args.top)
    print_table("By expected ROI", filter_rows(summarize_trades(settled, bucket_expected_roi), args.min_trades), args.top)
    print_table("By UTC hour", filter_rows(summarize_trades(settled, bucket_hour), args.min_trades), args.top)
    print_table("By weekday", filter_rows(summarize_trades(settled, bucket_weekday), args.min_trades), args.top)

    print("\n=== SKIP REASONS ===")
    skip_reasons: dict[str, int] = defaultdict(int)
    for signal in skipped:
        skip_reasons[str(signal.get("reason", "other"))] += 1
    for reason, count in sorted(skip_reasons.items(), key=lambda item: item[1], reverse=True)[:15]:
        print(f"  {count:<5} {reason}")

    print("\n=== QUICK TAKEAWAYS ===")
    confidence_rows = filter_rows(summarize_trades(settled, lambda s: bucket_confidence(float(s.get("confidence", 0)))), args.min_trades)
    delta_rows = filter_rows(summarize_trades(settled, lambda s: bucket_delta(float(s.get("delta", 0)))), args.min_trades)
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
