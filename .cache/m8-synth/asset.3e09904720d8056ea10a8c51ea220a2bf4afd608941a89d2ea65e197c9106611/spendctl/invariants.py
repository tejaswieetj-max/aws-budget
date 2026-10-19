"""Independent, consistent-read audit of ledger balances against reservations."""

from __future__ import annotations

import hashlib
import json
import time
from collections import defaultdict
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from boto3.dynamodb.types import TypeDeserializer, TypeSerializer
from botocore.exceptions import ClientError

_DESERIALIZER = TypeDeserializer()
_SERIALIZER = TypeSerializer()
_RESERVED_STATES = {"HELD", "ACTIVE", "CANCELLED", "EXPIRED"}


class InvariantChecker:
    """Recompute each budget's reserved and spent totals from reservation rows.

    Scans use ConsistentRead. Since DynamoDB does not provide a transactionally
    consistent snapshot across the two tables, mismatches are re-read once after
    a short delay before an alert is emitted.
    """

    def __init__(
        self,
        db: Any,
        *,
        budgets_table: str,
        reservations_table: str,
        alerts_table: str,
        eventbridge: Any,
        event_bus_name: str,
        sleep=time.sleep,
        now=lambda: datetime.now(UTC),
        recheck_delay: float = 0.25,
        emit=print,
    ):
        self.db = db
        self.budgets_table = budgets_table
        self.reservations_table = reservations_table
        self.alerts_table = alerts_table
        self.eventbridge = eventbridge
        self.event_bus_name = event_bus_name
        self.sleep = sleep
        self.now = now
        self.recheck_delay = recheck_delay
        self.emit = emit

    def check(self) -> dict:
        mismatches = self._mismatches()
        rechecked = False
        if mismatches:
            self.sleep(self.recheck_delay)
            mismatches = self._mismatches()
            rechecked = True

        window = int(self.now().timestamp()) // 300
        alert_ids = []
        for mismatch in mismatches:
            stable = f"{mismatch['team']}#{mismatch['period']}#{window}"
            alert_id = "INVARIANT#" + hashlib.sha256(stable.encode()).hexdigest()
            self._write_alert(alert_id, mismatch)
            self._publish(alert_id, mismatch)
            alert_ids.append(alert_id)

        self.emit(
            json.dumps(
                self._emf(len(mismatches)),
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return {
            "checked": True,
            "rechecked": rechecked,
            "discrepancy_count": len(mismatches),
            "alert_ids": alert_ids,
            "mismatches": mismatches,
        }

    def _mismatches(self) -> list[dict]:
        budgets = self._scan(self.budgets_table)
        reservations = [
            row
            for row in self._scan(self.reservations_table)
            if row.get("record_type") == "RESERVATION"
        ]
        totals: dict[tuple[str, str], dict[str, int]] = defaultdict(
            lambda: {"reserved_micro": 0, "spent_micro": 0, "open_reservations": 0}
        )
        unmatched = []
        for reservation in reservations:
            key = (reservation.get("team"), reservation.get("budget_period"))
            if not all(isinstance(value, str) and value for value in key):
                unmatched.append(
                    {
                        "reservation_id": reservation.get("reservation_id"),
                        "reason": "missing budget identity",
                    }
                )
                continue
            state = reservation.get("status")
            stage = reservation.get("settlement_stage")
            row = totals[key]
            if (
                state not in {"RELEASED", "SETTLED"} or stage == "PROVISIONAL"
            ) and stage != "FINAL":
                row["open_reservations"] += 1
            if state in _RESERVED_STATES and stage is None:
                row["reserved_micro"] += _integer(
                    reservation.get("reserved_micro"), "reserved_micro"
                )
            if stage == "PROVISIONAL":
                row["spent_micro"] += _integer(
                    reservation.get("provisional_micro"), "provisional_micro"
                )
            elif stage == "FINAL":
                row["spent_micro"] += _integer(
                    reservation.get("final_actual_micro"), "final_actual_micro"
                )
            elif state not in _RESERVED_STATES | {"RELEASED", "SETTLED"}:
                unmatched.append(
                    {
                        "reservation_id": reservation.get("reservation_id"),
                        "reason": f"unknown reservation state {state!r}",
                    }
                )

        budget_keys = set()
        mismatches = []
        for budget in budgets:
            key = (budget.get("team"), budget.get("period"))
            budget_keys.add(key)
            expected = totals[key]
            actual = {
                name: _integer(budget.get(name), name)
                for name in ("budget_micro", "available_micro", "reserved_micro", "spent_micro")
            }
            actual_open = _integer(budget.get("open_reservations"), "open_reservations")
            discrepancy = {
                "team": key[0],
                "period": key[1],
                "actual": {**actual, "open_reservations": actual_open},
                "computed": {
                    "reserved_micro": expected["reserved_micro"],
                    "spent_micro": expected["spent_micro"],
                    "open_reservations": expected["open_reservations"],
                    "invariant_total_micro": actual["available_micro"]
                    + expected["reserved_micro"]
                    + expected["spent_micro"],
                },
            }
            discrepancy["reasons"] = [
                name
                for name in ("reserved_micro", "spent_micro", "open_reservations")
                if (actual_open if name == "open_reservations" else actual[name]) != expected[name]
            ]
            if (
                actual["available_micro"] + actual["reserved_micro"] + actual["spent_micro"]
                != actual["budget_micro"]
            ):
                discrepancy["reasons"].append("budget_invariant")
            if (
                actual["available_micro"] + expected["reserved_micro"] + expected["spent_micro"]
                != actual["budget_micro"]
            ):
                discrepancy["reasons"].append("reservation_reconciliation")
            if discrepancy["reasons"]:
                mismatches.append(discrepancy)

        for key in totals.keys() - budget_keys:
            mismatches.append(
                {
                    "team": key[0],
                    "period": key[1],
                    "actual": {},
                    "computed": totals[key],
                    "reasons": ["budget_row_missing"],
                }
            )
        for item in unmatched:
            mismatches.append(
                {
                    "team": "unknown",
                    "period": "unknown",
                    "actual": {},
                    "computed": item,
                    "reasons": ["invalid_reservation_record"],
                }
            )
        return sorted(mismatches, key=lambda item: (str(item["team"]), str(item["period"])))

    def _scan(self, table: str) -> list[dict]:
        rows = []
        key = None
        while True:
            request = {"TableName": table, "ConsistentRead": True}
            if key:
                request["ExclusiveStartKey"] = key
            response = self.db.scan(**request)
            rows.extend(_decode(item) for item in response.get("Items", []))
            key = response.get("LastEvaluatedKey")
            if not key:
                return rows

    def _write_alert(self, alert_id: str, mismatch: dict) -> None:
        item = {
            "alert_id": alert_id,
            "alert_type": "InvariantViolation",
            "details": mismatch,
            "created_at": self.now().isoformat().replace("+00:00", "Z"),
            "acknowledged": False,
        }
        try:
            self.db.put_item(
                TableName=self.alerts_table,
                Item={key: _SERIALIZER.serialize(value) for key, value in item.items()},
                ConditionExpression="attribute_not_exists(alert_id)",
            )
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
                raise

    def _publish(self, alert_id: str, mismatch: dict) -> None:
        response = self.eventbridge.put_events(
            Entries=[
                {
                    "EventBusName": self.event_bus_name,
                    "Source": "spendctl.invariant",
                    "DetailType": "InvariantViolation",
                    "Detail": json.dumps(
                        {"event_id": alert_id, "schema_version": 1, **mismatch},
                        sort_keys=True,
                    ),
                }
            ]
        )
        if response.get("FailedEntryCount", 0):
            raise RuntimeError("failed to publish InvariantViolation")

    @staticmethod
    def _emf(count: int) -> dict:
        return {
            "_aws": {
                "Timestamp": int(time.time() * 1000),
                "CloudWatchMetrics": [
                    {
                        "Namespace": "Spendctl/Admission",
                        "Dimensions": [[]],
                        "Metrics": [{"Name": "InvariantViolation", "Unit": "Count"}],
                    }
                ],
            },
            "InvariantViolation": count,
        }


def _decode(item: dict) -> dict:
    return {key: _DESERIALIZER.deserialize(value) for key, value in item.items()}


def _integer(value, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, Decimal)):
        raise TypeError(f"{name} must be an integer in persisted ledger data")
    if isinstance(value, Decimal) and value != value.to_integral_value():
        raise TypeError(f"{name} must be an integer in persisted ledger data")
    return int(value)
