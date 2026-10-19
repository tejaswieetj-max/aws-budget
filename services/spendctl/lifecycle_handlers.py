"""AWS Lambda entry points for M7 reaping, sweeps and audit events."""

from __future__ import annotations

import hashlib
import os

import boto3

from .ledger import DynamoLedger, ReservationConflict, ReservationNotFound
from .lifecycle import LifecycleService, inspect_bypass_event, process_instance_state_event
from .provisioner import _assume_provisioner_role, _describe_pages
from .workflow_start import WorkflowStarter


def reaper_handler(event, context):
    operation = event.get("operation", "reap")
    if operation == "sweep":
        return _lifecycle_service(event).sweep(limit=50)
    return _lifecycle_service(event).reap(event)


def state_change_handler(event, context):
    ledger = _ledger()
    ec2 = boto3.client("ec2", region_name=_region())
    try:
        return process_instance_state_event(event, ledger=ledger, ec2=ec2)
    except (ReservationNotFound, ReservationConflict) as error:
        detail = event.get("detail") or {}
        instance_id = detail.get("instance-id") or detail.get("instanceId", "unknown")
        _record_alert(
            ledger,
            "UnattributedInstanceState",
            f"state:{event.get('id', instance_id)}",
            {"instance_id": instance_id, "reason": type(error).__name__},
        )
        return {"state": "UNATTRIBUTED", "instance_id": instance_id}


def bypass_handler(event, context):
    return inspect_bypass_event(
        event, ledger=_ledger(), provisioner_role_arn=_required_env("PROVISIONER_ROLE_ARN")
    )


def orphan_audit_handler(event, context):
    ledger = _ledger()
    ec2 = boto3.client("ec2", region_name=_region())
    filters = [{"Name": "tag:managed-by", "Values": ["spend-controller"]}]
    instances = _describe_pages(ec2.describe_instances, "Reservations", filters=filters)
    flattened = [instance for row in instances for instance in row.get("Instances", [])]
    volumes = _describe_pages(ec2.describe_volumes, "Volumes", filters=filters)
    unmatched = 0
    for resource_type, resources, id_key in (
        ("instance", flattened, "InstanceId"),
        ("volume", volumes, "VolumeId"),
    ):
        for resource in resources:
            tags = {tag.get("Key"): tag.get("Value") for tag in resource.get("Tags", [])}
            reservation_id = tags.get("reservation-id")
            reason = None
            try:
                reservation = ledger.get_reservation(reservation_id) if reservation_id else None
            except ReservationNotFound:
                reservation = None
            if not reservation:
                reason = "reservation not found"
            elif tags.get("team") != reservation.get("team"):
                reason = "team ownership tag does not match ledger"
            elif tags.get("project") != "spendctl" or tags.get("env") != "dev":
                reason = "project or environment ownership tag does not match"
            elif resource_type == "instance" and reservation.get("instance_id") != resource.get(id_key):
                reason = "instance is not the ledger-bound resource"
            elif (
                resource_type == "instance"
                and reservation.get("status") in {"RELEASED", "SETTLED", "EXPIRED", "CANCELLED"}
                and resource.get("State", {}).get("Name") not in {"terminated", "shutting-down"}
            ):
                reason = "live instance remains for a terminal reservation"
            elif resource_type == "volume" and resource.get(id_key) not in reservation.get("volume_ids", []):
                reason = "volume is not ledger-bound"
            elif (
                resource_type == "volume"
                and reservation.get("workflow_cleanup_state") == "COMPLETE"
                and resource.get("State") != "deleted"
            ):
                reason = "volume remains after cleanup was recorded complete"
            if reason:
                unmatched += 1
                _record_alert(
                    ledger,
                    "OrphanResourceDetected",
                    f"orphan:{resource_type}:{resource.get(id_key)}",
                    {
                        "resource_type": resource_type,
                        "resource_id": resource.get(id_key),
                        "reservation_id": reservation_id,
                        "team": tags.get("team"),
                        "reason": reason,
                    },
                )
    return {"state": "AUDITED", "resource_count": len(flattened) + len(volumes), "unmatched": unmatched}


def _lifecycle_service(event):
    ledger = _ledger()
    sts = boto3.client("sts", region_name=_region())
    def ec2_for_reservation(reservation):
        role_event = {
            "reservation_id": reservation["reservation_id"],
            "team": reservation["team"],
        }
        credentials = _assume_provisioner_role(role_event, _Boto3WithSts(sts))
        return boto3.client("ec2", region_name=_region(), **credentials)

    stepfunctions = boto3.client("stepfunctions", region_name=_region())
    workflow = WorkflowStarter(
        stepfunctions,
        _required_env("WORKFLOW_STATE_MACHINE_ARN"),
        ledger,
    )
    return LifecycleService(
        ledger,
        ec2=ec2_for_reservation,
        workflow_starter=workflow,
        stepfunctions=stepfunctions,
        eventbridge=boto3.client("events", region_name=_region()),
        event_bus_name=os.environ.get("EVENT_BUS_NAME"),
    )


class _Boto3WithSts:
    """Inject an existing STS client into the provisioner role-assumption helper."""

    def __init__(self, sts):
        self.sts = sts

    def client(self, service, **kwargs):
        if service == "sts":
            return self.sts
        return boto3.client(service, **kwargs)


def _ledger():
    return DynamoLedger(
        boto3.client("dynamodb", region_name=_region()),
        _required_env("BUDGETS_TABLE"),
        _required_env("RESERVATIONS_TABLE"),
        _required_env("ALERTS_TABLE"),
    )


def _record_alert(ledger, alert_type, stable_key, details):
    alert_id = hashlib.sha256(stable_key.encode()).hexdigest()
    return ledger.record_alert(alert_id=alert_id, alert_type=alert_type, details=details)


def _required_env(name):
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"missing required environment variable {name}")
    return value


def _region():
    return os.environ.get("AWS_REGION", "ap-south-1")
