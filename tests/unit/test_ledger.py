from concurrent.futures import ThreadPoolExecutor

import boto3
import pytest
from moto import mock_aws
from spendctl.ledger import (
    AmbiguousLedgerOutcome,
    BudgetAlreadyExists,
    DuplicateIdempotencyKey,
    DynamoLedger,
    InsufficientBudget,
    InvalidTransition,
    ReservationConflict,
)
from spendctl.workflow_handlers import _confirmed_failure_actual


@pytest.fixture
def ledger():
    with mock_aws():
        client = boto3.client("dynamodb", region_name="us-east-1")
        client.create_table(
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
        client.create_table(
            TableName="Reservations",
            KeySchema=[
                {"AttributeName": "pk", "KeyType": "HASH"},
                {"AttributeName": "sk", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "pk", "AttributeType": "S"},
                {"AttributeName": "sk", "AttributeType": "S"},
                {"AttributeName": "team", "AttributeType": "S"},
                {"AttributeName": "created_at", "AttributeType": "S"},
                {"AttributeName": "status", "AttributeType": "S"},
                {"AttributeName": "expires_at", "AttributeType": "S"},
            ],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": "team-created-index",
                    "KeySchema": [
                        {"AttributeName": "team", "KeyType": "HASH"},
                        {"AttributeName": "created_at", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                },
                {
                    "IndexName": "status-expiry-index",
                    "KeySchema": [
                        {"AttributeName": "status", "KeyType": "HASH"},
                        {"AttributeName": "expires_at", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                },
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        client.create_table(
            TableName="Alerts",
            KeySchema=[{"AttributeName": "alert_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "alert_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        instance = DynamoLedger(client, "Budgets", "Reservations", "Alerts")
        instance.create_budget(team="alpha", period="2026-10", budget_micro=10_000_000)
        yield instance


def request_record():
    return {
        "resource": {"type": "ec2", "instance_type": "t3.micro", "ebs_gb": 8},
        "planned_hours": 4,
        "estimate_micro": 1_000_000,
        "margin": "1.10",
        "price_snapshot_id": "fixture-1",
    }


def reserve(ledger, reservation_id="r-1", key="key-1", amount=3_000_000):
    return ledger.reserve(
        team="alpha",
        period="2026-10",
        reservation_id=reservation_id,
        idempotency_key=key,
        requester="user-1",
        reserved_micro=amount,
        record=request_record(),
    )


def assert_invariant(ledger):
    budget = ledger.get_budget("alpha", "2026-10")
    assert (
        budget["available_micro"] + budget["reserved_micro"] + budget["spent_micro"]
        == budget["budget_micro"]
    )
    return budget


def test_budget_create_and_reserve_replay_are_idempotent(ledger):
    first = reserve(ledger)
    replay = reserve(ledger, reservation_id="retry-id", key="key-1")
    assert first["reservation"]["reservation_id"] == replay["reservation"]["reservation_id"]
    assert replay["replayed"] is True
    assert assert_invariant(ledger)["reserved_micro"] == 3_000_000
    with pytest.raises(BudgetAlreadyExists):
        ledger.create_budget(team="alpha", period="2026-10", budget_micro=10_000_000)


def test_conflicting_idempotency_reuse_and_reservation_id_conflict(ledger):
    reserve(ledger)
    changed = request_record()
    changed["planned_hours"] = 5
    with pytest.raises(DuplicateIdempotencyKey):
        ledger.reserve(
            team="alpha",
            period="2026-10",
            reservation_id="r-other",
            idempotency_key="key-1",
            requester="user-1",
            reserved_micro=3_000_000,
            record=changed,
        )
    with pytest.raises(ReservationConflict):
        ledger.reserve(
            team="alpha",
            period="2026-10",
            reservation_id="r-1",
            idempotency_key="key-2",
            requester="user-1",
            reserved_micro=3_000_000,
            record=request_record(),
        )
    assert_invariant(ledger)


def test_idempotency_key_partition_has_unambiguous_team_and_key_boundaries(ledger):
    ledger.create_budget(team="x#y", period="2026-10", budget_micro=2_000_000)
    ledger.create_budget(team="x", period="2026-10", budget_micro=2_000_000)
    left = ledger.reserve(
        team="x#y",
        period="2026-10",
        reservation_id="r-left",
        idempotency_key="z",
        requester="user-1",
        reserved_micro=1_000_000,
        record=request_record(),
    )
    right = ledger.reserve(
        team="x",
        period="2026-10",
        reservation_id="r-right",
        idempotency_key="y#z",
        requester="user-1",
        reserved_micro=1_000_000,
        record=request_record(),
    )
    assert left["reservation"]["reservation_id"] == "r-left"
    assert right["reservation"]["reservation_id"] == "r-right"


def test_insufficient_budget_does_not_change_balances(ledger):
    with pytest.raises(InsufficientBudget):
        reserve(ledger, amount=10_000_001)
    budget = assert_invariant(ledger)
    assert budget["available_micro"] == 10_000_000
    assert budget["reserved_micro"] == 0


def test_concurrent_reservations_never_overdraw_budget(ledger):
    def submit(index):
        try:
            return reserve(
                ledger,
                reservation_id=f"r-{index}",
                key=f"key-{index}",
                amount=3_000_000,
            )["reservation"]["reservation_id"]
        except InsufficientBudget:
            return None

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(submit, range(8)))
    assert sum(result is not None for result in results) == 3
    budget = assert_invariant(ledger)
    assert budget["reserved_micro"] == 9_000_000
    assert budget["available_micro"] == 1_000_000


def test_release_replay_and_workflow_unknown_cannot_release(ledger):
    reserve(ledger)
    ledger.set_workflow_start_state("r-1", "STARTING")
    ledger.set_workflow_start_state("r-1", "UNKNOWN")
    with pytest.raises(InvalidTransition):
        ledger.release("r-1")
    assert ledger.get_reservation("r-1")["status"] == "HELD"
    ledger.set_workflow_start_state("r-1", "FAILED")
    first = ledger.release("r-1")
    replay = ledger.release("r-1")
    assert first["reservation"]["status"] == "RELEASED"
    assert replay["replayed"] is True
    budget = assert_invariant(ledger)
    assert budget["available_micro"] == budget["budget_micro"]
    assert budget["reserved_micro"] == 0


def test_prelaunch_workflow_failure_release_requires_durable_no_resource_evidence(ledger):
    reserve(ledger)
    ledger.set_workflow_start_state("r-1", "STARTING", execution_name="spendctl-r-1-deterministic")
    ledger.set_workflow_start_state(
        "r-1",
        "STARTED",
        execution_name="spendctl-r-1-deterministic",
        execution_arn="arn:execution:r-1",
        execution_status="RUNNING",
    )
    with pytest.raises(InvalidTransition, match="confirmed failure"):
        ledger.release_prelaunch_failure("r-1")
    ledger.record_workflow_execution(
        "r-1",
        execution_status="FAILED",
        cleanup_state="NOT_CREATED",
        launch_state="NOT_CREATED",
    )
    result = ledger.release_prelaunch_failure("r-1")
    assert result["reservation"]["status"] == "RELEASED"
    assert ledger.release_prelaunch_failure("r-1")["replayed"]
    budget = assert_invariant(ledger)
    assert budget["available_micro"] == budget["budget_micro"]
    assert budget["reserved_micro"] == 0


def _failed_cleanup(ledger, rid, *, cleanup="COMPLETE", resources=True, launch_state=None):
    reserve(ledger, reservation_id=rid, key=f"key-{rid}")
    ledger.set_workflow_start_state(rid, "STARTING", execution_name=f"wf-{rid}")
    ledger.set_workflow_start_state(
        rid,
        "STARTED",
        execution_name=f"wf-{rid}",
        execution_arn=f"arn:execution:{rid}",
        execution_status="RUNNING",
    )
    ledger.record_workflow_execution(
        rid,
        execution_status="FAILED",
        cleanup_state=cleanup,
        launch_state=launch_state or ("CREATED" if resources else "NOT_CREATED"),
        instance_id="i-test" if resources else None,
        launched_at="2026-10-19T10:00:00Z" if resources else None,
        terminated_at="2026-10-19T10:00:30Z" if cleanup == "COMPLETE" else None,
    )


def test_confirmed_cleanup_settlement_restores_unused_budget_and_is_idempotent(ledger):
    _failed_cleanup(ledger, "r-cleaned")
    first = ledger.release_after_confirmed_cleanup(
        "r-cleaned", 500_000, settlement_id="r-cleaned:cleanup-failure:v1"
    )
    replay = ledger.release_after_confirmed_cleanup(
        "r-cleaned", 500_000, settlement_id="r-cleaned:cleanup-failure:v1"
    )
    assert first["reservation"]["status"] == "RELEASED"
    assert replay["replayed"]
    budget = assert_invariant(ledger)
    assert budget["reserved_micro"] == 0
    assert budget["spent_micro"] == 500_000
    assert budget["available_micro"] == 9_500_000


def test_confirmed_cleanup_without_any_instance_restores_full_reserve(ledger):
    _failed_cleanup(ledger, "r-no-instance", resources=False)
    result = ledger.release_after_confirmed_cleanup(
        "r-no-instance", 0, settlement_id="r-no-instance:cleanup-failure:v1"
    )
    assert result["reservation"]["final_actual_micro"] == 0
    budget = assert_invariant(ledger)
    assert budget["available_micro"] == 10_000_000
    assert budget["spent_micro"] == 0


@pytest.mark.parametrize("cleanup", ["PENDING", "UNKNOWN"])
def test_partial_or_unknown_cleanup_never_refunds(ledger, cleanup):
    _failed_cleanup(ledger, f"r-{cleanup.lower()}", cleanup=cleanup)
    with pytest.raises(InvalidTransition, match="COMPLETE cleanup"):
        ledger.release_after_confirmed_cleanup(f"r-{cleanup.lower()}", 0)
    budget = assert_invariant(ledger)
    assert budget["reserved_micro"] == 3_000_000
    assert budget["available_micro"] == 7_000_000


def test_ambiguous_launch_with_complete_tagged_cleanup_still_cannot_refund(ledger):
    _failed_cleanup(ledger, "r-unknown-launch", resources=False, launch_state="UNKNOWN")
    with pytest.raises(InvalidTransition, match="confirmed launch outcome"):
        ledger.release_after_confirmed_cleanup("r-unknown-launch", 0)
    budget = assert_invariant(ledger)
    assert budget["reserved_micro"] == 3_000_000
    assert budget["available_micro"] == 7_000_000


def test_confirmed_failure_runtime_cost_uses_snapshot_rates_and_minimum_runtime():
    reservation = {
        "launched_at": "2026-10-19T10:00:00Z",
        "estimate_breakdown": {
            "compute_usd_per_hour": "0.012",
            "gp3_usd_per_gb_month": "0.08",
            "hours_per_month": 730,
        },
        "resource": {"ebs_gb": 8},
    }
    assert _confirmed_failure_actual(reservation, True, "2026-10-19T10:00:01Z") == 215
    assert _confirmed_failure_actual(reservation, False, "2026-10-19T10:00:01Z") == 0


def test_activate_cancel_provisional_final_and_correction(ledger):
    reserve(ledger)
    active = ledger.activate("r-1", launched_at="2026-10-19T10:00:00Z")
    assert active["reservation"]["status"] == "ACTIVE"
    assert active["reservation"]["expires_at"] == "2026-10-19T14:00:00Z"
    ledger.cancel_active("r-1")
    provisional = ledger.provisional_settle("r-1", 1_000_000, settlement_id="p-1")
    assert provisional["reservation"]["settlement_stage"] == "PROVISIONAL"
    assert ledger.provisional_settle("r-1", 1_000_000, settlement_id="p-1")["replayed"]
    assert assert_invariant(ledger)["spent_micro"] == 1_000_000
    with pytest.raises(InvalidTransition):
        ledger.close_budget(team="alpha", period="2026-10")
    final = ledger.final_settle("r-1", 1_200_000, settlement_id="f-1")
    assert final["reservation"]["status"] == "SETTLED"
    assert ledger.final_settle("r-1", 1_200_000, settlement_id="f-1")["replayed"]
    assert assert_invariant(ledger)["spent_micro"] == 1_200_000
    correction = ledger.final_settle("r-1", 900_000, settlement_id="f-2", settlement_version=2)
    assert correction["reservation"]["final_actual_micro"] == 900_000
    assert assert_invariant(ledger)["spent_micro"] == 900_000
    assert ledger.get_budget("alpha", "2026-10")["open_reservations"] == 0
    assert ledger.close_budget(team="alpha", period="2026-10")["status"] == "CLOSED"
    assert ledger.final_settle("r-1", 900_000, settlement_id="f-2", settlement_version=2)[
        "replayed"
    ]


def test_overrun_is_visible_and_alerted(ledger):
    reserve(ledger)
    ledger.activate("r-1")
    ledger.mark_expired("r-1")
    ledger.provisional_settle("r-1", 3_000_000)
    ledger.final_settle("r-1", 11_000_000)
    budget = assert_invariant(ledger)
    assert budget["available_micro"] == -1_000_000
    alerts = ledger.db.scan(TableName="Alerts")["Items"]
    assert len(alerts) == 1


def test_state_transitions_and_team_index_query(ledger):
    reserve(ledger)
    with pytest.raises(InvalidTransition):
        ledger.mark_expired("r-1")
    page = ledger.list_reservations(team="alpha")
    assert [item["reservation_id"] for item in page["items"]] == ["r-1"]
    by_status = ledger.list_reservations(status="HELD")
    assert [item["reservation_id"] for item in by_status["items"]] == ["r-1"]
    with pytest.raises(InvalidTransition):
        ledger.close_budget(team="alpha", period="2026-10")


def test_ambiguous_reserve_stays_unknown_when_reconciliation_fails(ledger):
    underlying = ledger.db

    class UnavailableDynamo:
        def transact_write_items(self, **kwargs):
            raise TimeoutError("transaction response timed out")

        def get_item(self, **kwargs):
            if kwargs["TableName"] == "Budgets":
                return underlying.get_item(**kwargs)
            raise TimeoutError("reconciliation timed out")

    ledger.db = UnavailableDynamo()
    with pytest.raises(AmbiguousLedgerOutcome):
        reserve(ledger)
    ledger.db = underlying
    assert ledger.get_budget("alpha", "2026-10")["available_micro"] == 10_000_000
    # The same key can safely be retried after the service becomes reachable.
    assert reserve(ledger)["reservation"]["status"] == "HELD"


def test_lost_success_response_is_resolved_from_durable_idempotency(ledger):
    underlying = ledger.db

    class CommitThenTimeout:
        def transact_write_items(self, **kwargs):
            underlying.transact_write_items(**kwargs)
            raise TimeoutError("response lost after commit")

        def __getattr__(self, name):
            return getattr(underlying, name)

    ledger.db = CommitThenTimeout()
    result = reserve(ledger)
    assert result["reservation"]["reservation_id"] == "r-1"
    assert result["replayed"] is True
    ledger.db = underlying
    assert assert_invariant(ledger)["reserved_micro"] == 3_000_000
