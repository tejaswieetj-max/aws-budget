import json
from datetime import UTC, datetime

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws
from spendctl.admission import AdmissionService, VerifiedIdentity
from spendctl.api import ApiServices, route_request
from spendctl.ledger import AmbiguousLedgerOutcome, DynamoLedger
from spendctl.pricing import FixturePricingProvider, PriceNotFound
from spendctl.workflow_start import WorkflowStarter

PERIOD = datetime.now(UTC).strftime("%Y-%m")
REQUEST = {
    "team": "alpha",
    "resource": {"type": "ec2", "instance_type": "t3.micro", "ebs_gb": 8},
    "duration_hours": 2,
    "purpose": "api test",
}


def event(*, body=None, claims=None, path=None, query=None, headers=None):
    return {
        "body": json.dumps(body) if body is not None else None,
        "headers": headers or {},
        "pathParameters": path or {},
        "queryStringParameters": query or {},
        "rawPath": "/v1/test",
        "version": "2.0",
        "requestContext": {
            "http": {"method": "GET", "path": "/v1/test"},
            "authorizer": {"jwt": {"claims": claims}},
        },
    }


def claims(team="alpha", groups=None, subject="user-1"):
    result = {"sub": subject, "custom:team": team}
    if groups is not None:
        result["cognito:groups"] = groups
    return result


def unpack(response):
    return response["statusCode"], json.loads(response["body"])


class FakeStepFunctions:
    def __init__(self, *, start_error=None, list_error=None, executions=None, description=None):
        self.start_error = start_error
        self.list_error = list_error
        self.executions = executions or []
        self.description = description or {}
        self.calls = []

    def start_execution(self, **kwargs):
        self.calls.append(("start", kwargs))
        if self.start_error:
            raise self.start_error
        return {"executionArn": "arn:aws:states:ap-south-1:123456789012:execution:sm:exec"}

    def list_executions(self, **kwargs):
        self.calls.append(("list", kwargs))
        if self.list_error:
            raise self.list_error
        return {"executions": self.executions}

    def describe_execution(self, **kwargs):
        self.calls.append(("describe", kwargs))
        return self.description


class FakeLifecycleDispatcher:
    def __init__(self):
        self.calls = []

    def dispatch(self, reservation, reason, event_id):
        self.calls.append((reservation["reservation_id"], reason, event_id))


@pytest.fixture
def services():
    with mock_aws():
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
        db.create_table(
            TableName="CostSamples",
            KeySchema=[
                {"AttributeName": "resource_class", "KeyType": "HASH"},
                {"AttributeName": "settled_at", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "resource_class", "AttributeType": "S"},
                {"AttributeName": "settled_at", "AttributeType": "S"},
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
        ledger.create_budget(team="alpha", period=PERIOD, budget_micro=5_000_000)
        admission = AdmissionService(ledger, FixturePricingProvider())
        workflow = WorkflowStarter(
            FakeStepFunctions(), "arn:aws:states:ap-south-1:123456789012:stateMachine:sm", ledger
        )
        yield ApiServices(
            ledger,
            admission,
            db,
            "Budgets",
            "Reservations",
            "CostSamples",
            "Alerts",
            workflow,
            FakeLifecycleDispatcher(),
        )


def test_missing_or_non_developer_claims_are_rejected(services):
    response = route_request("estimate", event(body=REQUEST), services)
    assert unpack(response)[0] == 401
    response = route_request("estimate", event(body=REQUEST, claims=claims(groups=[])), services)
    assert unpack(response) == (
        403,
        {
            "error": {
                "code": "POLICY_DENIED",
                "message": "Developer access is required",
                "details": {},
            }
        },
    )


def test_v2_group_claim_in_bracket_string_form_is_parsed(services):
    response = route_request(
        "calibration_summary",
        event(claims=claims(groups="[admins]")),
        services,
    )
    assert unpack(response)[0] == 200


def test_post_estimate_and_validation_error(services):
    status, body = unpack(
        route_request(
            "estimate", event(body=REQUEST, claims=claims(groups=["developers"])), services
        )
    )
    assert status == 200
    assert body["source"] == "fixture_unverified"
    assert body["compute_micro"] > 0 and body["storage_micro"] > 0
    assert (
        route_request(
            "estimate", event(body=REQUEST, claims=claims(groups=["developers"])), services
        )["headers"]["Access-Control-Allow-Origin"]
        == "http://localhost:5173"
    )
    bad = {**REQUEST, "resource": {**REQUEST["resource"], "ebs_gb": 31}}
    status, error = unpack(
        route_request("estimate", event(body=bad, claims=claims(groups=["developers"])), services)
    )
    assert status == 400
    assert error["error"]["code"] == "VALIDATION_ERROR"
    unauthorized_body = {**REQUEST, "team": "beta"}
    status, denied = unpack(
        route_request(
            "estimate",
            event(body=unauthorized_body, claims=claims(groups=["developers"])),
            services,
        )
    )
    assert status == 403 and denied["error"]["code"] == "POLICY_DENIED"


def test_pricing_failure_maps_to_retryable_contract_error(services):
    class BrokenPricing(FixturePricingProvider):
        def instance_hourly_usd(self, instance_type, region="ap-south-1"):
            raise PriceNotFound("fixture has no price")

    services = ApiServices(
        services.ledger,
        AdmissionService(services.ledger, BrokenPricing()),
        services.db,
        services.budgets_table,
        services.reservations_table,
        services.samples_table,
        services.alerts_table,
    )
    status, body = unpack(
        route_request(
            "estimate", event(body=REQUEST, claims=claims(groups=["developers"])), services
        )
    )
    assert status == 503
    assert body["error"]["code"] == "PRICING_UNAVAILABLE"


def test_create_request_idempotency_and_conflict(services):
    request_event = event(
        body=REQUEST,
        claims=claims(groups=["developers"]),
        headers={"Idempotency-Key": "api-key"},
    )
    status, first = unpack(route_request("create_request", request_event, services))
    assert status == 202
    assert first["provisioning_started"] is True
    assert first["workflow"]["state"] == "STARTED"
    assert first["reservation"]["workflow_execution_name"]
    assert first["reservation"]["workflow_execution_arn"].endswith(":exec")
    status, replay = unpack(route_request("create_request", request_event, services))
    assert status == 200
    assert replay["replayed"] is True
    assert replay["workflow"]["state"] == "STARTED"
    missing = event(body=REQUEST, claims=claims(groups=["developers"]))
    assert unpack(route_request("create_request", missing, services))[0] == 400
    changed = {**REQUEST, "duration_hours": 3}
    conflict = event(
        body=changed,
        claims=claims(groups=["developers"]),
        headers={"Idempotency-Key": "api-key"},
    )
    status, body = unpack(route_request("create_request", conflict, services))
    assert status == 409
    assert body["error"]["code"] == "IDEMPOTENCY_CONFLICT"


def test_ambiguous_workflow_start_stays_unknown_and_keeps_reservation(services):
    client = FakeStepFunctions(
        start_error=TimeoutError("start timed out"), list_error=TimeoutError("list unavailable")
    )
    services.workflow.client = client
    request_event = event(
        body=REQUEST,
        claims=claims(groups=["developers"]),
        headers={"Idempotency-Key": "ambiguous-start"},
    )
    status, body = unpack(route_request("create_request", request_event, services))
    assert status == 503
    assert body["error"]["code"] == "PENDING_UNKNOWN"
    reservation_id = body["error"]["details"]["reservation_id"]
    reservation = services.ledger.get_reservation(reservation_id)
    assert reservation["status"] == "HELD"
    assert reservation["workflow_start_state"] == "UNKNOWN"
    assert reservation["workflow_execution_name"]
    assert [item[0] for item in client.calls] == ["start", "list"]


def test_lost_reconciliation_after_prior_start_keeps_confirmed_start_and_hold(services):
    request_event = event(
        body=REQUEST,
        claims=claims(groups=["developers"]),
        headers={"Idempotency-Key": "lost-reconcile-after-start"},
    )
    status, first = unpack(route_request("create_request", request_event, services))
    assert status == 202 and first["workflow"]["state"] == "STARTED"
    services.workflow.client = FakeStepFunctions(
        start_error=TimeoutError("response lost"), list_error=TimeoutError("reconcile unavailable")
    )
    status, body = unpack(route_request("create_request", request_event, services))
    assert status == 503 and body["error"]["code"] == "PENDING_UNKNOWN"
    reservation = services.ledger.get_reservation(first["reservation_id"])
    assert reservation["status"] == "HELD"
    assert reservation["workflow_start_state"] == "STARTED"
    assert reservation["workflow_execution_status"] == "UNKNOWN"


def test_definitive_start_rejection_releases_reserved_funds(services):
    rejection = ClientError(
        {"Error": {"Code": "StateMachineDoesNotExist", "Message": "missing"}},
        "StartExecution",
    )
    services.workflow.client = FakeStepFunctions(start_error=rejection)
    request_event = event(
        body=REQUEST,
        claims=claims(groups=["developers"]),
        headers={"Idempotency-Key": "definitive-start-rejection"},
    )
    status, body = unpack(route_request("create_request", request_event, services))
    assert status == 503
    assert body["error"]["code"] == "WORKFLOW_START_FAILED"
    reservation = services.ledger.get_reservation(body["error"]["details"]["reservation_id"])
    assert reservation["status"] == "RELEASED"
    assert reservation["workflow_start_state"] == "FAILED"


def test_insufficient_budget_maps_to_frozen_409_contract(services):
    services.db.update_item(
        TableName="Budgets",
        Key={"team": {"S": "alpha"}, "period": {"S": PERIOD}},
        UpdateExpression="SET budget_micro=:n, available_micro=:n",
        ExpressionAttributeValues={":n": {"N": "1"}},
    )
    request_event = event(
        body=REQUEST,
        claims=claims(groups=["developers"]),
        headers={"Idempotency-Key": "no-budget"},
    )
    status, body = unpack(route_request("create_request", request_event, services))
    assert status == 409
    assert body["error"]["code"] == "INSUFFICIENT_BUDGET"


def test_request_read_not_found_and_cross_team_isolation(services):
    status, missing = unpack(
        route_request(
            "get_request",
            event(claims=claims(groups=["developers"]), path={"id": "missing"}),
            services,
        )
    )
    assert status == 404 and missing["error"]["code"] == "NOT_FOUND"
    created = services.admission.admit(
        REQUEST,
        identity=VerifiedIdentity("alpha", "user-1"),
        period=PERIOD,
        idempotency_key="read-test",
    )
    status, response = unpack(
        route_request(
            "get_request",
            event(
                claims=claims(groups=["developers"]),
                path={"id": created["reservation_id"]},
            ),
            services,
        )
    )
    assert status == 200
    assert response["request"]["reservation_id"] == created["reservation_id"]
    status, denied = unpack(
        route_request(
            "get_request",
            event(
                claims=claims(team="beta", groups=["developers"], subject="user-2"),
                path={"id": created["reservation_id"]},
            ),
            services,
        )
    )
    assert status == 403 and denied["error"]["code"] == "POLICY_DENIED"


def test_cancel_requests_idempotent_lifecycle_cleanup_without_releasing_reservation(services):
    created = services.admission.admit(
        REQUEST,
        identity=VerifiedIdentity("alpha", "user-1"),
        period=PERIOD,
        idempotency_key="cancel-test",
    )
    services.ledger.set_workflow_start_state(
        created["reservation_id"],
        "STARTING",
        execution_name="exec-cancel-test",
    )
    services.ledger.set_workflow_start_state(
        created["reservation_id"],
        "STARTED",
        execution_name="exec-cancel-test",
        execution_arn="arn:aws:states:ap-south-1:123:execution:sm:cancel",
    )
    services.ledger.activate(
        created["reservation_id"],
        instance_id="i-cancel",
        volume_ids=["vol-cancel"],
        execution_arn="arn:aws:states:ap-south-1:123:execution:sm:cancel",
    )
    status, body = unpack(
        route_request(
            "cancel_request",
            event(
                claims=claims(groups=["developers"]),
                path={"id": created["reservation_id"]},
            ),
            services,
        )
    )
    assert status == 202
    assert body["cancellation_requested"] is True
    assert body["cleanup_state"] == "PENDING"
    reservation = services.ledger.get_reservation(created["reservation_id"])
    assert reservation["status"] == "CANCELLED"
    assert services.ledger.get_budget("alpha", PERIOD)["reserved_micro"] == reservation["reserved_micro"]
    first_event = services.lifecycle_dispatcher.calls[0]
    status, retry = unpack(
        route_request(
            "cancel_request",
            event(
                claims=claims(groups=["developers"]),
                path={"id": created["reservation_id"]},
            ),
            services,
        )
    )
    assert status == 202 and retry["request"]["status"] == "CANCELLED"
    assert services.lifecycle_dispatcher.calls[1] == first_event


def test_cancel_rejects_held_unknown_start_and_preserves_hold(services):
    created = services.admission.admit(
        REQUEST,
        identity=VerifiedIdentity("alpha", "user-1"),
        period=PERIOD,
        idempotency_key="cancel-held-unknown",
    )
    services.ledger.set_workflow_start_state(
        created["reservation_id"], "STARTING", execution_name="unknown-start"
    )
    services.ledger.set_workflow_start_state(
        created["reservation_id"], "UNKNOWN", execution_name="unknown-start"
    )
    status, body = unpack(
        route_request(
            "cancel_request",
            event(
                claims=claims(groups=["developers"]),
                path={"id": created["reservation_id"]},
            ),
            services,
        )
    )
    assert status == 409 and body["error"]["code"] == "REQUEST_NOT_ACTIVE"
    assert services.ledger.get_reservation(created["reservation_id"])["status"] == "HELD"
    assert services.lifecycle_dispatcher.calls == []


def test_reservation_list_defaults_to_claim_team_and_denies_other_team(services):
    services.admission.admit(
        REQUEST,
        identity=VerifiedIdentity("alpha", "user-1"),
        period=PERIOD,
        idempotency_key="list-test",
    )
    status, body = unpack(
        route_request(
            "list_reservations",
            event(claims=claims(groups=["developers"])),
            services,
        )
    )
    assert status == 200 and len(body["items"]) == 1
    status, error = unpack(
        route_request(
            "list_reservations",
            event(claims=claims(groups=["developers"]), query={"team": "beta"}),
            services,
        )
    )
    assert status == 403 and error["error"]["code"] == "POLICY_DENIED"


def test_budget_get_put_and_admin_authorization(services):
    status, body = unpack(
        route_request(
            "get_budget",
            event(
                claims=claims(groups=["developers"]),
                path={"team": "alpha"},
                query={"period": PERIOD},
            ),
            services,
        )
    )
    assert status == 200 and body["budget"]["budget_micro"] == 5_000_000
    status, denied = unpack(
        route_request(
            "put_budget",
            event(
                body={"period": PERIOD, "budget_usd": "12.50"},
                claims=claims(groups=["developers"]),
                path={"team": "beta"},
            ),
            services,
        )
    )
    assert status == 403 and denied["error"]["code"] == "POLICY_DENIED"
    status, created = unpack(
        route_request(
            "put_budget",
            event(
                body={"period": PERIOD, "budget_usd": "12.50"},
                claims=claims(groups=["admins"]),
                path={"team": "beta"},
            ),
            services,
        )
    )
    assert status == 200 and created["budget"]["budget_micro"] == 12_500_000
    status, conflict = unpack(
        route_request(
            "put_budget",
            event(
                body={"period": PERIOD, "budget_micro": 1},
                claims=claims(groups=["admins"]),
                path={"team": "beta"},
            ),
            services,
        )
    )
    assert status == 409 and conflict["error"]["code"] == "BUDGET_ALREADY_EXISTS"


def test_admin_calibration_summary_uses_persisted_samples(services):
    services.db.put_item(
        TableName="CostSamples",
        Item={
            "resource_class": {"S": "ec2"},
            "settled_at": {"S": "2026-10-01T00:00:00Z"},
            "settlement_id": {"S": "settlement-1"},
            "settlement_stage": {"S": "FINAL"},
            "attributed": {"BOOL": True},
            "actual_micro": {"N": "100"},
            "reserved_micro": {"N": "110"},
            "modeled_runtime_micro": {"N": "100"},
        },
    )
    status, response = unpack(
        route_request("calibration_summary", event(claims=claims(groups=["admins"])), services)
    )
    assert status == 200
    assert response["sample_count"] == 1
    assert {item["policy"] for item in response["policies"]} == {
        "fixed",
        "rolling_quantile",
        "split_conformal",
        "adaptive",
    }
    status, error = unpack(
        route_request("calibration_summary", event(claims=claims(groups=["developers"])), services)
    )
    assert status == 403 and error["error"]["code"] == "POLICY_DENIED"


def test_admin_alerts_are_loaded_from_table_and_restricted(services):
    services.db.put_item(
        TableName="Alerts",
        Item={
            "alert_id": {"S": "alert-1"},
            "alert_type": {"S": "BudgetOverrun"},
            "created_at": {"S": "2026-10-01T00:00:00Z"},
        },
    )
    status, body = unpack(
        route_request("list_alerts", event(claims=claims(groups=["admins"])), services)
    )
    assert status == 200 and body["items"][0]["alert_id"] == "alert-1"
    status, error = unpack(
        route_request("list_alerts", event(claims=claims(groups=["developers"])), services)
    )
    assert status == 403 and error["error"]["code"] == "POLICY_DENIED"


def test_pending_unknown_maps_to_safe_error_envelope(services):
    class UnknownLedger:
        def __getattr__(self, name):
            return getattr(services.ledger, name)

        def get_reservation(self, reservation_id):
            raise AmbiguousLedgerOutcome("read timed out")

    admission = AdmissionService(UnknownLedger(), FixturePricingProvider())
    pending_services = ApiServices(
        admission.ledger,
        admission,
        services.db,
        services.budgets_table,
        services.reservations_table,
        services.samples_table,
        services.alerts_table,
    )
    status, body = unpack(
        route_request(
            "create_request",
            event(
                body=REQUEST,
                claims=claims(groups=["developers"]),
                headers={"Idempotency-Key": "pending"},
            ),
            pending_services,
        )
    )
    assert status == 503
    assert body["error"]["code"] == "PENDING_UNKNOWN"
    assert body["error"]["details"]["reconciliation_required"] is True
