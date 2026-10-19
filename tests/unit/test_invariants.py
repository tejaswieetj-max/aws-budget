from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import boto3
import pytest
from moto import mock_aws
from spendctl.invariants import InvariantChecker
from spendctl.ledger import DynamoLedger
from spendctl.naive_admission import NaiveAdmissionDisabled, naive_reserve_for_experiment


class FakeEvents:
    def __init__(self):
        self.entries = []

    def put_events(self, **kwargs):
        self.entries.extend(kwargs["Entries"])
        return {"FailedEntryCount": 0}


@pytest.fixture
def aws_tables(monkeypatch):
    with mock_aws():
        monkeypatch.setenv("ADMISSION_MODE", "naive")
        monkeypatch.setenv("ALLOW_NAIVE_ADMISSION", "true")
        db = boto3.client("dynamodb", region_name="ap-south-1")
        db.create_table(
            TableName="Budgets",
            KeySchema=[
                {"AttributeName": "team", "KeyType": "HASH"},
                {"AttributeName": "period", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "team", "AttributeType": "S"},
                {"AttributeName": "period", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        db.create_table(
            TableName="Reservations",
            KeySchema=[
                {"AttributeName": "pk", "KeyType": "HASH"},
                {"AttributeName": "sk", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "pk", "AttributeType": "S"},
                {"AttributeName": "sk", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        db.create_table(
            TableName="Alerts",
            KeySchema=[{"AttributeName": "alert_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "alert_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        ledger = DynamoLedger(db, "Budgets", "Reservations", "Alerts")
        ledger.create_budget(team="team-a", period="2026-10", budget_micro=100)
        yield db, ledger


def test_checker_recomputes_reservations_and_emits_deduplicated_alert(aws_tables):
    db, ledger = aws_tables
    ledger.reserve(
        team="team-a",
        period="2026-10",
        reservation_id="r-1",
        idempotency_key="k-1",
        requester="u-1",
        reserved_micro=40,
        record={"estimate_micro": 40, "planned_hours": 1, "resource": {"type": "ec2"}},
    )
    db.update_item(
        TableName="Budgets",
        Key={"team": {"S": "team-a"}, "period": {"S": "2026-10"}},
        UpdateExpression="SET available_micro = :a",
        ExpressionAttributeValues={":a": {"N": "59"}},
    )
    events = FakeEvents()
    outputs = []
    checker = InvariantChecker(
        db,
        budgets_table="Budgets",
        reservations_table="Reservations",
        alerts_table="Alerts",
        eventbridge=events,
        event_bus_name="spendctl-bus",
        sleep=lambda _: None,
        emit=outputs.append,
    )
    first = checker.check()
    second = checker.check()
    assert first["discrepancy_count"] == 1
    assert second["alert_ids"] == first["alert_ids"]
    assert len(events.entries) == 2
    assert events.entries[0]["DetailType"] == "InvariantViolation"
    assert len(outputs) == 2


def test_naive_mode_refuses_without_explicit_flag(aws_tables, monkeypatch):
    db, _ = aws_tables
    monkeypatch.setenv("ALLOW_NAIVE_ADMISSION", "false")
    with pytest.raises(NaiveAdmissionDisabled):
        naive_reserve_for_experiment(
            db, budgets_table="Budgets", team="team-a", period="2026-10", reserve_micro=60
        )


def test_naive_concurrent_admissions_demonstrate_oversubscription(aws_tables):
    db, _ = aws_tables
    barrier = Barrier(2)

    def reserve():
        return naive_reserve_for_experiment(
            db,
            budgets_table="Budgets",
            team="team-a",
            period="2026-10",
            reserve_micro=60,
            after_read=barrier,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _: reserve(), range(2)))
    item = db.get_item(
        TableName="Budgets", Key={"team": {"S": "team-a"}, "period": {"S": "2026-10"}}
    )["Item"]
    assert outcomes == [True, True]
    assert int(item["reserved_micro"]["N"]) == 120
    assert int(item["available_micro"]["N"]) == -20
