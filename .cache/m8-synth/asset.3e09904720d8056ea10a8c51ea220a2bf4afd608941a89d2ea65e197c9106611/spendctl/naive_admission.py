"""Deliberately non-atomic admission baseline for isolated concurrency tests only."""

from __future__ import annotations

import os
from threading import Barrier
from typing import Any


class NaiveAdmissionDisabled(RuntimeError):
    pass


def naive_reserve_for_experiment(
    db: Any,
    *,
    budgets_table: str,
    team: str,
    period: str,
    reserve_micro: int,
    after_read: Barrier | None = None,
) -> bool:
    """Perform the intentionally unsafe read/check/unconditional-update baseline.

    This helper has no API route and is used only by the opt-in experiment stack
    and Moto test. It intentionally bypasses the atomic M2 transaction.
    """
    if os.environ.get("ADMISSION_MODE", "atomic") != "naive":
        raise NaiveAdmissionDisabled("set ADMISSION_MODE=naive for the isolated experiment")
    if os.environ.get("ALLOW_NAIVE_ADMISSION", "false").lower() != "true":
        raise NaiveAdmissionDisabled(
            "naive admission requires ALLOW_NAIVE_ADMISSION=true in the isolated experiment"
        )
    if isinstance(reserve_micro, bool) or not isinstance(reserve_micro, int) or reserve_micro <= 0:
        raise ValueError("reserve_micro must be a positive integer")
    response = db.get_item(
        TableName=budgets_table,
        Key={"team": {"S": team}, "period": {"S": period}},
        ConsistentRead=True,
    )
    item = response.get("Item")
    if not item:
        return False
    available = int(item["available_micro"]["N"])
    if available < reserve_micro:
        return False
    if after_read is not None:
        after_read.wait(timeout=5)
    # Deliberately lacks a condition expression or transaction. Do not use in
    # ordinary traffic; this permits a concurrent test to demonstrate oversell.
    db.update_item(
        TableName=budgets_table,
        Key={"team": {"S": team}, "period": {"S": period}},
        UpdateExpression=(
            "SET available_micro = available_micro - :amount, "
            "reserved_micro = reserved_micro + :amount, version = version + :one"
        ),
        ExpressionAttributeValues={":amount": {"N": str(reserve_micro)}, ":one": {"N": "1"}},
    )
    return True


def naive_admission_handler(event, context):
    """Restricted test-stack entry point; normal application stacks do not expose it."""
    import boto3

    if os.environ.get("ADMISSION_MODE") != "naive":
        raise NaiveAdmissionDisabled("test stack must explicitly set ADMISSION_MODE=naive")
    if os.environ.get("ALLOW_NAIVE_ADMISSION", "false").lower() != "true":
        raise NaiveAdmissionDisabled("test stack must explicitly allow naive admission")
    db = boto3.client("dynamodb", region_name=os.environ.get("AWS_REGION", "ap-south-1"))
    accepted = naive_reserve_for_experiment(
        db,
        budgets_table=os.environ["BUDGETS_TABLE"],
        team=event["team"],
        period=event["period"],
        reserve_micro=event["reserve_micro"],
    )
    return {"accepted": accepted, "mode": "naive", "experiment_only": True}
