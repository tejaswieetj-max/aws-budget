"""Small Step Functions callbacks that persist lifecycle facts in the M2 ledger."""

from __future__ import annotations

import os
from datetime import UTC, datetime

import boto3

from .ledger import DynamoLedger


def record_resources_handler(event, context):
    ledger = _ledger()
    result = ledger.record_workflow_execution(
        event["reservation_id"],
        execution_status="RUNNING",
        instance_id=event["instance_id"],
        volume_ids=event.get("volume_ids", []),
        execution_arn=event["execution_arn"],
        launch_state="CREATED",
    )
    return {"reservation": result["reservation"]}


def activate_handler(event, context):
    ledger = _ledger()
    launched_at = event.get("launched_at") or datetime.now(UTC).isoformat().replace("+00:00", "Z")
    result = ledger.activate(
        event["reservation_id"],
        launched_at=launched_at,
        instance_id=event["instance_id"],
        volume_ids=event.get("volume_ids", []),
        execution_arn=event["execution_arn"],
    )
    ledger.record_workflow_execution(
        event["reservation_id"],
        execution_status="RUNNING",
        instance_id=event["instance_id"],
        volume_ids=event.get("volume_ids", []),
        execution_arn=event["execution_arn"],
        launch_state="CREATED",
    )
    return {"reservation": result["reservation"]}


def record_workflow_outcome_handler(event, context):
    reservation_id = event["reservation_id"]
    execution_status = event["execution_status"]
    cleanup_state = event.get("cleanup_state")
    result = _ledger().record_workflow_execution(
        reservation_id,
        execution_status=execution_status,
        cleanup_state=cleanup_state,
        launch_state=event.get("launch_state"),
    )
    return {"reservation": result["reservation"]}


def release_prelaunch_failure_handler(event, context):
    ledger = _ledger()
    ledger.record_workflow_execution(
        event["reservation_id"],
        execution_status="FAILED",
        cleanup_state="NOT_CREATED",
        launch_state="NOT_CREATED",
    )
    result = ledger.release_prelaunch_failure(event["reservation_id"])
    return {"reservation": result["reservation"], "released": True}


def _ledger() -> DynamoLedger:
    region = os.environ.get("AWS_REGION", "ap-south-1")
    client = boto3.client("dynamodb", region_name=region)
    return DynamoLedger(
        client,
        _required_env("BUDGETS_TABLE"),
        _required_env("RESERVATIONS_TABLE"),
        os.environ.get("ALERTS_TABLE", "spendctl-dev-alerts"),
    )


def _required_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"missing required environment variable {name}")
    return value
