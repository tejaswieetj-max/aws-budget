"""Conditional, transaction-backed DynamoDB ledger.

This adapter uses a low-level DynamoDB client. Reservation and idempotency
records share the Reservations table with ``pk``/``sk`` keys. Every balance
change is in the same DynamoDB transaction as its reservation state change.
"""

from __future__ import annotations

import hashlib
import json
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from botocore.exceptions import (
    ClientError,
    ConnectionClosedError,
    ConnectTimeoutError,
    EndpointConnectionError,
    HTTPClientError,
    ReadTimeoutError,
)

_TRANSPORT_ERRORS = (
    ConnectionClosedError,
    ConnectTimeoutError,
    EndpointConnectionError,
    HTTPClientError,
    ReadTimeoutError,
    TimeoutError,
    ConnectionError,
)
_CLOSED_RESERVATION_STATES = {"RELEASED", "SETTLED"}
_ACTIVE_BUDGET_STATES = {"OPEN", "CLOSED"}
_IDEMPOTENCY_OMIT_FIELDS = {
    "pk",
    "sk",
    "record_type",
    "reservation_id",
    "status",
    "created_at",
    "updated_at",
    "launched_at",
    "expires_at",
    "terminated_at",
    "settled_at",
    "idempotency_key",
    "request_fingerprint",
    "workflow_start_state",
    "workflow_execution_name",
    "workflow_execution_arn",
    "workflow_execution_status",
    "workflow_cleanup_state",
    "workflow_launch_state",
    "instance_id",
    "volume_ids",
    "settlement_stage",
    "settlement_version",
    "provisional_settlement_id",
    "final_settlement_id",
    "final_actual_micro",
    "ttl",
}


class LedgerError(Exception):
    """Base class for expected ledger rejections."""


class BudgetNotFound(LedgerError):
    pass


class BudgetAlreadyExists(LedgerError):
    pass


class BudgetClosed(LedgerError):
    pass


class InsufficientBudget(LedgerError):
    pass


class DuplicateIdempotencyKey(LedgerError):
    """Raised when a key is reused with a different request fingerprint."""


class ReservationConflict(LedgerError):
    pass


class ReservationNotFound(LedgerError):
    pass


class InvalidTransition(LedgerError):
    pass


class SettlementConflict(LedgerError):
    pass


class LedgerInvariantViolation(LedgerError):
    pass


class LedgerTransactionCancelled(LedgerError):
    pass


class AmbiguousLedgerOutcome(LedgerError):
    """Outcome may have committed; retry only with the same stable idempotency key."""


class LedgerConfigurationError(LedgerError):
    pass


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _money(value: int, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer number of micro-USD")
    return value


def _decimal_json(value: Any) -> Any:
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else str(value)
    if isinstance(value, dict):
        return {str(k): _decimal_json(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_decimal_json(v) for v in value]
    return value


def _encode(value: Any) -> dict:
    if isinstance(value, bool):
        return {"BOOL": value}
    if isinstance(value, int):
        return {"N": str(value)}
    if isinstance(value, Decimal):
        return {"N": str(value)}
    if isinstance(value, float):
        # Non-money fields such as margins may be floats. All *_micro inputs
        # are validated as integers before serialization.
        return {"N": str(Decimal(str(value)))}
    if isinstance(value, str):
        return {"S": value}
    if value is None:
        return {"NULL": True}
    if isinstance(value, (list, tuple)):
        return {"L": [_encode(item) for item in value]}
    if isinstance(value, dict):
        return {"M": {str(key): _encode(item) for key, item in value.items()}}
    raise TypeError(f"unsupported DynamoDB value type: {type(value).__name__}")


def _decode(value: dict) -> Any:
    if "S" in value:
        return value["S"]
    if "N" in value:
        number = Decimal(value["N"])
        return int(number) if number == number.to_integral_value() else number
    if "BOOL" in value:
        return value["BOOL"]
    if "NULL" in value:
        return None
    if "M" in value:
        return {key: _decode(item) for key, item in value["M"].items()}
    if "L" in value:
        return [_decode(item) for item in value["L"]]
    if "B" in value:
        return value["B"]
    raise TypeError(f"unsupported DynamoDB attribute value: {value!r}")


def _decode_item(item: dict | None) -> dict | None:
    return {key: _decode(value) for key, value in item.items()} if item else None


def _key(item: dict) -> dict:
    return {name: _encode(value) for name, value in item.items()}


class DynamoLedger:
    """Atomic ledger over Budgets, Reservations, and Alerts DynamoDB tables.

    ``idempotency_ttl_seconds`` applies only to durable request idempotency
    records. Reservation records never receive TTL. Use a stable key and
    reservation ID when retrying an ambiguous reserve outcome.
    """

    def __init__(
        self,
        dynamodb,
        budgets_table: str,
        reservations_table: str,
        alerts_table: str | None = None,
        *,
        idempotency_ttl_seconds: int = 365 * 24 * 60 * 60,
    ):
        if idempotency_ttl_seconds <= 0:
            raise ValueError("idempotency_ttl_seconds must be positive")
        self.db = dynamodb
        self.budgets = budgets_table
        self.reservations = reservations_table
        self.alerts = alerts_table
        self.idempotency_ttl_seconds = idempotency_ttl_seconds

    def create_budget(self, *, team: str, period: str, budget_micro: int) -> dict:
        """Create an OPEN budget only if absent; identical calls are replays."""
        amount = _money(budget_micro, "budget_micro")
        if amount == 0:
            raise ValueError("budget_micro must be greater than zero")
        item = {
            "team": team,
            "period": period,
            "budget_micro": amount,
            "available_micro": amount,
            "reserved_micro": 0,
            "spent_micro": 0,
            "open_reservations": 0,
            "version": 1,
            "status": "OPEN",
            "created_at": _now(),
        }
        try:
            self.db.put_item(
                TableName=self.budgets,
                Item={key: _encode(value) for key, value in item.items()},
                ConditionExpression="attribute_not_exists(team)",
            )
            return {**item, "replayed": False}
        except ClientError as error:
            if _error_code(error) != "ConditionalCheckFailedException":
                raise
            existing = self.get_budget(team, period)
            if (
                existing
                and existing.get("budget_micro") == amount
                and existing.get("status") == "OPEN"
                and existing.get("available_micro") == amount
                and existing.get("reserved_micro") == 0
                and existing.get("spent_micro") == 0
                and existing.get("open_reservations") == 0
            ):
                return {**existing, "replayed": True}
            raise BudgetAlreadyExists(
                f"budget {team}/{period} already exists; not resetting it"
            ) from error

    def close_budget(self, *, team: str, period: str) -> dict:
        """Close an OPEN period once no reservation can still settle or release."""
        budget = self.get_budget(team, period)
        if not budget:
            raise BudgetNotFound(f"budget {team}/{period} does not exist")
        if budget.get("status") == "CLOSED":
            return {**budget, "replayed": True}
        if budget.get("status") != "OPEN":
            raise InvalidTransition(f"cannot close budget in state {budget.get('status')}")
        if budget.get("reserved_micro") != 0 or budget.get("open_reservations") != 0:
            raise InvalidTransition("cannot close a budget with unsettled reservations")
        try:
            response = self.db.update_item(
                TableName=self.budgets,
                Key=_key({"team": team, "period": period}),
                UpdateExpression="SET #state = :closed, version = version + :one, updated_at = :now",
                ConditionExpression=(
                    "#state = :open AND reserved_micro = :zero AND open_reservations = :zero"
                ),
                ExpressionAttributeNames={"#state": "status"},
                ExpressionAttributeValues={
                    ":closed": _encode("CLOSED"),
                    ":open": _encode("OPEN"),
                    ":one": _encode(1),
                    ":zero": _encode(0),
                    ":now": _encode(_now()),
                },
                ReturnValues="ALL_NEW",
            )
            return {**_decode_item(response.get("Attributes")), "replayed": False}
        except ClientError as error:
            if _error_code(error) != "ConditionalCheckFailedException":
                raise
            latest = self.get_budget(team, period)
            if latest.get("status") == "CLOSED":
                return {**latest, "replayed": True}
            raise InvalidTransition("budget changed while it was being closed") from error

    def reserve(
        self,
        *,
        team: str,
        period: str,
        reservation_id: str,
        idempotency_key: str,
        requester: str,
        reserved_micro: int,
        record: dict,
    ) -> dict:
        """Atomically hold budget and durably claim an idempotency key."""
        amount = _money(reserved_micro, "reserved_micro")
        if amount == 0:
            raise ValueError("reserved_micro must be greater than zero")
        if not team or not period or not requester or not reservation_id or not idempotency_key:
            raise ValueError(
                "team, period, requester, reservation_id, and idempotency_key are required"
            )
        fingerprint = _request_fingerprint(
            {
                **record,
                "team": team,
                "period": period,
                "requester": requester,
                "reserved_micro": amount,
            }
        )
        now = _now()
        idem_item = {
            "pk": _idempotency_key(team, idempotency_key)["pk"],
            "sk": "KEY",
            "record_type": "IDEMPOTENCY",
            "team": team,
            "idempotency_key": idempotency_key,
            "request_fingerprint": fingerprint,
            "reservation_id": reservation_id,
            "created_at": now,
            "ttl": int(time.time()) + self.idempotency_ttl_seconds,
        }
        reservation_item = {
            **record,
            "pk": f"RES#{reservation_id}",
            "sk": "META",
            "record_type": "RESERVATION",
            "reservation_id": reservation_id,
            "team": team,
            "budget_period": period,
            "requester": requester,
            "idempotency_key": idempotency_key,
            "request_fingerprint": fingerprint,
            "reserved_micro": amount,
            "status": "HELD",
            "workflow_start_state": "NOT_STARTED",
            "settlement_version": 0,
            "created_at": now,
        }
        planned_hours = reservation_item.get(
            "planned_hours", reservation_item.get("duration_hours")
        )
        if (
            isinstance(planned_hours, bool)
            or not isinstance(planned_hours, int)
            or not 1 <= planned_hours <= 24
        ):
            raise ValueError("record must include planned_hours from 1 to 24")
        reservation_item["planned_hours"] = planned_hours
        planned_expiry = datetime.fromisoformat(now) + timedelta(hours=planned_hours)
        reservation_item["expires_at"] = planned_expiry.isoformat().replace("+00:00", "Z")
        actions = [
            {
                "Put": {
                    "TableName": self.reservations,
                    "Item": {key: _encode(value) for key, value in idem_item.items()},
                    "ConditionExpression": "attribute_not_exists(pk)",
                }
            },
            {
                "Put": {
                    "TableName": self.reservations,
                    "Item": {key: _encode(value) for key, value in reservation_item.items()},
                    "ConditionExpression": "attribute_not_exists(pk)",
                }
            },
            {
                "Update": {
                    "TableName": self.budgets,
                    "Key": _key({"team": team, "period": period}),
                    "UpdateExpression": (
                        "SET available_micro = available_micro - :r, "
                        "reserved_micro = reserved_micro + :r, version = version + :one, "
                        "open_reservations = open_reservations + :one, updated_at = :now"
                    ),
                    "ConditionExpression": (
                        "#state = :open AND available_micro >= :r AND "
                        "attribute_exists(budget_micro) AND attribute_exists(reserved_micro) "
                        "AND attribute_exists(spent_micro) AND attribute_exists(version) "
                        "AND attribute_exists(open_reservations)"
                    ),
                    "ExpressionAttributeNames": {"#state": "status"},
                    "ExpressionAttributeValues": {
                        ":r": _encode(amount),
                        ":one": _encode(1),
                        ":open": _encode("OPEN"),
                        ":now": _encode(now),
                    },
                }
            },
        ]
        self._require_open_budget(team, period)
        try:
            self.db.transact_write_items(TransactItems=actions)
        except ClientError as error:
            code = _error_code(error)
            if code == "TransactionCanceledException":
                return self._resolve_reserve_cancellation(
                    team, period, idempotency_key, reservation_id, fingerprint, amount, error
                )
            if _ambiguous_client_error(error):
                return self._resolve_ambiguous_reserve(
                    team, idempotency_key, reservation_id, fingerprint, error
                )
            raise
        except _TRANSPORT_ERRORS as error:
            return self._resolve_ambiguous_reserve(
                team, idempotency_key, reservation_id, fingerprint, error
            )
        return {"reservation": reservation_item, "replayed": False}

    def release(self, reservation_id: str, *, released_at: str | None = None) -> dict:
        """Release an unstarted or definitively failed HELD reservation once."""
        reservation = self.get_reservation(reservation_id)
        if reservation["status"] == "RELEASED":
            return {"reservation": reservation, "replayed": True}
        if reservation["status"] != "HELD":
            raise InvalidTransition(f"cannot release reservation in {reservation['status']}")
        workflow_state = reservation.get("workflow_start_state", "NOT_STARTED")
        if workflow_state not in {"NOT_STARTED", "FAILED"}:
            raise InvalidTransition(
                f"cannot release reservation while workflow start is {workflow_state}"
            )
        amount = _money(reservation["reserved_micro"], "reserved_micro")
        self._require_open_budget(reservation["team"], reservation["budget_period"])
        now = released_at or _now()
        actions = [
            self._budget_update(
                reservation,
                "SET available_micro = available_micro + :r, "
                "reserved_micro = reserved_micro - :r, "
                "open_reservations = open_reservations - :one, "
                "version = version + :one, updated_at = :now",
                "reserved_micro >= :r AND open_reservations >= :one AND #state = :open",
                {":r": amount, ":one": 1, ":now": now, ":open": "OPEN"},
            ),
            {
                "Update": {
                    "TableName": self.reservations,
                    "Key": _key(_reservation_key(reservation_id)),
                    "UpdateExpression": "SET #state = :released, released_at = :now, updated_at = :now",
                    "ConditionExpression": (
                        "#state = :held AND workflow_start_state IN (:not_started, :failed)"
                    ),
                    "ExpressionAttributeNames": {"#state": "status"},
                    "ExpressionAttributeValues": {
                        ":released": _encode("RELEASED"),
                        ":held": _encode("HELD"),
                        ":not_started": _encode("NOT_STARTED"),
                        ":failed": _encode("FAILED"),
                        ":now": _encode(now),
                    },
                }
            },
        ]
        try:
            self.db.transact_write_items(TransactItems=actions)
        except ClientError as error:
            if _error_code(error) == "TransactionCanceledException":
                return self._resolve_release_cancel(reservation_id, error)
            if _ambiguous_client_error(error):
                return self._resolve_ambiguous_state(reservation_id, "RELEASED", error)
            raise
        except _TRANSPORT_ERRORS as error:
            return self._resolve_ambiguous_state(reservation_id, "RELEASED", error)
        return {"reservation": self.get_reservation(reservation_id), "replayed": False}

    def release_prelaunch_failure(
        self, reservation_id: str, *, released_at: str | None = None
    ) -> dict:
        """Release only after a confirmed pre-launch failure and durable no-resource evidence."""
        reservation = self.get_reservation(reservation_id)
        if reservation["status"] == "RELEASED":
            return {"reservation": reservation, "replayed": True}
        if (
            reservation["status"] != "HELD"
            or reservation.get("workflow_start_state") != "STARTED"
            or reservation.get("workflow_execution_status") != "FAILED"
            or reservation.get("workflow_launch_state") != "NOT_CREATED"
            or reservation.get("instance_id")
            or reservation.get("volume_ids")
        ):
            raise InvalidTransition(
                "pre-launch release requires confirmed failure and no resources"
            )
        amount = _money(reservation["reserved_micro"], "reserved_micro")
        self._require_open_budget(reservation["team"], reservation["budget_period"])
        now = released_at or _now()
        actions = [
            self._budget_update(
                reservation,
                "SET available_micro = available_micro + :r, "
                "reserved_micro = reserved_micro - :r, "
                "open_reservations = open_reservations - :one, "
                "version = version + :one, updated_at = :now",
                "reserved_micro >= :r AND open_reservations >= :one AND #state = :open",
                {":r": amount, ":one": 1, ":now": now, ":open": "OPEN"},
            ),
            {
                "Update": {
                    "TableName": self.reservations,
                    "Key": _key(_reservation_key(reservation_id)),
                    "UpdateExpression": "SET #state = :released, released_at = :now, updated_at = :now",
                    "ConditionExpression": (
                        "#state = :held AND workflow_start_state = :started "
                        "AND workflow_execution_status = :failed "
                        "AND workflow_launch_state = :not_created "
                        "AND attribute_not_exists(instance_id) AND attribute_not_exists(volume_ids)"
                    ),
                    "ExpressionAttributeNames": {"#state": "status"},
                    "ExpressionAttributeValues": {
                        ":released": _encode("RELEASED"),
                        ":held": _encode("HELD"),
                        ":started": _encode("STARTED"),
                        ":failed": _encode("FAILED"),
                        ":not_created": _encode("NOT_CREATED"),
                        ":now": _encode(now),
                    },
                }
            },
        ]
        try:
            self.db.transact_write_items(TransactItems=actions)
        except ClientError as error:
            if _error_code(error) == "TransactionCanceledException":
                current = self.get_reservation(reservation_id)
                if current["status"] == "RELEASED":
                    return {"reservation": current, "replayed": True}
                raise InvalidTransition("pre-launch release preconditions changed") from error
            if _ambiguous_client_error(error):
                return self._resolve_ambiguous_state(reservation_id, "RELEASED", error)
            raise
        except _TRANSPORT_ERRORS as error:
            return self._resolve_ambiguous_state(reservation_id, "RELEASED", error)
        return {"reservation": self.get_reservation(reservation_id), "replayed": False}

    def release_after_confirmed_cleanup(
        self,
        reservation_id: str,
        actual_micro: int,
        *,
        settlement_id: str | None = None,
        settlement_version: int = 1,
        released_at: str | None = None,
    ) -> dict:
        """Settle a failed workflow only after durable COMPLETE cleanup evidence."""
        actual = _money(actual_micro, "actual_micro")
        if (
            isinstance(settlement_version, bool)
            or not isinstance(settlement_version, int)
            or settlement_version != 1
        ):
            raise ValueError("confirmed-cleanup settlement_version must be 1")
        sid = settlement_id or f"{reservation_id}:cleanup-failure:v{settlement_version}"
        _validate_identifier(sid, "settlement_id")
        reservation = self.get_reservation(reservation_id)
        if reservation.get("status") == "RELEASED":
            if (
                reservation.get("final_settlement_id") == sid
                and reservation.get("final_actual_micro") == actual
            ):
                return {"reservation": reservation, "replayed": True}
            raise SettlementConflict("reservation was released with a different settlement")
        if (
            reservation.get("status") not in {"HELD", "ACTIVE"}
            or reservation.get("workflow_execution_status")
            not in {"FAILED", "TIMED_OUT", "ABORTED"}
            or reservation.get("workflow_cleanup_state") != "COMPLETE"
            or reservation.get("workflow_start_state") != "STARTED"
            or reservation.get("workflow_launch_state") not in {"CREATED", "NOT_CREATED"}
        ):
            raise InvalidTransition(
                "budget restoration requires failed execution, confirmed launch outcome, and COMPLETE cleanup"
            )
        if reservation["workflow_launch_state"] == "CREATED" and (
            not reservation.get("instance_id") or not reservation.get("launched_at")
        ):
            raise InvalidTransition("created launch is missing runtime identity or launch time")
        if reservation["workflow_launch_state"] == "NOT_CREATED" and actual != 0:
            raise SettlementConflict("a confirmed non-created launch must have zero actual cost")
        if reservation["workflow_launch_state"] == "NOT_CREATED" and (
            reservation.get("instance_id") or reservation.get("volume_ids")
        ):
            raise InvalidTransition("NOT_CREATED launch has persisted resource identities")
        reserve = _money(reservation.get("reserved_micro"), "reserved_micro")
        if actual > reserve:
            raise SettlementConflict(
                "confirmed-cleanup actual cost cannot exceed the reserved amount"
            )
        self._require_open_budget(reservation["team"], reservation["budget_period"])
        now = released_at or _now()
        actions = [
            self._budget_update(
                reservation,
                "SET available_micro = available_micro + :refund, "
                "reserved_micro = reserved_micro - :reserve, spent_micro = spent_micro + :actual, "
                "open_reservations = open_reservations - :one, version = version + :one, updated_at = :now",
                "reserved_micro >= :reserve AND open_reservations >= :one AND #state = :open",
                {
                    ":refund": reserve - actual,
                    ":reserve": reserve,
                    ":actual": actual,
                    ":one": 1,
                    ":now": now,
                    ":open": "OPEN",
                },
            ),
            {
                "Update": {
                    "TableName": self.reservations,
                    "Key": _key(_reservation_key(reservation_id)),
                    "UpdateExpression": (
                        "SET #state = :released, settlement_stage = :final, final_actual_micro = :actual, "
                        "final_settlement_id = :sid, settlement_version = :version, released_at = :now, updated_at = :now"
                    ),
                    "ConditionExpression": (
                        "#state IN (:held, :active) AND workflow_execution_status IN (:failed, :timeout, :aborted) "
                        "AND workflow_cleanup_state = :complete AND workflow_start_state = :started "
                        "AND workflow_launch_state IN (:created, :not_created) "
                        "AND attribute_not_exists(final_settlement_id)"
                    ),
                    "ExpressionAttributeNames": {"#state": "status"},
                    "ExpressionAttributeValues": {
                        ":released": _encode("RELEASED"),
                        ":final": _encode("FINAL"),
                        ":actual": _encode(actual),
                        ":sid": _encode(sid),
                        ":version": _encode(settlement_version),
                        ":now": _encode(now),
                        ":held": _encode("HELD"),
                        ":active": _encode("ACTIVE"),
                        ":failed": _encode("FAILED"),
                        ":timeout": _encode("TIMED_OUT"),
                        ":aborted": _encode("ABORTED"),
                        ":complete": _encode("COMPLETE"),
                        ":started": _encode("STARTED"),
                        ":created": _encode("CREATED"),
                        ":not_created": _encode("NOT_CREATED"),
                    },
                }
            },
        ]
        try:
            self.db.transact_write_items(TransactItems=actions)
        except ClientError as error:
            if _error_code(error) == "TransactionCanceledException":
                latest = self.get_reservation(reservation_id)
                if (
                    latest.get("status") == "RELEASED"
                    and latest.get("final_settlement_id") == sid
                    and latest.get("final_actual_micro") == actual
                ):
                    return {"reservation": latest, "replayed": True}
                raise InvalidTransition(
                    "confirmed-cleanup settlement preconditions changed"
                ) from error
            if _ambiguous_client_error(error):
                return self._resolve_confirmed_cleanup_ambiguous(reservation_id, sid, actual, error)
            raise
        except _TRANSPORT_ERRORS as error:
            return self._resolve_confirmed_cleanup_ambiguous(reservation_id, sid, actual, error)
        return {"reservation": self.get_reservation(reservation_id), "replayed": False}

    def _resolve_confirmed_cleanup_ambiguous(self, reservation_id, sid, actual, error):
        try:
            current = self.get_reservation(reservation_id)
            if (
                current.get("status") == "RELEASED"
                and current.get("final_settlement_id") == sid
                and current.get("final_actual_micro") == actual
            ):
                return {"reservation": current, "replayed": True}
        except _TRANSPORT_ERRORS:
            pass
        raise AmbiguousLedgerOutcome(
            "confirmed-cleanup settlement outcome unknown; reconcile before retry"
        ) from error

    def activate(
        self,
        reservation_id: str,
        *,
        launched_at: str | None = None,
        expires_at: str | None = None,
        instance_id: str | None = None,
        volume_ids: list[str] | None = None,
        execution_arn: str | None = None,
    ) -> dict:
        launch = launched_at or _now()
        if expires_at is None:
            reservation = self.get_reservation(reservation_id)
            started = datetime.fromisoformat(launch)
            expires_at = (
                (started + timedelta(hours=reservation["planned_hours"]))
                .isoformat()
                .replace("+00:00", "Z")
            )
        return self._transition(
            reservation_id,
            allowed_from={"HELD"},
            target="ACTIVE",
            fields={
                key: value
                for key, value in {
                    "launched_at": launch,
                    "expires_at": expires_at,
                    "instance_id": instance_id,
                    "volume_ids": volume_ids,
                    "workflow_execution_arn": execution_arn,
                }.items()
                if value is not None
            },
            idempotent_states={"ACTIVE", "EXPIRED", "CANCELLED", "SETTLED"},
        )

    def mark_expired(self, reservation_id: str, *, expired_at: str | None = None) -> dict:
        return self._transition(
            reservation_id,
            allowed_from={"ACTIVE"},
            target="EXPIRED",
            fields={"expired_at": expired_at or _now()},
            idempotent_states={"EXPIRED", "SETTLED", "CANCELLED"},
        )

    def cancel_active(self, reservation_id: str, *, cancelled_at: str | None = None) -> dict:
        return self._transition(
            reservation_id,
            allowed_from={"ACTIVE"},
            target="CANCELLED",
            fields={"cancelled_at": cancelled_at or _now()},
            idempotent_states={"CANCELLED", "SETTLED"},
        )

    def set_workflow_start_state(
        self,
        reservation_id: str,
        state: str,
        *,
        execution_name: str | None = None,
        execution_arn: str | None = None,
        execution_status: str | None = None,
    ) -> dict:
        """Persist workflow-start knowledge; UNKNOWN never permits release."""
        allowed = {
            "NOT_STARTED": {"STARTING"},
            "STARTING": {"STARTED", "UNKNOWN", "FAILED"},
            "UNKNOWN": {"STARTING", "STARTED", "FAILED", "UNKNOWN"},
            "STARTED": {"STARTED"},
            "FAILED": {"FAILED"},
        }
        if state not in {"STARTING", "STARTED", "UNKNOWN", "FAILED"}:
            raise ValueError("invalid workflow start state")
        reservation = self.get_reservation(reservation_id)
        current = reservation.get("workflow_start_state", "NOT_STARTED")
        if (
            state == current
            and all(
                value is None or reservation.get(field) == value
                for field, value in (
                    ("workflow_execution_name", execution_name),
                    ("workflow_execution_arn", execution_arn),
                )
            )
            and (
                execution_status is None
                or reservation.get("workflow_execution_status") == execution_status
            )
        ):
            return {"reservation": reservation, "replayed": True}
        if state not in allowed.get(current, set()):
            raise InvalidTransition(f"cannot move workflow start from {current} to {state}")
        if execution_name and reservation.get("workflow_execution_name") not in (
            None,
            execution_name,
        ):
            raise ReservationConflict("workflow execution name is immutable for a reservation")
        if execution_arn and reservation.get("workflow_execution_arn") not in (None, execution_arn):
            raise ReservationConflict("workflow execution ARN is immutable for a reservation")
        assignments = ["workflow_start_state = :target", "updated_at = :now"]
        values = {
            ":target": _encode(state),
            ":current": _encode(current),
            ":now": _encode(_now()),
        }
        for index, (field, value) in enumerate(
            (
                ("workflow_execution_name", execution_name),
                ("workflow_execution_arn", execution_arn),
                ("workflow_execution_status", execution_status),
            )
        ):
            if value is not None:
                placeholder = f":field{index}"
                assignments.append(f"{field} = {placeholder}")
                values[placeholder] = _encode(value)
        try:
            self.db.update_item(
                TableName=self.reservations,
                Key=_key(_reservation_key(reservation_id)),
                UpdateExpression="SET " + ", ".join(assignments),
                ConditionExpression="workflow_start_state = :current",
                ExpressionAttributeValues=values,
            )
        except ClientError as error:
            if _error_code(error) == "ConditionalCheckFailedException":
                return self._resolve_workflow_state_race(reservation_id, state, error)
            if _ambiguous_client_error(error):
                return self._resolve_workflow_state_ambiguous(reservation_id, state, error)
            raise
        except _TRANSPORT_ERRORS as error:
            return self._resolve_workflow_state_ambiguous(reservation_id, state, error)
        return {"reservation": self.get_reservation(reservation_id), "replayed": False}

    def record_workflow_execution(
        self,
        reservation_id: str,
        *,
        execution_status: str,
        cleanup_state: str | None = None,
        instance_id: str | None = None,
        volume_ids: list[str] | None = None,
        execution_arn: str | None = None,
        launch_state: str | None = None,
        launched_at: str | None = None,
        terminated_at: str | None = None,
    ) -> dict:
        """Persist idempotent workflow/resource outcomes without changing balances."""
        allowed_statuses = {
            "RUNNING",
            "SUCCEEDED",
            "FAILED",
            "TIMED_OUT",
            "ABORTED",
            "UNKNOWN",
            "EXECUTION_NAME_CONFLICT",
        }
        if execution_status not in allowed_statuses:
            raise ValueError("invalid workflow execution status")
        reservation = self.get_reservation(reservation_id)
        if reservation.get("workflow_start_state") not in {"STARTING", "UNKNOWN", "STARTED"}:
            raise InvalidTransition(
                "workflow execution outcome requires a started or uncertain start"
            )
        current = reservation.get("workflow_execution_status")
        terminal = {"SUCCEEDED", "FAILED", "TIMED_OUT", "ABORTED", "EXECUTION_NAME_CONFLICT"}
        if current in terminal and execution_status != current:
            raise InvalidTransition(f"workflow execution is already terminal: {current}")
        if execution_arn and reservation.get("workflow_execution_arn") not in (None, execution_arn):
            raise ReservationConflict("workflow execution ARN is immutable for a reservation")
        fields = {"workflow_execution_status": execution_status, "updated_at": _now()}
        if cleanup_state is not None:
            fields["workflow_cleanup_state"] = cleanup_state
        if instance_id is not None:
            fields["instance_id"] = instance_id
        if volume_ids is not None:
            fields["volume_ids"] = volume_ids
        if execution_arn is not None:
            fields["workflow_execution_arn"] = execution_arn
        if launched_at is not None:
            fields["launched_at"] = launched_at
        if terminated_at is not None:
            fields["terminated_at"] = terminated_at
        if launch_state is not None:
            if launch_state not in {"NOT_CREATED", "CREATED", "UNKNOWN"}:
                raise ValueError("invalid workflow launch state")
            fields["workflow_launch_state"] = launch_state
        updates = []
        values = {":start_state": _encode(reservation["workflow_start_state"])}
        for index, (field, value) in enumerate(fields.items()):
            placeholder = f":v{index}"
            updates.append(f"{field} = {placeholder}")
            values[placeholder] = _encode(value)
        condition = "workflow_start_state = :start_state"
        if current is None:
            condition += " AND attribute_not_exists(workflow_execution_status)"
        else:
            condition += " AND workflow_execution_status = :current_status"
            values[":current_status"] = _encode(current)
        actions = [
            {
                "Update": {
                    "TableName": self.reservations,
                    "Key": _key(_reservation_key(reservation_id)),
                    "UpdateExpression": "SET " + ", ".join(updates),
                    "ConditionExpression": condition,
                    "ExpressionAttributeValues": values,
                }
            }
        ]
        resource_ids = set(reservation.get("volume_ids") or [])
        if reservation.get("instance_id"):
            resource_ids.add(reservation["instance_id"])
        resource_ids.update(volume_ids or [])
        if instance_id:
            resource_ids.add(instance_id)
        for resource_id in sorted(resource_ids):
            key = {"pk": f"RESOURCE#{resource_id}", "sk": "MAP"}
            existing_mapping = self._get_item(self.reservations, key)
            if existing_mapping:
                if (
                    existing_mapping.get("reservation_id") != reservation_id
                    or existing_mapping.get("team") != reservation["team"]
                ):
                    raise ReservationConflict(
                        f"resource {resource_id} is already mapped to another reservation"
                    )
                continue
            actions.append(
                self._conditional_put(
                    self.reservations,
                    {
                        "pk": f"RESOURCE#{resource_id}",
                        "sk": "MAP",
                        "record_type": "RESOURCE_ATTRIBUTION",
                        "resource_id": resource_id,
                        "reservation_id": reservation_id,
                        "team": reservation["team"],
                        "created_at": _now(),
                    },
                    "attribute_not_exists(pk)",
                )
            )
        try:
            self.db.transact_write_items(TransactItems=actions)
        except ClientError as error:
            if _error_code(error) == "ConditionalCheckFailedException":
                latest = self.get_reservation(reservation_id)
                if latest.get("workflow_execution_status") == execution_status and (
                    cleanup_state is None or latest.get("workflow_cleanup_state") == cleanup_state
                ):
                    return {"reservation": latest, "replayed": True}
                raise InvalidTransition(
                    "workflow execution outcome changed concurrently"
                ) from error
            if _error_code(error) == "TransactionCanceledException":
                latest = self.get_reservation(reservation_id)
                mappings_match = True
                for resource_id in resource_ids:
                    mapping = self._get_item(
                        self.reservations, {"pk": f"RESOURCE#{resource_id}", "sk": "MAP"}
                    )
                    if mapping and (
                        mapping.get("reservation_id") != reservation_id
                        or mapping.get("team") != reservation["team"]
                    ):
                        mappings_match = False
                        break
                if (
                    latest.get("workflow_execution_status") == execution_status
                    and (
                        cleanup_state is None
                        or latest.get("workflow_cleanup_state") == cleanup_state
                    )
                    and (instance_id is None or latest.get("instance_id") == instance_id)
                    and (volume_ids is None or latest.get("volume_ids") == volume_ids)
                    and mappings_match
                ):
                    return {"reservation": latest, "replayed": True}
                raise ReservationConflict(
                    "resource ID is already mapped to another reservation"
                ) from error
            if _ambiguous_client_error(error):
                return self._resolve_workflow_execution_ambiguous(
                    reservation_id, execution_status, cleanup_state, error
                )
            raise
        except _TRANSPORT_ERRORS as error:
            return self._resolve_workflow_execution_ambiguous(
                reservation_id, execution_status, cleanup_state, error
            )
        return {"reservation": self.get_reservation(reservation_id), "replayed": False}

    def provisional_settle(
        self,
        reservation_id: str,
        provisional_micro: int,
        *,
        settlement_id: str | None = None,
        settled_at: str | None = None,
    ) -> dict:
        """Move HELD dollars to provisional spend after EXPIRED/CANCELLED."""
        cost = _money(provisional_micro, "provisional_micro")
        sid = settlement_id or f"{reservation_id}:provisional"
        _validate_identifier(sid, "settlement_id")
        reservation = self.get_reservation(reservation_id)
        existing_stage = reservation.get("settlement_stage")
        if existing_stage == "PROVISIONAL":
            if (
                reservation.get("provisional_settlement_id") == sid
                and reservation.get("provisional_micro") == cost
            ):
                return {"reservation": reservation, "replayed": True}
            raise SettlementConflict("reservation already has a different provisional settlement")
        if reservation["status"] not in {"EXPIRED", "CANCELLED"}:
            raise InvalidTransition(
                f"cannot provisionally settle reservation in {reservation['status']}"
            )
        self._require_open_budget(reservation["team"], reservation["budget_period"])
        amount = reservation["reserved_micro"]
        now = settled_at or _now()
        idem_item = _settlement_id_item(sid, reservation_id, "PROVISIONAL", 1, cost, now)
        delta_available = amount - cost
        actions = [
            {
                "Update": {
                    "TableName": self.reservations,
                    "Key": _key(_reservation_key(reservation_id)),
                    "UpdateExpression": (
                        "SET settlement_stage = :stage, provisional_micro = :cost, "
                        "provisional_settlement_id = :sid, settled_at = :now, updated_at = :now"
                    ),
                    "ConditionExpression": (
                        "#state IN (:expired, :cancelled) AND attribute_not_exists(settlement_stage)"
                    ),
                    "ExpressionAttributeNames": {"#state": "status"},
                    "ExpressionAttributeValues": {
                        ":stage": _encode("PROVISIONAL"),
                        ":cost": _encode(cost),
                        ":sid": _encode(sid),
                        ":now": _encode(now),
                        ":expired": _encode("EXPIRED"),
                        ":cancelled": _encode("CANCELLED"),
                    },
                }
            },
            self._budget_update(
                reservation,
                "SET available_micro = available_micro + :delta, "
                "reserved_micro = reserved_micro - :reserve, spent_micro = spent_micro + :cost, "
                "version = version + :one, updated_at = :now",
                "reserved_micro >= :reserve AND open_reservations >= :one AND #state = :open",
                {
                    ":delta": delta_available,
                    ":reserve": amount,
                    ":cost": cost,
                    ":one": 1,
                    ":open": "OPEN",
                    ":now": now,
                },
            ),
            self._conditional_put(self.reservations, idem_item, "attribute_not_exists(pk)"),
        ]
        try:
            self.db.transact_write_items(TransactItems=actions)
        except ClientError as error:
            if _error_code(error) == "TransactionCanceledException":
                return self._resolve_provisional_cancel(reservation_id, sid, cost, error)
            if _ambiguous_client_error(error):
                return self._resolve_provisional_ambiguous(reservation_id, sid, cost, error)
            raise
        except _TRANSPORT_ERRORS as error:
            return self._resolve_provisional_ambiguous(reservation_id, sid, cost, error)
        return {"reservation": self.get_reservation(reservation_id), "replayed": False}

    def final_settle(
        self,
        reservation_id: str,
        actual_micro: int,
        *,
        settlement_id: str | None = None,
        settlement_version: int = 1,
        settled_at: str | None = None,
        billing_reconciliation: dict | None = None,
    ) -> dict:
        """Apply a final settlement version; later versions correct prior actuals."""
        actual = _money(actual_micro, "actual_micro")
        if (
            isinstance(settlement_version, bool)
            or not isinstance(settlement_version, int)
            or settlement_version < 1
        ):
            raise ValueError("settlement_version must be a positive integer")
        sid = settlement_id or f"{reservation_id}:final:{settlement_version}"
        _validate_identifier(sid, "settlement_id")
        reservation = self.get_reservation(reservation_id)
        current_version = reservation.get("settlement_version", 0)
        if settlement_version != current_version + 1:
            existing = self._get_item(self.reservations, {"pk": f"SETTLEID#{sid}", "sk": "ID"})
            if existing:
                return self._settlement_replay(
                    existing, reservation_id, sid, actual, settlement_version, phase="FINAL"
                )
            raise SettlementConflict(
                f"expected settlement version {current_version + 1}, received {settlement_version}"
            )
        self._require_open_budget(
            reservation["team"],
            reservation["budget_period"],
            allow_closed=current_version > 0,
        )
        stage = reservation.get("settlement_stage")
        if not stage:
            raise InvalidTransition("final settlement requires provisional settlement first")
        previous_actual = reservation.get("final_actual_micro")
        previous_cost = (
            reservation["provisional_micro"] if current_version == 0 else previous_actual
        )
        if previous_cost is None:
            raise LedgerInvariantViolation("settled reservation is missing prior actual cost")
        delta = actual - previous_cost
        if actual > reservation["reserved_micro"] and not self.alerts:
            raise LedgerConfigurationError("alerts_table is required to record a budget overrun")
        now = settled_at or _now()
        settlement_item = _settlement_id_item(
            sid, reservation_id, "FINAL", settlement_version, actual, now
        )
        version_item = {
            "pk": f"SETTLEVER#{reservation_id}",
            "sk": f"V#{settlement_version:010d}",
            "record_type": "SETTLEMENT_VERSION",
            "settlement_id": sid,
            "reservation_id": reservation_id,
            "settlement_version": settlement_version,
            "actual_micro": actual,
            "created_at": now,
        }
        budget_update = self._budget_update(
            reservation,
            "SET available_micro = available_micro - :delta, "
            "spent_micro = spent_micro + :delta, version = version + :one, updated_at = :now",
            "#state IN (:open, :closed)" if current_version > 0 else "#state = :open",
            {
                ":delta": delta,
                ":one": 1,
                ":open": "OPEN",
                ":now": now,
                **({":closed": "CLOSED"} if current_version > 0 else {}),
            },
        )
        if current_version == 0:
            budget_update["Update"]["UpdateExpression"] = (
                "SET available_micro = available_micro - :delta, "
                "spent_micro = spent_micro + :delta, "
                "open_reservations = open_reservations - :one, "
                "version = version + :one, updated_at = :now"
            )
            budget_update["Update"]["ConditionExpression"] += " AND open_reservations >= :one"
        released_cleanup_correction = (
            reservation.get("status") == "RELEASED"
            and current_version > 0
            and reservation.get("workflow_cleanup_state") == "COMPLETE"
        )
        reservation_values = {
            ":final": _encode("FINAL"),
            ":expected_stage": _encode("PROVISIONAL" if current_version == 0 else "FINAL"),
            ":actual": _encode(actual),
            ":sid": _encode(sid),
            ":version": _encode(settlement_version),
            ":previous_version": _encode(current_version),
            ":now": _encode(now),
        }
        if released_cleanup_correction:
            reservation_values.update(
                {":released": _encode("RELEASED"), ":complete": _encode("COMPLETE")}
            )
        else:
            reservation_values.update(
                {
                    ":expired": _encode("EXPIRED"),
                    ":cancelled": _encode("CANCELLED"),
                    ":settled": _encode("SETTLED"),
                }
            )
        reservation_update = {
            "TableName": self.reservations,
            "Key": _key(_reservation_key(reservation_id)),
            "UpdateExpression": (
                "SET settlement_stage = :final, final_actual_micro = :actual, final_settlement_id = :sid, "
                "settlement_version = :version, settled_at = :now, updated_at = :now"
            )
            if released_cleanup_correction
            else (
                "SET settlement_stage = :final, #state = :settled, final_actual_micro = :actual, "
                "final_settlement_id = :sid, settlement_version = :version, settled_at = :now, updated_at = :now"
            ),
            "ConditionExpression": (
                "settlement_stage = :expected_stage AND settlement_version = :previous_version "
                "AND #state = :released AND workflow_cleanup_state = :complete"
                if released_cleanup_correction
                else (
                    "settlement_stage = :expected_stage AND settlement_version = :previous_version "
                    "AND #state IN (:expired, :cancelled, :settled)"
                )
            ),
            "ExpressionAttributeNames": {"#state": "status"},
            "ExpressionAttributeValues": reservation_values,
        }
        actions = [
            {"Update": reservation_update},
            budget_update,
            self._conditional_put(self.reservations, settlement_item, "attribute_not_exists(pk)"),
            self._conditional_put(self.reservations, version_item, "attribute_not_exists(pk)"),
        ]
        if billing_reconciliation is not None:
            audit = dict(billing_reconciliation)
            order = audit["source_revision_order"]
            actions[0]["Update"]["UpdateExpression"] += (
                ", billing_source_revision_order = :billing_order"
            )
            actions[0]["Update"]["ConditionExpression"] += (
                " AND (attribute_not_exists(billing_source_revision_order) "
                "OR billing_source_revision_order < :billing_order)"
            )
            actions[0]["Update"]["ExpressionAttributeValues"][":billing_order"] = _encode(order)
            audit.update(
                {
                    "pk": f"BILLING#{audit['reconciliation_id']}",
                    "sk": "RECONCILIATION",
                    "record_type": "BILLING_RECONCILIATION",
                    "reservation_id": reservation_id,
                    "team": reservation["team"],
                    "billing_period": audit["billing_period"],
                    "source_revision": audit["source_revision"],
                    "source_revision_order": order,
                    "input_digest": audit["input_digest"],
                    "previous_actual_micro": previous_cost,
                    "new_actual_micro": actual,
                    "adjustment_micro": delta,
                    "settlement_id": sid,
                    "settlement_version": settlement_version,
                    "created_at": now,
                }
            )
            actions.append(
                self._conditional_put(self.reservations, audit, "attribute_not_exists(pk)")
            )
        if actual > reservation["reserved_micro"]:
            alert = {
                "alert_id": f"OVERRUN#{reservation_id}#V#{settlement_version:010d}",
                "alert_type": "BudgetOverrun",
                "details": {
                    "actual_micro": actual,
                    "reserved_micro": reservation["reserved_micro"],
                    "settlement_id": sid,
                },
                "created_at": now,
                "acknowledged": False,
                "reservation_id": reservation_id,
                "team": reservation["team"],
            }
            actions.append(
                self._conditional_put(self.alerts, alert, "attribute_not_exists(alert_id)")
            )
        try:
            self.db.transact_write_items(TransactItems=actions)
        except ClientError as error:
            if _error_code(error) == "TransactionCanceledException":
                return self._resolve_final_cancel(
                    reservation_id, sid, settlement_version, actual, error
                )
            if _ambiguous_client_error(error):
                return self._resolve_final_ambiguous(
                    reservation_id, sid, settlement_version, actual, error
                )
            raise
        except _TRANSPORT_ERRORS as error:
            return self._resolve_final_ambiguous(
                reservation_id, sid, settlement_version, actual, error
            )
        return {"reservation": self.get_reservation(reservation_id), "replayed": False}

    def reconcile_billing(
        self,
        reservation_id: str,
        actual_micro: int,
        *,
        reconciliation_id: str,
        billing_period: str,
        source_revision: str,
        source_revision_order: str,
        input_digest: str,
        attributed_record_ids: list[str],
        settled_at: str | None = None,
    ) -> dict:
        """Atomically apply a CUR target and persist its immutable audit identity."""
        actual = _money(actual_micro, "actual_micro")
        for value, label in (
            (reconciliation_id, "reconciliation_id"),
            (billing_period, "billing_period"),
            (source_revision, "source_revision"),
            (source_revision_order, "source_revision_order"),
            (input_digest, "input_digest"),
        ):
            _validate_identifier(value, label)
        audit_key = {"pk": f"BILLING#{reconciliation_id}", "sk": "RECONCILIATION"}
        existing = self._get_item(self.reservations, audit_key)
        if existing:
            if (
                existing.get("reservation_id") == reservation_id
                and existing.get("new_actual_micro") == actual
                and existing.get("billing_period") == billing_period
                and existing.get("source_revision") == source_revision
                and existing.get("source_revision_order") == source_revision_order
                and existing.get("input_digest") == input_digest
            ):
                return {
                    "reservation": self.get_reservation(reservation_id),
                    "replayed": True,
                    "audit": existing,
                }
            raise SettlementConflict(
                "billing reconciliation identity was reused with different input"
            )
        reservation = self.get_reservation(reservation_id)
        current_order = reservation.get("billing_source_revision_order")
        if current_order is not None and source_revision_order <= current_order:
            raise SettlementConflict("billing source revision is stale or out of order")
        if reservation.get("settlement_stage") not in {"PROVISIONAL", "FINAL"}:
            raise InvalidTransition("billing reconciliation requires a provisional settlement")
        version = reservation.get("settlement_version", 0) + 1
        sid = f"billing:{reservation_id}:{reconciliation_id}"
        audit = {
            "reconciliation_id": reconciliation_id,
            "billing_period": billing_period,
            "source_revision": source_revision,
            "source_revision_order": source_revision_order,
            "input_digest": input_digest,
            "attributed_record_ids": sorted(set(attributed_record_ids)),
        }
        result = self.final_settle(
            reservation_id,
            actual,
            settlement_id=sid,
            settlement_version=version,
            settled_at=settled_at,
            billing_reconciliation=audit,
        )
        persisted = self._get_item(self.reservations, audit_key)
        if not persisted:
            raise AmbiguousLedgerOutcome(
                "billing ledger update committed without readable audit record"
            )
        return {**result, "audit": persisted}

    def get_reservation_by_resource(self, resource_id: str) -> dict:
        """Resolve only an exact, durably recorded EC2/EBS resource identity."""
        mapping = self._get_item(self.reservations, {"pk": f"RESOURCE#{resource_id}", "sk": "MAP"})
        if not mapping or mapping.get("record_type") != "RESOURCE_ATTRIBUTION":
            raise ReservationNotFound(resource_id)
        reservation = self.get_reservation(mapping["reservation_id"])
        if reservation.get("team") != mapping.get("team"):
            raise ReservationConflict("resource mapping team does not match reservation")
        if resource_id != reservation.get("instance_id") and resource_id not in reservation.get(
            "volume_ids", []
        ):
            raise ReservationConflict("resource mapping is not present in reservation history")
        return reservation

    def get_budget(self, team: str, period: str) -> dict:
        return self._get_item(self.budgets, {"team": team, "period": period}) or {}

    def get_reservation(self, reservation_id: str) -> dict:
        item = self._get_item(self.reservations, _reservation_key(reservation_id))
        if not item:
            raise ReservationNotFound(reservation_id)
        return item

    def list_reservations(
        self,
        *,
        team: str | None = None,
        status: str | None = None,
        limit: int = 50,
        start_key: dict | None = None,
    ) -> dict:
        """Paginate via required team/created or status/expiry GSIs; never scan."""
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        if status and status not in {
            "HELD",
            "ACTIVE",
            "EXPIRED",
            "CANCELLED",
            "RELEASED",
            "SETTLED",
        }:
            raise ValueError("unknown reservation status")
        if team:
            index_name = "team-created-index"
            key_name = "team"
            key_value = team
        elif status:
            index_name = "status-expiry-index"
            key_name = "status"
            key_value = status
        else:
            raise ValueError("team or status is required to query reservations")
        request = {
            "TableName": self.reservations,
            "IndexName": index_name,
            "KeyConditionExpression": f"{key_name} = :key",
            "FilterExpression": "record_type = :reservation",
            "ExpressionAttributeValues": {":key": _encode(key_value)},
            "Limit": limit,
            "ScanIndexForward": False,
        }
        if status and team:
            request["FilterExpression"] += " AND #state = :status"
            request["ExpressionAttributeNames"] = {"#state": "status"}
            request["ExpressionAttributeValues"][":status"] = _encode(status)
        request["ExpressionAttributeValues"][":reservation"] = _encode("RESERVATION")
        if start_key:
            request["ExclusiveStartKey"] = _key(start_key)
        response = self.db.query(**request)
        return {
            "items": [_decode_item(item) for item in response.get("Items", [])],
            "last_evaluated_key": _decode_item(response.get("LastEvaluatedKey")),
        }

    def list_expired_active(
        self, *, now: str | None = None, limit: int = 100, start_key: dict | None = None
    ) -> dict:
        """Query only ACTIVE reservations whose indexed expiry is due."""
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        response = self.db.query(
            TableName=self.reservations,
            IndexName="status-expiry-index",
            KeyConditionExpression="#state = :active AND expires_at <= :now",
            FilterExpression="record_type = :reservation",
            ExpressionAttributeNames={"#state": "status"},
            ExpressionAttributeValues={
                ":active": _encode("ACTIVE"),
                ":now": _encode(now or _now()),
                ":reservation": _encode("RESERVATION"),
            },
            Limit=limit,
            ScanIndexForward=True,
            **({"ExclusiveStartKey": _key(start_key)} if start_key else {}),
        )
        return {
            "items": [_decode_item(item) for item in response.get("Items", [])],
            "last_evaluated_key": _decode_item(response.get("LastEvaluatedKey")),
        }

    def record_lifecycle_cleanup(
        self,
        reservation_id: str,
        *,
        cleanup_state: str,
        terminated_at: str | None = None,
    ) -> dict:
        """Persist monotonic cleanup evidence for a cancelled/expired reservation."""
        ranks = {"UNKNOWN": 0, "PENDING": 1, "COMPLETE": 2}
        if cleanup_state not in ranks:
            raise ValueError("cleanup_state must be UNKNOWN, PENDING, or COMPLETE")
        reservation = self.get_reservation(reservation_id)
        if reservation["status"] not in {"CANCELLED", "EXPIRED"}:
            raise InvalidTransition("cleanup evidence requires a cancelled or expired reservation")
        current = reservation.get("workflow_cleanup_state")
        if current in ranks and ranks[current] >= ranks[cleanup_state]:
            fields_match = (
                terminated_at is None or reservation.get("terminated_at") == terminated_at
            )
            if fields_match or reservation.get("terminated_at"):
                return {"reservation": reservation, "replayed": True}
        names = {"#state": "status"}
        values = {
            ":expired": _encode("EXPIRED"),
            ":cancelled": _encode("CANCELLED"),
            ":target": _encode(cleanup_state),
            ":now": _encode(_now()),
        }
        assignments = ["workflow_cleanup_state = :target", "updated_at = :now"]
        if terminated_at is not None:
            assignments.append("terminated_at = if_not_exists(terminated_at, :terminated_at)")
            values[":terminated_at"] = _encode(terminated_at)
        condition = "#state IN (:expired, :cancelled)"
        if current is None:
            condition += " AND attribute_not_exists(workflow_cleanup_state)"
        else:
            condition += " AND workflow_cleanup_state = :current"
            values[":current"] = _encode(current)
        try:
            self.db.update_item(
                TableName=self.reservations,
                Key=_key(_reservation_key(reservation_id)),
                UpdateExpression="SET " + ", ".join(assignments),
                ConditionExpression=condition,
                ExpressionAttributeNames=names,
                ExpressionAttributeValues=values,
            )
        except ClientError as error:
            if _error_code(error) == "ConditionalCheckFailedException":
                latest = self.get_reservation(reservation_id)
                if latest.get("workflow_cleanup_state") == cleanup_state and (
                    terminated_at is None or latest.get("terminated_at")
                ):
                    return {"reservation": latest, "replayed": True}
                raise InvalidTransition(
                    "lifecycle cleanup evidence changed concurrently"
                ) from error
            if _ambiguous_client_error(error):
                return self._resolve_lifecycle_cleanup_ambiguous(
                    reservation_id, cleanup_state, terminated_at, error
                )
            raise
        except _TRANSPORT_ERRORS as error:
            return self._resolve_lifecycle_cleanup_ambiguous(
                reservation_id, cleanup_state, terminated_at, error
            )
        return {"reservation": self.get_reservation(reservation_id), "replayed": False}

    def mark_expiry_event_published(self, reservation_id: str, event_id: str) -> dict:
        """Persist successful publication evidence for the stable expiry event."""
        reservation = self.get_reservation(reservation_id)
        if (
            reservation.get("status") != "EXPIRED"
            or reservation.get("settlement_stage") != "PROVISIONAL"
        ):
            raise InvalidTransition(
                "expiry event requires completed expiry and provisional settlement"
            )
        if reservation.get("expiry_event_published"):
            if reservation.get("expiry_event_id") != event_id:
                raise ReservationConflict("expiry event identity is immutable")
            return {"reservation": reservation, "replayed": True}
        try:
            self.db.update_item(
                TableName=self.reservations,
                Key=_key(_reservation_key(reservation_id)),
                UpdateExpression=(
                    "SET expiry_event_published = :yes, expiry_event_id = :event_id, updated_at = :now"
                ),
                ConditionExpression=(
                    "#state = :expired AND settlement_stage = :provisional "
                    "AND attribute_not_exists(expiry_event_published)"
                ),
                ExpressionAttributeNames={"#state": "status"},
                ExpressionAttributeValues={
                    ":yes": _encode(True),
                    ":event_id": _encode(event_id),
                    ":now": _encode(_now()),
                    ":expired": _encode("EXPIRED"),
                    ":provisional": _encode("PROVISIONAL"),
                },
            )
        except ClientError as error:
            if _error_code(error) == "ConditionalCheckFailedException":
                latest = self.get_reservation(reservation_id)
                if (
                    latest.get("expiry_event_published")
                    and latest.get("expiry_event_id") == event_id
                ):
                    return {"reservation": latest, "replayed": True}
                raise InvalidTransition(
                    "expiry event publication state changed concurrently"
                ) from error
            raise
        return {"reservation": self.get_reservation(reservation_id), "replayed": False}

    def record_instance_state(
        self, reservation_id: str, *, instance_id: str, state: str, occurred_at: str
    ) -> dict:
        """Record a state-change event only for the instance durably bound to this reservation."""
        if state not in {
            "pending",
            "running",
            "stopping",
            "stopped",
            "shutting-down",
            "terminated",
        }:
            raise ValueError("unsupported EC2 instance state")
        reservation = self.get_reservation(reservation_id)
        if reservation.get("instance_id") != instance_id:
            raise ReservationConflict("state event instance does not match reservation")
        prior_time = reservation.get("instance_state_at")
        if prior_time and prior_time > occurred_at:
            return {"reservation": reservation, "replayed": True, "stale_event": True}
        fields = {"instance_state": state, "instance_state_at": occurred_at, "updated_at": _now()}
        if state == "terminated":
            fields["terminated_at"] = reservation.get("terminated_at") or occurred_at
        elif state == "stopped":
            fields["stopped_at"] = reservation.get("stopped_at") or occurred_at
        assignments = []
        values = {":reservation_id": _encode(reservation_id), ":instance_id": _encode(instance_id)}
        for index, (field, value) in enumerate(fields.items()):
            name = f":v{index}"
            assignments.append(f"{field} = {name}")
            values[name] = _encode(value)
        try:
            self.db.update_item(
                TableName=self.reservations,
                Key=_key(_reservation_key(reservation_id)),
                UpdateExpression="SET " + ", ".join(assignments),
                ConditionExpression=(
                    "reservation_id = :reservation_id AND instance_id = :instance_id "
                    "AND #state IN (:active, :cancelled, :expired) "
                    "AND (attribute_not_exists(instance_state_at) OR instance_state_at <= :occurred_at)"
                ),
                ExpressionAttributeNames={"#state": "status"},
                ExpressionAttributeValues={
                    **values,
                    ":active": _encode("ACTIVE"),
                    ":cancelled": _encode("CANCELLED"),
                    ":expired": _encode("EXPIRED"),
                    ":occurred_at": _encode(occurred_at),
                },
            )
        except ClientError as error:
            if _error_code(error) == "ConditionalCheckFailedException":
                latest = self.get_reservation(reservation_id)
                if (
                    latest.get("instance_state") == state
                    and latest.get("instance_state_at") == occurred_at
                ):
                    return {"reservation": latest, "replayed": True}
                if latest.get("instance_state_at", "") > occurred_at:
                    return {"reservation": latest, "replayed": True, "stale_event": True}
                raise InvalidTransition(
                    "instance state event is stale or reservation is ineligible"
                ) from error
            raise
        return {"reservation": self.get_reservation(reservation_id), "replayed": False}

    def record_alert(self, *, alert_id: str, alert_type: str, details: dict) -> dict:
        """Write an alert once using a stable caller-provided event key."""
        item = {
            "alert_id": alert_id,
            "alert_type": alert_type,
            "details": details,
            "created_at": _now(),
            "acknowledged": False,
        }
        try:
            self.db.put_item(
                TableName=self.alerts,
                Item={key: _encode(value) for key, value in item.items()},
                ConditionExpression="attribute_not_exists(alert_id)",
            )
        except ClientError as error:
            if _error_code(error) == "ConditionalCheckFailedException":
                existing = self._get_item(self.alerts, {"alert_id": alert_id})
                return {"alert": existing or item, "replayed": True}
            raise
        return {"alert": item, "replayed": False}

    def _transition(
        self,
        reservation_id: str,
        *,
        allowed_from: set[str],
        target: str,
        fields: dict,
        idempotent_states: set[str],
    ) -> dict:
        reservation = self.get_reservation(reservation_id)
        current = reservation["status"]
        if current in idempotent_states:
            return {"reservation": reservation, "replayed": True}
        if current not in allowed_from:
            raise InvalidTransition(f"cannot move reservation from {current} to {target}")
        assignments = ["#state = :target", "updated_at = :now"]
        values = {":target": _encode(target), ":now": _encode(_now()), ":current": _encode(current)}
        names = {"#state": "status"}
        for index, (key, value) in enumerate(fields.items()):
            placeholder = f":field{index}"
            assignments.append(f"{key} = {placeholder}")
            values[placeholder] = _encode(value)
        try:
            self.db.update_item(
                TableName=self.reservations,
                Key=_key(_reservation_key(reservation_id)),
                UpdateExpression="SET " + ", ".join(assignments),
                ConditionExpression="#state = :current",
                ExpressionAttributeNames=names,
                ExpressionAttributeValues=values,
            )
        except ClientError as error:
            if _error_code(error) == "ConditionalCheckFailedException":
                latest = self.get_reservation(reservation_id)
                if latest["status"] == target or latest["status"] in idempotent_states:
                    return {"reservation": latest, "replayed": True}
                raise InvalidTransition(
                    f"reservation concurrently moved to {latest['status']}"
                ) from error
            if _ambiguous_client_error(error):
                return self._resolve_ambiguous_transition(
                    reservation_id, target, idempotent_states, error
                )
            raise
        except _TRANSPORT_ERRORS as error:
            return self._resolve_ambiguous_transition(
                reservation_id, target, idempotent_states, error
            )
        return {"reservation": self.get_reservation(reservation_id), "replayed": False}

    def _resolve_lifecycle_cleanup_ambiguous(
        self, reservation_id: str, cleanup_state: str, terminated_at: str | None, error
    ):
        try:
            current = self.get_reservation(reservation_id)
        except (ReservationNotFound, *_TRANSPORT_ERRORS, ClientError):
            current = None
        if (
            current
            and current.get("workflow_cleanup_state") == cleanup_state
            and (terminated_at is None or current.get("terminated_at"))
        ):
            return {"reservation": current, "replayed": True}
        raise AmbiguousLedgerOutcome(
            "lifecycle cleanup result is unknown; reconcile resources before acting again"
        ) from error

    def _budget_update(self, reservation: dict, update: str, condition: str, values: dict) -> dict:
        expression_values = {key: _encode(value) for key, value in values.items()}
        return {
            "Update": {
                "TableName": self.budgets,
                "Key": _key({"team": reservation["team"], "period": reservation["budget_period"]}),
                "UpdateExpression": update,
                "ConditionExpression": condition,
                "ExpressionAttributeNames": {"#state": "status"},
                "ExpressionAttributeValues": expression_values,
            }
        }

    def _require_open_budget(self, team: str, period: str, *, allow_closed: bool = False) -> dict:
        budget = self.get_budget(team, period)
        if not budget:
            raise BudgetNotFound(f"budget {team}/{period} does not exist")
        allowed = {"OPEN", "CLOSED"} if allow_closed else {"OPEN"}
        if budget.get("status") not in allowed:
            raise BudgetClosed(f"budget {team}/{period} is {budget.get('status')}")
        amounts = [
            budget.get(name)
            for name in ("budget_micro", "available_micro", "reserved_micro", "spent_micro")
        ]
        if any(isinstance(value, bool) or not isinstance(value, int) for value in amounts):
            raise LedgerInvariantViolation("budget has missing or non-integer balance fields")
        if sum(amounts[1:]) != amounts[0]:
            raise LedgerInvariantViolation(
                "available_micro + reserved_micro + spent_micro does not equal budget_micro"
            )
        open_reservations = budget.get("open_reservations")
        if isinstance(open_reservations, bool) or not isinstance(open_reservations, int):
            raise LedgerInvariantViolation("budget has missing or non-integer open_reservations")
        return budget

    def _conditional_put(self, table: str | None, item: dict, condition: str) -> dict:
        if not table:
            raise LedgerConfigurationError("required DynamoDB table is not configured")
        return {
            "Put": {
                "TableName": table,
                "Item": {key: _encode(value) for key, value in item.items()},
                "ConditionExpression": condition,
            }
        }

    def _get_item(self, table: str, key: dict) -> dict | None:
        response = self.db.get_item(TableName=table, Key=_key(key), ConsistentRead=True)
        return _decode_item(response.get("Item"))

    def _resolve_reserve_cancellation(
        self, team, period, idem_key, reservation_id, fingerprint, amount, error
    ):
        idem = self._get_item(self.reservations, _idempotency_key(team, idem_key))
        if idem:
            return self._resolve_existing_idempotency(idem, fingerprint)
        existing_reservation = self._get_item(self.reservations, _reservation_key(reservation_id))
        if existing_reservation:
            raise ReservationConflict(f"reservation_id {reservation_id} already exists") from error
        budget = self.get_budget(team, period)
        if not budget:
            raise BudgetNotFound(f"budget {team}/{period} does not exist") from error
        if budget.get("status") != "OPEN":
            raise BudgetClosed(f"budget {team}/{period} is {budget.get('status')}") from error
        if budget.get("available_micro", 0) < amount:
            raise InsufficientBudget("available budget is less than reserve") from error
        raise LedgerTransactionCancelled("DynamoDB cancelled the reserve transaction") from error

    def _resolve_ambiguous_reserve(self, team, idem_key, reservation_id, fingerprint, error):
        try:
            idem = self._get_item(self.reservations, _idempotency_key(team, idem_key))
            if idem:
                return self._resolve_existing_idempotency(idem, fingerprint)
        except _TRANSPORT_ERRORS:
            pass
        raise AmbiguousLedgerOutcome(
            "reserve outcome is unknown; retry only with the same team, idempotency key, "
            "request, and reservation ID"
        ) from error

    def _resolve_existing_idempotency(self, idem: dict, fingerprint: str) -> dict:
        if idem.get("request_fingerprint") != fingerprint:
            raise DuplicateIdempotencyKey(
                "Idempotency-Key was already used for a different request"
            )
        reservation_id = idem.get("reservation_id")
        reservation = self._get_item(self.reservations, _reservation_key(reservation_id))
        if not reservation:
            raise LedgerInvariantViolation(
                "idempotency record exists but its reservation record is missing"
            )
        return {"reservation": reservation, "replayed": True}

    def _resolve_release_cancel(self, reservation_id, error):
        current = self.get_reservation(reservation_id)
        if current["status"] == "RELEASED":
            return {"reservation": current, "replayed": True}
        if current["status"] != "HELD":
            raise InvalidTransition(f"cannot release reservation in {current['status']}") from error
        if current.get("workflow_start_state", "NOT_STARTED") not in {"NOT_STARTED", "FAILED"}:
            raise InvalidTransition(
                f"cannot release reservation while workflow start is {current['workflow_start_state']}"
            ) from error
        budget = self.get_budget(current["team"], current["budget_period"])
        if (
            budget.get("reserved_micro", 0) < current["reserved_micro"]
            or budget.get("open_reservations", 0) < 1
        ):
            raise LedgerInvariantViolation(
                "budget reserved amount is smaller than reservation"
            ) from error
        raise LedgerTransactionCancelled("DynamoDB cancelled release transaction") from error

    def _resolve_ambiguous_state(self, reservation_id, target, error):
        try:
            current = self.get_reservation(reservation_id)
        except _TRANSPORT_ERRORS:
            current = None
        if current and current["status"] == target:
            return {"reservation": current, "replayed": True}
        raise AmbiguousLedgerOutcome(
            f"{target} outcome is unknown; reconcile reservation and budget before retry"
        ) from error

    def _resolve_ambiguous_transition(self, reservation_id, target, idempotent_states, error):
        try:
            current = self.get_reservation(reservation_id)
        except _TRANSPORT_ERRORS:
            current = None
        if current and current["status"] in ({target} | idempotent_states):
            return {"reservation": current, "replayed": True}
        raise AmbiguousLedgerOutcome(
            f"transition to {target} is unknown; reconcile before retry"
        ) from error

    def _resolve_workflow_state_race(self, reservation_id, target, error):
        latest = self.get_reservation(reservation_id)
        if latest.get("workflow_start_state") == target:
            return {"reservation": latest, "replayed": True}
        raise InvalidTransition(
            f"workflow start state concurrently moved to {latest.get('workflow_start_state')}"
        ) from error

    def _resolve_workflow_state_ambiguous(self, reservation_id, target, error):
        try:
            latest = self.get_reservation(reservation_id)
        except _TRANSPORT_ERRORS:
            latest = None
        if latest and latest.get("workflow_start_state") == target:
            return {"reservation": latest, "replayed": True}
        raise AmbiguousLedgerOutcome(
            "workflow start state update is unknown; reconcile before release or retry"
        ) from error

    def _resolve_workflow_execution_ambiguous(
        self, reservation_id, execution_status, cleanup_state, error
    ):
        try:
            current = self.get_reservation(reservation_id)
        except (ReservationNotFound, *_TRANSPORT_ERRORS, ClientError):
            current = None
        if (
            current
            and current.get("workflow_execution_status") == execution_status
            and (cleanup_state is None or current.get("workflow_cleanup_state") == cleanup_state)
        ):
            return {"reservation": current, "replayed": True}
        raise AmbiguousLedgerOutcome(
            "workflow execution outcome is unknown; reconcile before release or retry"
        ) from error

    def _resolve_provisional_cancel(self, reservation_id, sid, cost, error):
        idem = self._get_item(self.reservations, _settlement_id_key(sid))
        if idem:
            return self._settlement_replay(idem, reservation_id, sid, cost, 1, phase="PROVISIONAL")
        current = self.get_reservation(reservation_id)
        if current.get("settlement_stage") == "PROVISIONAL":
            if (
                current.get("provisional_settlement_id") == sid
                and current.get("provisional_micro") == cost
            ):
                return {"reservation": current, "replayed": True}
            raise SettlementConflict(
                "reservation already has a different provisional settlement"
            ) from error
        if current["status"] not in {"EXPIRED", "CANCELLED"}:
            raise InvalidTransition(f"cannot settle reservation in {current['status']}") from error
        budget = self.get_budget(current["team"], current["budget_period"])
        if budget.get("reserved_micro", 0) < current["reserved_micro"]:
            raise LedgerInvariantViolation(
                "budget reserved amount is smaller than reservation"
            ) from error
        raise LedgerTransactionCancelled("DynamoDB cancelled provisional settlement") from error

    def _resolve_provisional_ambiguous(self, reservation_id, sid, cost, error):
        try:
            idem = self._get_item(self.reservations, _settlement_id_key(sid))
            if idem:
                return self._settlement_replay(
                    idem, reservation_id, sid, cost, 1, phase="PROVISIONAL"
                )
            current = self.get_reservation(reservation_id)
            if (
                current.get("settlement_stage") == "PROVISIONAL"
                and current.get("provisional_settlement_id") == sid
                and current.get("provisional_micro") == cost
            ):
                return {"reservation": current, "replayed": True}
        except _TRANSPORT_ERRORS:
            pass
        raise AmbiguousLedgerOutcome(
            "provisional settlement outcome unknown; reconcile before retry"
        ) from error

    def _resolve_final_cancel(self, reservation_id, sid, version, actual, error):
        idem = self._get_item(self.reservations, _settlement_id_key(sid))
        if idem:
            return self._settlement_replay(
                idem, reservation_id, sid, actual, version, phase="FINAL"
            )
        version_item = self._get_item(
            self.reservations, _settlement_version_key(reservation_id, version)
        )
        if version_item:
            raise SettlementConflict(
                f"settlement version {version} already has another result"
            ) from error
        current = self.get_reservation(reservation_id)
        if current.get("settlement_version", 0) + 1 != version:
            raise SettlementConflict(
                f"expected settlement version {current.get('settlement_version', 0) + 1}"
            ) from error
        raise LedgerTransactionCancelled("DynamoDB cancelled final settlement") from error

    def _resolve_final_ambiguous(self, reservation_id, sid, version, actual, error):
        try:
            idem = self._get_item(self.reservations, _settlement_id_key(sid))
            if idem:
                return self._settlement_replay(
                    idem, reservation_id, sid, actual, version, phase="FINAL"
                )
        except _TRANSPORT_ERRORS:
            pass
        raise AmbiguousLedgerOutcome(
            "final settlement outcome unknown; reconcile before retry"
        ) from error

    def _settlement_replay(self, idem, reservation_id, sid, amount, version, phase):
        if (
            idem.get("settlement_id") != sid
            or idem.get("reservation_id") != reservation_id
            or idem.get("amount_micro") != amount
            or idem.get("settlement_version") != version
            or idem.get("phase") != phase
        ):
            raise SettlementConflict("settlement id was reused with different settlement data")
        reservation = self.get_reservation(reservation_id)
        return {"reservation": reservation, "replayed": True}


def _error_code(error: ClientError) -> str | None:
    return error.response.get("Error", {}).get("Code")


def _ambiguous_client_error(error: ClientError) -> bool:
    return _error_code(error) in {
        "InternalServerError",
        "ServiceUnavailable",
        "RequestTimeout",
        "RequestTimeoutException",
        "TransactionInProgressException",
    }


def _validate_identifier(value: str, name: str) -> None:
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > 900:
        raise ValueError(f"{name} must be a non-empty string no longer than 900 bytes")


def _request_fingerprint(value: dict) -> str:
    semantic = {key: item for key, item in value.items() if key not in _IDEMPOTENCY_OMIT_FIELDS}
    encoded = json.dumps(_decimal_json(semantic), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _reservation_key(reservation_id: str) -> dict:
    return {"pk": f"RES#{reservation_id}", "sk": "META"}


def _idempotency_key(team: str, key: str) -> dict:
    composite = json.dumps([team, key], ensure_ascii=False, separators=(",", ":"))
    digest = hashlib.sha256(composite.encode("utf-8")).hexdigest()
    return {"pk": f"IDEMP#{digest}", "sk": "KEY"}


def _settlement_id_key(settlement_id: str) -> dict:
    return {"pk": f"SETTLEID#{settlement_id}", "sk": "ID"}


def _settlement_version_key(reservation_id: str, version: int) -> dict:
    return {"pk": f"SETTLEVER#{reservation_id}", "sk": f"V#{version:010d}"}


def _settlement_id_item(
    settlement_id: str,
    reservation_id: str,
    phase: str,
    version: int,
    amount_micro: int,
    created_at: str,
) -> dict:
    return {
        "pk": f"SETTLEID#{settlement_id}",
        "sk": "ID",
        "record_type": "SETTLEMENT_IDEMPOTENCY",
        "settlement_id": settlement_id,
        "reservation_id": reservation_id,
        "phase": phase,
        "settlement_version": version,
        "amount_micro": amount_micro,
        "created_at": created_at,
    }
