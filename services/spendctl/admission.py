"""API-independent validation, estimation, and atomic budget admission (M4)."""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from botocore.exceptions import (
    BotoCoreError,
    ClientError,
    ConnectionClosedError,
    ConnectTimeoutError,
    EndpointConnectionError,
    HTTPClientError,
    ReadTimeoutError,
)

from .calibration import MarginPolicies
from .ledger import (
    AmbiguousLedgerOutcome,
    BudgetClosed,
    BudgetNotFound,
    DuplicateIdempotencyKey,
    InsufficientBudget,
    LedgerTransactionCancelled,
    ReservationConflict,
    ReservationNotFound,
)
from .money import multiply_ceil
from .observability import emit_emf, structured_log
from .pricing import PricingError, PricingProvider, estimate_request
from .validation import Request, validate_request

_TRANSPORT_ERRORS = (
    ConnectionClosedError,
    ConnectTimeoutError,
    EndpointConnectionError,
    HTTPClientError,
    ReadTimeoutError,
    TimeoutError,
    ConnectionError,
)
_PERIOD_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


class AdmissionError(Exception):
    """Base class for expected M4 request outcomes."""


class UnauthorizedTeam(AdmissionError):
    """The verified identity is not authorized for the requested team."""


class AdmissionConflict(AdmissionError):
    """An idempotency key was reused with different request semantics."""


class AdmissionInsufficientBudget(AdmissionError):
    """The team's open budget cannot cover the required reserve."""


class AdmissionRetryableFailure(AdmissionError):
    """A known non-committed ledger failure that may be retried with the same key."""


class AdmissionPricingUnavailable(AdmissionError):
    """The estimate could not be produced from the configured provider."""


class AdmissionOutcomeUnknown(AdmissionError):
    """Reservation may have committed; reconcile using the same request/key."""

    def __init__(self, reservation_id: str):
        self.reservation_id = reservation_id
        self.state = "PENDING_UNKNOWN"
        self.reconciliation_required = True
        super().__init__(
            f"reservation outcome is unknown for {reservation_id}; retry/reconcile with the same "
            "team, period, idempotency key, and request"
        )


@dataclass(frozen=True)
class VerifiedIdentity:
    """Identity claims supplied by a future verified auth adapter, never request JSON."""

    team: str
    subject: str

    def __post_init__(self):
        if not isinstance(self.team, str) or not self.team.strip():
            raise ValueError("verified identity must include a team")
        if not isinstance(self.subject, str) or not self.subject.strip():
            raise ValueError("verified identity must include a subject")


@dataclass(frozen=True)
class AdmissionService:
    ledger: object
    pricing: PricingProvider
    policies: MarginPolicies | None = None
    reserve_multiplier: Decimal = Decimal("1.10")

    def __post_init__(self):
        if self.reserve_multiplier <= 0:
            raise ValueError("reserve_multiplier must be positive")

    def estimate(self, payload: dict, identity: VerifiedIdentity) -> dict:
        request = _authorized_request(payload, identity)
        try:
            return estimate_request(_normalized_payload(payload, request), self.pricing)
        except PricingError as error:
            raise AdmissionPricingUnavailable(str(error)) from error

    def admit(
        self,
        payload: dict,
        *,
        identity: VerifiedIdentity,
        period: str,
        idempotency_key: str,
    ) -> dict:
        """Reserve budget atomically. This method never starts provisioning."""
        started = time.perf_counter()
        result = "error"
        try:
            outcome = self._admit(
                payload,
                identity=identity,
                period=period,
                idempotency_key=idempotency_key,
            )
            result = "replay" if outcome.get("replayed") else "admitted"
            return outcome
        except AdmissionOutcomeUnknown:
            result = "pending_unknown"
            raise
        except AdmissionInsufficientBudget:
            result = "insufficient_budget"
            raise
        except AdmissionConflict:
            result = "conflict"
            raise
        except AdmissionPricingUnavailable:
            result = "pricing_unavailable"
            raise
        except AdmissionRetryableFailure:
            result = "retryable_error"
            raise
        except (TypeError, ValueError):
            result = "invalid_request"
            raise
        finally:
            _emit_admission_emf(result, (time.perf_counter() - started) * 1000)

    def _admit(
        self,
        payload: dict,
        *,
        identity: VerifiedIdentity,
        period: str,
        idempotency_key: str,
    ) -> dict:
        request = _authorized_request(payload, identity)
        _validate_admission_metadata(period, idempotency_key)
        reservation_id = _reservation_id(identity.team, identity.subject, idempotency_key)

        # A durable row wins over current pricing. This makes a replay stable even
        # if the provider's live price changes after the first successful reserve.
        try:
            existing = self.ledger.get_reservation(reservation_id)
        except ReservationNotFound:
            existing = None
        except (AmbiguousLedgerOutcome, BotoCoreError, ClientError, *_TRANSPORT_ERRORS) as error:
            raise AdmissionOutcomeUnknown(reservation_id) from error
        if existing is not None:
            _check_existing(existing, request, identity, period, idempotency_key)
            return _result(existing, replayed=True)

        try:
            budget = self.ledger.get_budget(identity.team, period)
        except (BotoCoreError, ClientError, *_TRANSPORT_ERRORS) as error:
            raise AdmissionRetryableFailure(
                "budget lookup failed before any reservation was attempted"
            ) from error
        if not budget:
            raise AdmissionInsufficientBudget("team has no budget for the requested period")
        if budget.get("status") == "CLOSED":
            raise AdmissionInsufficientBudget("team budget is closed")

        try:
            estimate = estimate_request(_normalized_payload(payload, request), self.pricing)
        except PricingError as error:
            raise AdmissionPricingUnavailable(str(error)) from error

        multiplier = self._multiplier()
        reserved_micro = multiply_ceil(estimate["total_micro"], multiplier)
        policies = self.policies or MarginPolicies()
        shadow_candidates = {
            policy: str(Decimal(str(policies.get("ec2", policy))))
            for policy in ("fixed", "rolling_quantile", "split_conformal", "adaptive")
        }
        record = {
            "resource": {
                "type": "ec2",
                "instance_type": request.instance_type,
                "ebs_gb": request.ebs_gb,
            },
            "planned_hours": request.duration_hours,
            "estimate_micro": estimate["total_micro"],
            "compute_micro": estimate["compute_micro"],
            "storage_micro": estimate["storage_micro"],
            "margin": str(multiplier),
            "shadow_margin_candidates": shadow_candidates,
            "price_snapshot_id": estimate["price_snapshot_id"],
            "price_fetched_at": getattr(self.pricing, "price_fetched_at", None)
            or datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "price_source": estimate["source"],
            "estimate_breakdown": estimate["breakdown"],
            "region": estimate["region"],
            "purpose": request.purpose,
        }
        try:
            outcome = self.ledger.reserve(
                team=identity.team,
                period=period,
                reservation_id=reservation_id,
                idempotency_key=idempotency_key,
                requester=identity.subject,
                reserved_micro=reserved_micro,
                record=record,
            )
        except (InsufficientBudget, BudgetNotFound) as error:
            raise AdmissionInsufficientBudget(str(error)) from error
        except (DuplicateIdempotencyKey, ReservationConflict) as error:
            raise AdmissionConflict(str(error)) from error
        except LedgerTransactionCancelled as error:
            emit_emf("Spendctl/Admission", {"TransactionConflicts": (1, "Count")})
            structured_log(
                "M4", "reserve", "error", reservation_id=reservation_id,
                error_category="transaction_cancelled",
            )
            raise AdmissionRetryableFailure(
                "ledger transaction was cancelled without a confirmed reservation; retry with the same key"
            ) from error
        except AmbiguousLedgerOutcome as error:
            # Resolve only through a consistent durable reservation read. If it
            # cannot be confirmed, leave the hold alone and report unknown.
            try:
                existing = self.ledger.get_reservation(reservation_id)
            except (
                ReservationNotFound,
                AmbiguousLedgerOutcome,
                BotoCoreError,
                ClientError,
                *_TRANSPORT_ERRORS,
            ):
                raise AdmissionOutcomeUnknown(reservation_id) from error
            _check_existing(existing, request, identity, period, idempotency_key)
            return _result(existing, replayed=True)
        except BudgetClosed as error:
            raise AdmissionInsufficientBudget(str(error)) from error

        reservation = outcome["reservation"]
        if outcome.get("replayed"):
            return _result(reservation, replayed=True)
        _emit_budget_utilization(self.ledger, identity.team, period)
        print(
            json.dumps(
                {
                    "event": "AdmissionReserved",
                    "reservation_id": reservation_id,
                    "estimate_breakdown": estimate["breakdown"],
                    "price_snapshot_id": estimate["price_snapshot_id"],
                    "shadow_margin_candidates": shadow_candidates,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return {
            "state": "ADMITTED",
            "http_status": 200 if outcome.get("replayed") else 202,
            "reservation_id": reservation["reservation_id"],
            "reserved_micro": reservation["reserved_micro"],
            "estimate_micro": reservation["estimate_micro"],
            "estimate": estimate,
            "reservation": reservation,
            "replayed": bool(outcome.get("replayed")),
            "provisioning_started": False,
        }

    def reconcile(
        self,
        payload: dict,
        *,
        identity: VerifiedIdentity,
        period: str,
        idempotency_key: str,
    ) -> dict:
        """Idempotent reconciliation path: retry the exact admission identity."""
        return self.admit(
            payload,
            identity=identity,
            period=period,
            idempotency_key=idempotency_key,
        )

    def _multiplier(self) -> Decimal:
        configured = self.reserve_multiplier
        if self.policies is not None:
            configured = Decimal(str(self.policies.get("ec2", "fixed")))
        if not Decimal("1.0") <= configured <= Decimal("2.0"):
            raise ValueError("reserve multiplier must be between 1.0 and 2.0")
        return configured


def _authorized_request(payload: dict, identity: VerifiedIdentity) -> Request:
    request = validate_request(payload)
    if request.team != identity.team:
        raise UnauthorizedTeam("request team does not match the verified identity team")
    return request


def _normalized_payload(payload: dict, request: Request) -> dict:
    # Keep validation as the source of contract policy while ensuring provider
    # input has the authorized team and no caller-controlled region/SKU fields.
    return {
        "team": request.team,
        "resource": {
            "type": "ec2",
            "instance_type": request.instance_type,
            "ebs_gb": request.ebs_gb,
        },
        "duration_hours": request.duration_hours,
        "purpose": request.purpose,
    }


def _validate_admission_metadata(period: str, idempotency_key: str) -> None:
    if not isinstance(period, str) or not _PERIOD_RE.fullmatch(period):
        raise ValueError("period must use YYYY-MM format")
    if not isinstance(idempotency_key, str) or not idempotency_key or len(idempotency_key) > 256:
        raise ValueError("idempotency_key must contain 1 to 256 characters")


def _reservation_id(team: str, subject: str, key: str) -> str:
    material = json.dumps([team, subject, key], ensure_ascii=False, separators=(",", ":"))
    digest = hashlib.sha256(material.encode()).hexdigest()
    return f"rsv_{digest[:40]}"


def _emit_admission_emf(result: str, latency_ms: float) -> None:
    """Write Embedded Metric Format to Lambda stdout for CloudWatch extraction."""
    outcome = (
        "admitted"
        if result in {"admitted", "replay"}
        else "denied"
        if result in {"insufficient_budget", "invalid_request", "conflict"}
        else "error"
    )
    metrics = [
        {"Name": "AdmissionDecision", "Unit": "Count"},
        {"Name": "AdmissionLatencyMs", "Unit": "Milliseconds"},
    ]
    values = {"AdmissionDecision": 1, "AdmissionLatencyMs": latency_ms}
    if result == "replay":
        metrics.append({"Name": "IdempotentReplays", "Unit": "Count"})
        values["IdempotentReplays"] = 1
    if result == "insufficient_budget":
        metrics.append({"Name": "InsufficientBudgetDenials", "Unit": "Count"})
        values["InsufficientBudgetDenials"] = 1
    print(
        json.dumps(
            {
                "_aws": {
                    "Timestamp": int(time.time() * 1000),
                    "CloudWatchMetrics": [
                        {
                            "Namespace": "Spendctl/Admission",
                            "Dimensions": [["result"]],
                            "Metrics": metrics,
                        }
                    ],
                },
                "result": outcome,
                **values,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )


def _emit_budget_utilization(ledger, team: str, period: str) -> None:
    """Publish a dimensionless team utilization sample for 50/80/100% alarms."""
    try:
        budget = ledger.get_budget(team, period)
    except (BotoCoreError, ClientError, *_TRANSPORT_ERRORS):
        structured_log("M11", "budget_utilization", "error", error_category="budget_read_failed")
        return
    if not budget or not budget.get("budget_micro"):
        return
    used = int(budget.get("reserved_micro", 0)) + int(budget.get("spent_micro", 0))
    percent = used * 100.0 / int(budget["budget_micro"])
    emit_emf("Spendctl/Budget", {"BudgetUtilizationPercent": (percent, "Percent")})


def _check_existing(
    reservation: dict, request: Request, identity: VerifiedIdentity, period: str, key: str
) -> None:
    resource = reservation.get("resource", {})
    if (
        reservation.get("team") != identity.team
        or reservation.get("requester") != identity.subject
        or reservation.get("budget_period") != period
        or reservation.get("idempotency_key") != key
        or resource.get("type") != "ec2"
        or resource.get("instance_type") != request.instance_type
        or resource.get("ebs_gb") != request.ebs_gb
        or reservation.get("planned_hours") != request.duration_hours
        or reservation.get("purpose", "") != request.purpose
    ):
        raise AdmissionConflict("idempotency key is already bound to a different admission")


def _result(reservation: dict, *, replayed: bool) -> dict:
    return {
        "state": "ADMITTED",
        "http_status": 200,
        "reservation_id": reservation["reservation_id"],
        "reserved_micro": reservation["reserved_micro"],
        "estimate_micro": reservation.get("estimate_micro"),
        "estimate": {
            "compute_micro": reservation.get("compute_micro"),
            "storage_micro": reservation.get("storage_micro"),
            "total_micro": reservation.get("estimate_micro"),
            "breakdown": reservation.get("estimate_breakdown"),
            "region": reservation.get("region"),
            "price_snapshot_id": reservation.get("price_snapshot_id"),
            "source": reservation.get("price_source"),
        },
        "reservation": reservation,
        "replayed": replayed,
        "provisioning_started": False,
    }
