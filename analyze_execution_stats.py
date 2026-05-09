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

    latencies = [
        float(r.get("maker_fill_latency_ms"))
        for r in filled
        if _safe_float(r.get("maker_fill_latency_ms"), default=None) is not None
    ]

    improvements = []
    for r in filled:
        pm = _safe_float(r.get("pm"), default=None)
        fill = _safe_float(r.get("maker_fill_price"), default=None)
        if pm is None or fill is None:
            continue
        # For a BUY: lower fill vs PM is positive improvement.
        improvements.append(pm - fill)

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
        "by_hour": by_hour,
        "by_dow": by_dow,
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


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--window-samples-jsonl", default="window_samples.jsonl")
    ap.add_argument("--signals", default="signals.json")
    ap.add_argument("--source", choices=["window_samples", "signals"], default="window_samples")
    ap.add_argument("--time-breakdown", action="store_true", help="Print fill rate by hour/day-of-week")
    args = ap.parse_args()

    if args.source == "signals":
        records = _load_json_array(Path(args.signals))
    else:
        records = list(_iter_jsonl(Path(args.window_samples_jsonl)))

    maker = summarize_maker_entry(records)
    pnl = summarize_realized_pnl(records)
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
    if maker["cancel_reasons"]:
        print("cancel reasons (not filled):")
        for reason, cnt in maker["cancel_reasons"].most_common(20):
            print(f"  - {reason}: {cnt} ({_pct(cnt, maker['attempts']):.1f}%)")

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

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
