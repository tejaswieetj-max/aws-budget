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
from evaluate import atomic_vs_naive_experiment, calibration_lag_sensitivity
from spendctl.calibration import MarginPolicies, Sample


def main():
    parser = argparse.ArgumentParser(
        description="Regenerate seeded synthetic reserve-policy, concurrency, and feedback-lag artifacts. No AWS data is used."
    )
    parser.add_argument("--config", default="research/config.json")
    args = parser.parse_args()
    cfg = json.loads(Path(args.config).read_text())
    _validate_config(cfg)
    rng = random.Random(cfg["seed"])
    out = Path(cfg["output_dir"])
    out.mkdir(parents=True, exist_ok=True)
    policies = ["fixed_1.10", "rolling_quantile", "split_conformal", "adaptive"]
    records = []
    calibration = MarginPolicies(cfg["gamma"], cfg["target_alpha"])
    reserve_floor, reserve_ceiling = map(float, cfg["reserve_bounds"])
    # Log-normal multiplicative model error with deterministic regime shift.
    ratios = [
        math.exp(rng.gauss(0.015 if i < cfg["requests"] // 2 else 0.06, 0.18))
        for i in range(cfg["requests"])
    ]
    # Non-adaptive split conformal uses an early calibration partition, frozen before evaluation.
    split = sorted(ratios[: cfg["warmup"]])
    split_multiplier = min(
        reserve_ceiling,
        max(reserve_floor, split[min(len(split) - 1, math.ceil(0.95 * (len(split) + 1)) - 1)]),
    )
    multiplier_hist = {p: [] for p in policies}
    for i, ratio in enumerate(ratios):
        modeled = 1_000_000
        candidates = {
            "fixed_1.10": cfg["fixed_multiplier"],
            "rolling_quantile": calibration.get("ec2:t3", "rolling_quantile"),
            "split_conformal": split_multiplier,
            "adaptive": calibration.get("ec2:t3", "adaptive"),
        }
        for policy, margin in candidates.items():
            margin = min(reserve_ceiling, max(reserve_floor, margin))
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
    concurrency = atomic_vs_naive_experiment(
        seed=cfg["seed"], concurrency=cfg.get("concurrency_levels", [1, 2, 4, 8, 16, 32]),
        request_count=cfg.get("concurrency_requests", 200), budget_micro=cfg["budget_micro"],
    )
    lag = calibration_lag_sensitivity(
        seed=cfg["seed"], requests=cfg.get("lag_requests", 1000),
        lags=cfg.get("calibration_lags", [0, 1, 7, 30]),
    )
    (out / "synthetic_concurrency.json").write_text(
        json.dumps({"classification": "SYNTHETIC_LOCAL_SCHEDULE_MODEL", "seed": cfg["seed"], "results": concurrency}, indent=2) + "\n"
    )
    (out / "synthetic_calibration_lag.json").write_text(
        json.dumps({"classification": "SYNTHETIC_NOT_AWS_BILLING", "seed": cfg["seed"], "results": lag}, indent=2) + "\n"
    )
    with (out / "synthetic_concurrency.csv").open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=concurrency[0].keys())
        writer.writeheader(); writer.writerows(concurrency)
    with (out / "synthetic_calibration_lag.csv").open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=lag[0].keys())
        writer.writeheader(); writer.writerows(lag)
    _write_svg(
        out / "synthetic_concurrency.svg",
        "Synthetic local schedule: atomic vs naive over-admission",
        [row["concurrency"] for row in concurrency],
        {"Atomic": [row["atomic_over_admission_micro"] for row in concurrency],
         "Naive": [row["naive_over_admission_micro"] for row in concurrency]},
        "Over-admission (micro-USD)",
    )
    _write_svg(
        out / "synthetic_calibration_lag.svg",
        "Synthetic adaptive reserve sensitivity to delayed feedback",
        [row["feedback_lag_requests"] for row in lag],
        {"Adaptive overrun frequency": [row["adaptive_overrun_frequency"] for row in lag]},
        "Overrun frequency",
    )
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


def _validate_config(cfg):
    if not isinstance(cfg, dict):
        raise TypeError("research config must be a JSON object")
    for field in ("seed", "requests", "teams", "budget_micro", "gamma", "target_alpha",
                  "fixed_multiplier", "warmup", "output_dir"):
        if field not in cfg:
            raise ValueError(f"research config is missing {field}")
    for field in ("requests", "teams", "budget_micro", "warmup"):
        value = cfg[field]
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(f"{field} must be a positive integer")
    if cfg["warmup"] > cfg["requests"]:
        raise ValueError("warmup cannot exceed requests")
    if not isinstance(cfg["seed"], int) or isinstance(cfg["seed"], bool):
        raise TypeError("seed must be an integer")
    if not 0.01 <= float(cfg["gamma"]) <= 0.05 or float(cfg["target_alpha"]) != 0.05:
        raise ValueError("gamma must be in [0.01, 0.05] and target_alpha must be 0.05")
    if float(cfg["fixed_multiplier"]) != 1.10:
        raise ValueError("fixed_multiplier must remain the production baseline 1.10")
    bounds = cfg.get("reserve_bounds")
    if not isinstance(bounds, list) or len(bounds) != 2:
        raise ValueError("reserve_bounds must contain [minimum, maximum]")
    lower, upper = map(float, bounds)
    if not 1.0 <= lower <= upper <= 2.0:
        raise ValueError("reserve_bounds must be ordered within [1.0, 2.0]")


def _write_svg(path, title, labels, series, y_label):
    width, height = 820, 420
    left, top, plot_width, plot_height = 80, 50, 700, 285
    maximum = max((float(value) for values in series.values() for value in values), default=1.0)
    maximum = maximum if maximum > 0 else 1.0
    colors = ["#166b53", "#dc6a3d", "#7359a8"]
    lines = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}">',
             '<rect width="100%" height="100%" fill="white"/>',
             f'<text x="410" y="25" text-anchor="middle" font-family="sans-serif" font-size="16">{title}</text>']
    for tick in range(6):
        value = maximum * tick / 5
        y = top + plot_height * (1 - tick / 5)
        lines.append(f'<path d="M {left} {y:.1f} H {left + plot_width}" stroke="#e1e5e3"/>')
        lines.append(f'<text x="{left - 8}" y="{y + 4:.1f}" text-anchor="end" font-family="sans-serif" font-size="10">{value:.3g}</text>')
    for index, (name, values) in enumerate(series.items()):
        points = []
        for i, value in enumerate(values):
            x = left + plot_width * i / max(1, len(values) - 1)
            y = top + plot_height * (1 - float(value) / maximum)
            points.append(f"{x:.1f},{y:.1f}")
            lines.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3" fill="{colors[index % len(colors)]}"/>')
        lines.append(f'<polyline fill="none" stroke="{colors[index % len(colors)]}" stroke-width="2" points="{" ".join(points)}"/>')
        lines.append(f'<text x="{left + 18 + index * 205}" y="390" font-family="sans-serif" font-size="11" fill="{colors[index % len(colors)]}">{name}</text>')
    for i, label in enumerate(labels):
        x = left + plot_width * i / max(1, len(labels) - 1)
        lines.append(f'<text x="{x:.1f}" y="354" text-anchor="middle" font-family="sans-serif" font-size="10">{label}</text>')
    lines.append(f'<text x="{left + plot_width / 2}" y="415" text-anchor="middle" font-family="sans-serif" font-size="11">Concurrency / feedback lag (requests)</text>')
    lines.append(f'<text transform="translate(16 {top + plot_height / 2}) rotate(-90)" text-anchor="middle" font-family="sans-serif" font-size="11">{y_label}</text>')
    lines.append("</svg>")
    path.write_text("\n".join(lines) + "\n")


if __name__ == "__main__":
    main()
