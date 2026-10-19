from datetime import UTC, datetime

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws
from spendctl import workflow_handlers
from spendctl.ledger import DynamoLedger, InvalidTransition
from spendctl.lifecycle import LifecycleService, inspect_bypass_event, process_instance_state_event
from spendctl.workflow_start import WorkflowStartOutcomeUnknown

NOW = datetime(2026, 10, 20, tzinfo=UTC)
RESERVATION_ID = "rsv_lifecycle_001"
INSTANCE_ID = "i-lifecycle-001"
VOLUME_ID = "vol-lifecycle-001"


@pytest.fixture
def ledger():
    with mock_aws():
        client = boto3.client("dynamodb", region_name="ap-south-1")
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
        value = DynamoLedger(client, "Budgets", "Reservations", "Alerts")
        value.create_budget(team="alpha", period="2026-10", budget_micro=10_000_000)
        yield value


def reserve(ledger, rid=RESERVATION_ID):
    return ledger.reserve(
        team="alpha",
        period="2026-10",
        reservation_id=rid,
        idempotency_key=f"key-{rid}",
        requester="user-1",
        reserved_micro=3_000_000,
        record={
            "resource": {"type": "ec2", "instance_type": "t3.micro", "ebs_gb": 8},
            "planned_hours": 2,
            "estimate_micro": 2_700_000,
        },
    )


def activate(ledger, rid=RESERVATION_ID):
    ledger.set_workflow_start_state(rid, "STARTING", execution_name=f"wf-{rid}")
    ledger.set_workflow_start_state(
        rid,
        "STARTED",
        execution_name=f"wf-{rid}",
        execution_arn=f"arn:aws:states:ap-south-1:123:execution:sm:{rid}",
    )
    return ledger.activate(
        rid,
        launched_at="2026-10-19T00:00:00Z",
        expires_at="2026-10-19T02:00:00Z",
        instance_id=INSTANCE_ID,
        volume_ids=[VOLUME_ID],
        execution_arn=f"arn:aws:states:ap-south-1:123:execution:sm:{rid}",
    )["reservation"]


def tags(*, team="alpha", reservation_id=RESERVATION_ID):
    return [
        {"Key": "managed-by", "Value": "spend-controller"},
        {"Key": "reservation-id", "Value": reservation_id},
        {"Key": "team", "Value": team},
        {"Key": "project", "Value": "spendctl"},
        {"Key": "env", "Value": "dev"},
    ]


class FakeEC2:
    def __init__(self, state="running", *, instance_tags=None, volume_state="missing"):
        self.state = state
        self.instance_tags = instance_tags or tags()
        self.volume_state = volume_state
        self.calls = []
        self.fail_describe = None

    def describe_instances(self, **kwargs):
        self.calls.append(("describe_instances", kwargs))
        if self.fail_describe:
            raise self.fail_describe
        if "Filters" in kwargs:
            return {"Reservations": []}
        return {
            "Reservations": [
                {
                    "Instances": [
                        {
                            "InstanceId": INSTANCE_ID,
                            "State": {"Name": self.state},
                            "Tags": self.instance_tags,
                        }
                    ]
                }
            ]
        }

    def terminate_instances(self, **kwargs):
        self.calls.append(("terminate_instances", kwargs))
        self.state = "shutting-down"

    def describe_volumes(self, **kwargs):
        self.calls.append(("describe_volumes", kwargs))
        if "Filters" in kwargs:
            return {"Volumes": []}
        if self.volume_state == "missing":
            raise ClientError({"Error": {"Code": "InvalidVolume.NotFound"}}, "DescribeVolumes")
        return {
            "Volumes": [
                {
                    "VolumeId": VOLUME_ID,
                    "State": self.volume_state,
                    "Tags": tags(),
                    "Attachments": [],
                }
            ]
        }

    def delete_volume(self, **kwargs):
        self.calls.append(("delete_volume", kwargs))
        self.volume_state = "deleting"


class FakeEvents:
    def __init__(self):
        self.calls = []

    def put_events(self, **kwargs):
        self.calls.append(kwargs)
        return {"FailedEntryCount": 0}


def assert_invariant(ledger):
    budget = ledger.get_budget("alpha", "2026-10")
    assert (
        budget["available_micro"] + budget["reserved_micro"] + budget["spent_micro"]
        == budget["budget_micro"]
    )
    return budget


def test_expiry_reaper_terminates_owned_instance_then_provisionally_settles_once(ledger):
    reserve(ledger)
    activate(ledger)
    ec2 = FakeEC2()
    eventbridge = FakeEvents()
    manager = LifecycleService(
        ledger, ec2=ec2, eventbridge=eventbridge, event_bus_name="spendctl-bus", now=lambda: NOW
    )
    event = {"reservation_id": RESERVATION_ID, "reason": "expiry", "event_id": "event-1"}

    pending = manager.reap(event)
    assert pending["state"] == "PENDING"
    assert ledger.get_reservation(RESERVATION_ID)["status"] == "EXPIRED"
    assert assert_invariant(ledger)["reserved_micro"] == 3_000_000
    assert ("terminate_instances", {"InstanceIds": [INSTANCE_ID]}) in ec2.calls

    ec2.state = "terminated"
    complete = manager.reap(event)
    assert complete["state"] == "COMPLETE"
    reservation = ledger.get_reservation(RESERVATION_ID)
    assert reservation["settlement_stage"] == "PROVISIONAL"
    assert reservation["provisional_micro"] == 3_000_000
    assert reservation["workflow_cleanup_state"] == "COMPLETE"
    assert_invariant(ledger)
    assert eventbridge.calls[0]["Entries"][0]["DetailType"] == "ReservationExpired"
    assert ledger.get_reservation(RESERVATION_ID)["expiry_event_published"] is True

    duplicate = manager.reap(event)
    assert duplicate["state"] == "COMPLETE"
    assert ledger.get_budget("alpha", "2026-10")["spent_micro"] == 3_000_000
    assert len([call for call in ec2.calls if call[0] == "terminate_instances"]) == 1


def test_expiry_event_publication_is_retried_after_cleanup_settles(ledger):
    reserve(ledger)
    activate(ledger)
    ec2 = FakeEC2(state="terminated")

    class FailOnceEvents(FakeEvents):
        def put_events(self, **kwargs):
            self.calls.append(kwargs)
            if len(self.calls) == 1:
                return {"FailedEntryCount": 1}
            return {"FailedEntryCount": 0}

    eventbridge = FailOnceEvents()
    manager = LifecycleService(
        ledger, ec2=ec2, eventbridge=eventbridge, event_bus_name="spendctl-bus", now=lambda: NOW
    )
    with pytest.raises(RuntimeError, match="failed to publish"):
        manager.reap({"reservation_id": RESERVATION_ID, "reason": "expiry"})
    reservation = ledger.get_reservation(RESERVATION_ID)
    assert reservation["settlement_stage"] == "PROVISIONAL"
    assert reservation.get("expiry_event_published") is not True
    assert_invariant(ledger)

    manager.sweep(now="2026-10-20T00:00:00Z")
    reservation = ledger.get_reservation(RESERVATION_ID)
    assert reservation["expiry_event_published"] is True
    assert len(eventbridge.calls) == 2
    assert (
        eventbridge.calls[0]["Entries"][0]["Detail"] == eventbridge.calls[1]["Entries"][0]["Detail"]
    )


def test_sweeper_does_not_process_same_reservation_twice_after_expiry(ledger):
    reserve(ledger)
    activate(ledger)
    ec2 = FakeEC2(state="terminated")
    manager = LifecycleService(ledger, ec2=ec2, now=lambda: NOW)
    result = manager.sweep(now="2026-10-20T00:00:00Z")
    assert result["processed"] == 1
    assert len([call for call in ec2.calls if call[0] == "describe_instances"]) == 1


def test_sweeper_retries_settlement_after_cleanup_state_was_persisted(ledger):
    reserve(ledger)
    activate(ledger)
    ledger.mark_expired(RESERVATION_ID)
    ledger.record_lifecycle_cleanup(RESERVATION_ID, cleanup_state="COMPLETE")
    manager = LifecycleService(ledger, ec2=FakeEC2(state="terminated"), now=lambda: NOW)

    result = manager.sweep(now="2026-10-20T00:00:00Z")

    assert result["processed"] == 1
    reservation = ledger.get_reservation(RESERVATION_ID)
    assert reservation["settlement_stage"] == "PROVISIONAL"
    assert_invariant(ledger)


def test_cancel_reaper_marks_cancelled_and_retries_cleanup_without_double_settlement(ledger):
    reserve(ledger)
    activate(ledger)
    ledger.cancel_active(RESERVATION_ID)
    ec2 = FakeEC2(state="terminated")
    manager = LifecycleService(ledger, ec2=ec2, now=lambda: NOW)
    event = {"reservation_id": RESERVATION_ID, "reason": "cancel", "event_id": "cancel-event"}
    result = manager.reap(event)
    assert result["state"] == "COMPLETE"
    again = manager.reap(event)
    assert again["state"] == "COMPLETE"
    budget = assert_invariant(ledger)
    assert budget["reserved_micro"] == 0 and budget["spent_micro"] == 3_000_000


def test_cleanup_failure_keeps_hold_reserved_and_retries(ledger):
    reserve(ledger)
    activate(ledger)
    ledger.mark_expired(RESERVATION_ID)
    ec2 = FakeEC2()
    ec2.fail_describe = ClientError({"Error": {"Code": "ServiceUnavailable"}}, "DescribeInstances")
    manager = LifecycleService(ledger, ec2=ec2, now=lambda: NOW)
    with pytest.raises(ClientError):
        manager.reap({"reservation_id": RESERVATION_ID, "reason": "expiry"})
    reservation = ledger.get_reservation(RESERVATION_ID)
    assert reservation["status"] == "EXPIRED"
    assert reservation["workflow_cleanup_state"] == "UNKNOWN"
    assert assert_invariant(ledger)["reserved_micro"] == 3_000_000


def test_ownership_mismatch_never_terminates_and_creates_deduplicated_alert(ledger):
    reserve(ledger)
    activate(ledger)
    ledger.cancel_active(RESERVATION_ID)
    ec2 = FakeEC2(instance_tags=tags(team="other"))
    manager = LifecycleService(ledger, ec2=ec2, now=lambda: NOW)
    event = {"reservation_id": RESERVATION_ID, "reason": "cancel", "event_id": "cancel-2"}
    assert manager.reap(event)["state"] == "UNKNOWN"
    assert manager.reap(event)["state"] == "UNKNOWN"
    assert not any(call[0] == "terminate_instances" for call in ec2.calls)
    assert len(ledger.db.scan(TableName="Alerts")["Items"]) == 1


def test_dry_run_does_not_transition_or_terminate(ledger):
    reserve(ledger)
    activate(ledger)
    ec2 = FakeEC2()
    manager = LifecycleService(ledger, ec2=ec2, now=lambda: NOW)
    result = manager.reap({"reservation_id": RESERVATION_ID, "reason": "expiry", "dry_run": True})
    assert result["state"] == "DRY_RUN"
    assert result["plan"]["would_terminate"] is True
    assert ledger.get_reservation(RESERVATION_ID)["status"] == "ACTIVE"
    assert not any(call[0] == "terminate_instances" for call in ec2.calls)


def test_unknown_start_reconciles_same_identity_and_never_releases_when_resources_unknown(ledger):
    reserve(ledger)
    ledger.set_workflow_start_state(RESERVATION_ID, "STARTING", execution_name="same-execution")
    reservation = ledger.set_workflow_start_state(
        RESERVATION_ID, "UNKNOWN", execution_name="same-execution"
    )["reservation"]

    class UnknownStarter:
        def start(self, value):
            assert value["workflow_execution_name"] == "same-execution"
            raise WorkflowStartOutcomeUnknown(RESERVATION_ID)

    manager = LifecycleService(
        ledger, ec2=FakeEC2(), workflow_starter=UnknownStarter(), now=lambda: NOW
    )
    result = manager.reconcile_held(reservation)
    assert result["state"] == "UNKNOWN"
    assert ledger.get_reservation(RESERVATION_ID)["status"] == "HELD"
    assert assert_invariant(ledger)["reserved_micro"] == 3_000_000


def test_sweeper_releases_held_prelaunch_failure_only_with_durable_no_resource_evidence(ledger):
    reserve(ledger)
    ledger.set_workflow_start_state(RESERVATION_ID, "STARTING", execution_name="wf-prelaunch")
    ledger.set_workflow_start_state(
        RESERVATION_ID,
        "STARTED",
        execution_name="wf-prelaunch",
        execution_arn="arn:aws:states:ap-south-1:123:execution:sm:prelaunch",
    )
    ledger.record_workflow_execution(
        RESERVATION_ID,
        execution_status="FAILED",
        cleanup_state="NOT_CREATED",
        launch_state="NOT_CREATED",
    )

    class FailedExecution:
        def describe_execution(self, **_kwargs):
            return {"status": "FAILED"}

    result = LifecycleService(
        ledger, ec2=FakeEC2(), stepfunctions=FailedExecution(), now=lambda: NOW
    ).sweep(now="2026-10-20T00:00:00Z")

    assert result["processed"] == 1
    assert ledger.get_reservation(RESERVATION_ID)["status"] == "RELEASED"
    budget = assert_invariant(ledger)
    assert budget["reserved_micro"] == 0
    assert budget["available_micro"] == 10_000_000


def test_succeeded_workflow_with_held_reservation_alerts_without_releasing(ledger):
    reserve(ledger)
    ledger.set_workflow_start_state(RESERVATION_ID, "STARTING", execution_name="wf-succeeded")
    reservation = ledger.set_workflow_start_state(
        RESERVATION_ID,
        "STARTED",
        execution_name="wf-succeeded",
        execution_arn="arn:aws:states:ap-south-1:123:execution:sm:succeeded",
    )["reservation"]

    class SucceededExecution:
        def describe_execution(self, **_kwargs):
            return {"status": "SUCCEEDED"}

    result = LifecycleService(
        ledger, ec2=FakeEC2(), stepfunctions=SucceededExecution(), now=lambda: NOW
    ).reconcile_held(reservation)

    assert result["state"] == "UNKNOWN"
    assert ledger.get_reservation(RESERVATION_ID)["status"] == "HELD"
    assert assert_invariant(ledger)["reserved_micro"] == 3_000_000
    assert (
        ledger.db.scan(TableName="Alerts")["Items"][0]["alert_type"]["S"]
        == "WorkflowReconciliationUnknown"
    )


def test_sweeper_queries_only_expired_active_rows(ledger):
    reserve(ledger)
    activate(ledger)
    ec2 = FakeEC2(state="terminated")
    manager = LifecycleService(ledger, ec2=ec2, now=lambda: NOW)
    results = manager.sweep(now="2026-10-20T00:00:00Z")
    assert results["processed"] == 1
    assert ledger.get_reservation(RESERVATION_ID)["status"] == "EXPIRED"
    assert_invariant(ledger)


def test_instance_state_events_are_attributed_and_out_of_order_event_is_ignored(ledger):
    reserve(ledger)
    activate(ledger)
    event = {
        "id": "state-event-1",
        "time": "2026-10-19T01:00:00Z",
        "detail": {"instance-id": INSTANCE_ID, "state": "terminated"},
    }
    result = process_instance_state_event(event, ledger=ledger, ec2=FakeEC2(state="terminated"))
    assert result["reservation"]["instance_state"] == "terminated"
    older = {
        **event,
        "id": "state-event-0",
        "time": "2026-10-19T00:30:00Z",
        "detail": {"instance-id": INSTANCE_ID, "state": "running"},
    }
    ignored = process_instance_state_event(older, ledger=ledger, ec2=FakeEC2(state="terminated"))
    assert ignored["reservation"]["instance_state"] == "terminated"
    assert ignored["stale_event"] is True


def test_bypass_detector_alerts_unknown_actor_and_ignores_valid_controller_launch(ledger):
    reserve(ledger)
    ledger.set_workflow_start_state(RESERVATION_ID, "STARTING", execution_name="wf")
    ledger.set_workflow_start_state(
        RESERVATION_ID,
        "STARTED",
        execution_name="wf",
        execution_arn="arn:aws:states:ap-south-1:123:execution:sm:1",
    )
    event = {
        "id": "ct-1",
        "detail": {
            "eventName": "RunInstances",
            "userIdentity": {"arn": "arn:aws:iam::123:user/operator"},
            "requestParameters": {
                "tagSpecificationSet": {
                    "items": [
                        {
                            "tags": [
                                {"key": "reservation-id", "value": RESERVATION_ID},
                                {"key": "team", "value": "alpha"},
                            ]
                        }
                    ]
                }
            },
        },
    }
    assert (
        inspect_bypass_event(event, ledger=ledger, provisioner_role_arn="arn:role/provisioner")[
            "state"
        ]
        == "ALERTED"
    )
    assert len(ledger.db.scan(TableName="Alerts")["Items"]) == 1


def test_bypass_detector_requires_environment_tag_for_controller_launch(ledger):
    reserve(ledger)
    ledger.set_workflow_start_state(RESERVATION_ID, "STARTING", execution_name="wf")
    ledger.set_workflow_start_state(
        RESERVATION_ID,
        "STARTED",
        execution_name="wf",
        execution_arn="arn:aws:states:ap-south-1:123:execution:sm:1",
    )
    valid_event = {
        "id": "ct-controller",
        "detail": {
            "userIdentity": {"sessionContext": {"sessionIssuer": {"arn": "arn:role/provisioner"}}},
            "requestParameters": {
                "tagSpecificationSet": {
                    "items": [
                        {
                            "tags": [
                                {"key": "reservation-id", "value": RESERVATION_ID},
                                {"key": "team", "value": "alpha"},
                                {"key": "managed-by", "value": "spend-controller"},
                                {"key": "project", "value": "spendctl"},
                                {"key": "env", "value": "dev"},
                            ]
                        }
                    ]
                }
            },
        },
    }
    assert (
        inspect_bypass_event(
            valid_event, ledger=ledger, provisioner_role_arn="arn:role/provisioner"
        )["state"]
        == "CONTROLLER_ROLE"
    )
    invalid_event = {
        **valid_event,
        "id": "ct-controller-missing-env",
        "detail": {
            **valid_event["detail"],
            "requestParameters": {
                "tagSpecificationSet": {
                    "items": [
                        {
                            "tags": [
                                tag
                                for tag in valid_event["detail"]["requestParameters"][
                                    "tagSpecificationSet"
                                ]["items"][0]["tags"]
                                if tag["key"] != "env"
                            ]
                        }
                    ]
                }
            },
        },
    }
    assert (
        inspect_bypass_event(
            invalid_event, ledger=ledger, provisioner_role_arn="arn:role/provisioner"
        )["state"]
        == "ALERTED"
    )


def test_expiry_before_due_is_a_noop_and_held_cancel_is_rejected(ledger):
    reserve(ledger)
    active = activate(ledger)
    manager = LifecycleService(
        ledger, ec2=FakeEC2(), now=lambda: datetime(2026, 10, 19, 1, tzinfo=UTC)
    )
    assert (
        manager.reap({"reservation_id": RESERVATION_ID, "reason": "expiry"})["state"] == "NOT_DUE"
    )
    assert ledger.get_reservation(RESERVATION_ID)["status"] == "ACTIVE"
    reserve(ledger, rid="rsv_held_cancel")
    with pytest.raises(InvalidTransition):
        ledger.cancel_active("rsv_held_cancel")
    assert active["status"] == "ACTIVE"


def test_activation_expiry_schedule_uses_stable_name_and_is_idempotent(monkeypatch):
    calls = []
    schedule = {}

    class Scheduler:
        def create_schedule(self, **request):
            calls.append(request)
            if request["Name"] in schedule:
                raise ClientError({"Error": {"Code": "ConflictException"}}, "CreateSchedule")
            schedule[request["Name"]] = request

        def get_schedule(self, *, Name, GroupName):
            assert GroupName == "spendctl-expiry"
            return schedule[Name]

    client = Scheduler()
    monkeypatch.setattr(workflow_handlers.boto3, "client", lambda *_args, **_kwargs: client)
    monkeypatch.setenv("EXPIRY_SCHEDULE_GROUP", "spendctl-expiry")
    monkeypatch.setenv("LIFECYCLE_REAPER_ARN", "arn:aws:lambda:ap-south-1:123:function:reaper")
    monkeypatch.setenv("SCHEDULER_TARGET_ROLE_ARN", "arn:aws:iam::123:role/scheduler")
    reservation = {
        "reservation_id": RESERVATION_ID,
        "team": "alpha",
        "expires_at": "2026-10-21T00:00:00Z",
    }

    workflow_handlers._ensure_expiry_schedule(reservation)
    workflow_handlers._ensure_expiry_schedule(reservation)

    assert len(calls) == 2
    assert calls[0] == calls[1]
    assert calls[0]["ScheduleExpression"] == "at(2026-10-21T00:00:00)"
    assert calls[0]["ActionAfterCompletion"] == "DELETE"
    assert calls[0]["Target"]["Input"] == schedule[calls[0]["Name"]]["Target"]["Input"]
