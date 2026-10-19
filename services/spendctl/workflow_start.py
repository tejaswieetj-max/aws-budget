"""Crash-safe Step Functions starter/recovery primitives."""

from __future__ import annotations

import hashlib
import json
import time

try:
    from botocore.exceptions import (
        BotoCoreError,
        ClientError,
        ConnectionClosedError,
        ConnectTimeoutError,
        EndpointConnectionError,
        HTTPClientError,
        ReadTimeoutError,
    )
except ModuleNotFoundError as import_error:  # Permit offline tests only when botocore is absent.
    if import_error.name not in {"botocore", "botocore.exceptions"}:
        raise

    class BotoCoreError(Exception):
        """Fallback type used only when botocore is absent in a local test environment."""

        def __init__(self, *args, **kwargs):
            super().__init__(*args)
            self.kwargs = kwargs
            self.message = str(args[0]) if args else str(kwargs.get("message", ""))
            for name, value in kwargs.items():
                setattr(self, name, value)

    class ClientError(BotoCoreError):
        """Minimal botocore ClientError shape for offline tests."""

        def __init__(self, error_response, operation_name):
            super().__init__(error_response)
            self.response = error_response
            self.operation_name = operation_name

    class EndpointConnectionError(BotoCoreError):
        pass

    class ConnectTimeoutError(BotoCoreError):
        pass

    class ReadTimeoutError(BotoCoreError):
        pass

    class ConnectionClosedError(BotoCoreError):
        pass

    class HTTPClientError(BotoCoreError):
        pass


_AMBIGUOUS_TRANSPORT_ERRORS = (
    EndpointConnectionError,
    HTTPClientError,
    ConnectTimeoutError,
    ReadTimeoutError,
    ConnectionClosedError,
    TimeoutError,
    ConnectionError,
)
_DEFINITIVE_START_REJECTIONS = {
    "AccessDeniedException",
    "InvalidArn",
    "InvalidExecutionInput",
    "InvalidName",
    "StateMachineDoesNotExist",
    "StateMachineDeleting",
    "ValidationException",
}


class WorkflowStartOutcomeUnknown(Exception):
    """A reservation exists but the workflow start cannot yet be established."""

    def __init__(self, reservation_id: str, result: dict | None = None):
        self.reservation_id = reservation_id
        self.result = result or {"state": "START_UNKNOWN", "recovery_required": True}
        super().__init__(f"Step Functions start outcome is unknown for {reservation_id}")


def execution_name(reservation_id: str) -> str:
    # Step Functions names permit a restricted character set and max 80 chars.
    safe = "".join(c for c in reservation_id if c.isalnum() or c in "-_")[:48]
    digest = hashlib.sha256(reservation_id.encode()).hexdigest()[:20]
    return f"spendctl-{safe}-{digest}"[:80]


def start_or_reconcile(client, state_machine_arn: str, reservation_id: str, payload: dict) -> dict:
    """Attempt deterministic start; on ambiguous response inspect execution first.

    Never release a hold based on a timeout alone. Caller persists STARTING and
    invokes recovery_worker if this function cannot establish execution status.
    """
    name = execution_name(reservation_id)
    serialized_input = json.dumps(payload, sort_keys=True)
    try:
        response = client.start_execution(
            stateMachineArn=state_machine_arn,
            name=name,
            input=serialized_input,
        )
        return {"state": "STARTED", "executionArn": response["executionArn"]}
    except ClientError as error:
        error_code = error.response.get("Error", {}).get("Code")
        # ExecutionAlreadyExists proves that this deterministic name is taken,
        # but not that the existing execution has the same input. Describe it.
        # Other service errors are also reconciled conservatively; a service
        # response is not treated as proof that no workflow/resource exists.
        return _reconcile_start(client, state_machine_arn, name, payload, error_code)
    except (BotoCoreError, *_AMBIGUOUS_TRANSPORT_ERRORS):
        # Timeouts, disconnects, and SDK transport errors are ambiguous. Inspect
        # the deterministic name; if inspection also fails, preserve the HELD
        # reservation and require recovery instead of releasing it.
        return _reconcile_start(client, state_machine_arn, name, payload)


def _reconcile_start(
    client, state_machine_arn: str, name: str, payload: dict, start_error_code: str | None = None
) -> dict:
    try:
        token = None
        match = None
        while True:
            request = {"stateMachineArn": state_machine_arn, "maxResults": 1000}
            if token:
                request["nextToken"] = token
            page = client.list_executions(**request)
            match = next(
                (item for item in page.get("executions", []) if item.get("name") == name), None
            )
            if match:
                break
            token = page.get("nextToken")
            if not token:
                break
        if match is None:
            if start_error_code in _DEFINITIVE_START_REJECTIONS:
                return {
                    "state": "START_REJECTED",
                    "executionArn": None,
                    "recovery_required": False,
                    "error_code": start_error_code,
                }
            return _unknown_result(start_error_code)

        execution = client.describe_execution(executionArn=match["executionArn"])
        try:
            existing_input = json.loads(execution.get("input", "{}"))
        except (TypeError, json.JSONDecodeError):
            return {
                "state": "EXECUTION_NAME_CONFLICT",
                "executionArn": match["executionArn"],
                "recovery_required": True,
            }
        if existing_input != payload:
            return {
                "state": "EXECUTION_NAME_CONFLICT",
                "executionArn": match["executionArn"],
                "recovery_required": True,
            }
        return {
            "state": execution.get("status", match.get("status", "START_UNKNOWN")),
            "executionArn": match["executionArn"],
            "recovery_required": False,
        }
    except (ClientError, BotoCoreError, *_AMBIGUOUS_TRANSPORT_ERRORS):
        return _unknown_result(start_error_code)


def _unknown_result(start_error_code: str | None = None) -> dict:
    result = {"state": "START_UNKNOWN", "executionArn": None, "recovery_required": True}
    if start_error_code == "ExecutionAlreadyExists":
        result["duplicate_name_unresolved"] = True
    return result


class WorkflowStarter:
    """Persist start intent, then perform deterministic start/reconciliation."""

    def __init__(self, client, state_machine_arn: str, ledger):
        self.client = client
        self.state_machine_arn = state_machine_arn
        self.ledger = ledger

    def start(self, reservation: dict) -> dict:
        reservation_id = reservation["reservation_id"]
        _workflow_metric("WorkflowStartAttempts")
        try:
            result = self._start(reservation)
        except WorkflowStartOutcomeUnknown as error:
            _workflow_metric("WorkflowUnknownOutcomes", result="unknown")
            _workflow_log("start_execution", "unknown", reservation_id=reservation_id,
                          error_category=type(error).__name__)
            raise
        outcome = result.get("state", "STARTED")
        success = outcome in {"STARTED", "SUCCEEDED"}
        _workflow_metric("WorkflowStartSuccesses" if success else "WorkflowStartFailures",
                         result="success" if success else "failure")
        _workflow_log("start_execution", "success" if success else "failure",
                      reservation_id=reservation_id, workflow_state=outcome,
                      execution_arn=result.get("executionArn"))
        return result

    def _start(self, reservation: dict) -> dict:
        ambiguous_ledger_errors = _ledger_ambiguous_errors()
        reservation_id = reservation["reservation_id"]
        name = execution_name(reservation_id)
        payload = {
            "reservation_id": reservation_id,
            "team": reservation["team"],
            "resource": reservation["resource"],
            "planned_hours": reservation["planned_hours"],
            "region": reservation.get("region", "ap-south-1"),
            "purpose": reservation.get("purpose", ""),
            # This flag is intentionally fixed by the server; request JSON can
            # never enable failure injection.
            "failure_injection": False,
        }
        if reservation.get("workflow_start_state") == "FAILED":
            release = self.ledger.release(reservation_id)
            return {
                "state": "START_REJECTED",
                "execution_name": reservation.get("workflow_execution_name", name),
                "reservation_released": release["reservation"]["status"] == "RELEASED",
                "replayed": True,
            }
        if reservation.get("workflow_start_state", "NOT_STARTED") != "STARTED":
            try:
                self.ledger.set_workflow_start_state(
                    reservation_id, "STARTING", execution_name=name
                )
            except (
                *ambiguous_ledger_errors,
                BotoCoreError,
                ClientError,
                *_AMBIGUOUS_TRANSPORT_ERRORS,
            ) as error:
                raise WorkflowStartOutcomeUnknown(reservation_id) from error

        try:
            result = start_or_reconcile(
                self.client, self.state_machine_arn, reservation_id, payload
            )
        except (BotoCoreError, ClientError, *_AMBIGUOUS_TRANSPORT_ERRORS) as error:
            result = _unknown_result()
            try:
                self.ledger.set_workflow_start_state(
                    reservation_id,
                    "UNKNOWN",
                    execution_name=name,
                )
            except (
                *ambiguous_ledger_errors,
                BotoCoreError,
                ClientError,
                *_AMBIGUOUS_TRANSPORT_ERRORS,
            ):
                pass
            raise WorkflowStartOutcomeUnknown(reservation_id, result) from error

        state = result["state"]
        try:
            if state == "START_REJECTED":
                self.ledger.set_workflow_start_state(
                    reservation_id,
                    "FAILED",
                    execution_name=name,
                    execution_status="START_REJECTED",
                )
                released = (
                    self.ledger.release(reservation_id)["reservation"]["status"] == "RELEASED"
                )
                return {**result, "execution_name": name, "reservation_released": released}
            if state == "START_UNKNOWN" or state == "EXECUTION_NAME_CONFLICT":
                persisted_status = (
                    "EXECUTION_NAME_CONFLICT" if state == "EXECUTION_NAME_CONFLICT" else "UNKNOWN"
                )
                persisted_state = (
                    "STARTED" if reservation.get("workflow_start_state") == "STARTED" else "UNKNOWN"
                )
                self.ledger.set_workflow_start_state(
                    reservation_id,
                    persisted_state,
                    execution_name=name,
                    execution_arn=result.get("executionArn"),
                )
                self.ledger.record_workflow_execution(
                    reservation_id,
                    execution_status=persisted_status,
                    execution_arn=result.get("executionArn"),
                )
            else:
                persisted_state = "STARTED"
                self.ledger.set_workflow_start_state(
                    reservation_id,
                    persisted_state,
                    execution_name=name,
                    execution_arn=result.get("executionArn"),
                )
                if state in {"SUCCEEDED", "FAILED", "TIMED_OUT", "ABORTED"}:
                    self.ledger.record_workflow_execution(
                        reservation_id,
                        execution_status=state,
                        execution_arn=result.get("executionArn"),
                    )
        except (
            *ambiguous_ledger_errors,
            BotoCoreError,
            ClientError,
            *_AMBIGUOUS_TRANSPORT_ERRORS,
        ) as error:
            # StartExecution may have succeeded; preserve the hold and reconcile
            # using the already-persisted deterministic name on the next retry.
            raise WorkflowStartOutcomeUnknown(reservation_id, result) from error
        if state in {"START_UNKNOWN", "EXECUTION_NAME_CONFLICT"}:
            raise WorkflowStartOutcomeUnknown(reservation_id, result)
        return {**result, "execution_name": name, "reservation_released": False}


def _workflow_metric(name: str, result: str | None = None) -> None:
    dimensions = [["result"]] if result else [[]]
    body = {
        "_aws": {
            "Timestamp": int(time.time() * 1000),
            "CloudWatchMetrics": [{
                "Namespace": "Spendctl/Workflow",
                "Dimensions": dimensions,
                "Metrics": [{"Name": name, "Unit": "Count"}],
            }],
        },
        name: 1,
    }
    if result:
        body["result"] = result
    print(json.dumps(body, sort_keys=True, separators=(",", ":")))


def _workflow_log(operation: str, outcome: str, **fields) -> None:
    print(json.dumps({"timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                      "service": "spendctl", "module": "M6", "operation": operation,
                      "outcome": outcome, **fields}, sort_keys=True, separators=(",", ":")))


def _ledger_ambiguous_errors():
    from .ledger import AmbiguousLedgerOutcome, InvalidTransition

    return AmbiguousLedgerOutcome, InvalidTransition


def recover_held_reservation(reservation: dict, execution: dict | None, describe_resources) -> str:
    """Return an idempotent recovery decision after inspecting all evidence."""
    if execution and execution.get("status") in {"RUNNING", "SUCCEEDED"}:
        return "KEEP_AND_RECONCILE"
    resources = describe_resources(reservation)
    if resources:
        return "CLEANUP_THEN_RECONCILE"
    if execution and execution.get("status") in {"FAILED", "TIMED_OUT", "ABORTED"}:
        return "RELEASE_IF_STILL_HELD"
    if execution and execution.get("state") == "EXECUTION_NAME_CONFLICT":
        return "MANUAL_RECONCILIATION_REQUIRED"
    return "RETRY_SAME_EXECUTION_NAME"
