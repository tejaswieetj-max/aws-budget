"""Human-run live AWS Price List comparison; intentionally not part of local validation."""

from __future__ import annotations

from decimal import Decimal

import boto3
from spendctl.pricing import PriceList, estimate_request

CONFIGS = (
    ("t3.micro", 8, 2),
    ("t3.small", 20, 6),
    ("t3.micro", 30, 24),
)


def main() -> None:
    provider = PriceList(boto3.client("pricing", region_name="us-east-1"))
    print("Live Price List estimate; compare these line items with AWS Pricing Calculator")
    print("instance_type | gp3_GB | hours | compute_USD | storage_USD | total_USD | snapshot_id")
    for instance_type, ebs_gb, hours in CONFIGS:
        estimate = estimate_request(
            {
                "resource": {"type": "ec2", "instance_type": instance_type, "ebs_gb": ebs_gb},
                "duration_hours": hours,
                "purpose": "manual-pricing-verification",
            },
            provider,
        )
        compute = Decimal(estimate["compute_micro"]) / Decimal(1_000_000)
        storage = Decimal(estimate["storage_micro"]) / Decimal(1_000_000)
        total = Decimal(estimate["total_micro"]) / Decimal(1_000_000)
        print(
            f"{instance_type} | {ebs_gb} | {hours} | {compute:.6f} | {storage:.6f} "
            f"| {total:.6f} | {estimate['price_snapshot_id']}"
        )


if __name__ == "__main__":
    main()
