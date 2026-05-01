from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from analyze_signals import build_core_ev_rulebook, load_signals, select_core_ev_records


BOT_DIR = Path(__file__).parent
DEFAULT_SIGNALS_FILE = BOT_DIR / "signals.json"
DEFAULT_WINDOW_SAMPLES_FILE = BOT_DIR / "window_samples.json"
DEFAULT_ACTIVE_RULEBOOK_FILE = BOT_DIR / "core_ev_rules.json"
DEFAULT_CANDIDATE_RULEBOOK_FILE = BOT_DIR / "core_ev_rules.candidate.json"
DEFAULT_PREV_RULEBOOK_FILE = BOT_DIR / "core_ev_rules.prev.json"
DEFAULT_HISTORY_FILE = BOT_DIR / "rulebook_history.jsonl"


def atomic_write_text(path: Path, content: str) -> None:
    tmp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp_path.write_text(content, encoding="utf-8")
    os.replace(tmp_path, path)


def load_rulebook(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def parse_ts(value: str) -> datetime | None:
    if not value:
        return None
    try:
        if value.endswith("Z"):
            return datetime.fromisoformat(value[:-1] + "+00:00")
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def filter_recent_records(records: list[dict], lookback_hours: float) -> list[dict]:
    if lookback_hours <= 0:
        return records
    cutoff = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)
    filtered = []
    for record in records:
        dt = parse_ts(str(record.get("timestamp", "") or ""))
        if dt is not None and dt >= cutoff:
            filtered.append(record)
    return filtered


def build_args(args: argparse.Namespace) -> SimpleNamespace:
    return SimpleNamespace(
        core_pm_min=args.core_pm_min,
        core_pm_max=args.core_pm_max,
        core_recent_hours=args.core_recent_hours,
        core_min_bucket_trades_l1=args.core_min_bucket_trades_l1,
        core_min_bucket_trades_l2=args.core_min_bucket_trades_l2,
        core_min_bucket_trades_l3=args.core_min_bucket_trades_l3,
        core_min_recent_trades=args.core_min_recent_trades,
        core_strong_roi_min=args.core_strong_roi_min,
        default_amount=args.default_amount,
        core_ev_source_label=args.core_ev_source_label,
    )


def rulebook_allow_count(rulebook: dict) -> int:
    return int(rulebook.get("allow_bucket_count", 0) or 0)


def summarize_rulebook(rulebook: dict) -> dict:
    return {
        "generated_at": str(rulebook.get("generated_at", "unknown") or "unknown"),
        "source_type": str(rulebook.get("source_type", "unknown") or "unknown"),
        "source_signals": int(rulebook.get("source_signals", 0) or 0),
        "resolved_eligible_signals": int(rulebook.get("resolved_eligible_signals", 0) or 0),
        "allow_bucket_count": int(rulebook.get("allow_bucket_count", 0) or 0),
        "deny_bucket_count": int(rulebook.get("deny_bucket_count", 0) or 0),
        "watch_bucket_count": int(rulebook.get("watch_bucket_count", 0) or 0),
        "bucket_count": int(rulebook.get("bucket_count", 0) or 0),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Safely rebuild Core EV rulebook with guardrails")
    parser.add_argument("--file", default=str(DEFAULT_SIGNALS_FILE), help="Path to signals.json")
    parser.add_argument("--window-file", default=str(DEFAULT_WINDOW_SAMPLES_FILE), help="Path to window_samples.json")
    parser.add_argument("--source", choices=["window", "signals"], default="window", help="Dataset source for rulebook generation")
    parser.add_argument("--active-rules", default=str(DEFAULT_ACTIVE_RULEBOOK_FILE), help="Active rulebook path")
    parser.add_argument("--candidate-rules", default=str(DEFAULT_CANDIDATE_RULEBOOK_FILE), help="Candidate rulebook path")
    parser.add_argument("--prev-rules", default=str(DEFAULT_PREV_RULEBOOK_FILE), help="Backup path for previous active rulebook")
    parser.add_argument("--history-file", default=str(DEFAULT_HISTORY_FILE), help="Append-only rebuild history log")
    parser.add_argument("--lookback-hours", type=float, default=24.0 * 7, help="Only use records newer than this many hours; <=0 disables")
    parser.add_argument("--min-resolved-eligible", type=int, default=80, help="Reject candidate if too few resolved eligible signals")
    parser.add_argument("--max-allow-bucket-drop-pct", type=float, default=35.0, help="Reject candidate if allow buckets drop more than this percent vs active")
    parser.add_argument("--core-pm-min", type=float, default=0.58, help="Core EV PM minimum")
    parser.add_argument("--core-pm-max", type=float, default=0.70, help="Core EV PM maximum")
    parser.add_argument("--core-recent-hours", type=float, default=72.0, help="Recent window for Core EV bucket freshness")
    parser.add_argument("--core-min-bucket-trades-l1", type=int, default=2, help="Minimum trades for L1 buckets")
    parser.add_argument("--core-min-bucket-trades-l2", type=int, default=2, help="Minimum trades for L2 buckets")
    parser.add_argument("--core-min-bucket-trades-l3", type=int, default=2, help="Minimum trades for L3 buckets")
    parser.add_argument("--core-min-recent-trades", type=int, default=2, help="Minimum recent trades before recent ROI can downgrade bucket quality")
    parser.add_argument("--core-strong-roi-min", type=float, default=5.0, help="ROI threshold for strong_allow buckets")
    parser.add_argument("--default-amount", type=float, default=10.0, help="Fallback amount for records without amount field")
    args = parser.parse_args()

    source_path = Path(args.window_file if args.source == "window" else args.file)
    if not source_path.exists():
        print(f"Source file not found: {source_path}")
        return 1

    raw_records = load_signals(source_path)
    if args.source == "window":
        core_records = select_core_ev_records(raw_records)
        args.core_ev_source_label = "window_samples"
    else:
        core_records = raw_records
        args.core_ev_source_label = "signals"

    filtered_records = filter_recent_records(core_records, args.lookback_hours)
    if filtered_records:
        core_records = filtered_records

    build_ns = build_args(args)
    candidate_rulebook = build_core_ev_rulebook(core_records, build_ns)
    candidate_summary = summarize_rulebook(candidate_rulebook)
    active_path = Path(args.active_rules)
    candidate_path = Path(args.candidate_rules)
    prev_path = Path(args.prev_rules)
    history_path = Path(args.history_file)
    active_rulebook = load_rulebook(active_path)
    active_summary = summarize_rulebook(active_rulebook) if active_rulebook else {}

    candidate_payload = json.dumps(candidate_rulebook, indent=2, ensure_ascii=True)
    atomic_write_text(candidate_path, candidate_payload)

    reject_reasons = []
    resolved_eligible = candidate_summary["resolved_eligible_signals"]
    allow_bucket_count = candidate_summary["allow_bucket_count"]
    bucket_count = candidate_summary["bucket_count"]
    if resolved_eligible < args.min_resolved_eligible:
        reject_reasons.append(
            f"resolved_eligible_signals {resolved_eligible} < minimum {args.min_resolved_eligible}"
        )
    if bucket_count <= 0:
        reject_reasons.append("candidate bucket_count is zero")
    if allow_bucket_count <= 0:
        reject_reasons.append("candidate allow_bucket_count is zero")

    active_allow_count = int(active_summary.get("allow_bucket_count", 0) or 0)
    if active_allow_count > 0:
        min_allowed_count = active_allow_count * max(0.0, 1.0 - args.max_allow_bucket_drop_pct / 100.0)
        if allow_bucket_count < min_allowed_count:
            reject_reasons.append(
                f"allow_bucket_count dropped from {active_allow_count} to {allow_bucket_count} "
                f"(limit {args.max_allow_bucket_drop_pct:.1f}%)"
            )

    activation_status = "promoted"
    if reject_reasons:
        activation_status = "rejected_guardrail"
    else:
        if active_path.exists():
            atomic_write_text(prev_path, active_path.read_text(encoding="utf-8"))
        atomic_write_text(active_path, candidate_payload)

    history_entry = {
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "activation_status": activation_status,
        "source": args.source,
        "lookback_hours": args.lookback_hours,
        "guardrails": {
            "min_resolved_eligible": args.min_resolved_eligible,
            "max_allow_bucket_drop_pct": args.max_allow_bucket_drop_pct,
        },
        "active_before": active_summary,
        "candidate": candidate_summary,
        "reject_reasons": reject_reasons,
        "paths": {
            "active": str(active_path),
            "candidate": str(candidate_path),
            "prev": str(prev_path),
        },
    }
    with history_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(history_entry, ensure_ascii=True) + "\n")

    print("=== CORE EV RULEBOOK REBUILD ===")
    print(f"source: {args.source}")
    print(f"records_used: {candidate_summary['source_signals']}")
    print(f"resolved_eligible_signals: {resolved_eligible}")
    print(f"allow_bucket_count: {allow_bucket_count}")
    print(f"bucket_count: {bucket_count}")
    if active_summary:
        print(f"active_allow_bucket_count_before: {active_allow_count}")
    print(f"candidate_written_to: {candidate_path}")
    print(f"activation_status: {activation_status}")
    if reject_reasons:
        for reason in reject_reasons:
            print(f"reject_reason: {reason}")
        print("Active rulebook unchanged.")
        return 2

    print(f"active_rulebook_updated: {active_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
