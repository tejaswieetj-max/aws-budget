"""Fixture-testable CUR 2.0 normalization, attribution, and ledger reconciliation.

This module deliberately does not guess at missing resources or incomplete
periods. The live export's field names must be confirmed before enabling its
scheduled adapter.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation
from typing import Protocol

from .ledger import LedgerError, ReservationConflict, ReservationNotFound

MICRO_USD = Decimal(1_000_000)
_IDENTIFIER = re.compile(r"^[A-Za-z0-9_.:/-]{1,256}$")
_SUPPORTED_PRODUCTS = {"AmazonEC2", "AmazonEBS"}


class BillingError(Exception):
    """Base class for malformed or unsafe billing input."""


class InvalidBillingRecord(BillingError):
    pass


class IncompleteBillingSnapshot(BillingError):
    pass


class AmbiguousAttribution(BillingError):
    pass


class BillingLedgerConflict(BillingError):
    pass


@dataclass(frozen=True)
class NormalizedBillingRecord:
    record_id: str
    billing_period: str
    usage_start: str
    usage_end: str
    account_id: str | None
    service_code: str
    usage_type: str | None
    operation: str | None
    resource_id: str | None
    reservation_tag: str | None
    team_tag: str | None
    line_item_type: str
    currency: str
    amount_micro: int
    cost_field: str
    classification: str
    raw_fingerprint: str


def _required(row: dict, key: str) -> str:
    value = row.get(key)
    if value is None or str(value).strip() == "":
        raise InvalidBillingRecord(f"missing required CUR field: {key}")
    return str(value).strip()


def _micro_usd(value: object, field: str) -> int:
    try:
        usd = Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise InvalidBillingRecord(f"invalid decimal in {field}") from error
    if not usd.is_finite():
        raise InvalidBillingRecord(f"non-finite decimal in {field}")
    # One USD is 1,000,000 micro-USD. Half-even is deterministic and unbiased;
    # signed credits/refunds use the same rule but are never applied as usage.
    return int((usd * MICRO_USD).quantize(Decimal(1), rounding=ROUND_HALF_EVEN))


def normalize_record(row: dict, *, object_id: str) -> NormalizedBillingRecord:
    """Normalize documented CUR 2.0 aliases; fail closed on required fields."""
    if not isinstance(row, dict):
        raise InvalidBillingRecord("CUR row must be an object")
    record_id = row.get("identity_line_item_id") or row.get("record_id")
    if not record_id:
        raise InvalidBillingRecord(
            "identity_line_item_id is required for stable record deduplication"
        )
    period = _required(row, "bill_billing_period_start_date")[:7]
    service = _required(row, "product_product_code")
    line_type = _required(row, "line_item_line_item_type")
    currency = str(row.get("line_item_currency_code") or "USD").upper()
    if currency != "USD":
        raise InvalidBillingRecord(f"unsupported CUR currency: {currency}")
    cost_field = (
        "line_item_net_unblended_cost"
        if row.get("line_item_net_unblended_cost") not in (None, "")
        else "line_item_unblended_cost"
    )
    if row.get(cost_field) in (None, ""):
        raise InvalidBillingRecord("CUR row has neither net nor unblended cost")
    amount = _micro_usd(row[cost_field], cost_field)
    classification = _classify(service, line_type, amount)
    raw_fingerprint = hashlib.sha256(
        json.dumps(row, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()
    reservation_tag = _nonempty(row.get("resource_tags_user_reservation_id"))
    team_tag = _nonempty(row.get("resource_tags_user_team"))
    return NormalizedBillingRecord(
        record_id=f"{object_id}:{record_id}",
        billing_period=period,
        usage_start=_required(row, "line_item_usage_start_date"),
        usage_end=_required(row, "line_item_usage_end_date"),
        account_id=_nonempty(row.get("line_item_usage_account_id")),
        service_code=service,
        usage_type=_nonempty(row.get("line_item_usage_type")),
        operation=_nonempty(row.get("line_item_operation")),
        resource_id=_nonempty(row.get("line_item_resource_id")),
        reservation_tag=reservation_tag,
        team_tag=team_tag,
        line_item_type=line_type,
        currency=currency,
        amount_micro=amount,
        cost_field=cost_field,
        classification=classification,
        raw_fingerprint=raw_fingerprint,
    )


def _nonempty(value) -> str | None:
    return str(value).strip() if value is not None and str(value).strip() else None


def _classify(service: str, line_type: str, amount: int) -> str:
    if amount < 0 or line_type.lower() in {"credit", "refund", "discount", "edp discount"}:
        return "ADJUSTMENT_EXCLUDED"
    if line_type != "Usage":
        return "NON_USAGE_EXCLUDED"
    if service not in _SUPPORTED_PRODUCTS:
        return "OUT_OF_SCOPE"
    return "USAGE_CHARGE"


class BillingLedger(Protocol):
    def get_reservation(self, reservation_id: str) -> dict: ...
    def get_reservation_by_resource(self, resource_id: str) -> dict: ...
    def reconcile_billing(self, reservation_id: str, actual_micro: int, **kwargs) -> dict: ...


@dataclass
class ReconciliationResult:
    processed: int = 0
    unattributed: int = 0
    excluded: int = 0
    duplicates: int = 0
    adjustments: list[dict] | None = None
    reasons: list[dict] | None = None

    def __post_init__(self):
        self.adjustments = [] if self.adjustments is None else self.adjustments
        self.reasons = [] if self.reasons is None else self.reasons


class BillingReconciler:
    """Apply only complete, identified snapshots and exact resource attribution."""

    def __init__(self, ledger: BillingLedger, *, emit=None):
        self.ledger = ledger
        self.emit = emit or (lambda _name, _detail: None)

    def reconcile(
        self,
        records: list[NormalizedBillingRecord],
        *,
        billing_period: str,
        source_revision: str,
        source_revision_order: str,
        complete: bool,
    ) -> ReconciliationResult:
        if not complete:
            raise IncompleteBillingSnapshot("incomplete CUR snapshot cannot change the ledger")
        if (
            not _IDENTIFIER.fullmatch(billing_period)
            or not _IDENTIFIER.fullmatch(source_revision)
            or not _IDENTIFIER.fullmatch(source_revision_order)
        ):
            raise InvalidBillingRecord("invalid billing period or source revision")
        totals: dict[str, dict] = {}
        seen: dict[str, str] = {}
        result = ReconciliationResult()
        for record in records:
            if record.billing_period != billing_period:
                raise InvalidBillingRecord("snapshot includes a row from another billing period")
            prior_fingerprint = seen.get(record.record_id)
            if prior_fingerprint is not None:
                if prior_fingerprint != record.raw_fingerprint:
                    raise InvalidBillingRecord(
                        "duplicate CUR identity has conflicting amounts or fields"
                    )
                result.duplicates += 1
                continue
            seen[record.record_id] = record.raw_fingerprint
            if record.classification != "USAGE_CHARGE":
                result.excluded += 1
                continue
            reservation = self._attribute(record, result)
            if reservation is None:
                continue
            if not self._single_period_reservation(reservation, billing_period):
                self._unattributed(
                    result,
                    record,
                    "reservation runtime period is incomplete or spans billing periods",
                )
                continue
            entry = totals.setdefault(reservation["reservation_id"], {"micro": 0, "ids": []})
            entry["micro"] += record.amount_micro
            entry["ids"].append((record.record_id, record.raw_fingerprint))
        for reservation_id, aggregate in sorted(totals.items()):
            digest = hashlib.sha256(
                "\n".join(
                    f"{record_id}:{fingerprint}"
                    for record_id, fingerprint in sorted(aggregate["ids"])
                ).encode()
                + b"\0"
                + str(aggregate["micro"]).encode()
            ).hexdigest()
            reconciliation_id = hashlib.sha256(
                f"{reservation_id}\0{billing_period}\0{source_revision}\0{digest}".encode()
            ).hexdigest()
            try:
                settled = self.ledger.reconcile_billing(
                    reservation_id,
                    aggregate["micro"],
                    reconciliation_id=reconciliation_id,
                    billing_period=billing_period,
                    source_revision=source_revision,
                    source_revision_order=source_revision_order,
                    input_digest=digest,
                    attributed_record_ids=[record_id for record_id, _ in aggregate["ids"]],
                )
            except LedgerError as error:
                # Never convert ledger/transport uncertainty into a retry with a
                # new identity or into a refund. Surface the error to the caller.
                self.emit(
                    "BillingLedgerConflict",
                    {"reservation_id": reservation_id, "reason": type(error).__name__},
                )
                if "stale" in str(error).lower() or "out of order" in str(error).lower():
                    self.emit(
                        "BillingStaleRevision",
                        {"reservation_id": reservation_id, "reason": type(error).__name__},
                    )
                raise BillingLedgerConflict(reservation_id) from error
            result.processed += 1
            result.adjustments.append(
                {
                    "reservation_id": reservation_id,
                    "reconciliation_id": reconciliation_id,
                    "previous_actual_micro": settled["audit"].get("previous_actual_micro"),
                    "new_actual_micro": settled["audit"].get("new_actual_micro"),
                    "adjustment_micro": settled["audit"].get("adjustment_micro"),
                    "replayed": settled["replayed"],
                    "team": settled["reservation"].get("team"),
                    "settlement_id": settled["reservation"].get("final_settlement_id"),
                    "settlement_version": settled["reservation"].get("settlement_version"),
                }
            )
            self.emit("BillingReconciled", result.adjustments[-1])
            self.emit("ReservationSettled", result.adjustments[-1])
        return result

    def _attribute(
        self, record: NormalizedBillingRecord, result: ReconciliationResult
    ) -> dict | None:
        by_resource = None
        if record.resource_id:
            try:
                by_resource = self.ledger.get_reservation_by_resource(record.resource_id)
            except ReservationNotFound:
                pass
            except ReservationConflict:
                return self._unattributed(
                    result, record, "resource mapping conflicts with reservation history"
                )
        tagged = None
        if record.reservation_tag:
            try:
                tagged = self.ledger.get_reservation(record.reservation_tag)
            except ReservationNotFound:
                tagged = None
        if by_resource and tagged and by_resource["reservation_id"] != tagged["reservation_id"]:
            return self._unattributed(
                result, record, "reservation tag conflicts with resource mapping"
            )
        reservation = tagged or by_resource
        if not reservation:
            return self._unattributed(
                result, record, "no exact reservation tag or resource mapping"
            )
        if record.team_tag and record.team_tag != reservation.get("team"):
            return self._unattributed(result, record, "team tag does not match reservation")
        return reservation

    def _unattributed(self, result, record, reason):
        result.unattributed += 1
        detail = {
            "record_id": record.record_id,
            "resource_id": record.resource_id,
            "reason": reason,
        }
        result.reasons.append(detail)
        self.emit("BillingAttributionAmbiguous", detail)

    @staticmethod
    def _single_period_reservation(reservation: dict, billing_period: str) -> bool:
        start = reservation.get("launched_at")
        end = reservation.get("terminated_at") or reservation.get("expires_at")
        if not start or not end:
            return False
        try:
            start_dt = datetime.fromisoformat(start)
            end_dt = datetime.fromisoformat(end)
        except ValueError:
            return False
        return (
            start_dt.strftime("%Y-%m") == billing_period
            and end_dt.strftime("%Y-%m") == billing_period
        )


def validate_manifest(manifest: dict) -> tuple[str, str, str]:
    """Validate the minimal manifest contract before querying Athena.

    Data Exports manifest field names are an explicit VERIFY assumption until
    confirmed from an account export; unknown shapes fail closed.
    """
    if not isinstance(manifest, dict):
        raise InvalidBillingRecord("manifest must be a JSON object")
    revision = manifest.get("assemblyId") or manifest.get("assemblyVersion")
    period = manifest.get("billingPeriod")
    created = manifest.get("createTimestamp")
    files = manifest.get("dataFilePaths")
    if not revision or not period or not created or not isinstance(files, list) or not files:
        raise InvalidBillingRecord(
            "manifest missing revision, createTimestamp, billingPeriod, or dataFilePaths"
        )
    period = str(period)
    if len(period) == 6 and period.isdigit():
        period = f"{period[:4]}-{period[4:]}"
    if not _IDENTIFIER.fullmatch(str(revision)) or not re.fullmatch(r"\d{4}-\d{2}", period):
        raise InvalidBillingRecord("manifest revision or billing period has an invalid format")
    try:
        timestamp = datetime.fromisoformat(str(created))
    except ValueError as error:
        raise InvalidBillingRecord("manifest createTimestamp must be an ISO timestamp") from error
    if timestamp.tzinfo is None:
        raise InvalidBillingRecord("manifest createTimestamp must include a timezone")
    order = timestamp.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
    return period, str(revision), order
