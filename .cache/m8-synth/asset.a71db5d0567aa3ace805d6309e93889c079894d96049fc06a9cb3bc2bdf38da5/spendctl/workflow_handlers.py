"""Small Step Functions callbacks that persist lifecycle facts in the M2 ledger."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime
from decimal import ROUND_CEILING, Decimal

import boto3
from botocore.exceptions import ClientError

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
        launched_at=event.get("launched_at"),
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
    reservation = result["reservation"]
    _ensure_expiry_schedule(reservation)
    ledger.record_workflow_execution(
        event["reservation_id"],
        execution_status="RUNNING",
        instance_id=event["instance_id"],
        volume_ids=event.get("volume_ids", []),
        execution_arn=event["execution_arn"],
        launch_state="CREATED",
    )
    _publish_activated(reservation)
    return {"reservation": reservation}


def record_workflow_outcome_handler(event, context):
    reservation_id = event["reservation_id"]
    execution_status = event["execution_status"]
    cleanup_state = event.get("cleanup_state")
    ledger = _ledger()
    terminal_failure = execution_status in {"FAILED", "TIMED_OUT", "ABORTED"}
    terminated_at = event.get("terminated_at")
    if terminal_failure and cleanup_state == "COMPLETE":
        terminated_at = terminated_at or datetime.now(UTC).isoformat().replace("+00:00", "Z")
    result = ledger.record_workflow_execution(
        reservation_id,
        execution_status=execution_status,
        cleanup_state=cleanup_state,
        launch_state=event.get("launch_state"),
        terminated_at=terminated_at,
    )
    reservation = result["reservation"]
    if terminal_failure:
        if cleanup_state != "COMPLETE":
            ledger.record_alert(
                alert_id=f"CLEANUP_UNKNOWN#{reservation_id}",
                alert_type="WorkflowCleanupUncertain",
                details={
                    "reservation_id": reservation_id,
                    "cleanup_state": cleanup_state or "UNKNOWN",
                },
            )
            return {"reservation": reservation, "released": False, "state": "HELD_UNKNOWN"}
        launch_state = reservation.get("workflow_launch_state")
        if (
            reservation.get("workflow_start_state") != "STARTED"
            or launch_state
            not in {
                "CREATED",
                "NOT_CREATED",
            }
            or (
                launch_state == "CREATED"
                and (not reservation.get("instance_id") or not reservation.get("launched_at"))
            )
        ):
            ledger.record_alert(
                alert_id=f"LAUNCH_OUTCOME_UNKNOWN#{reservation_id}",
                alert_type="WorkflowCleanupUncertain",
                details={
                    "reservation_id": reservation_id,
                    "launch_state": launch_state or "UNKNOWN",
                },
            )
            return {"reservation": reservation, "released": False, "state": "HELD_UNKNOWN"}
        try:
            had_resources = bool(
                event.get("had_resources")
                or reservation.get("instance_id")
                or launch_state == "CREATED"
            )
            actual_micro = _confirmed_failure_actual(reservation, had_resources, terminated_at)
        except (KeyError, ValueError, TypeError):
            ledger.record_alert(
                alert_id=f"CLEANUP_COST_UNKNOWN#{reservation_id}",
                alert_type="WorkflowCleanupUncertain",
                details={
                    "reservation_id": reservation_id,
                    "reason": "runtime cost evidence is incomplete",
                },
            )
            return {"reservation": reservation, "released": False, "state": "HELD_UNKNOWN"}
        released = ledger.release_after_confirmed_cleanup(
            reservation_id,
            actual_micro,
            settlement_id=f"{reservation_id}:cleanup-failure:v1",
        )
        return {
            "reservation": released["reservation"],
            "released": True,
            "actual_micro": actual_micro,
        }
    return {"reservation": result["reservation"]}


def _confirmed_failure_actual(reservation: dict, had_resources: bool, terminated_at: str) -> int:
    """Prorate recorded compute and EBS estimates over confirmed resource runtime."""
    if not had_resources:
        return 0
    launched_at = reservation["launched_at"]
    start = datetime.fromisoformat(launched_at)
    stop = datetime.fromisoformat(terminated_at)
    elapsed = max(Decimal(60), Decimal(str((stop - start).total_seconds())))
    breakdown = reservation["estimate_breakdown"]
    compute_usd_per_hour = Decimal(str(breakdown["compute_usd_per_hour"]))
    gp3_usd_per_gb_month = Decimal(str(breakdown["gp3_usd_per_gb_month"]))
    hours_per_month = Decimal(str(breakdown["hours_per_month"]))
    ebs_gb = Decimal(str(reservation["resource"]["ebs_gb"]))
    actual_usd = compute_usd_per_hour * elapsed / Decimal(3600)
    actual_usd += gp3_usd_per_gb_month * ebs_gb * elapsed / (hours_per_month * Decimal(3600))
    return int((actual_usd * Decimal(1_000_000)).to_integral_value(rounding=ROUND_CEILING))


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


def _ensure_expiry_schedule(reservation: dict) -> None:
    group = _required_env("EXPIRY_SCHEDULE_GROUP")
    target_arn = _required_env("LIFECYCLE_REAPER_ARN")
    target_role = _required_env("SCHEDULER_TARGET_ROLE_ARN")
    expires_at = datetime.fromisoformat(reservation["expires_at"])
    expression = f"at({expires_at.astimezone(UTC).strftime('%Y-%m-%dT%H:%M:%S')})"
    name = _schedule_name(reservation["reservation_id"])
    event_id = hashlib.sha256(
        f"{reservation['reservation_id']}:expiry:{reservation['expires_at']}".encode()
    ).hexdigest()
    target_input = json.dumps(
        {
            "operation": "reap",
            "reason": "expiry",
            "reservation_id": reservation["reservation_id"],
            "team": reservation["team"],
            "event_id": event_id,
        },
        sort_keys=True,
    )
    scheduler = boto3.client("scheduler", region_name=os.environ.get("AWS_REGION", "ap-south-1"))
    request = {
        "Name": name,
        "GroupName": group,
        "ScheduleExpression": expression,
        "ScheduleExpressionTimezone": "UTC",
        "FlexibleTimeWindow": {"Mode": "OFF"},
        "ActionAfterCompletion": "DELETE",
        "Target": {
            "Arn": target_arn,
            "RoleArn": target_role,
            "Input": target_input,
            "RetryPolicy": {"MaximumEventAgeInSeconds": 86400, "MaximumRetryAttempts": 185},
        },
    }
    try:
        scheduler.create_schedule(**request)
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") != "ConflictException":
            raise
        existing = scheduler.get_schedule(Name=name, GroupName=group)
        actual_target = existing.get("Target", {})
        if (
            existing.get("ScheduleExpression") != expression
            or actual_target.get("Arn") != target_arn
            or actual_target.get("RoleArn") != target_role
            or actual_target.get("Input") != target_input
        ):
            raise RuntimeError(
                "existing expiry schedule conflicts with durable reservation"
            ) from error


def _publish_activated(reservation: dict) -> None:
    bus = _required_env("EVENT_BUS_NAME")
    event_id = hashlib.sha256(
        f"{reservation['reservation_id']}:activated:{reservation['launched_at']}".encode()
    ).hexdigest()
    result = boto3.client(
        "events", region_name=os.environ.get("AWS_REGION", "ap-south-1")
    ).put_events(
        Entries=[
            {
                "EventBusName": bus,
                "Source": "spendctl.lifecycle",
                "DetailType": "ReservationActivated",
                "Time": datetime.fromisoformat(reservation["launched_at"]),
                "Detail": json.dumps(
                    {
                        "event_id": event_id,
                        "schema_version": 1,
                        "reservation_id": reservation["reservation_id"],
                        "team": reservation["team"],
                        "occurred_at": reservation["launched_at"],
                        "expires_at": reservation["expires_at"],
                    },
                    sort_keys=True,
                ),
            }
        ]
    )
    if result.get("FailedEntryCount", 0):
        raise RuntimeError("failed to publish ReservationActivated event")


def _schedule_name(reservation_id: str) -> str:
    safe = "".join(c for c in reservation_id if c.isalnum() or c in "-_.")[:48]
    digest = hashlib.sha256(reservation_id.encode()).hexdigest()[:12]
    return f"expiry-{safe}-{digest}"[:64]
