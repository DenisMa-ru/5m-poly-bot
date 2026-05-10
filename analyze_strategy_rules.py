"""analyze_strategy_rules.py

Quick offline research helper to find simple, robust-looking entry rules.

This does NOT change bot behavior.

It searches over a small grid of thresholds on already-logged features and
reports the best rules by expected PnL (counterfactual) and win-rate.

Intended workflow:
  1) Keep bot running in --dry-run/--paper and logging window_samples.jsonl
  2) Run this script to identify which conditions produce positive expectation
  3) Only then translate the winning rule into a small runtime gate

No external dependencies.
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


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


def _safe_float(v, default=None):
    try:
        if v is None:
            return default
        return float(v)
    except Exception:
        return default


def _parse_ts(value: str | None):
    if not value:
        return None
    try:
        return datetime.strptime(str(value), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _pct(num: float, den: float) -> float:
    return 0.0 if den <= 0 else (num / den) * 100.0


@dataclass(frozen=True)
class Rule:
    side: str | None
    time_left_min: float
    time_left_max: float
    spread_max: float | None
    imbalance_min: float | None
    imbalance_max: float | None
    trend_aligned: bool | None
    trend_conflict: bool | None
    market_regime: str | None
    delta_abs_min: float | None
    pm_vs_delta_gap_min: float | None
    underpricing_min: float | None
    vol_ratio_min: float | None
    vol_ratio_max: float | None

    def label(self) -> str:
        parts = [f"tl=[{self.time_left_min:.0f},{self.time_left_max:.0f}]" ]
        if self.side:
            parts.append(f"side={self.side}")
        if self.spread_max is not None:
            parts.append(f"spread<={self.spread_max:.2f}")
        if self.imbalance_min is not None:
            parts.append(f"imb>={self.imbalance_min:.3f}")
        if self.imbalance_max is not None:
            parts.append(f"imb<={self.imbalance_max:.3f}")
        if self.trend_aligned is not None:
            parts.append(f"trend_aligned={self.trend_aligned}")
        if self.trend_conflict is not None:
            parts.append(f"trend_conflict={self.trend_conflict}")
        if self.market_regime:
            parts.append(f"regime={self.market_regime}")
        if self.delta_abs_min is not None:
            parts.append(f"abs_delta>={self.delta_abs_min:.4f}")
        if self.pm_vs_delta_gap_min is not None:
            parts.append(f"gap>={self.pm_vs_delta_gap_min:.3f}")
        if self.underpricing_min is not None:
            parts.append(f"underpricing>={self.underpricing_min:.3f}")
        if self.vol_ratio_min is not None:
            parts.append(f"vol_ratio>={self.vol_ratio_min:.2f}")
        if self.vol_ratio_max is not None:
            parts.append(f"vol_ratio<={self.vol_ratio_max:.2f}")
        return " | ".join(parts)


def _matches(rule: Rule, r: dict) -> bool:
    tl = _safe_float(r.get("time_left"), default=None)
    if tl is None:
        return False
    if not (rule.time_left_min <= tl <= rule.time_left_max):
        return False

    if rule.side is not None:
        if str(r.get("side") or "") != rule.side:
            return False

    if rule.spread_max is not None:
        spread = _safe_float(r.get("spread_at_entry"), default=None)
        if spread is None or spread > rule.spread_max + 1e-9:
            return False

    if rule.imbalance_min is not None or rule.imbalance_max is not None:
        imb = _safe_float(r.get("book_imbalance_at_entry"), default=None)
        if imb is None:
            return False
        if rule.imbalance_min is not None and imb < rule.imbalance_min - 1e-12:
            return False
        if rule.imbalance_max is not None and imb > rule.imbalance_max + 1e-12:
            return False

    if rule.trend_aligned is not None:
        if bool(r.get("trend_aligned")) != bool(rule.trend_aligned):
            return False
    if rule.trend_conflict is not None:
        if bool(r.get("trend_conflict")) != bool(rule.trend_conflict):
            return False

    if rule.market_regime is not None:
        if str(r.get("market_regime") or "") != rule.market_regime:
            return False

    if rule.delta_abs_min is not None:
        d = _safe_float(r.get("delta"), default=None)
        if d is None:
            return False
        if abs(d) + 1e-12 < rule.delta_abs_min:
            return False

    if rule.pm_vs_delta_gap_min is not None:
        gap = _safe_float(r.get("pm_vs_delta_gap"), default=None)
        if gap is None or gap + 1e-12 < rule.pm_vs_delta_gap_min:
            return False

    if rule.underpricing_min is not None:
        u = _safe_float(r.get("underpricing_score"), default=None)
        if u is None or u + 1e-12 < rule.underpricing_min:
            return False

    if rule.vol_ratio_min is not None or rule.vol_ratio_max is not None:
        vr = _safe_float(r.get("vol_ratio_5m_ma7"), default=None)
        if vr is None:
            return False
        if rule.vol_ratio_min is not None and vr + 1e-12 < rule.vol_ratio_min:
            return False
        if rule.vol_ratio_max is not None and vr > rule.vol_ratio_max + 1e-12:
            return False

    return True


def evaluate_rules(records: list[dict], rules: list[Rule], *, top: int = 30) -> list[dict]:
    scored = []
    for rule in rules:
        pnls = []
        wins = 0
        for r in records:
            if not _matches(rule, r):
                continue
            pnl = _safe_float(r.get("pnl_if_entered"), default=None)
            if pnl is None:
                continue
            pnls.append(pnl)
            if pnl > 0:
                wins += 1
        if not pnls:
            continue
        scored.append({
            "rule": rule.label(),
            "trades": len(pnls),
            "win_rate_pct": round(_pct(wins, len(pnls)), 2),
            "pnl_sum": round(sum(pnls), 4),
            "pnl_avg": round(sum(pnls) / len(pnls), 4),
            "pnl_min": round(min(pnls), 4),
            "pnl_max": round(max(pnls), 4),
        })

    scored.sort(key=lambda r: (r["pnl_sum"], r["trades"], r["win_rate_pct"]), reverse=True)
    return scored[:top]


def build_rule_grid(args) -> list[Rule]:
    sides = [None]
    if args.sides:
        sides = [s.strip() for s in args.sides.split(",") if s.strip()]

    tl_windows = [(args.time_left_min, args.time_left_max)]
    if args.time_left_presets:
        tl_windows = [(20, 35), (35, 60), (60, 120)]

    spread_maxes = [None] if not args.spread_grid else [0.01, 0.02]

    imbalance_ranges = [(None, None)]
    if args.imbalance_grid:
        imbalance_ranges = [
            (None, None),
            (0.0, None),
            (0.05, None),
            (None, 0.0),
            (None, -0.05),
        ]

    trend_aligned_opts = [None]
    if args.trend_aligned_grid:
        trend_aligned_opts = [None, True, False]
    trend_conflict_opts = [None]
    if args.trend_conflict_grid:
        trend_conflict_opts = [None, True, False]

    regimes = [None]
    if args.regime_grid:
        regimes = [None, "chop", "trend_up", "trend_down", "unknown"]

    delta_abs_mins = [None]
    if args.delta_abs_grid:
        delta_abs_mins = [None, 0.005, 0.01, 0.015, 0.02]

    gap_mins = [None]
    if args.gap_grid:
        gap_mins = [None, 0.15, 0.25, 0.35, 0.45]

    underpricing_mins = [None]
    if args.underpricing_grid:
        # underpricing_score is often negative; higher (closer to 0 or positive) means less underpriced.
        underpricing_mins = [None, -0.6, -0.4, -0.2, 0.0]

    vol_ratio_ranges = [(None, None)]
    if args.vol_ratio_grid:
        # Research ranges: spikes (>=1.2..2.0) and dry periods (<=0.9..0.6)
        vol_ratio_ranges = [
            (None, None),
            (1.2, None),
            (1.5, None),
            (2.0, None),
            (None, 0.9),
            (None, 0.75),
            (None, 0.6),
        ]

    rules: list[Rule] = []
    for tl_min, tl_max in tl_windows:
        for side in sides:
            for spread_max in spread_maxes:
                for imb_min, imb_max in imbalance_ranges:
                    for ta in trend_aligned_opts:
                        for tc in trend_conflict_opts:
                            for rg in regimes:
                                for dmin in delta_abs_mins:
                                    for gmin in gap_mins:
                                        for umin in underpricing_mins:
                                            for vr_min, vr_max in vol_ratio_ranges:
                                                rules.append(Rule(
                                                    side=side,
                                                    time_left_min=float(tl_min),
                                                    time_left_max=float(tl_max),
                                                    spread_max=spread_max,
                                                    imbalance_min=imb_min,
                                                    imbalance_max=imb_max,
                                                    trend_aligned=ta,
                                                    trend_conflict=tc,
                                                    market_regime=rg,
                                                    delta_abs_min=dmin,
                                                    pm_vs_delta_gap_min=gmin,
                                                    underpricing_min=umin,
                                                    vol_ratio_min=vr_min,
                                                    vol_ratio_max=vr_max,
                                                ))
    return rules


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--window-samples-jsonl", default="window_samples.jsonl")
    ap.add_argument("--since-ts", default="")
    ap.add_argument("--top", type=int, default=30)
    ap.add_argument("--min-trades", type=int, default=10)

    ap.add_argument("--time-left-min", type=float, default=20)
    ap.add_argument("--time-left-max", type=float, default=35)
    ap.add_argument("--time-left-presets", action="store_true", help="Search common time_left windows")
    ap.add_argument("--sides", default="Up,Down")

    ap.add_argument("--spread-grid", action="store_true", help="Search spread<=0.01/0.02")
    ap.add_argument("--imbalance-grid", action="store_true", help="Search imbalance thresholds")
    ap.add_argument("--trend-aligned-grid", action="store_true")
    ap.add_argument("--trend-conflict-grid", action="store_true")
    ap.add_argument("--regime-grid", action="store_true")
    ap.add_argument("--delta-abs-grid", action="store_true", help="Search abs(delta) minimum thresholds")
    ap.add_argument("--gap-grid", action="store_true", help="Search pm_vs_delta_gap minimum thresholds")
    ap.add_argument("--underpricing-grid", action="store_true", help="Search underpricing_score minimum thresholds")
    ap.add_argument("--vol-ratio-grid", action="store_true", help="Search vol_ratio_5m_ma7 thresholds")

    args = ap.parse_args()

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

    # We only use counterfactual / skipped entries for research to avoid
    # mixing execution decisions into the label.
    usable = [
        r for r in records
        if str(r.get("record_type") or "") == "window_sample"
        and r.get("pnl_if_entered") is not None
    ]

    rules = build_rule_grid(args)
    scored = evaluate_rules(usable, rules, top=int(args.top or 30))

    print("=== STRATEGY RULE SEARCH (counterfactual pnl_if_entered) ===")
    print(f"records_with_pnl_if_entered: {len(usable)}")
    print(f"rules_tested: {len(rules)}")
    print(f"top_rules: {len(scored)} (min_trades filter applied after scoring output)")

    shown = 0
    for row in scored:
        if row["trades"] < int(args.min_trades or 10):
            continue
        shown += 1
        print(
            f"- trades={row['trades']} win_rate={row['win_rate_pct']}% "
            f"pnl_sum={row['pnl_sum']} pnl_avg={row['pnl_avg']} min/max={row['pnl_min']}/{row['pnl_max']}\n"
            f"  {row['rule']}"
        )
        if shown >= int(args.top or 30):
            break

    if shown == 0:
        print("(no rules met min_trades; try lowering --min-trades or widening grids)")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
