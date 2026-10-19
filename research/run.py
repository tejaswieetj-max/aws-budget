"""Reproducible synthetic policy evaluation; contains no AWS billing records."""

import argparse
import csv
import json
import math
import random
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services"))
from spendctl.calibration import MarginPolicies, Sample


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="research/config.json")
    args = parser.parse_args()
    cfg = json.loads(Path(args.config).read_text())
    rng = random.Random(cfg["seed"])
    out = Path(cfg["output_dir"])
    out.mkdir(parents=True, exist_ok=True)
    policies = ["fixed_1.10", "rolling_quantile", "split_conformal", "adaptive"]
    records = []
    calibration = MarginPolicies(cfg["gamma"], cfg["target_alpha"])
    # Log-normal multiplicative model error with deterministic regime shift.
    ratios = [
        math.exp(rng.gauss(0.015 if i < cfg["requests"] // 2 else 0.06, 0.18))
        for i in range(cfg["requests"])
    ]
    # Non-adaptive split conformal uses an early calibration partition, frozen before evaluation.
    split = sorted(ratios[: cfg["warmup"]])
    split_multiplier = min(
        2.0, max(1.0, split[min(len(split) - 1, math.ceil(0.95 * (len(split) + 1)) - 1)])
    )
    multiplier_hist = {p: [] for p in policies}
    for i, ratio in enumerate(ratios):
        modeled = 1_000_000
        candidates = {
            "fixed_1.10": 1.1,
            "rolling_quantile": calibration.get("ec2:t3", "rolling_quantile"),
            "split_conformal": split_multiplier,
            "adaptive": calibration.get("ec2:t3", "adaptive"),
        }
        for policy, margin in candidates.items():
            margin = min(2.0, max(1.0, margin))
            multiplier_hist[policy].append(margin)
            reserve = modeled * margin
            actual = modeled * ratio
            records.append(
                {
                    "index": i,
                    "policy": policy,
                    "modeled_micro": modeled,
                    "actual_micro": round(actual),
                    "reserve_micro": round(reserve),
                    "multiplier": margin,
                    "overrun": int(actual > reserve),
                    "severity_ratio": max(0.0, (actual - reserve) / reserve),
                    "admitted": int(reserve <= cfg["budget_micro"]),
                    "synthetic": True,
                }
            )
        # Settlement arrives only after this request's prediction and outcome.
        # Early warmup outcomes are reserved for the frozen split-conformal fit.
        if i >= cfg["warmup"]:
            adaptive_reserve = int(modeled * multiplier_hist["adaptive"][-1])
            calibration.add_settlement(
                "ec2:t3",
                Sample(
                    f"settle-{i:06d}", ratio, adaptive_reserve, int(modeled * ratio), modeled, i
                ),
            )
    raw = out / "synthetic_raw.csv"
    with raw.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=records[0].keys())
        w.writeheader()
        w.writerows(records)
    summaries = []
    for p in policies:
        rs = [r for r in records if r["policy"] == p]
        severity = sorted(r["severity_ratio"] for r in rs)
        summaries.append(
            {
                "policy": p,
                "requests": len(rs),
                "budget_overrun_frequency": statistics.mean(r["overrun"] for r in rs),
                "p95_overrun_severity": severity[math.ceil(0.95 * len(severity)) - 1],
                "p99_overrun_severity": severity[math.ceil(0.99 * len(severity)) - 1],
                "average_reserve_micro": statistics.mean(r["reserve_micro"] for r in rs),
                "valid_admission_rate": statistics.mean(r["admitted"] for r in rs),
                "rejection_rate": statistics.mean(1 - r["admitted"] for r in rs),
                "reservation_latency_ms": None,
                "oversubscription_count": None,
                "duplicate_reservations": None,
                "orphaned_holds": None,
                "calibration_lag_sensitivity": None,
                "attribution_rate": None,
                "cleanup_success": None,
                "metric_note": "cloud-dependent metrics are null in simulation; see methodology",
            }
        )
    (out / "synthetic_summary.json").write_text(
        json.dumps(
            {
                "classification": "SYNTHETIC_NOT_AWS_BILLING",
                "seed": cfg["seed"],
                "config": cfg,
                "results": summaries,
            },
            indent=2,
        )
        + "\n"
    )
    with (out / "synthetic_summary.csv").open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=summaries[0].keys())
        w.writeheader()
        w.writerows(summaries)
    colors = {
        "fixed_1.10": "#2563eb",
        "rolling_quantile": "#dc2626",
        "split_conformal": "#16a34a",
        "adaptive": "#9333ea",
    }
    width, height = 900, 460
    left, top, plotw, ploth = 75, 45, 790, 330
    svg = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        '<rect width="100%" height="100%" fill="white"/>',
        '<text x="450" y="24" text-anchor="middle" font-family="sans-serif" font-size="17">Synthetic spend reserve policy evaluation</text>',
    ]
    for tick in range(6):
        y = top + ploth * tick / 5
        svg.append(f'<path d="M {left} {y:.1f} H {left + plotw}" stroke="#ddd"/>')
        svg.append(
            f'<text x="{left - 10}" y="{y + 4:.1f}" text-anchor="end" font-family="sans-serif" font-size="11">{1 - tick / 5:.1f}</text>'
        )
    svg += [
        f'<text x="{left + plotw / 2}" y="430" text-anchor="middle" font-family="sans-serif">Request index</text>',
        f'<text transform="translate(18 {top + ploth / 2}) rotate(-90)" text-anchor="middle" font-family="sans-serif">Rolling overrun frequency (100 requests)</text>',
    ]
    for pi, p in enumerate(policies):
        rs = [r for r in records if r["policy"] == p]
        window = 100
        vals = [
            statistics.mean(x["overrun"] for x in rs[max(0, j - window + 1) : j + 1])
            for j in range(len(rs))
        ]
        points = " ".join(
            f"{left + plotw * j / max(1, len(vals) - 1):.1f},{top + ploth * (1 - v):.1f}"
            for j, v in enumerate(vals)
        )
        svg.append(
            f'<polyline fill="none" stroke="{colors[p]}" stroke-width="1.5" points="{points}"/>'
        )
        lx = 90 + pi * 190
        svg.append(
            f'<path d="M {lx} 405 h 25" stroke="{colors[p]}" stroke-width="2"/><text x="{lx + 30}" y="409" font-family="sans-serif" font-size="12">{p}</text>'
        )
    svg.append("</svg>")
    (out / "synthetic_overrun.svg").write_text("\n".join(svg) + "\n")
    print(f"Wrote synthetic results to {out}; no AWS data was used.")


if __name__ == "__main__":
    main()
