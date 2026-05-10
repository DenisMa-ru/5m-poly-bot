"""analyze_execution_stats.py

Offline helper: summarize maker-entry execution quality from window_samples.jsonl / signals.json.

Goals (Phase 1 rollout):
  - maker fill rate
  - cancel/skip reasons
  - latency distribution
  - price improvement vs PM snapshot
  - realized PnL stats (no fees)

No external deps.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path
from datetime import datetime, timezone


def _iter_jsonl(path: Path):
    if not path.exists():
        return
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            if isinstance(obj, dict):
                yield obj


def _load_json_array(path: Path):
    if not path.exists():
        return []
    try:
        with path.open("r", encoding="utf-8") as fh:
            payload = json.load(fh)
        return payload if isinstance(payload, list) else []
    except Exception:
        return []


def _safe_float(value, default=None):
    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default


def _pct(num: float, den: float) -> float:
    if den <= 0:
        return 0.0
    return (num / den) * 100.0


def _mean(values: list[float]) -> float | None:
    if not values:
        return None
    return sum(values) / len(values)


def _summarize_numeric(values: list[float]) -> dict:
    if not values:
        return {"avg": None, "min": None, "max": None}
    return {
        "avg": round(_mean(values) or 0.0, 4),
        "min": round(min(values), 4),
        "max": round(max(values), 4),
    }


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _reason_bucket(record: dict) -> str:
    """Normalize maker cancel/skip reasons for reporting."""
    reason = str(record.get("maker_cancel_reason", "") or "").strip() or str(record.get("failure_type", "") or "").strip()
    if not reason:
        return "unknown"

    alias = {
        "maker_timeout": "timeout",
        "timeout": "timeout",
        "wide_spread": "wide_spread",
        "late_signal": "late_signal",
        "signal_stale": "signal_stale",
        "empty_book": "empty_book",
        "partial_fill_below_min_ratio": "partial_fill_low_ratio",
        "rejected": "post_only_reject",
        "canceled": "post_only_reject",
        "cancelled": "post_only_reject",
        "canceled": "post_only_reject",
    }
    lowered = reason.lower()
    return alias.get(lowered, lowered)


def summarize_maker_entry(records: list[dict]) -> dict:
    attempts = [
        r
        for r in records
        if str(r.get("execution_mode", "") or "") == "maker_entry"
        and str(r.get("record_type", "") or "") == "window_sample"
        and str(r.get("sample_source", "") or "") == "entry_execution"
    ]

    filled = [r for r in attempts if bool(r.get("maker_filled"))]
    cancels = Counter(_reason_bucket(r) for r in attempts if not bool(r.get("maker_filled")))

    not_filled = [r for r in attempts if not bool(r.get("maker_filled"))]

    latencies = [
        float(r.get("maker_fill_latency_ms"))
        for r in filled
        if _safe_float(r.get("maker_fill_latency_ms"), default=None) is not None
    ]

    improvements = []
    improvements_vs_ask = []
    slippage_vs_bid = []
    pm_minus_mid = []
    pm_minus_bid = []
    pm_minus_ask = []
    for r in filled:
        pm = _safe_float(r.get("pm"), default=None)
        fill = _safe_float(r.get("maker_fill_price"), default=None)
        if pm is None or fill is None:
            continue
        # For a BUY: lower fill vs PM is positive improvement.
        improvements.append(pm - fill)

        best_bid = _safe_float(r.get("best_bid_at_entry"), default=None)
        best_ask = _safe_float(r.get("best_ask_at_entry"), default=None)
        # For a BUY:
        # - improvement vs ask: best_ask - fill (positive is good)
        # - slippage vs bid: fill - best_bid (positive is worse vs joining bid)
        if best_ask is not None:
            improvements_vs_ask.append(best_ask - fill)
            pm_minus_ask.append(pm - best_ask)
        if best_bid is not None:
            slippage_vs_bid.append(fill - best_bid)
            pm_minus_bid.append(pm - best_bid)
        if best_bid is not None and best_ask is not None and best_bid > 0 and best_ask > 0:
            pm_minus_mid.append(pm - ((best_bid + best_ask) / 2.0))

    # Time buckets
    by_hour = defaultdict(lambda: {"attempts": 0, "filled": 0})
    by_dow = defaultdict(lambda: {"attempts": 0, "filled": 0})
    for r in attempts:
        ts = _parse_ts(r.get("timestamp"))
        if ts is None:
            continue
        by_hour[ts.hour]["attempts"] += 1
        by_dow[ts.weekday()]["attempts"] += 1
        if bool(r.get("maker_filled")):
            by_hour[ts.hour]["filled"] += 1
            by_dow[ts.weekday()]["filled"] += 1

    # Simple diagnostics: which book contexts correlate with fills.
    def collect(records: list[dict], key: str) -> list[float]:
        out: list[float] = []
        for rr in records:
            v = _safe_float(rr.get(key), default=None)
            if v is None:
                continue
            out.append(v)
        return out

    diag_keys = [
        "spread_at_entry",
        "bid_depth_top_n",
        "ask_depth_top_n",
        "book_imbalance_at_entry",
        "signal_age_sec_at_order_submit",
        "order_rest_seconds",
    ]
    diagnostics = {
        k: {
            "filled": _summarize_numeric(collect(filled, k)),
            "not_filled": _summarize_numeric(collect(not_filled, k)),
        }
        for k in diag_keys
    }

    return {
        "attempts": len(attempts),
        "filled": len(filled),
        "fill_rate_pct": round(_pct(len(filled), len(attempts)), 2),
        "cancel_reasons": cancels,
        "latency_ms_avg": None if not latencies else round(_mean(latencies) or 0.0, 1),
        "latency_ms_min": None if not latencies else int(min(latencies)),
        "latency_ms_max": None if not latencies else int(max(latencies)),
        "price_improvement_avg": None if not improvements else round(_mean(improvements) or 0.0, 4),
        "price_improvement_min": None if not improvements else round(min(improvements), 4),
        "price_improvement_max": None if not improvements else round(max(improvements), 4),
        "improvement_vs_ask_avg": None if not improvements_vs_ask else round(_mean(improvements_vs_ask) or 0.0, 4),
        "improvement_vs_ask_min": None if not improvements_vs_ask else round(min(improvements_vs_ask), 4),
        "improvement_vs_ask_max": None if not improvements_vs_ask else round(max(improvements_vs_ask), 4),
        "slippage_vs_bid_avg": None if not slippage_vs_bid else round(_mean(slippage_vs_bid) or 0.0, 4),
        "slippage_vs_bid_min": None if not slippage_vs_bid else round(min(slippage_vs_bid), 4),
        "slippage_vs_bid_max": None if not slippage_vs_bid else round(max(slippage_vs_bid), 4),
        "pm_minus_mid_avg": None if not pm_minus_mid else round(_mean(pm_minus_mid) or 0.0, 4),
        "pm_minus_mid_min": None if not pm_minus_mid else round(min(pm_minus_mid), 4),
        "pm_minus_mid_max": None if not pm_minus_mid else round(max(pm_minus_mid), 4),
        "pm_minus_bid_avg": None if not pm_minus_bid else round(_mean(pm_minus_bid) or 0.0, 4),
        "pm_minus_bid_min": None if not pm_minus_bid else round(min(pm_minus_bid), 4),
        "pm_minus_bid_max": None if not pm_minus_bid else round(max(pm_minus_bid), 4),
        "pm_minus_ask_avg": None if not pm_minus_ask else round(_mean(pm_minus_ask) or 0.0, 4),
        "pm_minus_ask_min": None if not pm_minus_ask else round(min(pm_minus_ask), 4),
        "pm_minus_ask_max": None if not pm_minus_ask else round(max(pm_minus_ask), 4),
        "by_hour": by_hour,
        "by_dow": by_dow,
        "diagnostics": diagnostics,
    }


def summarize_counterfactual_skips(records: list[dict]) -> dict:
    skipped = [
        r for r in records
        if str(r.get("record_type", "") or "") == "window_sample"
        and str(r.get("sample_source", "") or "") == "entry_execution"
        and not bool(r.get("entered"))
        and r.get("pnl_if_entered") is not None
    ]
    pnls = [_safe_float(r.get("pnl_if_entered"), default=None) for r in skipped]
    pnls = [p for p in pnls if p is not None]
    positive = sum(1 for p in pnls if p > 0)
    negative = sum(1 for p in pnls if p < 0)
    return {
        "skipped": len(skipped),
        "pnl_sum": None if not pnls else round(sum(pnls), 4),
        "pnl_avg": None if not pnls else round(_mean(pnls) or 0.0, 4),
        "pnl_positive": positive,
        "pnl_negative": negative,
    }


def summarize_realized_pnl(records: list[dict]) -> dict:
    entered = [r for r in records if bool(r.get("entered"))]
    resolved = [r for r in entered if r.get("realized_pnl") is not None]
    pnls = [_safe_float(r.get("realized_pnl"), default=None) for r in resolved]
    pnls = [p for p in pnls if p is not None]
    wins = [r for r in resolved if bool(r.get("won"))]
    return {
        "entered": len(entered),
        "resolved": len(resolved),
        "win_rate_pct": round(_pct(len(wins), len(resolved)), 2),
        "pnl_avg": None if not pnls else round(_mean(pnls) or 0.0, 4),
        "pnl_sum": None if not pnls else round(sum(pnls), 4),
        "pnl_min": None if not pnls else round(min(pnls), 4),
        "pnl_max": None if not pnls else round(max(pnls), 4),
    }


def _bucket_spread(spread: float | None) -> str:
    if spread is None:
        return "unknown"
    try:
        s = float(spread)
    except Exception:
        return "unknown"
    if s <= 0:
        return "unknown"
    if s <= 0.0100001:
        return "0.01"
    if s <= 0.0200001:
        return "0.02"
    if s <= 0.0500001:
        return "0.03-0.05"
    return ">0.05"


def _bucket_time_left(t: float | None) -> str:
    if t is None:
        return "unknown"
    try:
        v = float(t)
    except Exception:
        return "unknown"
    if v < 0:
        return "unknown"
    if v < 20:
        return "<20"
    if v < 35:
        return "20-35"
    if v < 60:
        return "35-60"
    return ">=60"


def _bucket_imbalance(x: float | None) -> str:
    if x is None:
        return "unknown"
    try:
        v = float(x)
    except Exception:
        return "unknown"
    if v <= -0.05:
        return "<=-0.05"
    if v < 0:
        return "(-0.05..0)"
    if v < 0.05:
        return "[0..0.05)"
    return ">=0.05"


def summarize_realized_pnl_buckets(records: list[dict], *, min_trades: int = 3, top: int = 25) -> list[dict]:
    """Bucket realized pnl for entered+resolved trades.

    Purpose: quickly see which contexts (spread/imbalance/time_left/tier) are profitable.
    """
    resolved = [r for r in records if bool(r.get("entered")) and r.get("realized_pnl") is not None]
    buckets: dict[str, list[float]] = defaultdict(list)
    bucket_wins: dict[str, int] = defaultdict(int)

    for r in resolved:
        pnl = _safe_float(r.get("realized_pnl"), default=None)
        if pnl is None:
            continue
        spread_bucket = _bucket_spread(_safe_float(r.get("spread_at_entry"), default=None))
        imb_bucket = _bucket_imbalance(_safe_float(r.get("book_imbalance_at_entry"), default=None))
        tl_bucket = _bucket_time_left(_safe_float(r.get("time_left"), default=None))
        tier = str(r.get("signal_tier", "") or "").strip() or "unknown"
        key = f"spread={spread_bucket} | imb={imb_bucket} | tl={tl_bucket} | tier={tier}"
        buckets[key].append(pnl)
        if bool(r.get("won")):
            bucket_wins[key] += 1

    rows = []
    for key, pnls in buckets.items():
        if len(pnls) < int(min_trades):
            continue
        wins = bucket_wins.get(key, 0)
        rows.append({
            "bucket": key,
            "trades": len(pnls),
            "win_rate_pct": round(_pct(wins, len(pnls)), 1),
            "pnl_sum": round(sum(pnls), 4),
            "pnl_avg": round(_mean(pnls) or 0.0, 4),
            "pnl_min": round(min(pnls), 4),
            "pnl_max": round(max(pnls), 4),
        })

    rows.sort(key=lambda r: (r["pnl_sum"], r["trades"]), reverse=True)
    return rows[: int(top)]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--window-samples-jsonl", default="window_samples.jsonl")
    ap.add_argument("--signals", default="signals.json")
    ap.add_argument("--source", choices=["window_samples", "signals"], default="window_samples")
    ap.add_argument("--time-breakdown", action="store_true", help="Print fill rate by hour/day-of-week")
    ap.add_argument(
        "--since-ts",
        default="",
        help="Only include records with timestamp >= this UTC ISO string (e.g. 2026-05-09T00:00:00Z)",
    )
    args = ap.parse_args()

    if args.source == "signals":
        records = _load_json_array(Path(args.signals))
    else:
        records = list(_iter_jsonl(Path(args.window_samples_jsonl)))

    if args.since_ts:
        since = _parse_ts(args.since_ts)
        if since is not None:
            filtered = []
            for r in records:
                ts = _parse_ts(r.get("timestamp"))
                if ts is None or ts < since:
                    continue
                filtered.append(r)
            records = filtered

    maker = summarize_maker_entry(records)
    pnl = summarize_realized_pnl(records)
    pnl_buckets = summarize_realized_pnl_buckets(records)
    skips = summarize_counterfactual_skips(records) if args.source == "window_samples" else None

    print("=== MAKER ENTRY (Phase 1) ===")
    print(f"attempts: {maker['attempts']}")
    print(f"filled:   {maker['filled']}")
    print(f"fill_rate:{maker['fill_rate_pct']:.2f}%")
    if maker["latency_ms_avg"] is not None:
        print(f"latency_ms avg/min/max: {maker['latency_ms_avg']} / {maker['latency_ms_min']} / {maker['latency_ms_max']}")
    if maker["price_improvement_avg"] is not None:
        print(
            "price_improvement (pm - fill) avg/min/max: "
            f"{maker['price_improvement_avg']} / {maker['price_improvement_min']} / {maker['price_improvement_max']}"
        )
    if maker["improvement_vs_ask_avg"] is not None:
        print(
            "improvement_vs_best_ask_at_entry (ask - fill) avg/min/max: "
            f"{maker['improvement_vs_ask_avg']} / {maker['improvement_vs_ask_min']} / {maker['improvement_vs_ask_max']}"
        )
    if maker["slippage_vs_bid_avg"] is not None:
        print(
            "slippage_vs_best_bid_at_entry (fill - bid) avg/min/max: "
            f"{maker['slippage_vs_bid_avg']} / {maker['slippage_vs_bid_min']} / {maker['slippage_vs_bid_max']}"
        )
    if maker.get("pm_minus_mid_avg") is not None:
        print(
            "pm_minus_clob_mid_at_entry (pm - mid) avg/min/max: "
            f"{maker['pm_minus_mid_avg']} / {maker['pm_minus_mid_min']} / {maker['pm_minus_mid_max']}"
        )
    if maker.get("pm_minus_bid_avg") is not None:
        print(
            "pm_minus_best_bid_at_entry (pm - bid) avg/min/max: "
            f"{maker['pm_minus_bid_avg']} / {maker['pm_minus_bid_min']} / {maker['pm_minus_bid_max']}"
        )
    if maker.get("pm_minus_ask_avg") is not None:
        print(
            "pm_minus_best_ask_at_entry (pm - ask) avg/min/max: "
            f"{maker['pm_minus_ask_avg']} / {maker['pm_minus_ask_min']} / {maker['pm_minus_ask_max']}"
        )
    if maker["cancel_reasons"]:
        print("cancel reasons (not filled):")
        for reason, cnt in maker["cancel_reasons"].most_common(20):
            print(f"  - {reason}: {cnt} ({_pct(cnt, maker['attempts']):.1f}%)")

    if maker.get("diagnostics") and maker["attempts"]:
        print("\nbook/context diagnostics (filled vs not_filled):")
        for key, groups in maker["diagnostics"].items():
            f = groups.get("filled") or {}
            nf = groups.get("not_filled") or {}
            if f.get("avg") is None and nf.get("avg") is None:
                continue
            print(
                f"  - {key}: filled avg/min/max={f.get('avg')} / {f.get('min')} / {f.get('max')} | "
                f"not_filled avg/min/max={nf.get('avg')} / {nf.get('min')} / {nf.get('max')}"
            )

    if args.time_breakdown and maker["attempts"]:
        print("\nfill rate by hour (UTC):")
        for hour in range(24):
            bucket = maker["by_hour"].get(hour)
            if not bucket:
                continue
            a = bucket["attempts"]
            f = bucket["filled"]
            print(f"  - {hour:02d}: attempts={a} filled={f} fill_rate={_pct(f, a):.1f}%")

        dow_names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
        print("\nfill rate by day-of-week (UTC):")
        for dow in range(7):
            bucket = maker["by_dow"].get(dow)
            if not bucket:
                continue
            a = bucket["attempts"]
            f = bucket["filled"]
            print(f"  - {dow_names[dow]}: attempts={a} filled={f} fill_rate={_pct(f, a):.1f}%")

    if skips is not None:
        print("\n=== COUNTERFACTUAL PNL (missed maker entries) ===")
        print(f"skipped_entries_with_pnl: {skips['skipped']}")
        if skips["pnl_sum"] is not None:
            print(f"counterfactual_pnl_sum: {skips['pnl_sum']}")
            print(f"counterfactual_pnl_avg: {skips['pnl_avg']}")
            print(f"positive/negative: {skips['pnl_positive']} / {skips['pnl_negative']}")

    print("\n=== REALIZED PNL (no fees) ===")
    print(f"entered:  {pnl['entered']}")
    print(f"resolved: {pnl['resolved']}")
    if pnl["resolved"]:
        print(f"win_rate: {pnl['win_rate_pct']:.2f}%")
        print(f"pnl avg/sum/min/max: {pnl['pnl_avg']} / {pnl['pnl_sum']} / {pnl['pnl_min']} / {pnl['pnl_max']}")

    if pnl_buckets:
        print("\n=== REALIZED PNL BREAKDOWN (top buckets, min_trades=3) ===")
        for row in pnl_buckets:
            print(
                f"  - trades={row['trades']} win_rate={row['win_rate_pct']}% "
                f"pnl_sum={row['pnl_sum']} pnl_avg={row['pnl_avg']} min/max={row['pnl_min']}/{row['pnl_max']}"
            )
            print(f"    {row['bucket']}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
