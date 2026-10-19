"""M7 safe reaping, cancellation and reconciliation primitives."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any

from botocore.exceptions import BotoCoreError, ClientError

from .ledger import AmbiguousLedgerOutcome, InvalidTransition, ReservationNotFound
from .provisioner import (
    OwnershipVerificationError,
    cleanup_reservation,
    find_reservation_resources,
    inspect_expected_cleanup,
    terminate_expected_resources,
)
from .workflow_start import WorkflowStartOutcomeUnknown

_RETRYABLE_AWS_ERRORS = (BotoCoreError, ClientError, TimeoutError, ConnectionError)
_TERMINAL_EXECUTIONS = {"FAILED", "TIMED_OUT", "ABORTED"}


class LifecycleService:
    """Lifecycle service with injected AWS clients; no operation releases uncertain funds."""

    def __init__(
        self,
        ledger,
        *,
        ec2: Any,
        workflow_starter=None,
        stepfunctions=None,
        eventbridge=None,
        event_bus_name: str | None = None,
        now=None,
    ):
        self.ledger = ledger
        self.ec2 = ec2
        self.workflow_starter = workflow_starter
        self.stepfunctions = stepfunctions
        self.eventbridge = eventbridge
        self.event_bus_name = event_bus_name
        self.now = now or (lambda: datetime.now(UTC))

    def request_cancel(self, reservation_id: str, *, event_id: str | None = None) -> dict:
        """Make ACTIVE -> CANCELLED durable before any asynchronous cleanup."""
        reservation = self.ledger.get_reservation(reservation_id)
        if reservation["status"] == "ACTIVE":
            reservation = self.ledger.cancel_active(reservation_id)["reservation"]
        elif reservation["status"] not in {"CANCELLED", "EXPIRED"}:
            raise InvalidTransition(
                f"cannot cancel reservation in {reservation['status']}; only active resources can be cancelled"
            )
        return {
            "reservation": reservation,
            "event_id": event_id or lifecycle_event_id(reservation, "cancel"),
            "replayed": reservation.get("status") in {"CANCELLED", "EXPIRED"},
        }

    def reap(self, event: dict) -> dict:
        reservation_id = event["reservation_id"]
        reservation = self.ledger.get_reservation(reservation_id)
        reason = event.get("reason", "expiry")
        if event.get("dry_run"):
            if reservation["status"] == "ACTIVE" and reason in {"expiry", "sweep"}:
                if _parse_time(reservation["expires_at"]) > self.now():
                    return {"state": "NOT_DUE", "reservation_id": reservation_id}
                return self._dry_run_plan(reservation)
            if reservation["status"] in {"CANCELLED", "EXPIRED"}:
                return self._dry_run_plan(reservation)
            return {"state": "NOT_ELIGIBLE", "reservation_id": reservation_id}
        if reservation["status"] == "ACTIVE":
            if reason == "cancel":
                reservation = self.ledger.cancel_active(reservation_id)["reservation"]
            elif reason in {"expiry", "sweep"}:
                if _parse_time(reservation["expires_at"]) > self.now():
                    return {"state": "NOT_DUE", "reservation_id": reservation_id}
                reservation = self.ledger.mark_expired(reservation_id)["reservation"]
            else:
                raise ValueError("unsupported lifecycle reason")
        if reservation["status"] not in {"CANCELLED", "EXPIRED"}:
            return self._reconcile_nonterminal(reservation)

        if not reservation.get("instance_id"):
            self._alert(
                reservation,
                "LifecycleResourceUnknown",
                f"missing-instance:{reservation_id}",
                {"reason": "reservation has no ledger-bound instance id"},
            )
            self.ledger.record_lifecycle_cleanup(reservation_id, cleanup_state="UNKNOWN")
            return {"state": "UNKNOWN", "reservation_id": reservation_id}

        try:
            result = terminate_expected_resources(
                {
                    **reservation,
                    "reservation_id": reservation_id,
                    "team": reservation["team"],
                    "instance_id": reservation["instance_id"],
                },
                ec2=self._ec2(reservation),
            )
        except OwnershipVerificationError as error:
            self.ledger.record_lifecycle_cleanup(reservation_id, cleanup_state="UNKNOWN")
            self._alert(
                reservation,
                "OwnershipVerificationFailed",
                f"ownership:{reservation_id}:{reservation['instance_id']}",
                {"message": str(error), "instance_id": reservation["instance_id"]},
            )
            return {"state": "UNKNOWN", "reservation_id": reservation_id}
        except _RETRYABLE_AWS_ERRORS:
            self.ledger.record_lifecycle_cleanup(reservation_id, cleanup_state="UNKNOWN")
            self._alert(
                reservation,
                "LifecycleCleanupFailed",
                f"cleanup-failed:{reservation_id}",
                {"reason": "EC2 cleanup request failed; periodic retry is scheduled"},
            )
            raise

        cleanup_state = result["cleanup_state"]
        terminated_at = None
        if cleanup_state == "COMPLETE":
            terminated_at = reservation.get("terminated_at") or _iso(self.now())
        self.ledger.record_lifecycle_cleanup(
            reservation_id, cleanup_state=cleanup_state, terminated_at=terminated_at
        )
        if cleanup_state != "COMPLETE":
            if cleanup_state == "UNKNOWN":
                self._alert(
                    reservation,
                    "LifecycleResourceUnknown",
                    f"cleanup-unknown:{reservation_id}",
                    {"reason": result.get("reason", "resource state is not confirmed")},
                )
            return {"state": cleanup_state, "reservation_id": reservation_id}

        # Until M8 billing is available, move the full hold to provisional spend.
        # This is conservative and preserves the budget invariant; it does not
        # claim that the estimate equals actual cost.
        settled = self.ledger.provisional_settle(
            reservation_id,
            reservation["reserved_micro"],
            settlement_id=f"{reservation_id}:provisional",
            settled_at=terminated_at,
        )
        if settled["reservation"]["status"] == "EXPIRED":
            self._publish_expired(settled["reservation"])
        return {
            "state": "COMPLETE",
            "reservation_id": reservation_id,
            "reservation": settled["reservation"],
        }

    def sweep(self, *, now: str | None = None, limit: int = 100) -> dict:
        """Reap overdue ACTIVE and retry terminal reservations with incomplete cleanup."""
        instant = now or _iso(self.now())
        processed = []
        processed_ids = set()

        def process(reservation, reason):
            reservation_id = reservation["reservation_id"]
            if reservation_id in processed_ids:
                return
            processed_ids.add(reservation_id)
            try:
                result = self.reap(
                    {
                        "reservation_id": reservation_id,
                        "reason": reason,
                        "event_id": lifecycle_event_id(reservation, reason),
                    }
                )
            except _RETRYABLE_AWS_ERRORS as error:
                self._alert(
                    reservation,
                    "LifecycleRetryScheduled",
                    f"sweep-retry:{reservation_id}:{reason}",
                    {"reason": type(error).__name__},
                )
                result = {"state": "UNKNOWN", "reservation_id": reservation_id}
            processed.append(result)

        token = None
        while True:
            page = self.ledger.list_expired_active(now=instant, limit=limit, start_key=token)
            for reservation in page["items"]:
                process(reservation, "sweep")
            token = page.get("last_evaluated_key") or None
            if not token:
                break

        for status in ("CANCELLED", "EXPIRED"):
            token = None
            while True:
                page = self.ledger.list_reservations(status=status, limit=limit, start_key=token)
                for reservation in page["items"]:
                    if (
                        reservation.get("workflow_cleanup_state") == "COMPLETE"
                        and reservation.get("settlement_stage") == "PROVISIONAL"
                        and (
                            status == "CANCELLED"
                            or reservation.get("expiry_event_published")
                            or self.eventbridge is None
                            or not self.event_bus_name
                        )
                    ):
                        continue
                    process(reservation, "cancel" if status == "CANCELLED" else "expiry")
                token = page.get("last_evaluated_key") or None
                if not token:
                    break

        token = None
        while True:
            page = self.ledger.list_reservations(status="HELD", limit=limit, start_key=token)
            for reservation in page["items"]:
                if reservation.get("workflow_start_state") in {"STARTING", "UNKNOWN", "STARTED"}:
                    processed.append(self.reconcile_held(reservation))
            token = page.get("last_evaluated_key") or None
            if not token:
                break

        return {
            "processed": len(processed),
            "results": processed,
            "unknown": sum(result.get("state") == "UNKNOWN" for result in processed),
        }

    def reconcile_held(self, reservation: dict) -> dict:
        """Retry the same workflow identity only after checking for tagged resources."""
        if reservation.get("status") != "HELD":
            return {"state": "NOT_HELD", "reservation_id": reservation["reservation_id"]}
        start_state = reservation.get("workflow_start_state")
        if start_state == "STARTED":
            return self._reconcile_started_held(reservation)
        if start_state not in {"STARTING", "UNKNOWN"}:
            return {"state": "NO_AUTOMATIC_ACTION", "reservation_id": reservation["reservation_id"]}
        event = {"reservation_id": reservation["reservation_id"], "team": reservation["team"]}
        try:
            resources = find_reservation_resources(event, ec2=self._ec2(reservation))
        except _RETRYABLE_AWS_ERRORS:
            self._alert(
                reservation,
                "WorkflowReconciliationUnknown",
                f"resource-query:{reservation['reservation_id']}",
                {"reason": "could not verify tagged resources"},
            )
            return {"state": "UNKNOWN", "reservation_id": reservation["reservation_id"]}
        if resources["instances"] or resources["volumes"]:
            self._alert(
                reservation,
                "WorkflowReconciliationRequired",
                f"resources-found:{reservation['reservation_id']}",
                {
                    "instance_ids": [item.get("InstanceId") for item in resources["instances"]],
                    "volume_ids": [item.get("VolumeId") for item in resources["volumes"]],
                },
            )
            return {"state": "UNKNOWN", "reservation_id": reservation["reservation_id"]}
        if self.workflow_starter is None:
            return {"state": "UNKNOWN", "reservation_id": reservation["reservation_id"]}
        try:
            outcome = self.workflow_starter.start(reservation)
        except (
            WorkflowStartOutcomeUnknown,
            AmbiguousLedgerOutcome,
            InvalidTransition,
            BotoCoreError,
            ClientError,
            TimeoutError,
            ConnectionError,
        ) as error:
            self._alert(
                reservation,
                "WorkflowReconciliationUnknown",
                f"start:{reservation['reservation_id']}",
                {"reason": type(error).__name__},
            )
            return {"state": "UNKNOWN", "reservation_id": reservation["reservation_id"]}
        return {
            "state": outcome.get("state", "UNKNOWN"),
            "reservation_id": reservation["reservation_id"],
        }

    def _reconcile_started_held(self, reservation: dict) -> dict:
        arn = reservation.get("workflow_execution_arn")
        if not arn or self.stepfunctions is None:
            self._alert(
                reservation,
                "WorkflowReconciliationUnknown",
                f"execution-identity:{reservation['reservation_id']}",
                {"reason": "started workflow has no execution ARN"},
            )
            return {"state": "UNKNOWN", "reservation_id": reservation["reservation_id"]}
        try:
            execution = self.stepfunctions.describe_execution(executionArn=arn)
        except _RETRYABLE_AWS_ERRORS:
            self._alert(
                reservation,
                "WorkflowReconciliationUnknown",
                f"execution-status:{reservation['reservation_id']}",
                {"reason": "execution status could not be read"},
            )
            return {"state": "UNKNOWN", "reservation_id": reservation["reservation_id"]}
        status = execution.get("status", "UNKNOWN")
        if status == "RUNNING":
            return {"state": "RUNNING", "reservation_id": reservation["reservation_id"]}
        if status not in _TERMINAL_EXECUTIONS:
            self._alert(
                reservation,
                "WorkflowReconciliationUnknown",
                f"unexpected-execution-status:{reservation['reservation_id']}:{status}",
                {
                    "execution_status": status,
                    "reason": "held reservation has unresolved execution state",
                },
            )
            return {"state": "UNKNOWN", "reservation_id": reservation["reservation_id"]}
        if reservation.get("workflow_execution_status") != status:
            reservation = self.ledger.record_workflow_execution(
                reservation["reservation_id"], execution_status=status, execution_arn=arn
            )["reservation"]
        try:
            resources = find_reservation_resources(
                {"reservation_id": reservation["reservation_id"], "team": reservation["team"]},
                ec2=self._ec2(reservation),
            )
        except _RETRYABLE_AWS_ERRORS as error:
            self._alert(
                reservation,
                "WorkflowCleanupUncertain",
                f"terminal-resource-query:{reservation['reservation_id']}",
                {"reason": type(error).__name__},
            )
            return {"state": "UNKNOWN", "reservation_id": reservation["reservation_id"]}
        if not resources["instances"] and not resources["volumes"]:
            if (
                reservation.get("workflow_execution_status") == "FAILED"
                and reservation.get("workflow_launch_state") == "NOT_CREATED"
            ):
                released = self.ledger.release_prelaunch_failure(reservation["reservation_id"])
                return {"state": "RELEASED_PRELAUNCH", "reservation": released["reservation"]}
            self._alert(
                reservation,
                "WorkflowCleanupUncertain",
                f"terminal-no-resources:{reservation['reservation_id']}",
                {
                    "execution_status": status,
                    "launch_state": reservation.get("workflow_launch_state", "UNKNOWN"),
                },
            )
            return {"state": "UNKNOWN", "reservation_id": reservation["reservation_id"]}

        try:
            result = cleanup_reservation(
                {"reservation_id": reservation["reservation_id"], "team": reservation["team"]},
                ec2=self._ec2(reservation),
            )
        except _RETRYABLE_AWS_ERRORS as error:
            self._alert(
                reservation,
                "WorkflowCleanupUncertain",
                f"terminal-cleanup:{reservation['reservation_id']}",
                {"reason": type(error).__name__},
            )
            return {"state": "UNKNOWN", "reservation_id": reservation["reservation_id"]}
        self.ledger.record_workflow_execution(
            reservation["reservation_id"],
            execution_status=status,
            cleanup_state=result["cleanup_state"],
            launch_state=reservation.get("workflow_launch_state", "UNKNOWN"),
        )
        self._alert(
            reservation,
            "WorkflowCleanupReconciled",
            f"terminal-resources:{reservation['reservation_id']}",
            {
                "execution_status": status,
                "cleanup_state": result["cleanup_state"],
                "instance_ids": result["instance_ids"],
                "volume_ids": result["volume_ids"],
            },
        )
        return {
            "state": "CLEANUP_" + result["cleanup_state"],
            "reservation_id": reservation["reservation_id"],
        }

    def _reconcile_nonterminal(self, reservation: dict) -> dict:
        if reservation["status"] == "HELD":
            return self.reconcile_held(reservation)
        return {"state": "NOT_ELIGIBLE", "reservation_id": reservation["reservation_id"]}

    def _dry_run_plan(self, reservation: dict) -> dict:
        result = inspect_expected_cleanup(
            {
                **reservation,
                "reservation_id": reservation["reservation_id"],
                "team": reservation["team"],
                "instance_id": reservation.get("instance_id"),
            },
            ec2=self._ec2(reservation),
        )
        return {"state": "DRY_RUN", "plan": result, "reservation_id": reservation["reservation_id"]}

    def _alert(self, reservation: dict, alert_type: str, stable_key: str, details: dict) -> None:
        alert_id = hashlib.sha256(stable_key.encode()).hexdigest()
        self.ledger.record_alert(
            alert_id=alert_id,
            alert_type=alert_type,
            details={
                **details,
                "reservation_id": reservation.get("reservation_id"),
                "team": reservation.get("team"),
            },
        )

    def _ec2(self, reservation: dict):
        if callable(self.ec2):
            return self.ec2(reservation)
        return self.ec2

    def _publish_expired(self, reservation: dict) -> None:
        if self.eventbridge is None or not self.event_bus_name:
            return
        event_id = lifecycle_event_id(reservation, "expired")
        if reservation.get("expiry_event_published"):
            return
        response = self.eventbridge.put_events(
            Entries=[
                {
                    "EventBusName": self.event_bus_name,
                    "Source": "spendctl.lifecycle",
                    "DetailType": "ReservationExpired",
                    "Time": _parse_time(reservation["terminated_at"]),
                    "Detail": json.dumps(
                        {
                            "event_id": event_id,
                            "schema_version": 1,
                            "reservation_id": reservation["reservation_id"],
                            "team": reservation["team"],
                            "occurred_at": reservation["terminated_at"],
                        },
                        sort_keys=True,
                    ),
                }
            ]
        )
        if response.get("FailedEntryCount", 0):
            raise RuntimeError("failed to publish ReservationExpired event")
        self.ledger.mark_expiry_event_published(reservation["reservation_id"], event_id)


def lifecycle_event_id(reservation: dict, action: str) -> str:
    stamp = (
        reservation.get("cancelled_at")
        or reservation.get("expired_at")
        or reservation.get("expires_at", "")
    )
    source = f"{reservation['reservation_id']}:{action}:{stamp}"
    return hashlib.sha256(source.encode()).hexdigest()


def process_instance_state_event(event: dict, *, ledger, ec2) -> dict:
    detail = event.get("detail") or {}
    instance_id = detail.get("instance-id") or detail.get("instanceId")
    state = detail.get("state")
    if not instance_id or not state:
        raise ValueError("EC2 state-change event is missing instance-id or state")
    response = ec2.describe_instances(InstanceIds=[instance_id])
    instances = [
        i for group in response.get("Reservations", []) for i in group.get("Instances", [])
    ]
    if not instances:
        return {"state": "UNKNOWN", "instance_id": instance_id}
    tags = {tag.get("Key"): tag.get("Value") for tag in instances[0].get("Tags", [])}
    if (
        tags.get("managed-by") != "spend-controller"
        or tags.get("project") != "spendctl"
        or tags.get("env") != "dev"
    ):
        return {"state": "IGNORED", "instance_id": instance_id}
    reservation_id = tags.get("reservation-id")
    if not reservation_id:
        return {"state": "UNATTRIBUTED", "instance_id": instance_id}
    occurred_at = event.get("time") or _iso(datetime.now(UTC))
    result = ledger.record_instance_state(
        reservation_id, instance_id=instance_id, state=state, occurred_at=occurred_at
    )
    return {
        "state": "RECORDED",
        "reservation": result["reservation"],
        "stale_event": result.get("stale_event", False),
    }


def inspect_bypass_event(event: dict, *, ledger, provisioner_role_arn: str) -> dict:
    detail = event.get("detail") or {}
    identity = detail.get("userIdentity") or {}
    issuer = (identity.get("sessionContext") or {}).get("sessionIssuer") or {}
    issuer_arn = issuer.get("arn")
    request = detail.get("requestParameters") or {}
    tag_sets = (request.get("tagSpecificationSet") or {}).get("items", [])
    tags = {tag.get("key"): tag.get("value") for spec in tag_sets for tag in spec.get("tags", [])}
    reservation = None
    if tags.get("reservation-id"):
        try:
            reservation = ledger.get_reservation(tags["reservation-id"])
        except ReservationNotFound:
            reservation = None
    response_items = ((detail.get("responseElements") or {}).get("instancesSet") or {}).get(
        "items", []
    )
    response_ids = [item.get("instanceId") for item in response_items]
    valid_controller_launch = (
        issuer_arn == provisioner_role_arn
        and reservation is not None
        and tags.get("managed-by") == "spend-controller"
        and tags.get("project") == "spendctl"
        and tags.get("env") == "dev"
        and tags.get("team") == reservation.get("team")
        and reservation.get("workflow_start_state") == "STARTED"
        and (not reservation.get("instance_id") or reservation.get("instance_id") in response_ids)
    )
    if valid_controller_launch:
        return {"state": "CONTROLLER_ROLE", "reservation_id": reservation["reservation_id"]}
    event_id = (
        event.get("id") or hashlib.sha256(json.dumps(event, sort_keys=True).encode()).hexdigest()
    )
    ledger.record_alert(
        alert_id=hashlib.sha256(f"bypass:{event_id}".encode()).hexdigest(),
        alert_type="BypassDetected",
        details={
            "cloudtrail_event_id": event_id,
            "actor_arn": identity.get("arn"),
            "reservation_id": tags.get("reservation-id"),
            "managed_by": tags.get("managed-by"),
            "event_name": detail.get("eventName", "RunInstances"),
        },
    )
    return {"state": "ALERTED", "event_id": event_id}


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
