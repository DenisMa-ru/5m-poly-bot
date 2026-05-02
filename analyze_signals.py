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
DEFAULT_WINDOW_SAMPLES_FILE = Path(__file__).with_name("window_samples.json")
DEFAULT_CORE_EV_RULES_FILE = Path(__file__).with_name("core_ev_rules.json")


def load_signals(path: Path) -> list[dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError("signals file must contain a JSON list")
    return data


def select_core_ev_records(records: list[dict]) -> list[dict]:
    selected = []
    for record in records:
        record_type = str(record.get("record_type", "") or "").strip().lower()
        if not record_type or record_type == "window_sample":
            selected.append(record)
    return selected


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


def signal_tier_label(signal: dict) -> str:
    return str(signal.get("signal_tier", "unknown") or "unknown")


def signal_tier_reason_label(signal: dict) -> str:
    return str(signal.get("signal_tier_reason", "unknown") or "unknown")


def shadow_profile_label(signal: dict) -> str:
    return str(signal.get("shadow_entry_profile", "none") or "none")


def shadow_reason_label(signal: dict) -> str:
    return str(signal.get("shadow_entry_reason", "") or "none")


def market_regime_label(signal: dict) -> str:
    return str(signal.get("market_regime", "unknown") or "unknown")


def shadow_live_decision_label(signal: dict) -> str:
    return str(signal.get("shadow_live_decision", "unknown") or "unknown")


def shadow_live_reason_label(signal: dict) -> str:
    return str(signal.get("shadow_live_reason", "") or "none")


def shadow_live_mode_label(signal: dict) -> str:
    return str(signal.get("shadow_live_mode", "unknown") or "unknown")


def indicator_reason_label(signal: dict) -> str:
    reason = str(signal.get("indicator_reason", "") or "")
    return reason if reason else "none"


def in_normal_pm_zone(signal: dict) -> bool:
    pm = float(signal.get("pm", 0) or 0)
    return 0.58 <= pm <= 0.70


def reason_label(signal: dict) -> str:
    return str(signal.get("reason", "other") or "other")


def core_ev_decision_label(signal: dict) -> str:
    return str(signal.get("core_ev_decision", "unknown") or "unknown")


def core_ev_reason_label(signal: dict) -> str:
    reason = str(signal.get("core_ev_reason", "") or "")
    return reason if reason else "none"


def core_ev_branch_label(signal: dict) -> str:
    reason = core_ev_reason_label(signal).lower()
    if reason == "none":
        return "none"
    if "high-pm micro entry outside flex zone" in reason:
        return "high_pm_outside_flex_micro"
    if "global expensive pm denied by runtime envelope" in reason:
        return "global_expensive_pm_deny"
    if "trend conflict haircut to micro-size entry" in reason:
        return "trend_conflict_micro"
    if "late trend conflict micro denied by runtime envelope" in reason:
        return "trend_conflict_late_deny"
    if "shadow live deny" in reason:
        return "shadow_live_deny"
    if "reversal risk not recovered" in reason:
        return "reversal_not_recovered"
    if "undersampled but positive core ev bucket" in reason:
        return "undersampled_positive_micro"
    if "flex pm outside base zone but undersampled-positive bucket" in reason:
        return "flex_undersampled_positive_micro"
    if "flex pm outside base zone with unknown core ev bucket" in reason:
        return "flex_unknown_bucket_micro"
    if "undersampled or unknown core ev bucket" in reason:
        return "undersampled_or_unknown_deny"
    if "flex pm bucket remains historically negative" in reason:
        return "flex_historical_negative_deny"
    if "expensive or late flex pm outside base zone denied by runtime envelope" in reason:
        return "flex_expensive_runtime_deny"
    if "flex pm outside base zone" in reason and "downgraded to micro-size" in reason:
        return "flex_outside_base_micro"
    if "l3 unknown, using positive l2 fallback" in reason:
        return "l2_fallback_from_unknown_l3"
    if "full-window l1 fallback" in reason:
        return "full_window_l1_fallback"
    if "reduced-size core ev fallback below" in reason:
        return "reduced_specificity_micro"
    if "full-window requires" in reason and "bucket specificity" in reason:
        return "full_window_specificity_deny"
    if "core ev watch downgraded to micro-size entry" in reason:
        return "watch_to_micro"
    if reason == "core ev allow":
        return "base_allow"
    if reason == "core ev strong_allow":
        return "base_strong_allow"
    if "strong_allow early mid-pm slice denied by runtime envelope" in reason:
        return "strong_allow_early_mid_pm_deny"
    if reason == "core ev watch":
        return "base_watch"
    if "core ev requires aligned non-conflicting trend" in reason:
        return "trend_alignment_deny"
    if "pm outside flexible core ev zone" in reason:
        return "pm_outside_flex_deny"
    if reason == "core ev disabled":
        return "core_ev_disabled"
    if reason.startswith("core ev deny"):
        return "base_deny"
    return "other"


def combo_bucket(signal: dict, *parts) -> str:
    return " | ".join(part(signal) for part in parts)


def bucket_stable_ticks(signal: dict) -> str:
    ticks = int(signal.get("stable_ticks", 0) or 0)
    if ticks <= 0:
        return "0"
    if ticks == 1:
        return "1"
    if ticks == 2:
        return "2"
    if ticks == 3:
        return "3"
    if ticks == 4:
        return "4"
    return ">=5"


def bucket_recent_streak(signal: dict) -> str:
    streak = int(signal.get("recent_5m_streak", 0) or 0)
    if streak <= 0:
        return "0"
    if streak == 1:
        return "1"
    if streak == 2:
        return "2"
    return ">=3"


def bucket_window_progress(signal: dict) -> str:
    progress = float(signal.get("window_progress_pct", 0) or 0)
    if progress < 0.20:
        return "0-19%"
    if progress < 0.40:
        return "20-39%"
    if progress < 0.60:
        return "40-59%"
    if progress < 0.80:
        return "60-79%"
    return "80-100%"


def bucket_shadow_progress_value(progress: float | None) -> str:
    if progress is None:
        return "unknown"
    if progress < 0.20:
        return "0-19%"
    if progress < 0.40:
        return "20-39%"
    if progress < 0.60:
        return "40-59%"
    if progress < 0.80:
        return "60-79%"
    return "80-100%"


def bucket_shadow_first_candidate_progress(signal: dict) -> str:
    raw = signal.get("shadow_first_candidate_progress_pct")
    progress = float(raw) if raw is not None else None
    return bucket_shadow_progress_value(progress)


def bucket_shadow_first_live_decision_progress(signal: dict) -> str:
    raw = signal.get("shadow_first_live_decision_progress_pct")
    progress = float(raw) if raw is not None else None
    return bucket_shadow_progress_value(progress)


def bucket_shadow_observation_count(signal: dict) -> str:
    count = int(signal.get("shadow_observation_count", 0) or 0)
    if count <= 0:
        return "0"
    if count == 1:
        return "1"
    if count == 2:
        return "2"
    if count == 3:
        return "3"
    if count <= 5:
        return "4-5"
    return ">=6"


def early_shadow_candidate_label(signal: dict) -> str:
    progress = signal.get("shadow_first_candidate_progress_pct")
    if progress is None:
        return "no_candidate"
    progress = float(progress)
    if progress < 0.60:
        return "candidate_before_60%"
    if progress < 0.70:
        return "candidate_60-69%"
    if progress < 0.80:
        return "candidate_70-79%"
    return "candidate_80-100%"


def bucket_underpricing(signal: dict) -> str:
    score = float(signal.get("underpricing_score", 0) or 0)
    if score < -0.50:
        return "<-0.50"
    if score < -0.10:
        return "-0.50..-0.10"
    if score < 0.10:
        return "-0.10..0.09"
    if score < 0.30:
        return "0.10..0.29"
    if score < 0.50:
        return "0.30..0.49"
    return ">=0.50"


def bucket_gap(signal: dict) -> str:
    gap = float(signal.get("pm_vs_delta_gap", 0) or 0)
    if gap < -0.30:
        return "<-0.30"
    if gap < -0.10:
        return "-0.30..-0.10"
    if gap < 0.0:
        return "-0.10..0.00"
    if gap < 0.10:
        return "0.00..0.09"
    if gap < 0.20:
        return "0.10..0.19"
    return ">=0.20"


def resolve_outcome_pnl(signal: dict) -> float | None:
    if signal.get("entered") and signal.get("realized_pnl") is not None:
        return float(signal.get("realized_pnl", 0) or 0)
    if (not signal.get("entered")) and signal.get("pnl_if_entered") is not None:
        return float(signal.get("pnl_if_entered", 0) or 0)
    return None


def bucket_stable_ticks_value(stable_ticks: int) -> str:
    if stable_ticks <= 0:
        return "0"
    if stable_ticks == 1:
        return "1"
    if stable_ticks == 2:
        return "2"
    if stable_ticks == 3:
        return "3"
    if stable_ticks == 4:
        return "4"
    return ">=5"


def core_l1_pm_bucket(pm_price: float) -> str:
    if pm_price < 0.62:
        return "0.58-0.619"
    return "0.62-0.70"


def core_l1_delta_bucket(delta_pct: float) -> str:
    if delta_pct < 0.010:
        return "<0.010%"
    if delta_pct < 0.030:
        return "0.010-0.029%"
    return ">=0.030%"


def core_l1_time_bucket(time_left: float) -> str:
    if time_left < 10:
        return "<10s"
    if time_left < 20:
        return "10-19s"
    if time_left < 30:
        return "20-29s"
    if time_left < 60:
        return "30-59s"
    if time_left < 120:
        return "60-119s"
    return "120-300s"


def core_pm_eligible(signal: dict, pm_min: float, pm_max: float) -> bool:
    pm = float(signal.get("pm", 0) or 0)
    return pm_min <= pm <= pm_max


def derive_core_signal_tier(signal: dict) -> str:
    explicit = str(signal.get("signal_tier", "") or "").strip().lower()
    if explicit in {"candidate", "trade", "observe"}:
        return explicit

    pm = float(signal.get("pm", 0) or 0)
    delta_pct = float(signal.get("delta", 0) or 0)
    confidence = float(signal.get("confidence", 0) or 0)
    indicator_confirm = float(signal.get("indicator_confirm", 0) or 0)
    time_left = float(signal.get("time_left", 0) or 0)

    if time_left < 10 or time_left > 305:
        return "observe"
    if pm < 0.58 or pm > 0.72:
        return "observe"
    if pm >= 0.60 and pm <= 0.68 and delta_pct >= 0.015 and confidence >= 0.10 and indicator_confirm >= 0.15:
        return "trade"
    if pm >= 0.58 and pm <= 0.70 and delta_pct >= 0.008 and indicator_confirm >= 0.0:
        return "candidate"
    if pm >= 0.60 and pm <= 0.70 and delta_pct >= 0.012 and indicator_confirm > 0:
        return "candidate"
    if pm >= 0.58 and pm <= 0.68 and confidence >= 0.05:
        return "candidate"
    return "observe"


def derive_core_trend_aligned(signal: dict) -> bool:
    if signal.get("trend_aligned") is not None:
        return bool(signal.get("trend_aligned"))
    tier_reason = str(signal.get("signal_tier_reason", "") or "").strip().lower()
    if "no trend or 1m support" in tier_reason:
        return False
    return True


def derive_core_trend_conflict(signal: dict) -> bool:
    if signal.get("trend_conflict") is not None:
        return bool(signal.get("trend_conflict"))
    reason = str(signal.get("reason", "") or "").strip().lower()
    return "direction mismatch" in reason


def core_hard_eligible(signal: dict, pm_min: float, pm_max: float) -> bool:
    if not core_pm_eligible(signal, pm_min, pm_max):
        return False
    if not derive_core_trend_aligned(signal):
        return False
    if derive_core_trend_conflict(signal):
        return False
    if derive_core_signal_tier(signal) not in {"candidate", "trade"}:
        return False
    if str(signal.get("shadow_live_decision", "neutral") or "neutral") == "deny":
        return False
    if bool(signal.get("reversal_flag")) and not bool(signal.get("pullback_recovered")):
        return False
    return True


def core_bucket_keys(signal: dict) -> dict[str, str]:
    pm_bucket = bucket_pm(float(signal.get("pm", 0) or 0))
    delta_bucket = bucket_delta(float(signal.get("delta", 0) or 0))
    time_bucket = bucket_time_left(float(signal.get("time_left", 0) or 0))
    l1_pm_bucket = core_l1_pm_bucket(float(signal.get("pm", 0) or 0))
    l1_delta_bucket = core_l1_delta_bucket(float(signal.get("delta", 0) or 0))
    l1_time_bucket = core_l1_time_bucket(float(signal.get("time_left", 0) or 0))
    confirm_bucket = bucket_indicator_confirm(signal)
    regime = market_regime_label(signal)
    stable_bucket = bucket_stable_ticks_value(int(signal.get("stable_ticks", 0) or 0))
    profile = shadow_profile_label(signal)
    tier = derive_core_signal_tier(signal)
    trend_flag = "trend_ok" if derive_core_trend_aligned(signal) and not derive_core_trend_conflict(signal) else "trend_bad"
    return {
        "L1": " | ".join(["L1", f"pm:{l1_pm_bucket}", f"delta:{l1_delta_bucket}", f"time:{l1_time_bucket}", trend_flag]),
        "L2": " | ".join(["L2", f"pm:{pm_bucket}", f"delta:{delta_bucket}", f"time:{time_bucket}", f"regime:{regime}", f"stable:{stable_bucket}", f"tier:{tier}"]),
        "L3": " | ".join(["L3", f"pm:{pm_bucket}", f"delta:{delta_bucket}", f"time:{time_bucket}", f"confirm:{confirm_bucket}", f"regime:{regime}", f"stable:{stable_bucket}", f"profile:{profile}", f"tier:{tier}"]),
    }


def build_core_ev_rulebook(signals: list[dict], args) -> dict:
    source_label = getattr(args, "core_ev_source_label", "signals")
    resolved = []
    for signal in signals:
        outcome_pnl = resolve_outcome_pnl(signal)
        if outcome_pnl is None:
            continue
        if not core_hard_eligible(signal, args.core_pm_min, args.core_pm_max):
            continue
        resolved.append({
            **signal,
            "realized_pnl": outcome_pnl,
            "won": outcome_pnl > 0,
        })

    recent_cutoff = datetime.now(timezone.utc).timestamp() - args.core_recent_hours * 3600 if args.core_recent_hours > 0 else None
    groups: dict[str, list[dict]] = defaultdict(list)
    for signal in resolved:
        for key in core_bucket_keys(signal).values():
            groups[key].append(signal)

    mins = {
        "L1": args.core_min_bucket_trades_l1,
        "L2": args.core_min_bucket_trades_l2,
        "L3": args.core_min_bucket_trades_l3,
    }
    buckets = {}
    allow_rows = []
    deny_rows = []
    watch_rows = []
    for key, items in groups.items():
        level = key.split(" | ", 1)[0]
        min_trades = mins.get(level, args.core_min_bucket_trades_l2)
        trades = len(items)
        wins = sum(1 for item in items if item.get("won") is True)
        total_pnl = sum(float(item.get("realized_pnl", 0) or 0) for item in items)
        total_amount = sum(float(item.get("amount", args.default_amount) or args.default_amount) for item in items)
        roi = (total_pnl / total_amount * 100) if total_amount else 0.0
        win_rate = (wins / trades * 100) if trades else 0.0
        recent_items = []
        for item in items:
            if recent_cutoff is None:
                recent_items.append(item)
                continue
            dt = parse_ts(str(item.get("timestamp", "")))
            if dt and dt.timestamp() >= recent_cutoff:
                recent_items.append(item)
        recent_trades = len(recent_items)
        recent_pnl = sum(float(item.get("realized_pnl", 0) or 0) for item in recent_items)
        recent_amount = sum(float(item.get("amount", args.default_amount) or args.default_amount) for item in recent_items)
        recent_roi = (recent_pnl / recent_amount * 100) if recent_amount else 0.0

        if trades < min_trades:
            decision = "unknown"
        elif roi <= 0 or total_pnl <= 0:
            decision = "deny"
        elif recent_trades >= args.core_min_recent_trades and recent_roi < 0:
            decision = "watch"
        elif trades >= min_trades + 3 and roi >= args.core_strong_roi_min and recent_roi >= 0:
            decision = "strong_allow"
        else:
            decision = "allow"

        buckets[key] = {
            "level": level,
            "trades": trades,
            "wins": wins,
            "losses": trades - wins,
            "win_rate": round(win_rate, 4),
            "total_pnl": round(total_pnl, 4),
            "roi": round(roi, 4),
            "recent_trades": recent_trades,
            "recent_roi": round(recent_roi, 4),
            "decision": decision,
        }

        row = {"key": key, **buckets[key]}
        if decision in {"allow", "strong_allow"}:
            allow_rows.append(row)
        elif decision == "deny":
            deny_rows.append(row)
        elif decision == "watch":
            watch_rows.append(row)

    allow_rows.sort(key=lambda row: (row["roi"], row["trades"], row["win_rate"]), reverse=True)
    deny_rows.sort(key=lambda row: (row["roi"], -row["trades"]))
    watch_rows.sort(key=lambda row: (row["recent_roi"], row["roi"], row["trades"]))

    summary = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source_type": source_label,
        "source_signals": len(signals),
        "resolved_eligible_signals": len(resolved),
        "core_pm_min": args.core_pm_min,
        "core_pm_max": args.core_pm_max,
        "recent_hours": args.core_recent_hours,
        "min_bucket_trades": mins,
        "min_recent_trades": args.core_min_recent_trades,
        "strong_roi_min": args.core_strong_roi_min,
        "allow_bucket_count": len(allow_rows),
        "deny_bucket_count": len(deny_rows),
        "watch_bucket_count": len(watch_rows),
        "bucket_count": len(buckets),
    }

    return {
        "generated_at": summary["generated_at"],
        "source_type": summary["source_type"],
        "source_signals": summary["source_signals"],
        "resolved_eligible_signals": summary["resolved_eligible_signals"],
        "core_pm_min": summary["core_pm_min"],
        "core_pm_max": summary["core_pm_max"],
        "recent_hours": summary["recent_hours"],
        "min_bucket_trades": summary["min_bucket_trades"],
        "min_recent_trades": summary["min_recent_trades"],
        "strong_roi_min": summary["strong_roi_min"],
        "summary": summary,
        "allow_rules": allow_rows,
        "deny_rules": deny_rows,
        "watch_rules": watch_rows,
        "buckets": buckets,
    }


def print_core_ev_rulebook_summary(rulebook: dict, top: int) -> None:
    print("=== CORE EV RULEBOOK ===")
    print(f"generated_at: {rulebook.get('generated_at', 'unknown')}")
    print(f"source_type: {rulebook.get('source_type', 'unknown')}")
    print(f"source_signals: {rulebook.get('source_signals', 0)}")
    print(f"resolved_eligible_signals: {rulebook.get('resolved_eligible_signals', 0)}")
    rows = [{"key": key, **stats} for key, stats in rulebook.get("buckets", {}).items()]
    allow_rows = [row for row in rows if row.get("decision") in {"allow", "strong_allow"}]
    deny_rows = [row for row in rows if row.get("decision") == "deny"]
    allow_rows.sort(key=lambda row: (row["roi"], row["trades"], row["win_rate"]), reverse=True)
    deny_rows.sort(key=lambda row: (row["roi"], -row["trades"]))
    print(f"allow-ish buckets: {len(allow_rows)} | deny buckets: {len(deny_rows)}")
    print("Top allow buckets:")
    if not allow_rows:
        print("  None")
    else:
        for row in allow_rows[:top]:
            print(
                f"  {row['key']} | trades={row['trades']} | win_rate={fmt_pct(row['win_rate'])} | "
                f"roi={fmt_pct(row['roi'])} | recent_trades={row['recent_trades']} | recent_roi={fmt_pct(row['recent_roi'])} | {row['decision']}"
            )
    print("Top deny buckets:")
    if not deny_rows:
        print("  None")
    else:
        for row in deny_rows[:top]:
            print(
                f"  {row['key']} | trades={row['trades']} | win_rate={fmt_pct(row['win_rate'])} | "
                f"roi={fmt_pct(row['roi'])} | recent_trades={row['recent_trades']} | recent_roi={fmt_pct(row['recent_roi'])}"
            )


def shadow_pm_eligible(signal: dict, pm_floor: float) -> bool:
    return float(signal.get("pm", 0) or 0) >= pm_floor


def select_shadow_sample(resolved: list[dict], shadow_pm_floor: float) -> tuple[list[dict], str]:
    cleaned = [signal for signal in resolved if shadow_pm_eligible(signal, shadow_pm_floor)]
    if cleaned:
        return cleaned, "cleaned"
    return resolved, "raw"


def is_real_shadow_candidate(signal: dict) -> bool:
    if signal.get("shadow_entry_candidate") is True:
        return True
    profile = str(signal.get("shadow_entry_profile", "none") or "none")
    if profile != "none":
        return True
    first_profile = str(signal.get("shadow_first_candidate_profile", "none") or "none")
    return first_profile != "none"


def shadow_similarity_core_label(signal: dict) -> str:
    return combo_bucket(
        signal,
        shadow_profile_label,
        market_regime_label,
        bucket_stable_ticks,
        bucket_window_progress,
    )


def shadow_similarity_extended_label(signal: dict) -> str:
    return combo_bucket(
        signal,
        shadow_profile_label,
        market_regime_label,
        bucket_stable_ticks,
        bucket_window_progress,
        bucket_underpricing,
        bucket_gap,
    )


def shadow_market_context_label(signal: dict) -> str:
    return combo_bucket(
        signal,
        lambda s: bucket_pm(float(s.get("pm", 0) or 0)),
        lambda s: bucket_delta(float(s.get("delta", 0) or 0)),
        lambda s: bucket_time_left(float(s.get("time_left", 0) or 0)),
    )


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


def print_indicator_confirm_report(signals: list[dict], min_trades: int) -> None:
    print("\n=== 1M CONFIRM REPORT ===")
    entered = [signal for signal in signals if signal.get("entered")]
    settled = [signal for signal in entered if signal.get("realized_pnl") is not None]
    if not settled:
        print("No settled entered trades to analyze.")
        return

    if not any(signal.get("indicator_confirm") is not None for signal in settled):
        print("No settled trades contain 1m confirm data yet.")
        return

    rows = filter_rows(summarize_trades(settled, bucket_indicator_confirm), min_trades)
    if not rows:
        print(f"No 1m confirm buckets with >= {min_trades} settled trades")
        return

    for row in rows:
        matching = [signal for signal in settled if bucket_indicator_confirm(signal) == row["key"]]
        avg_confirm = avg([float(signal.get("indicator_confirm", 0) or 0) for signal in matching])
        avg_conf = avg([float(signal.get("confidence", 0) or 0) * 100 for signal in matching])
        avg_pm = avg([float(signal.get("pm", 0) or 0) for signal in matching])
        print(
            f"  {row['key']:<12} trades={row['count']:<3} win_rate={fmt_pct(row['win_rate']):<7} "
            f"pnl={fmt_money(row['total_pnl']):<10} avg_1m={avg_confirm:+.2f} "
            f"conf={avg_conf:.1f}% pm={avg_pm:.3f}"
        )


def print_indicator_reason_report(signals: list[dict], min_trades: int, top: int) -> None:
    print("\n=== INDICATOR REASONS ===")
    skipped = [signal for signal in signals if not signal.get("entered")]
    resolved = [signal for signal in skipped if signal.get("pnl_if_entered") is not None]
    if not resolved:
        print("No resolved skipped signals with pnl_if_entered yet.")
        return

    normalized = [
        {
            **signal,
            "realized_pnl": float(signal.get("pnl_if_entered", 0) or 0),
            "won": float(signal.get("pnl_if_entered", 0) or 0) > 0,
        }
        for signal in resolved
    ]

    reason_rows = filter_rows(summarize_trades(normalized, indicator_reason_label), min_trades)
    print_table("By indicator_reason", reason_rows, top)

    pm_reason_rows = filter_rows(
        summarize_trades(
            normalized,
            lambda signal: combo_bucket(signal, lambda s: bucket_pm(float(s.get("pm", 0) or 0)), indicator_reason_label),
        ),
        min_trades,
    )
    print_table("By PM x indicator_reason", pm_reason_rows, top)


def print_normal_pm_zone_report(signals: list[dict], min_trades: int, top: int) -> None:
    print("\n=== NORMAL PM ZONE ONLY (0.58-0.70) ===")
    skipped = [signal for signal in signals if not signal.get("entered")]
    resolved = [signal for signal in skipped if signal.get("pnl_if_entered") is not None and in_normal_pm_zone(signal)]
    if not resolved:
        print("No resolved skipped signals in PM 0.58-0.70 yet.")
        return

    normalized = [
        {
            **signal,
            "realized_pnl": float(signal.get("pnl_if_entered", 0) or 0),
            "won": float(signal.get("pnl_if_entered", 0) or 0) > 0,
        }
        for signal in resolved
    ]

    reports = [
        ("By indicator_reason", indicator_reason_label),
        ("By delta", lambda signal: bucket_delta(float(signal.get("delta", 0) or 0))),
        ("By trend_conflict", lambda signal: "trend_conflict" if signal.get("trend_conflict") else "trend_ok"),
        ("By signal_tier_reason", signal_tier_reason_label),
    ]

    for title, key_fn in reports:
        rows = filter_rows(summarize_trades(normalized, key_fn), min_trades)
        print_table(title, rows, top)


def print_core_ev_pm_expansion_report(signals: list[dict], min_trades: int, top: int) -> None:
    print("\n=== CORE EV PM EXPANSION (>0.70) ===")
    resolved = []
    for signal in signals:
        pnl = resolve_outcome_pnl(signal)
        if pnl is None:
            continue
        pm = float(signal.get("pm", 0) or 0)
        if pm <= 0.70:
            continue
        if pm > 0.95:
            continue
        if not derive_core_trend_aligned(signal):
            continue
        if derive_core_trend_conflict(signal):
            continue
        if derive_core_signal_tier(signal) not in {"candidate", "trade"}:
            continue
        if str(signal.get("shadow_live_decision", "neutral") or "neutral") == "deny":
            continue
        if bool(signal.get("reversal_flag")) and not bool(signal.get("pullback_recovered")):
            continue
        resolved.append({
            **signal,
            "realized_pnl": pnl,
            "won": pnl > 0,
        })

    if not resolved:
        print("No resolved high-PM core-like signals yet.")
        return

    print(
        f"signals={len(resolved)} | total={fmt_money(sum(float(s.get('realized_pnl', 0) or 0) for s in resolved))} | "
        f"avg_pm={avg([float(s.get('pm', 0) or 0) for s in resolved]):.3f} | avg_time={avg([float(s.get('time_left', 0) or 0) for s in resolved]):.1f}s"
    )

    reports = [
        ("By PM price", lambda signal: bucket_pm(float(signal.get("pm", 0) or 0))),
        ("By time left", lambda signal: bucket_time_left(float(signal.get("time_left", 0) or 0))),
        (
            "By PM x time",
            lambda signal: combo_bucket(
                signal,
                lambda s: bucket_pm(float(s.get("pm", 0) or 0)),
                lambda s: bucket_time_left(float(s.get("time_left", 0) or 0)),
            ),
        ),
        (
            "By PM x delta",
            lambda signal: combo_bucket(
                signal,
                lambda s: bucket_pm(float(s.get("pm", 0) or 0)),
                lambda s: bucket_delta(float(s.get("delta", 0) or 0)),
            ),
        ),
    ]

    for title, key_fn in reports:
        rows = filter_rows(summarize_trades(resolved, key_fn), min_trades)
        print_table(title, rows, top)


def print_core_ev_timing_report(signals: list[dict], min_trades: int, top: int) -> None:
    print("\n=== CORE EV TIMING REPORT ===")
    resolved = []
    for signal in signals:
        pnl = resolve_outcome_pnl(signal)
        if pnl is None:
            continue
        if not core_hard_eligible(signal, 0.58, 0.95):
            continue
        resolved.append({
            **signal,
            "realized_pnl": pnl,
            "won": pnl > 0,
        })

    if not resolved:
        print("No resolved core-like timing samples yet.")
        return

    print(
        f"signals={len(resolved)} | total={fmt_money(sum(float(s.get('realized_pnl', 0) or 0) for s in resolved))} | "
        f"avg_pm={avg([float(s.get('pm', 0) or 0) for s in resolved]):.3f} | avg_time={avg([float(s.get('time_left', 0) or 0) for s in resolved]):.1f}s"
    )
    print("Note: this report only reflects time slices the bot actually observed historically, not the full 0-300s window.")

    reports = [
        ("By time left", lambda signal: bucket_time_left(float(signal.get("time_left", 0) or 0))),
        ("By PM x time", lambda signal: combo_bucket(signal, lambda s: bucket_pm(float(s.get("pm", 0) or 0)), lambda s: bucket_time_left(float(s.get("time_left", 0) or 0)))),
        ("By delta x time", lambda signal: combo_bucket(signal, lambda s: bucket_delta(float(s.get("delta", 0) or 0)), lambda s: bucket_time_left(float(s.get("time_left", 0) or 0)))),
    ]

    for title, key_fn in reports:
        rows = filter_rows(summarize_trades(resolved, key_fn), min_trades)
        print_table(title, rows, top)


def print_full_window_core_ev_report(signals: list[dict], min_trades: int, top: int) -> None:
    print("\n=== FULL-WINDOW CORE EV REPORT ===")
    resolved = []
    for signal in signals:
        pnl = resolve_outcome_pnl(signal)
        if pnl is None:
            continue
        if not core_hard_eligible(signal, 0.58, 0.70):
            continue
        resolved.append({
            **signal,
            "realized_pnl": pnl,
            "won": pnl > 0,
        })

    if not resolved:
        print("No resolved full-window core-like samples yet.")
        return

    print(
        f"signals={len(resolved)} | total={fmt_money(sum(float(s.get('realized_pnl', 0) or 0) for s in resolved))} | "
        f"avg_pm={avg([float(s.get('pm', 0) or 0) for s in resolved]):.3f} | avg_time={avg([float(s.get('time_left', 0) or 0) for s in resolved]):.1f}s"
    )

    reports = [
        ("By time left", lambda signal: bucket_time_left(float(signal.get("time_left", 0) or 0))),
        ("By L1 time", lambda signal: core_l1_time_bucket(float(signal.get("time_left", 0) or 0))),
        (
            "By PM x time",
            lambda signal: combo_bucket(
                signal,
                lambda s: bucket_pm(float(s.get("pm", 0) or 0)),
                lambda s: bucket_time_left(float(s.get("time_left", 0) or 0)),
            ),
        ),
        (
            "By delta x time",
            lambda signal: combo_bucket(
                signal,
                lambda s: bucket_delta(float(s.get("delta", 0) or 0)),
                lambda s: bucket_time_left(float(s.get("time_left", 0) or 0)),
            ),
        ),
    ]

    for title, key_fn in reports:
        rows = filter_rows(summarize_trades(resolved, key_fn), min_trades)
        print_table(title, rows, top)


def print_execution_failed_report(signals: list[dict], min_trades: int, top: int) -> None:
    print("\n=== EXECUTION FAILED PROFILE ===")
    failed = [signal for signal in signals if str(signal.get("reason", "")) == "execution failed"]
    resolved = [signal for signal in failed if signal.get("pnl_if_entered") is not None]
    if not resolved:
        print("No resolved execution-failed signals yet.")
        return

    normalized = [
        {
            **signal,
            "realized_pnl": float(signal.get("pnl_if_entered", 0) or 0),
            "won": float(signal.get("pnl_if_entered", 0) or 0) > 0,
        }
        for signal in resolved
    ]

    total = sum(float(signal.get("realized_pnl", 0) or 0) for signal in normalized)
    wins = sum(1 for signal in normalized if signal.get("won") is True)
    losses = sum(1 for signal in normalized if signal.get("won") is False)
    print(f"trades={len(normalized)} | would_win={wins} | would_lose={losses} | total_if_entered={fmt_money(total)}")

    reports = [
        ("By failure type", lambda signal: str(signal.get("execution_failure_type", "") or "unknown")),
        (
            "By failure type x order status",
            lambda signal: combo_bucket(
                signal,
                lambda s: str(s.get("execution_failure_type", "") or "unknown"),
                lambda s: str(s.get("execution_order_status", "") or "none"),
            ),
        ),
        ("By PM price", lambda signal: bucket_pm(float(signal.get("pm", 0) or 0))),
        ("By delta", lambda signal: bucket_delta(float(signal.get("delta", 0) or 0))),
        ("By indicator_reason", indicator_reason_label),
        ("By time left", lambda signal: bucket_time_left(float(signal.get("time_left", 0) or 0))),
    ]

    for title, key_fn in reports:
        rows = filter_rows(summarize_trades(normalized, key_fn), min_trades)
        print_table(title, rows, top)


def print_core_ev_causal_report(signals: list[dict], min_trades: int, top: int) -> None:
    print("\n=== CORE EV CAUSAL REPORT ===")
    core_ev_signals = [signal for signal in signals if signal.get("core_ev_decision") is not None]
    if not core_ev_signals:
        print("No signals with core_ev_decision fields yet.")
        return

    entered = [signal for signal in core_ev_signals if signal.get("entered")]
    settled = [signal for signal in entered if signal.get("realized_pnl") is not None]
    failed = [signal for signal in core_ev_signals if str(signal.get("reason", "") or "") == "execution failed"]
    failed_resolved = [signal for signal in failed if signal.get("pnl_if_entered") is not None]

    print(
        f"core_ev_signals={len(core_ev_signals)} | entered={len(entered)} | "
        f"settled={len(settled)} | execution_failed={len(failed)} | resolved_failed={len(failed_resolved)}"
    )

    decision_counts: dict[str, int] = defaultdict(int)
    branch_counts: dict[str, int] = defaultdict(int)
    for signal in core_ev_signals:
        decision_counts[core_ev_decision_label(signal)] += 1
        branch_counts[core_ev_branch_label(signal)] += 1

    print("Runtime decision mix:")
    for key, count in sorted(decision_counts.items(), key=lambda item: (-item[1], item[0])):
        share = count / len(core_ev_signals) * 100 if core_ev_signals else 0.0
        print(f"  {key:<20} count={count:<4} share={fmt_pct(share)}")

    print("Runtime branch mix:")
    for key, count in sorted(branch_counts.items(), key=lambda item: (-item[1], item[0]))[:12]:
        share = count / len(core_ev_signals) * 100 if core_ev_signals else 0.0
        print(f"  {key:<32} count={count:<4} share={fmt_pct(share)}")

    if settled:
        reports = [
            ("Settled by core_ev_decision", core_ev_decision_label),
            ("Settled by core_ev_branch", core_ev_branch_label),
            ("Settled by core_ev_reason", core_ev_reason_label),
            ("Settled by PM x core_ev_decision", lambda signal: combo_bucket(signal, lambda s: bucket_pm(float(s.get("pm", 0) or 0)), core_ev_decision_label)),
            ("Settled by PM x core_ev_branch", lambda signal: combo_bucket(signal, lambda s: bucket_pm(float(s.get("pm", 0) or 0)), core_ev_branch_label)),
            ("Settled by time x core_ev_decision", lambda signal: combo_bucket(signal, lambda s: bucket_time_left(float(s.get("time_left", 0) or 0)), core_ev_decision_label)),
            ("Settled by time x core_ev_branch", lambda signal: combo_bucket(signal, lambda s: bucket_time_left(float(s.get("time_left", 0) or 0)), core_ev_branch_label)),
            ("Settled by PM x time x branch", lambda signal: combo_bucket(signal, lambda s: bucket_pm(float(s.get("pm", 0) or 0)), lambda s: bucket_time_left(float(s.get("time_left", 0) or 0)), core_ev_branch_label)),
        ]
        for title, key_fn in reports:
            rows = filter_rows(summarize_trades(settled, key_fn), min_trades)
            print_table(title, rows, top)
    else:
        print("No settled entered core EV trades yet.")

    if failed_resolved:
        normalized = [
            {
                **signal,
                "realized_pnl": float(signal.get("pnl_if_entered", 0) or 0),
                "won": float(signal.get("pnl_if_entered", 0) or 0) > 0,
            }
            for signal in failed_resolved
        ]
        reports = [
            ("Execution-failed by core_ev_decision", core_ev_decision_label),
            ("Execution-failed by core_ev_branch", core_ev_branch_label),
            ("Execution-failed by PM x branch", lambda signal: combo_bucket(signal, lambda s: bucket_pm(float(s.get("pm", 0) or 0)), core_ev_branch_label)),
            ("Execution-failed by time x branch", lambda signal: combo_bucket(signal, lambda s: bucket_time_left(float(s.get("time_left", 0) or 0)), core_ev_branch_label)),
        ]
        for title, key_fn in reports:
            rows = filter_rows(summarize_trades(normalized, key_fn), min_trades)
            print_table(title, rows, top)
    else:
        print("No resolved execution-failed core EV signals yet.")


def print_shadow_entry_report(signals: list[dict], min_trades: int, top: int, shadow_pm_floor: float) -> None:
    print("\n=== SHADOW ENTRY PROFILES ===")
    shadow_annotated = [
        signal for signal in signals
        if any(
            key in signal
            for key in (
                "shadow_entry_candidate",
                "shadow_entry_profile",
                "shadow_entry_score",
                "stable_ticks",
                "market_regime",
            )
        )
    ]
    if not shadow_annotated:
        print("No shadow-annotated signals yet. Collect new live logs after the crypto_bot.py shadow patch.")
        return

    resolved = []
    for signal in shadow_annotated:
        outcome_pnl = resolve_outcome_pnl(signal)
        if outcome_pnl is None:
            continue
        resolved.append({
            **signal,
            "realized_pnl": outcome_pnl,
            "won": outcome_pnl > 0,
        })

    if not resolved:
        print("No shadow-annotated resolved signals yet.")
        return

    sample, sample_label = select_shadow_sample(resolved, shadow_pm_floor)
    print(
        f"economic_pm_floor>={shadow_pm_floor:.3f} | raw_resolved={len(resolved)} | "
        f"sample={sample_label} ({len(sample)})"
    )

    candidates = [signal for signal in sample if signal.get("shadow_entry_candidate") is True]
    entered_candidates = [signal for signal in candidates if signal.get("entered")]
    skipped_candidates = [signal for signal in candidates if not signal.get("entered")]
    total = sum(float(signal.get("realized_pnl", 0) or 0) for signal in candidates)
    wins = sum(1 for signal in candidates if signal.get("won") is True)
    losses = sum(1 for signal in candidates if signal.get("won") is False)

    print(
        f"shadow_annotated={len(shadow_annotated)} | resolved={len(resolved)} | "
        f"candidates={len(candidates)} | entered_candidates={len(entered_candidates)} | "
        f"skipped_candidates={len(skipped_candidates)}"
    )
    if candidates:
        print(
            f"candidate_outcomes: wins={wins} | losses={losses} | total_pnl={fmt_money(total)}"
        )
    else:
        print("No resolved shadow candidates yet.")

    reports = [
        ("By shadow profile", shadow_profile_label),
        ("By shadow reason", shadow_reason_label),
        ("By market regime", market_regime_label),
        ("By stable ticks", bucket_stable_ticks),
        ("By recent 5m streak", bucket_recent_streak),
        ("By window progress", bucket_window_progress),
        ("By underpricing score", bucket_underpricing),
        ("By PM vs delta gap", bucket_gap),
        (
            "By profile x regime",
            lambda signal: combo_bucket(signal, shadow_profile_label, market_regime_label),
        ),
        (
            "By profile x stable ticks",
            lambda signal: combo_bucket(signal, shadow_profile_label, bucket_stable_ticks),
        ),
        (
            "By candidate flag x entered",
            lambda signal: combo_bucket(
                signal,
                lambda s: "candidate" if s.get("shadow_entry_candidate") else "non_candidate",
                lambda s: "entered" if s.get("entered") else "skipped",
            ),
        ),
    ]

    for title, key_fn in reports:
        rows = filter_rows(summarize_trades(sample, key_fn), min_trades)
        print_table(title, rows, top)

    if skipped_candidates:
        print("\n=== SHADOW CANDIDATE COUNTERFACTUALS ===")
        candidate_reports = [
            ("Skipped candidates by profile", shadow_profile_label),
            ("Skipped candidates by regime", market_regime_label),
            (
                "Skipped candidates by profile x regime",
                lambda signal: combo_bucket(signal, shadow_profile_label, market_regime_label),
            ),
        ]
        for title, key_fn in candidate_reports:
            rows = filter_rows(summarize_trades(skipped_candidates, key_fn), min_trades)
            print_table(title, rows, top)


def print_shadow_similarity_report(signals: list[dict], min_trades: int, top: int, shadow_pm_floor: float) -> None:
    print("\n=== SHADOW HISTORICAL SIMILARITY ===")
    shadow_annotated = [
        signal for signal in signals
        if any(
            key in signal
            for key in (
                "shadow_entry_candidate",
                "shadow_entry_profile",
                "shadow_entry_score",
                "stable_ticks",
                "market_regime",
            )
        )
    ]
    if not shadow_annotated:
        print("No shadow-annotated signals yet. Historical similarity needs new post-patch logs.")
        return

    resolved = []
    for signal in shadow_annotated:
        outcome_pnl = resolve_outcome_pnl(signal)
        if outcome_pnl is None:
            continue
        resolved.append({
            **signal,
            "realized_pnl": outcome_pnl,
            "won": outcome_pnl > 0,
        })

    if not resolved:
        print("No shadow-annotated resolved signals yet.")
        return

    resolved_sample, sample_label = select_shadow_sample(resolved, shadow_pm_floor)
    print(
        f"economic_pm_floor>={shadow_pm_floor:.3f} | raw_resolved={len(resolved)} | "
        f"sample={sample_label} ({len(resolved_sample)})"
    )

    candidate_resolved = [signal for signal in resolved_sample if is_real_shadow_candidate(signal)]
    if not candidate_resolved:
        print("No cleaned real shadow candidates yet; skipping similarity clustering to avoid profile=none noise.")
        return
    sample = candidate_resolved
    print(f"Using {len(sample)} shadow candidates for similarity clustering.")

    reports = [
        ("Core similarity cluster", shadow_similarity_core_label),
        ("Extended similarity cluster", shadow_similarity_extended_label),
        ("Market context cluster", shadow_market_context_label),
        (
            "Profile x regime x PM",
            lambda signal: combo_bucket(
                signal,
                shadow_profile_label,
                market_regime_label,
                lambda s: bucket_pm(float(s.get("pm", 0) or 0)),
            ),
        ),
        (
            "Profile x streak x underpricing",
            lambda signal: combo_bucket(
                signal,
                shadow_profile_label,
                bucket_recent_streak,
                bucket_underpricing,
            ),
        ),
    ]

    for title, key_fn in reports:
        rows = filter_rows(summarize_trades(sample, key_fn), min_trades)
        print_table(title, rows, top)

    print("\n=== SHADOW SIMILARITY SHORTLIST ===")
    shortlist_rows = filter_rows(summarize_trades(sample, shadow_similarity_extended_label), min_trades)
    if not shortlist_rows:
        print(f"No similarity clusters with >= {min_trades} resolved signals yet.")
        return

    best_rows = list(reversed(shortlist_rows[-top:]))
    worst_rows = shortlist_rows[:top]

    print("Best historical analogs:")
    for row in best_rows:
        print(
            f"  {row['key']} | trades={row['count']} | win_rate={fmt_pct(row['win_rate'])} | "
            f"total={fmt_money(row['total_pnl'])} | roi={fmt_pct(row['roi'])} | "
            f"pm={row['avg_pm']:.3f} | delta={row['avg_delta']:.3f}% | t={row['avg_time']:.1f}s"
        )

    print("Worst historical analogs:")
    for row in worst_rows:
        print(
            f"  {row['key']} | trades={row['count']} | win_rate={fmt_pct(row['win_rate'])} | "
            f"total={fmt_money(row['total_pnl'])} | roi={fmt_pct(row['roi'])} | "
            f"pm={row['avg_pm']:.3f} | delta={row['avg_delta']:.3f}% | t={row['avg_time']:.1f}s"
        )


def print_shadow_live_recommendations(signals: list[dict], min_trades: int, top: int, shadow_pm_floor: float) -> None:
    print("\n=== SHADOW LIVE RECOMMENDATIONS ===")
    shadow_annotated = [
        signal for signal in signals
        if any(
            key in signal
            for key in (
                "shadow_entry_candidate",
                "shadow_entry_profile",
                "shadow_entry_score",
                "stable_ticks",
                "market_regime",
            )
        )
    ]
    if not shadow_annotated:
        print("No shadow-annotated signals yet. Cannot build live recommendations.")
        return

    resolved = []
    for signal in shadow_annotated:
        outcome_pnl = resolve_outcome_pnl(signal)
        if outcome_pnl is None:
            continue
        resolved.append({
            **signal,
            "realized_pnl": outcome_pnl,
            "won": outcome_pnl > 0,
        })

    if not resolved:
        print("No shadow-annotated resolved signals yet.")
        return

    resolved_sample, sample_label = select_shadow_sample(resolved, shadow_pm_floor)
    print(
        f"economic_pm_floor>={shadow_pm_floor:.3f} | raw_resolved={len(resolved)} | "
        f"sample={sample_label} ({len(resolved_sample)})"
    )

    candidate_resolved = [signal for signal in resolved_sample if is_real_shadow_candidate(signal)]
    if not candidate_resolved:
        print("No cleaned real shadow candidates yet; skipping allowlist/denylist clustering to avoid profile=none noise.")
        decision_rows = summarize_trades(resolved_sample, shadow_live_decision_label)
        early_watch_min = max(2, min(min_trades, 2))
        print("Observe-only decision summary:")
        decision_filtered = [row for row in decision_rows if row["count"] >= early_watch_min]
        if not decision_filtered:
            print("  No shadow_live_decision sample yet.")
        else:
            for row in list(reversed(decision_filtered[-top:])):
                print(
                    f"  {row['key']} | trades={row['count']} | win_rate={fmt_pct(row['win_rate'])} | "
                    f"total={fmt_money(row['total_pnl'])} | roi={fmt_pct(row['roi'])}"
                )
        print("\nInterpretation:")
        print("  Пока нет очищенных реальных shadow candidates; profile=none решения не считаем shortlist-материалом.")
        return
    sample = candidate_resolved
    print(f"Evaluating {len(sample)} shadow candidates for live shortlist.")

    cluster_rows = summarize_trades(sample, shadow_similarity_extended_label)
    core_rows = summarize_trades(sample, shadow_similarity_core_label)
    profile_rows = summarize_trades(sample, shadow_profile_label)
    decision_rows = summarize_trades(sample, shadow_live_decision_label)

    reliable_min = max(min_trades, 3)
    early_watch_min = max(2, min(min_trades, 2))

    allow_rows = [
        row for row in cluster_rows
        if row["count"] >= reliable_min and row["total_pnl"] > 0 and row["roi"] > 0 and row["win_rate"] >= 55.0
    ]
    strong_allow_rows = [
        row for row in allow_rows
        if row["count"] >= reliable_min + 1 and row["win_rate"] >= 60.0 and row["roi"] >= 5.0
    ]
    deny_rows = [
        row for row in cluster_rows
        if row["count"] >= reliable_min and row["total_pnl"] < 0 and row["roi"] < 0 and row["win_rate"] <= 45.0
    ]
    watch_rows = [
        row for row in cluster_rows
        if early_watch_min <= row["count"] < reliable_min and row["total_pnl"] > 0 and row["win_rate"] >= 50.0
    ]

    print(f"reliable_min={reliable_min} | early_watch_min={early_watch_min}")

    print("Potential allowlist clusters:")
    if not allow_rows:
        print("  None yet.")
    else:
        for row in list(reversed(allow_rows[-top:])):
            print(
                f"  {row['key']} | trades={row['count']} | win_rate={fmt_pct(row['win_rate'])} | "
                f"total={fmt_money(row['total_pnl'])} | roi={fmt_pct(row['roi'])}"
            )

    print("Stronger allowlist clusters:")
    if not strong_allow_rows:
        print("  None yet.")
    else:
        for row in list(reversed(strong_allow_rows[-top:])):
            print(
                f"  {row['key']} | trades={row['count']} | win_rate={fmt_pct(row['win_rate'])} | "
                f"total={fmt_money(row['total_pnl'])} | roi={fmt_pct(row['roi'])}"
            )

    print("Potential denylist clusters:")
    if not deny_rows:
        print("  None yet.")
    else:
        for row in deny_rows[:top]:
            print(
                f"  {row['key']} | trades={row['count']} | win_rate={fmt_pct(row['win_rate'])} | "
                f"total={fmt_money(row['total_pnl'])} | roi={fmt_pct(row['roi'])}"
            )

    print("Promising but under-sampled clusters:")
    if not watch_rows:
        print("  None yet.")
    else:
        for row in list(reversed(watch_rows[-top:])):
            print(
                f"  {row['key']} | trades={row['count']} | win_rate={fmt_pct(row['win_rate'])} | "
                f"total={fmt_money(row['total_pnl'])} | roi={fmt_pct(row['roi'])}"
            )

    print("Observe-only decision summary:")
    decision_filtered = [row for row in decision_rows if row["count"] >= early_watch_min]
    if not decision_filtered:
        print("  No shadow_live_decision sample yet.")
    else:
        for row in list(reversed(decision_filtered[-top:])):
            print(
                f"  {row['key']} | trades={row['count']} | win_rate={fmt_pct(row['win_rate'])} | "
                f"total={fmt_money(row['total_pnl'])} | roi={fmt_pct(row['roi'])}"
            )

    print("\nContext summary by core cluster:")
    core_filtered = [row for row in core_rows if row["count"] >= reliable_min]
    if not core_filtered:
        print("  No core clusters with enough resolved samples yet.")
    else:
        for row in list(reversed(core_filtered[-top:])):
            print(
                f"  {row['key']} | trades={row['count']} | win_rate={fmt_pct(row['win_rate'])} | "
                f"total={fmt_money(row['total_pnl'])} | roi={fmt_pct(row['roi'])}"
            )

    print("Profile summary:")
    profile_filtered = [row for row in profile_rows if row["count"] >= early_watch_min]
    if not profile_filtered:
        print("  No profile-level sample yet.")
    else:
        for row in list(reversed(profile_filtered[-top:])):
            print(
                f"  {row['key']} | trades={row['count']} | win_rate={fmt_pct(row['win_rate'])} | "
                f"total={fmt_money(row['total_pnl'])} | roi={fmt_pct(row['roi'])}"
            )

    print("\nInterpretation:")
    print("  allowlist = кластеры, которые уже можно рассматривать как кандидатов для будущего live-входа.")
    print("  denylist  = кластеры, которые лучше явно блокировать, если паттерн повторяется.")
    print("  watchlist = кластеры выглядят интересно, но данных пока мало; это ещё не правило для live.")


def print_shadow_live_decision_report(signals: list[dict], min_trades: int, top: int, shadow_pm_floor: float) -> None:
    print("\n=== SHADOW LIVE DECISION REPORT ===")
    shadow_live_signals = [
        signal for signal in signals
        if "shadow_live_decision" in signal or "shadow_live_reason" in signal
    ]
    if not shadow_live_signals:
        print("No shadow live decision data yet. Collect new post-patch bot logs.")
        return

    resolved = []
    for signal in shadow_live_signals:
        outcome_pnl = resolve_outcome_pnl(signal)
        if outcome_pnl is None:
            continue
        resolved.append({
            **signal,
            "realized_pnl": outcome_pnl,
            "won": outcome_pnl > 0,
        })

    if not resolved:
        print("No resolved shadow live decision signals yet.")
        return

    sample, sample_label = select_shadow_sample(resolved, shadow_pm_floor)
    print(
        f"economic_pm_floor>={shadow_pm_floor:.3f} | raw_resolved={len(resolved)} | "
        f"sample={sample_label} ({len(sample)})"
    )

    reports = [
        ("By shadow live mode", shadow_live_mode_label),
        ("By shadow live decision", shadow_live_decision_label),
        ("By shadow live reason", shadow_live_reason_label),
        (
            "By mode x decision",
            lambda signal: combo_bucket(signal, shadow_live_mode_label, shadow_live_decision_label),
        ),
        (
            "By decision x profile",
            lambda signal: combo_bucket(signal, shadow_live_decision_label, shadow_profile_label),
        ),
        (
            "By decision x regime",
            lambda signal: combo_bucket(signal, shadow_live_decision_label, market_regime_label),
        ),
        (
            "By decision x stable ticks",
            lambda signal: combo_bucket(signal, shadow_live_decision_label, bucket_stable_ticks),
        ),
    ]

    for title, key_fn in reports:
        rows = filter_rows(summarize_trades(sample, key_fn), min_trades)
        print_table(title, rows, top)


def print_shadow_early_timing_report(signals: list[dict], min_trades: int, top: int, shadow_pm_floor: float) -> None:
    print("\n=== SHADOW EARLY TIMING REPORT ===")
    early_shadow_signals = [
        signal for signal in signals
        if any(
            key in signal
            for key in (
                "shadow_observation_count",
                "shadow_first_candidate_progress_pct",
                "shadow_first_live_decision_progress_pct",
                "shadow_max_score",
            )
        )
    ]
    if not early_shadow_signals:
        print("No early shadow timing data yet. Collect new post-patch bot logs.")
        return

    resolved = []
    for signal in early_shadow_signals:
        outcome_pnl = resolve_outcome_pnl(signal)
        if outcome_pnl is None:
            continue
        resolved.append({
            **signal,
            "realized_pnl": outcome_pnl,
            "won": outcome_pnl > 0,
        })

    if not resolved:
        print("No resolved early shadow timing signals yet.")
        return

    cleaned = [signal for signal in resolved if shadow_pm_eligible(signal, shadow_pm_floor)]
    print(
        f"economic_pm_floor>={shadow_pm_floor:.3f} | raw_resolved={len(resolved)} | "
        f"cleaned_resolved={len(cleaned)}"
    )

    active_sample, sample_label = select_shadow_sample(resolved, shadow_pm_floor)

    with_candidate = [signal for signal in active_sample if signal.get("shadow_first_candidate_progress_pct") is not None]
    with_live_decision = [signal for signal in active_sample if signal.get("shadow_first_live_decision_progress_pct") is not None]
    early_candidates = [signal for signal in with_candidate if float(signal.get("shadow_first_candidate_progress_pct", 1) or 1) < 0.80]
    early_live = [signal for signal in with_live_decision if float(signal.get("shadow_first_live_decision_progress_pct", 1) or 1) < 0.80]

    print(
        f"sample={sample_label} | resolved={len(active_sample)} | with_candidate_timing={len(with_candidate)} | "
        f"with_live_decision_timing={len(with_live_decision)}"
    )
    print(
        f"early_candidates(<80%)={len(early_candidates)} | "
        f"early_live_decisions(<80%)={len(early_live)}"
    )

    reports = [
        ("By observation count", bucket_shadow_observation_count),
        ("By first candidate progress", bucket_shadow_first_candidate_progress),
        ("By first live decision progress", bucket_shadow_first_live_decision_progress),
        ("By early candidate timing", early_shadow_candidate_label),
        (
            "By first candidate profile x timing",
            lambda signal: combo_bucket(
                signal,
                lambda s: str(s.get("shadow_first_candidate_profile", "none") or "none"),
                bucket_shadow_first_candidate_progress,
            ),
        ),
        (
            "By first live decision x timing",
            lambda signal: combo_bucket(
                signal,
                lambda s: str(s.get("shadow_first_live_decision", "neutral") or "neutral"),
                bucket_shadow_first_live_decision_progress,
            ),
        ),
        (
            "By max profile x early timing",
            lambda signal: combo_bucket(
                signal,
                lambda s: str(s.get("shadow_max_score_profile", "none") or "none"),
                early_shadow_candidate_label,
            ),
        ),
    ]

    for title, key_fn in reports:
        rows = filter_rows(summarize_trades(active_sample, key_fn), min_trades)
        print_table(title, rows, top)

    if early_candidates:
        print("\nEarly candidate shortlist:")
        shortlist = filter_rows(
            summarize_trades(
                early_candidates,
                lambda signal: combo_bucket(
                    signal,
                    lambda s: str(s.get("shadow_first_candidate_profile", "none") or "none"),
                    bucket_shadow_first_candidate_progress,
                    market_regime_label,
                ),
            ),
            max(2, min_trades),
        )
        if not shortlist:
            print("  No early candidate clusters with enough resolved samples yet.")
        else:
            for row in list(reversed(shortlist[-top:])):
                print(
                    f"  {row['key']} | trades={row['count']} | win_rate={fmt_pct(row['win_rate'])} | "
                    f"total={fmt_money(row['total_pnl'])} | roi={fmt_pct(row['roi'])}"
                )


def print_combo_report(signals: list[dict], min_trades: int, top: int) -> None:
    print("\n=== SETUP COMBOS ===")
    entered = [signal for signal in signals if signal.get("entered")]
    settled = [signal for signal in entered if signal.get("realized_pnl") is not None]
    if not settled:
        print("No settled entered trades to analyze.")
        return

    reports = [
        (
            "PM x 1m confirm",
            lambda signal: combo_bucket(signal, lambda s: bucket_pm(float(s.get("pm", 0) or 0)), bucket_indicator_confirm),
        ),
        (
            "PM x delta",
            lambda signal: combo_bucket(signal, lambda s: bucket_pm(float(s.get("pm", 0) or 0)), lambda s: bucket_delta(float(s.get("delta", 0) or 0))),
        ),
        (
            "Tier x trend",
            lambda signal: combo_bucket(signal, signal_tier_label, lambda s: "trend_conflict" if s.get("trend_conflict") else "trend_ok"),
        ),
        (
            "Tier x tier-reason",
            lambda signal: combo_bucket(signal, signal_tier_label, signal_tier_reason_label),
        ),
    ]

    for title, key_fn in reports:
        rows = filter_rows(summarize_trades(settled, key_fn), min_trades)
        print_table(title, rows, top)


def print_signal_tier_reason_report(signals: list[dict], min_trades: int, top: int) -> None:
    print("\n=== SIGNAL TIER REASONS ===")
    skipped = [signal for signal in signals if not signal.get("entered")]
    resolved = [signal for signal in skipped if signal.get("pnl_if_entered") is not None]
    if not resolved:
        print("No resolved skipped signals with pnl_if_entered yet.")
        return

    normalized = [
        {
            **signal,
            "realized_pnl": float(signal.get("pnl_if_entered", 0) or 0),
            "won": float(signal.get("pnl_if_entered", 0) or 0) > 0,
        }
        for signal in resolved
    ]

    rows = filter_rows(summarize_trades(normalized, signal_tier_reason_label), min_trades)
    print_table("By signal_tier_reason", rows, top)


def print_counterfactual_skip_report(signals: list[dict], min_trades: int) -> None:
    print("\n=== SKIPPED SIGNAL COUNTERFACTUALS ===")
    skipped = [signal for signal in signals if not signal.get("entered")]
    resolved = [signal for signal in skipped if signal.get("pnl_if_entered") is not None]
    if not resolved:
        print("No resolved skipped signals with pnl_if_entered yet.")
        return

    wins = sum(1 for signal in resolved if float(signal.get("pnl_if_entered", 0) or 0) > 0)
    losses = sum(1 for signal in resolved if float(signal.get("pnl_if_entered", 0) or 0) < 0)
    total = sum(float(signal.get("pnl_if_entered", 0) or 0) for signal in resolved)
    print(f"resolved skipped: {len(resolved)} | would_win: {wins} | would_lose: {losses} | total_if_entered: {fmt_money(total)}")

    rows = filter_rows(summarize_trades(
        [
            {
                **signal,
                "realized_pnl": float(signal.get("pnl_if_entered", 0) or 0),
                "won": float(signal.get("pnl_if_entered", 0) or 0) > 0,
            }
            for signal in resolved
        ],
        signal_tier_label,
    ), min_trades)
    if not rows:
        print(f"No skipped tiers with >= {min_trades} resolved signals")
        return

    for row in rows:
        print(
            f"  {row['key']:<12} trades={row['count']:<4} would_win_rate={fmt_pct(row['win_rate']):<7} "
            f"would_pnl={fmt_money(row['total_pnl']):<10} avg_pm={row['avg_pm']:.3f} avg_delta={row['avg_delta']:.3f}%"
        )


def print_skip_reason_counterfactual_report(signals: list[dict], min_trades: int, top: int) -> None:
    print("\n=== SKIP REASON COUNTERFACTUALS ===")
    skipped = [signal for signal in signals if not signal.get("entered")]
    resolved = [signal for signal in skipped if signal.get("pnl_if_entered") is not None]
    if not resolved:
        print("No resolved skipped signals with pnl_if_entered yet.")
        return

    normalized = [
        {
            **signal,
            "realized_pnl": float(signal.get("pnl_if_entered", 0) or 0),
            "won": float(signal.get("pnl_if_entered", 0) or 0) > 0,
        }
        for signal in resolved
    ]

    reason_rows = filter_rows(summarize_trades(normalized, reason_label), min_trades)
    print_table("By skip reason", reason_rows, top)

    combo_rows = filter_rows(
        summarize_trades(
            normalized,
            lambda signal: combo_bucket(signal, reason_label, signal_tier_label),
        ),
        min_trades,
    )
    print_table("By skip reason x tier", combo_rows, top)

    trend_rows = filter_rows(
        summarize_trades(
            normalized,
            lambda signal: combo_bucket(
                signal,
                lambda s: "trend_conflict" if s.get("trend_conflict") else "trend_ok",
                reason_label,
            ),
        ),
        min_trades,
    )
    print_table("By trend flag x skip reason", trend_rows, top)


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
    parser.add_argument("--window-file", default=str(DEFAULT_WINDOW_SAMPLES_FILE), help="Path to window_samples.json for full-window Core EV analysis")
    parser.add_argument("--build-core-ev-rules", action="store_true", help="Build Core EV rulebook JSON from resolved signals")
    parser.add_argument("--core-ev-source", choices=["signals", "window"], default="window", help="Source dataset for Core EV rulebook generation")
    parser.add_argument("--rules-out", default=str(DEFAULT_CORE_EV_RULES_FILE), help="Path to write Core EV rulebook JSON")
    parser.add_argument("--top", type=int, default=6, help="Number of best/worst rows to show")
    parser.add_argument("--min-trades", type=int, default=3, help="Hide segment rows with fewer than this many settled trades")
    parser.add_argument("--recent-hours", type=float, default=6.0, help="Recent time window for skip-flow diagnostics")
    parser.add_argument("--optimize", action="store_true", help="Run offline filter grid-search using resolved signals")
    parser.add_argument("--top-configs", type=int, default=10, help="Number of best configs to print in optimize mode")
    parser.add_argument("--min-sim-trades", type=int, default=30, help="Minimum simulated trade count per config in optimize mode")
    parser.add_argument("--default-amount", type=float, default=10.0, help="Fallback amount for signals without amount field")
    parser.add_argument("--pm-floor", type=float, default=0.10, help="Ignore signals with PM price below this floor in optimize mode")
    parser.add_argument("--pm-ceiling", type=float, default=0.99, help="Ignore signals with PM price above this ceiling in optimize mode")
    parser.add_argument("--shadow-pm-floor", type=float, default=0.05, help="Ignore shadow-analysis rows with PM price below this floor when possible")
    parser.add_argument("--conf-grid", default="0.45,0.50,0.55,0.60", help="Comma-separated confidence thresholds")
    parser.add_argument("--delta-grid", default="0.0008,0.0010,0.0012,0.0015", help="Comma-separated delta_skip thresholds")
    parser.add_argument("--price-min-btc-grid", default="0.82,0.86,0.90,0.94", help="Comma-separated BTC min prices")
    parser.add_argument("--price-min-eth-grid", default="0.80,0.84,0.88,0.92", help="Comma-separated ETH min prices")
    parser.add_argument("--price-max-grid", default="0.94,0.95,0.96,0.97", help="Comma-separated max prices")
    parser.add_argument("--entry-min-grid", default="10,12,15", help="Comma-separated entry_min values")
    parser.add_argument("--entry-max-grid", default="25,30,35", help="Comma-separated entry_max values")
    parser.add_argument("--core-pm-min", type=float, default=0.58, help="Core EV PM minimum")
    parser.add_argument("--core-pm-max", type=float, default=0.70, help="Core EV PM maximum")
    parser.add_argument("--core-recent-hours", type=float, default=72.0, help="Recent window for Core EV bucket freshness")
    parser.add_argument("--core-min-bucket-trades-l1", type=int, default=2, help="Minimum trades for L1 Core EV buckets")
    parser.add_argument("--core-min-bucket-trades-l2", type=int, default=2, help="Minimum trades for L2 Core EV buckets")
    parser.add_argument("--core-min-bucket-trades-l3", type=int, default=2, help="Minimum trades for L3 Core EV buckets")
    parser.add_argument("--core-min-recent-trades", type=int, default=2, help="Minimum recent trades before recent ROI can downgrade bucket quality")
    parser.add_argument("--core-strong-roi-min", type=float, default=5.0, help="ROI threshold for strong_allow Core EV buckets")
    args = parser.parse_args()

    path = Path(args.file)
    if not path.exists():
        print(f"Signals file not found: {path}")
        print("Tip: copy signals.json from your VPS into the repo root or pass --file /path/to/signals.json")
        return 1

    signals = load_signals(path)

    if args.build_core_ev_rules:
        if args.core_ev_source == "window":
            window_path = Path(args.window_file)
            if not window_path.exists():
                print(f"Window samples file not found: {window_path}")
                print("Tip: run the updated bot first so it writes window_samples.json, or pass --core-ev-source signals.")
                return 1
            core_records = select_core_ev_records(load_signals(window_path))
            args.core_ev_source_label = "window_samples"
        else:
            core_records = signals
            args.core_ev_source_label = "signals"

        rulebook = build_core_ev_rulebook(core_records, args)
        out_path = Path(args.rules_out)
        out_path.write_text(json.dumps(rulebook, indent=2), encoding="utf-8")
        print_core_ev_rulebook_summary(rulebook, args.top)
        if args.core_ev_source == "window":
            print_full_window_core_ev_report(core_records, args.min_trades, args.top)
        print(f"\nWrote Core EV rules to: {out_path}")
        return 0

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
    print_indicator_reason_report(signals, args.min_trades, args.top)
    print_normal_pm_zone_report(signals, args.min_trades, args.top)
    print_core_ev_pm_expansion_report(signals, args.min_trades, args.top)
    print_core_ev_timing_report(signals, args.min_trades, args.top)
    print_core_ev_causal_report(signals, args.min_trades, args.top)
    print_execution_failed_report(signals, args.min_trades, args.top)
    print_shadow_entry_report(signals, args.min_trades, args.top, args.shadow_pm_floor)
    print_shadow_similarity_report(signals, args.min_trades, args.top, args.shadow_pm_floor)
    print_shadow_live_recommendations(signals, args.min_trades, args.top, args.shadow_pm_floor)
    print_shadow_live_decision_report(signals, args.min_trades, args.top, args.shadow_pm_floor)
    print_shadow_early_timing_report(signals, args.min_trades, args.top, args.shadow_pm_floor)
    print_combo_report(signals, args.min_trades, args.top)
    print_signal_tier_reason_report(signals, args.min_trades, args.top)
    print_counterfactual_skip_report(signals, args.min_trades)
    print_skip_reason_counterfactual_report(signals, args.min_trades, args.top)
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
