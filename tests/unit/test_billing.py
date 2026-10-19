from concurrent.futures import ThreadPoolExecutor

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws
from spendctl import billing_handlers
from spendctl.billing import (
    BillingLedgerConflict,
    BillingReconciler,
    IncompleteBillingSnapshot,
    InvalidBillingRecord,
    normalize_record,
    validate_manifest,
)
from spendctl.ledger import DynamoLedger


@pytest.fixture
def ledger():
    with mock_aws():
        db = boto3.client("dynamodb", region_name="us-east-1")
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
        obj = DynamoLedger(db, "Budgets", "Reservations", "Alerts")
        obj.create_budget(team="alpha", period="2026-07", budget_micro=20_000_000)
        obj.reserve(
            team="alpha",
            period="2026-07",
            reservation_id="r-1",
            idempotency_key="key-1",
            requester="user-1",
            reserved_micro=5_000_000,
            record={
                "resource": {"type": "ec2", "instance_type": "t3.micro", "ebs_gb": 8},
                "planned_hours": 3,
            },
        )["reservation"]
        obj.set_workflow_start_state("r-1", "STARTING", execution_name="start-r-1")
        obj.record_workflow_execution(
            "r-1",
            execution_status="SUCCEEDED",
            instance_id="i-123",
            volume_ids=["vol-123"],
            launch_state="CREATED",
            launched_at="2026-07-01T00:00:00Z",
        )
        obj.activate(
            "r-1", launched_at="2026-07-01T00:00:00Z", instance_id="i-123", volume_ids=["vol-123"]
        )
        obj.mark_expired("r-1", expired_at="2026-07-02T00:00:00Z")
        obj.provisional_settle("r-1", 2_000_000)
        yield obj


def row(**changes):
    value = {
        "identity_line_item_id": "line-1",
        "bill_billing_period_start_date": "2026-07-01 00:00:00",
        "line_item_usage_start_date": "2026-07-01 00:00:00",
        "line_item_usage_end_date": "2026-07-01 01:00:00",
        "line_item_usage_account_id": "123456789012",
        "product_product_code": "AmazonEC2",
        "line_item_usage_type": "BoxUsage:t3.micro",
        "line_item_operation": "RunInstances",
        "line_item_resource_id": "i-123",
        "line_item_line_item_type": "Usage",
        "line_item_currency_code": "USD",
        "line_item_unblended_cost": "0.0000015",
        "resource_tags_user_reservation_id": None,
        "resource_tags_user_team": "alpha",
    }
    value.update(changes)
    return value


def test_normalize_uses_decimal_half_even_and_adjustments_are_classified():
    record = normalize_record(row(), object_id="export/manifest#rev-1")
    assert record.amount_micro == 2  # 1.5 micro-USD rounds half-even to 2.
    credit = normalize_record(
        row(
            identity_line_item_id="line-credit",
            line_item_unblended_cost="-0.05",
            line_item_line_item_type="Credit",
        ),
        object_id="manifest",
    )
    assert credit.amount_micro == -50_000
    assert credit.classification == "ADJUSTMENT_EXCLUDED"


def test_schema_validation_and_manifest_fail_closed():
    with pytest.raises(InvalidBillingRecord, match="identity_line_item_id"):
        normalize_record(row(identity_line_item_id=None), object_id="manifest")
    assert validate_manifest(
        {
            "assemblyId": "rev-1",
            "createTimestamp": "2026-08-01T00:00:00Z",
            "billingPeriod": "202607",
            "dataFilePaths": ["part.parquet"],
        }
    ) == ("2026-07", "rev-1", "2026-08-01T00:00:00.000000Z")
    with pytest.raises(InvalidBillingRecord):
        validate_manifest({"billingPeriod": "202607", "dataFilePaths": []})


def test_reconcile_attribution_revision_and_duplicate_are_atomic(ledger):
    reconciler = BillingReconciler(ledger)
    records = [normalize_record(row(line_item_unblended_cost="1.25"), object_id="rev-1")]
    first = reconciler.reconcile(
        records,
        billing_period="2026-07",
        source_revision="rev-1",
        source_revision_order="2026-08-01T00:00:00Z",
        complete=True,
    )
    assert first.adjustments[0]["adjustment_micro"] == -750_000
    replay = reconciler.reconcile(
        records,
        billing_period="2026-07",
        source_revision="rev-1",
        source_revision_order="2026-08-01T00:00:00Z",
        complete=True,
    )
    assert replay.adjustments[0]["replayed"] is True
    revised = [normalize_record(row(line_item_unblended_cost="3.00"), object_id="rev-2")]
    second = reconciler.reconcile(
        revised,
        billing_period="2026-07",
        source_revision="rev-2",
        source_revision_order="2026-08-02T00:00:00Z",
        complete=True,
    )
    assert second.adjustments[0]["previous_actual_micro"] == 1_250_000
    assert second.adjustments[0]["adjustment_micro"] == 1_750_000
    current = ledger.get_reservation("r-1")
    assert current["final_actual_micro"] == 3_000_000
    budget = ledger.get_budget("alpha", "2026-07")
    assert (
        budget["available_micro"] + budget["reserved_micro"] + budget["spent_micro"]
        == budget["budget_micro"]
    )
    assert budget["spent_micro"] == 3_000_000
    assert (
        reconciler.reconcile(
            records,
            billing_period="2026-07",
            source_revision="rev-1",
            source_revision_order="2026-08-01T00:00:00Z",
            complete=True,
        ).adjustments[0]["replayed"]
        is True
    )
    assert ledger.get_reservation("r-1")["final_actual_micro"] == 3_000_000
    with pytest.raises(BillingLedgerConflict, match="r-1"):
        reconciler.reconcile(
            [normalize_record(row(line_item_unblended_cost="1.00"), object_id="late-old")],
            billing_period="2026-07",
            source_revision="late-old",
            source_revision_order="2026-07-31T23:59:00Z",
            complete=True,
        )


def test_exact_resource_mapping_team_conflict_and_unknown_stay_unattributed(ledger):
    reconciler = BillingReconciler(ledger)
    found = reconciler.reconcile(
        [normalize_record(row(line_item_unblended_cost="1.50"), object_id="rev")],
        billing_period="2026-07",
        source_revision="rev",
        source_revision_order="2026-08-01T00:00:00Z",
        complete=True,
    )
    assert found.processed == 1
    wrong_team = normalize_record(
        row(
            identity_line_item_id="line-2",
            resource_tags_user_reservation_id="r-1",
            resource_tags_user_team="beta",
        ),
        object_id="rev",
    )
    unknown = normalize_record(
        row(
            identity_line_item_id="line-3",
            line_item_resource_id="i-unknown",
            resource_tags_user_reservation_id=None,
        ),
        object_id="rev",
    )
    result = reconciler.reconcile(
        [wrong_team, unknown],
        billing_period="2026-07",
        source_revision="rev-2",
        source_revision_order="2026-08-02T00:00:00Z",
        complete=True,
    )
    assert result.unattributed == 2
    assert ledger.get_reservation("r-1")["final_actual_micro"] == 1_500_000


def test_exact_reservation_tag_is_attributed_and_unknown_tag_is_not(ledger):
    reconciler = BillingReconciler(ledger)
    tagged = normalize_record(
        row(
            identity_line_item_id="tagged",
            line_item_resource_id=None,
            resource_tags_user_reservation_id="r-1",
        ),
        object_id="manifest-tag",
    )
    result = reconciler.reconcile(
        [tagged],
        billing_period="2026-07",
        source_revision="rev-tag",
        source_revision_order="2026-08-01T00:00:00Z",
        complete=True,
    )
    assert result.processed == 1
    unknown_tag = normalize_record(
        row(
            identity_line_item_id="bad-tag",
            line_item_resource_id=None,
            resource_tags_user_reservation_id="missing-reservation",
        ),
        object_id="manifest-tag",
    )
    result = reconciler.reconcile(
        [unknown_tag],
        billing_period="2026-07",
        source_revision="rev-tag-2",
        source_revision_order="2026-08-02T00:00:00Z",
        complete=True,
    )
    assert result.unattributed == 1


def test_cross_period_runtime_remains_unattributed_without_partial_refund(ledger):
    ledger.db.update_item(
        TableName="Reservations",
        Key={"pk": {"S": "RES#r-1"}, "sk": {"S": "META"}},
        UpdateExpression="SET launched_at = :start, expires_at = :end",
        ExpressionAttributeValues={
            ":start": {"S": "2026-07-31T23:30:00Z"},
            ":end": {"S": "2026-08-01T02:30:00Z"},
        },
    )
    record = normalize_record(row(line_item_unblended_cost="1.00"), object_id="cross-period")
    result = BillingReconciler(ledger).reconcile(
        [record],
        billing_period="2026-07",
        source_revision="cross-period",
        source_revision_order="2026-08-01T00:00:00Z",
        complete=True,
    )
    assert result.unattributed == 1
    assert "spans billing periods" in result.reasons[0]["reason"]
    reservation = ledger.get_reservation("r-1")
    assert reservation["settlement_stage"] == "PROVISIONAL"
    budget = ledger.get_budget("alpha", "2026-07")
    assert (
        budget["available_micro"] + budget["reserved_micro"] + budget["spent_micro"]
        == budget["budget_micro"]
    )


def test_explicit_attributed_zero_usage_is_valid_but_absence_is_not(ledger):
    record = normalize_record(row(line_item_unblended_cost="0.00"), object_id="zero-row")
    result = BillingReconciler(ledger).reconcile(
        [record],
        billing_period="2026-07",
        source_revision="zero-row-rev",
        source_revision_order="2026-08-01T00:00:00Z",
        complete=True,
    )
    assert result.processed == 1
    reservation = ledger.get_reservation("r-1")
    assert reservation["final_actual_micro"] == 0
    budget = ledger.get_budget("alpha", "2026-07")
    assert budget["spent_micro"] == 0
    assert budget["available_micro"] == budget["budget_micro"]
    assert (
        budget["available_micro"] + budget["reserved_micro"] + budget["spent_micro"]
        == budget["budget_micro"]
    )


def test_incomplete_snapshot_never_changes_ledger(ledger):
    with pytest.raises(IncompleteBillingSnapshot):
        BillingReconciler(ledger).reconcile(
            [],
            billing_period="2026-07",
            source_revision="rev",
            source_revision_order="2026-08-01T00:00:00Z",
            complete=False,
        )
    assert ledger.get_reservation("r-1")["settlement_stage"] == "PROVISIONAL"
    no_rows = BillingReconciler(ledger).reconcile(
        [],
        billing_period="2026-07",
        source_revision="empty-but-complete",
        source_revision_order="2026-08-01T00:00:00Z",
        complete=True,
    )
    assert no_rows.processed == 0
    assert ledger.get_reservation("r-1")["settlement_stage"] == "PROVISIONAL"


def test_final_correction_on_closed_budget_preserves_terminal_status_and_invariant(ledger):
    reconciler = BillingReconciler(ledger)
    first = normalize_record(row(line_item_unblended_cost="1.00"), object_id="close-1")
    reconciler.reconcile(
        [first],
        billing_period="2026-07",
        source_revision="close-1",
        source_revision_order="2026-08-01T00:00:00Z",
        complete=True,
    )
    ledger.close_budget(team="alpha", period="2026-07")
    revised = normalize_record(row(line_item_unblended_cost="1.25"), object_id="close-2")
    reconciler.reconcile(
        [revised],
        billing_period="2026-07",
        source_revision="close-2",
        source_revision_order="2026-08-02T00:00:00Z",
        complete=True,
    )
    budget = ledger.get_budget("alpha", "2026-07")
    assert budget["status"] == "CLOSED"
    assert budget["spent_micro"] == 1_250_000
    assert (
        budget["available_micro"] + budget["reserved_micro"] + budget["spent_micro"]
        == budget["budget_micro"]
    )


def test_m6_cleanup_release_can_receive_late_billing_correction_on_closed_budget(ledger):
    # Settle the fixture reservation first so the period can be safely closed.
    BillingReconciler(ledger).reconcile(
        [normalize_record(row(line_item_unblended_cost="1.00"), object_id="r1")],
        billing_period="2026-07",
        source_revision="r1",
        source_revision_order="2026-08-01T00:00:00Z",
        complete=True,
    )
    ledger.reserve(
        team="alpha",
        period="2026-07",
        reservation_id="r-2",
        idempotency_key="key-2",
        requester="user-1",
        reserved_micro=1_000_000,
        record={
            "resource": {"type": "ec2", "instance_type": "t3.micro", "ebs_gb": 8},
            "planned_hours": 2,
        },
    )
    ledger.set_workflow_start_state("r-2", "STARTING", execution_name="start-r-2")
    ledger.set_workflow_start_state("r-2", "STARTED")
    ledger.record_workflow_execution(
        "r-2",
        execution_status="FAILED",
        cleanup_state="COMPLETE",
        instance_id="i-2",
        volume_ids=["vol-2"],
        launch_state="CREATED",
        launched_at="2026-07-03T00:00:00Z",
        terminated_at="2026-07-03T01:00:00Z",
    )
    ledger.release_after_confirmed_cleanup("r-2", 200_000)
    ledger.close_budget(team="alpha", period="2026-07")
    record = normalize_record(
        row(
            identity_line_item_id="r2-line",
            line_item_resource_id="i-2",
            line_item_unblended_cost="0.30",
        ),
        object_id="r2-billing",
    )
    BillingReconciler(ledger).reconcile(
        [record],
        billing_period="2026-07",
        source_revision="r2-rev",
        source_revision_order="2026-08-02T00:00:00Z",
        complete=True,
    )
    reservation = ledger.get_reservation("r-2")
    assert reservation["status"] == "RELEASED"
    assert reservation["final_actual_micro"] == 300_000
    budget = ledger.get_budget("alpha", "2026-07")
    assert budget["status"] == "CLOSED"
    assert budget["spent_micro"] == 1_300_000
    assert (
        budget["available_micro"] + budget["reserved_micro"] + budget["spent_micro"]
        == budget["budget_micro"]
    )


def test_failed_financial_transaction_leaves_budget_and_audit_unchanged(ledger, monkeypatch):
    before = ledger.get_budget("alpha", "2026-07")

    def fail_transaction(**_kwargs):
        raise ClientError(
            {"Error": {"Code": "TransactionCanceledException", "Message": "injected failure"}},
            "TransactWriteItems",
        )

    monkeypatch.setattr(ledger.db, "transact_write_items", fail_transaction)
    record = normalize_record(row(line_item_unblended_cost="1.00"), object_id="failure")
    with pytest.raises(BillingLedgerConflict):
        BillingReconciler(ledger).reconcile(
            [record],
            billing_period="2026-07",
            source_revision="failure-rev",
            source_revision_order="2026-08-01T00:00:00Z",
            complete=True,
        )
    after = ledger.get_budget("alpha", "2026-07")
    assert after["available_micro"] == before["available_micro"]
    assert after["reserved_micro"] == before["reserved_micro"]
    assert after["spent_micro"] == before["spent_micro"]
    assert (
        after["available_micro"] + after["reserved_micro"] + after["spent_micro"]
        == after["budget_micro"]
    )
    assert ledger.get_reservation("r-1")["settlement_stage"] == "PROVISIONAL"


def test_concurrent_same_snapshot_is_applied_once(ledger):
    records = [normalize_record(row(line_item_unblended_cost="1.00"), object_id="same")]

    def reconcile():
        return BillingReconciler(ledger).reconcile(
            records,
            billing_period="2026-07",
            source_revision="same-rev",
            source_revision_order="2026-08-01T00:00:00Z",
            complete=True,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: reconcile(), range(2)))
    assert sum(result.adjustments[0]["replayed"] is False for result in results) == 1
    assert ledger.get_reservation("r-1")["final_actual_micro"] == 1_000_000
    budget = ledger.get_budget("alpha", "2026-07")
    assert budget["spent_micro"] == 1_000_000
    assert (
        budget["available_micro"] + budget["reserved_micro"] + budget["spent_micro"]
        == budget["budget_micro"]
    )


class _Paginator:
    def __init__(self, result):
        self.result = result

    def paginate(self, **_kwargs):
        return iter([self.result])


class _Athena:
    def __init__(self, status="SUCCEEDED", columns=None):
        self.status = status
        self.columns = columns or [
            *billing_handlers._REQUIRED_CUR_COLUMNS,
            "line_item_unblended_cost",
        ]
        self.sql = None

    def start_query_execution(self, **kwargs):
        self.sql = kwargs["QueryString"]
        return {"QueryExecutionId": "query-1"}

    def get_query_execution(self, **_kwargs):
        return {
            "QueryExecution": {
                "Status": {"State": self.status, "StateChangeReason": "test failure"}
            }
        }

    def get_paginator(self, _name):
        columns = [{"Name": name} for name in self.columns]
        data = [{"VarCharValue": name} for name in self.columns]
        return _Paginator(
            {"ResultSet": {"ResultSetMetadata": {"ColumnInfo": columns}, "Rows": [{"Data": data}]}}
        )


class _Glue:
    def __init__(self, columns, partitions=None):
        self.columns = columns
        self.partitions = partitions or []

    def get_table(self, **_kwargs):
        return {
            "Table": {
                "StorageDescriptor": {"Columns": [{"Name": name} for name in self.columns]},
                "PartitionKeys": [{"Name": name} for name in self.partitions],
            }
        }


def test_athena_query_is_period_filtered_and_uses_actual_glue_columns(monkeypatch):
    athena = _Athena()
    glue_columns = [*billing_handlers._REQUIRED_CUR_COLUMNS, "line_item_unblended_cost"]
    glue = _Glue(glue_columns, partitions=["bill_billing_period"])
    monkeypatch.setenv("CUR_TABLE", "fixture_table")
    monkeypatch.setenv("ATHENA_WORKGROUP", "test-workgroup")
    monkeypatch.setattr(billing_handlers, "_emit_event", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        billing_handlers.boto3, "client", lambda name, **_kwargs: glue if name == "glue" else athena
    )
    assert billing_handlers._query_rows(athena, "2026-07") == []
    assert '"spendctl_billing"."fixture_table"' in athena.sql
    assert "2026-07" in athena.sql
    assert "\"bill_billing_period\" = '2026-07'" in athena.sql


def test_athena_schema_mismatch_and_query_failure_are_visible(monkeypatch):
    monkeypatch.setenv("CUR_TABLE", "fixture_table")
    monkeypatch.setenv("ATHENA_WORKGROUP", "test-workgroup")
    monkeypatch.setattr(billing_handlers, "_emit_event", lambda *_args, **_kwargs: None)
    athena = _Athena()
    monkeypatch.setattr(
        billing_handlers.boto3,
        "client",
        lambda name, **_kwargs: _Glue(["wrong_column"]) if name == "glue" else athena,
    )
    with pytest.raises(InvalidBillingRecord, match="missing required columns"):
        billing_handlers._query_rows(athena, "2026-07")
    monkeypatch.setattr(
        billing_handlers.boto3,
        "client",
        lambda name, **_kwargs: (
            _Glue([*billing_handlers._REQUIRED_CUR_COLUMNS, "line_item_unblended_cost"])
            if name == "glue"
            else _Athena("FAILED")
        ),
    )
    with pytest.raises(RuntimeError, match="Athena query FAILED"):
        billing_handlers._query_rows(_Athena("FAILED"), "2026-07")
