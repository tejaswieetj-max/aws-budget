"""Offline M9 analysis for exported spendctl reservation and billing rows.

The input is a JSON object with ``reservations`` and ``billing_audits`` arrays,
exported by an operator. This module never creates AWS clients or writes ledger
data. Reports retain integer micro-USD amounts and label their provenance.
"""

from __future__ import annotations

import argparse
import json
import math
import random
import sys
from dataclasses import asdict, dataclass
from decimal import ROUND_CEILING, Decimal
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services"))


@dataclass(frozen=True)
class Observation:
    reservation_id: str
    instance_type: str
    region: str
    ebs_gb: int
    requested_runtime_hours: int | None
    actual_runtime_hours: Decimal | None
    estimated_compute_micro: int
    estimated_storage_micro: int
    reserve_margin: Decimal
    reserved_micro: int
    provisional_micro: int | None
    actual_micro: int
    price_snapshot_id: str | None
    price_fetched_at: str | None
    billing_period: str
    reconciliation_status: str
    provenance: str


def build_dataset(export: dict[str, Any], *, provenance: str = "unknown") -> dict[str, Any]:
    """Join final billing audits to their persisted reservation; report exclusions."""
    if not isinstance(export, dict):
        raise TypeError("export must be an object")
    reservations = export.get("reservations", [])
    audits = export.get("billing_audits", [])
    if not isinstance(reservations, list) or not isinstance(audits, list):
        raise TypeError("reservations and billing_audits must be arrays")
    by_id: dict[str, dict] = {}
    duplicate_reservations: set[str] = set()
    excluded: list[dict] = []
    for reservation in reservations:
        if not isinstance(reservation, dict) or not reservation.get("reservation_id"):
            excluded.append({"reason": "reservation_missing_stable_id"})
            continue
        reservation_id = str(reservation["reservation_id"])
        if reservation_id in by_id:
            excluded.append({"reservation_id": reservation_id, "reason": "duplicate_reservation"})
            by_id.pop(reservation_id, None)
            duplicate_reservations.add(reservation_id)
            continue
        if reservation_id in duplicate_reservations:
            continue
        by_id[reservation_id] = reservation

    audit_groups: dict[str, list[dict]] = {}
    for audit in audits:
        if not isinstance(audit, dict) or not audit.get("reservation_id"):
            excluded.append({"reason": "audit_missing_reservation_id"})
            continue
        rid = str(audit["reservation_id"])
        audit_groups.setdefault(rid, []).append(audit)

    latest: dict[str, dict] = {}
    for rid, rows in audit_groups.items():
        orders = [str(row.get("source_revision_order", "")) for row in rows]
        if "" in orders or len(orders) != len(set(orders)):
            excluded.append({"reservation_id": rid, "reason": "duplicate_or_missing_revision_order"})
            continue
        latest[rid] = max(rows, key=lambda row: str(row["source_revision_order"]))

    observations: list[Observation] = []
    for rid, audit in sorted(latest.items()):
        reservation = by_id.get(rid)
        if reservation is None:
            excluded.append({"reservation_id": rid, "reason": "reservation_not_found"})
            continue
        if audit.get("record_type") not in (None, "BILLING_RECONCILIATION"):
            excluded.append({"reservation_id": rid, "reason": "not_billing_reconciliation"})
            continue
        if reservation.get("settlement_stage") != "FINAL":
            excluded.append({"reservation_id": rid, "reason": "not_finally_reconciled"})
            continue
        try:
            actual = _int(audit.get("new_actual_micro"), "new_actual_micro", nonnegative=True)
            reserved = _int(reservation.get("reserved_micro"), "reserved_micro", nonnegative=True)
            compute = _int(reservation.get("compute_micro"), "compute_micro", nonnegative=True)
            storage = _int(reservation.get("storage_micro"), "storage_micro", nonnegative=True)
            margin = _decimal(reservation.get("margin", reservation.get("reserve_margin", reservation.get("reserve_multiplier", "1.10"))))
            ebs_gb = _int(reservation.get("resource", {}).get("ebs_gb", reservation.get("ebs_gb", 0)), "ebs_gb", nonnegative=True)
        except (ValueError, TypeError, ArithmeticError) as error:
            excluded.append({"reservation_id": rid, "reason": f"invalid_financial_field:{error}"})
            continue
        resource = reservation.get("resource") or {}
        instance_type = str(resource.get("instance_type", reservation.get("instance_type", "unknown")))
        region = str(reservation.get("region", "ap-south-1"))
        runtime = _optional_int(reservation.get("planned_hours", reservation.get("duration_hours")))
        if instance_type not in {"t3.micro", "t3.small"} or region != "ap-south-1" or not 1 <= ebs_gb <= 30:
            excluded.append({"reservation_id": rid, "reason": "unsupported_resource_configuration"})
            continue
        if runtime is not None and not 1 <= runtime <= 24:
            excluded.append({"reservation_id": rid, "reason": "unsupported_requested_runtime"})
            continue
        if margin <= 0:
            excluded.append({"reservation_id": rid, "reason": "invalid_reserve_margin"})
            continue
        actual_runtime = _optional_decimal(reservation.get("actual_runtime_hours"))
        if actual_runtime is not None and (not actual_runtime.is_finite() or actual_runtime < 0):
            excluded.append({"reservation_id": rid, "reason": "invalid_actual_runtime"})
            continue
        billing_period = str(audit.get("billing_period", ""))
        if len(billing_period) != 7 or billing_period[4] != "-":
            excluded.append({"reservation_id": rid, "reason": "missing_or_invalid_billing_period"})
            continue
        if not reservation.get("price_snapshot_id") or not reservation.get("price_fetched_at"):
            excluded.append({"reservation_id": rid, "reason": "incomplete_price_provenance"})
            continue
        observations.append(
            Observation(
                reservation_id=rid,
                instance_type=instance_type,
                region=region,
                ebs_gb=ebs_gb,
                requested_runtime_hours=runtime,
                actual_runtime_hours=actual_runtime,
                estimated_compute_micro=compute,
                estimated_storage_micro=storage,
                reserve_margin=margin,
                reserved_micro=reserved,
                provisional_micro=_optional_int(reservation.get("provisional_micro")),
                actual_micro=actual,
                price_snapshot_id=reservation.get("price_snapshot_id"),
                price_fetched_at=reservation.get("price_fetched_at"),
                billing_period=billing_period,
                reconciliation_status="FINAL",
                provenance=provenance,
            )
        )
    missing_audit = sorted(set(by_id) - set(latest))
    excluded.extend({"reservation_id": rid, "reason": "no_billing_audit"} for rid in missing_audit)
    return {
        "classification": "AWS_EXPORT_ASSERTED_UNVERIFIED" if provenance == "aws_export" else provenance.upper(),
        "observation_count": len(observations),
        "observations": [asdict(row) for row in observations],
        "exclusions": excluded,
        "exclusion_counts": _count_reasons(excluded),
    }


def estimation_metrics(rows: list[Observation]) -> dict[str, Any]:
    """Compute error metrics with explicit treatment for zero/negative actuals."""
    if not rows:
        return {"observation_count": 0, "mae_micro": None, "rmse_micro": None,
                "mape_percent": None, "mean_signed_bias_micro": None,
                "underestimate_count": 0, "overestimate_count": 0,
                "negative_actual_count": 0,
                "actual_to_estimated_mean": None, "reserve_deficit_count": 0,
                "reserve_deficit_micro": 0, "worst_deficit_micro": 0,
                "total_reserved_micro": 0, "total_actual_micro": 0, "unused_reserve_micro": 0,
                "mape_excluded_zero_or_negative_actual": 0}
    errors = [Decimal(r.actual_micro - r.estimated_compute_micro - r.estimated_storage_micro) for r in rows]
    ratios = [Decimal(r.actual_micro) / Decimal(r.estimated_compute_micro + r.estimated_storage_micro)
              for r in rows if r.estimated_compute_micro + r.estimated_storage_micro > 0]
    deficits = [max(0, r.actual_micro - r.reserved_micro) for r in rows]
    positive_actual = [abs(e) / Decimal(r.actual_micro) for e, r in zip(errors, rows) if r.actual_micro > 0]
    n = Decimal(len(rows))
    return {
        "observation_count": len(rows),
        "mae_micro": _decimal_json(sum(map(abs, errors), Decimal(0)) / n),
        "rmse_micro": _decimal_json((sum((e * e for e in errors), Decimal(0)) / n).sqrt()),
        "mape_percent": _decimal_json(sum(positive_actual, Decimal(0)) / len(positive_actual) * 100) if positive_actual else None,
        "mape_excluded_zero_or_negative_actual": sum(r.actual_micro <= 0 for r in rows),
        "negative_actual_count": sum(r.actual_micro < 0 for r in rows),
        "mean_signed_bias_micro": _decimal_json(sum(errors, Decimal(0)) / n),
        "underestimate_count": sum(e > 0 for e in errors),
        "overestimate_count": sum(e < 0 for e in errors),
        "actual_to_estimated_mean": _decimal_json(sum(ratios, Decimal(0)) / len(ratios)) if ratios else None,
        "total_reserved_micro": sum(r.reserved_micro for r in rows),
        "average_reserved_micro": _decimal_json(Decimal(sum(r.reserved_micro for r in rows)) / n),
        "total_actual_micro": sum(r.actual_micro for r in rows),
        "unused_reserve_micro": sum(max(0, r.reserved_micro - r.actual_micro) for r in rows),
        "reserve_deficit_count": sum(d > 0 for d in deficits),
        "reserve_deficit_micro": sum(deficits),
        "worst_deficit_micro": max(deficits, default=0),
    }


def evaluate_margins(rows: list[Observation], candidates: list[Decimal]) -> dict[str, Any]:
    ordered = sorted(rows, key=lambda row: (row.billing_period, row.reservation_id))
    periods = {row.billing_period for row in ordered if row.billing_period}
    holdout_available = len(ordered) >= 20 and len(periods) >= 2
    holdout_start = int(len(ordered) * 0.8)
    holdout = ordered[holdout_start:] if holdout_available else []
    output = []
    for margin in candidates:
        if margin <= 0:
            raise ValueError("candidate margins must be positive")
        reserves = [_ceil_micro(Decimal(r.estimated_compute_micro + r.estimated_storage_micro) * margin) for r in rows]
        deficits = [max(0, r.actual_micro - reserved) for r, reserved in zip(rows, reserves)]
        count = len(rows)
        holdout_deficits = [
            max(0, row.actual_micro - _ceil_micro(Decimal(row.estimated_compute_micro + row.estimated_storage_micro) * margin))
            for row in holdout
        ]
        output.append({
            "margin": str(margin), "sample_size": count,
            "total_reserved_micro": sum(reserves), "total_actual_micro": sum(r.actual_micro for r in rows),
            "unused_reserve_micro": sum(max(0, reserve - r.actual_micro) for r, reserve in zip(rows, reserves)),
            "insufficient_count": sum(d > 0 for d in deficits),
            "insufficient_percent": (sum(d > 0 for d in deficits) * 100 / count) if count else None,
            "aggregate_deficit_micro": sum(deficits), "worst_deficit_micro": max(deficits, default=0),
            "holdout": "chronological_last_20_percent" if holdout_available else "exploratory_insufficient_chronological_data",
            "holdout_sample_size": len(holdout),
            "holdout_insufficient_count": sum(value > 0 for value in holdout_deficits),
            "holdout_insufficient_percent": (sum(value > 0 for value in holdout_deficits) * 100 / len(holdout)) if holdout else None,
            "holdout_worst_deficit_micro": max(holdout_deficits, default=0),
        })
    return {"candidate_margins": output, "limitations": [
        "recommendations only; production policy is unchanged",
        "metrics are descriptive; no coverage guarantee is implied",
    ]}


def atomic_vs_naive_experiment(*, seed: int, concurrency: list[int], request_count: int = 100,
                               budget_micro: int = 25_000_000) -> list[dict[str, Any]]:
    """Controlled race model; naive participants read one snapshot per wave."""
    if request_count < 1 or budget_micro < 1:
        raise ValueError("request_count and budget_micro must be positive")
    rng = random.Random(seed)
    amounts = [rng.randint(1_000_000, 5_000_000) for _ in range(request_count)]
    reports = []
    for workers in concurrency:
        if workers < 1:
            raise ValueError("concurrency values must be positive")
        available = budget_micro
        atomic = []
        for amount in amounts:
            if amount <= available:
                atomic.append(True)
                available -= amount
            else:
                atomic.append(False)
        atomic_over = sum(amounts[i] for i, accepted in enumerate(atomic) if accepted) > budget_micro
        naive_available = budget_micro
        naive_accepted = 0
        naive_spend = 0
        for start in range(0, len(amounts), workers):
            wave = amounts[start:start + workers]
            observed_available = naive_available
            accepted = [amount for amount in wave if amount <= observed_available]
            naive_accepted += len(accepted)
            naive_spend += sum(accepted)
            naive_available -= sum(accepted)
        reports.append({
            "concurrency": workers, "requests": request_count, "budget_micro": budget_micro,
            "atomic_accepted": sum(atomic), "atomic_denied": request_count - sum(atomic),
            "atomic_over_admission_micro": max(0, sum(amounts[i] for i, ok in enumerate(atomic) if ok) - budget_micro),
            # The deterministic model has no DynamoDB transaction engine; do
            # not mislabel budget denials as transaction conflicts.
            "atomic_transaction_conflicts": None,
            "atomic_retries": 0,
            "atomic_reservation_latency_ms": None,
            "naive_accepted": naive_accepted, "naive_denied": request_count - naive_accepted,
            "naive_over_admission_micro": max(0, naive_spend - budget_micro),
            "naive_retries": 0,
            "naive_reservation_latency_ms": None,
            "duplicate_reservations": 0,
            "orphaned_holds": 0,
            "attribution_rate": None,
            "cleanup_success_rate": None,
            "atomic_invariant_holds": not atomic_over and available >= 0,
            "naive_invariant_holds": naive_available >= 0,
            "latency_ms": None, "retries": None,
            "note": "deterministic local schedule model; not DynamoDB timing evidence",
        })
    return reports


def calibration_lag_sensitivity(*, seed: int, requests: int = 1000,
                                 lags: list[int] | None = None) -> list[dict[str, Any]]:
    """Measure adaptive reserve outcomes when feedback arrives after 0/1/7/30 rows."""
    from spendctl.calibration import MarginPolicies, Sample
    delays = lags or [0, 1, 7, 30]
    if requests < 1:
        raise ValueError("requests must be positive")
    rng = random.Random(seed)
    ratios = [Decimal(str(math.exp(rng.gauss(0.03 if i < requests // 2 else 0.10, 0.2))))
              for i in range(requests)]
    reports = []
    for delay in delays:
        if delay < 0:
            raise ValueError("feedback lag must be nonnegative")
        policy = MarginPolicies(gamma=0.02)
        overrun = 0
        reserve_sum = Decimal(0)
        for index, ratio in enumerate(ratios):
            multiplier = Decimal(str(policy.get("ec2:t3", "adaptive")))
            reserve_sum += multiplier
            overrun += int(ratio > multiplier)
            feedback_index = index - delay
            if feedback_index >= 0:
                actual = int(1_000_000 * ratios[feedback_index])
                policy.add_settlement(
                    "ec2:t3",
                    Sample(f"lag-{delay}-settle-{feedback_index}", float(ratios[feedback_index]),
                           1_100_000, actual, 1_000_000, feedback_index),
                )
        reports.append({"feedback_lag_requests": delay, "requests": requests,
                        "adaptive_overrun_frequency": overrun / requests,
                        "average_reserve_multiplier": str(reserve_sum / requests),
                        "seed": seed, "classification": "SYNTHETIC_NOT_AWS_BILLING"})
    return reports


def main() -> None:
    parser = argparse.ArgumentParser(description="Analyze exported spendctl research data offline; never calls AWS.")
    parser.add_argument("--input", type=Path, required=True, help="JSON export with reservations and billing_audits arrays")
    parser.add_argument("--provenance", choices=("aws_export", "synthetic_fixture", "unknown"), default="unknown")
    parser.add_argument("--output", type=Path, required=True, help="Output JSON report path")
    parser.add_argument("--margins", default="1.00,1.05,1.10,1.15,1.25,1.50")
    parser.add_argument("--experiment", action="store_true", help="Include isolated deterministic atomic/naive concurrency experiment")
    parser.add_argument("--seed", type=int, default=20261020)
    args = parser.parse_args()
    export = json.loads(args.input.read_text())
    dataset = build_dataset(export, provenance=args.provenance)
    observations = [Observation(**{**item, "reserve_margin": Decimal(str(item["reserve_margin"])),
                                   "actual_runtime_hours": Decimal(str(item["actual_runtime_hours"])) if item["actual_runtime_hours"] is not None else None})
                    for item in dataset["observations"]]
    margins = [Decimal(part.strip()) for part in args.margins.split(",")]
    report = {"dataset": dataset, "metrics": estimation_metrics(observations),
              "margin_evaluation": evaluate_margins(observations, margins)}
    if args.experiment:
        report["concurrency_experiment"] = atomic_vs_naive_experiment(seed=args.seed, concurrency=[1, 2, 4, 8, 16, 32])
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, default=_decimal_json) + "\n")
    markdown_path = args.output.with_suffix(".md")
    markdown_path.write_text(render_markdown(report))
    print(
        f"Wrote {args.output} and {markdown_path}; "
        f"{len(observations)} observations, provenance={dataset['classification']}"
    )


def render_markdown(report: dict[str, Any]) -> str:
    """Render a stable, timestamp-free Markdown summary alongside JSON output."""
    dataset = report["dataset"]
    metrics = report["metrics"]
    lines = [
        "# Spendctl offline research report",
        "",
        f"**Classification:** `{dataset['classification']}`  ",
        f"**Included observations:** {dataset['observation_count']}  ",
        f"**Excluded observations:** {len(dataset['exclusions'])}",
        "",
        (
            "Financial figures are integer micro-USD unless identified as ratios or percentages. "
            "Candidate margins are descriptive and do not modify production policy."
        ),
        "",
        "## Estimation metrics",
        "",
        "| Metric | Value |",
        "|---|---:|",
    ]
    metric_names = (
        ("MAE (micro-USD)", "mae_micro"),
        ("RMSE (micro-USD)", "rmse_micro"),
        ("MAPE (%)", "mape_percent"),
        ("Mean signed bias (micro-USD)", "mean_signed_bias_micro"),
        ("Underestimates", "underestimate_count"),
        ("Overestimates", "overestimate_count"),
        ("Reserve deficits", "reserve_deficit_count"),
        ("Aggregate reserve deficit (micro-USD)", "reserve_deficit_micro"),
        ("Worst reserve deficit (micro-USD)", "worst_deficit_micro"),
        ("Average reserved (micro-USD)", "average_reserved_micro"),
    )
    lines.extend(
        f"| {label} | {metrics.get(key) if metrics.get(key) is not None else 'n/a'} |"
        for label, key in metric_names
    )
    lines.extend(["", "## Candidate reserve margins", "", "| Margin | N | Insufficient | Deficit (micro-USD) | Holdout |", "|---:|---:|---:|---:|---|"])
    for row in report["margin_evaluation"]["candidate_margins"]:
        insufficient_percent = row["insufficient_percent"]
        percentage = f"{insufficient_percent}%" if insufficient_percent is not None else "n/a"
        lines.append(
            f"| {row['margin']} | {row['sample_size']} | {row['insufficient_count']} "
            f"({percentage}) | {row['aggregate_deficit_micro']} | {row['holdout']} |"
        )
    lines.extend(["", "## Exclusions", ""])
    if dataset["exclusion_counts"]:
        lines.extend(f"- `{reason}`: {count}" for reason, count in dataset["exclusion_counts"].items())
    else:
        lines.append("No records were excluded.")
    lines.extend(["", "## Limitations", ""])
    lines.extend(f"- {item}" for item in report["margin_evaluation"]["limitations"])
    if dataset["observation_count"] == 0:
        lines.append("- No verified real billing observations are present in this report.")
    return "\n".join(lines) + "\n"


def _int(value: Any, name: str, *, nonnegative: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, Decimal)) or Decimal(value) != Decimal(value).to_integral_value():
        raise ValueError(f"{name} must be an integer")
    result = int(value)
    if nonnegative and result < 0:
        raise ValueError(f"{name} must be nonnegative")
    return result


def _decimal(value: Any) -> Decimal:
    result = Decimal(str(value))
    if not result.is_finite():
        raise ValueError("decimal must be finite")
    return result


def _optional_int(value: Any) -> int | None:
    return _int(value, "optional integer", nonnegative=True) if value is not None else None


def _optional_decimal(value: Any) -> Decimal | None:
    return _decimal(value) if value is not None else None


def _ceil_micro(value: Decimal) -> int:
    return int(value.to_integral_value(rounding=ROUND_CEILING))


def _decimal_json(value: Any) -> str | None:
    return str(value) if isinstance(value, Decimal) else None


def _count_reasons(rows: list[dict]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        reason = str(row.get("reason", "unknown"))
        counts[reason] = counts.get(reason, 0) + 1
    return dict(sorted(counts.items()))


if __name__ == "__main__":
    main()
