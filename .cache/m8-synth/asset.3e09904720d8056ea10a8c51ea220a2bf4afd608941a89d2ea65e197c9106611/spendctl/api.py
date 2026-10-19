"""Authenticated API Gateway Lambda handlers for the frozen M5 routes.

The JWT signature/issuer/audience is validated by API Gateway's Cognito
authorizer. This module only accepts claims from ``requestContext.authorizer``;
it never treats an Authorization header or request-body team as identity proof.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from typing import Any

import boto3
from boto3.dynamodb.types import TypeDeserializer, TypeSerializer
from botocore.exceptions import BotoCoreError, ClientError

from .admission import (
    AdmissionConflict,
    AdmissionInsufficientBudget,
    AdmissionOutcomeUnknown,
    AdmissionPricingUnavailable,
    AdmissionRetryableFailure,
    AdmissionService,
    UnauthorizedTeam,
    VerifiedIdentity,
)
from .calibration import MarginPolicies, Sample
from .ledger import (
    BudgetAlreadyExists,
    BudgetNotFound,
    DynamoLedger,
    InvalidTransition,
    ReservationNotFound,
)
from .lifecycle import lifecycle_event_id
from .money import usd_to_micro
from .pricing import (
    CachedPricingProvider,
    DynamoDBPriceCache,
    InMemoryPriceCache,
    PriceList,
    ResilientPricingProvider,
    TieredPriceCache,
)
from .workflow_start import WorkflowStarter, WorkflowStartOutcomeUnknown

_DESERIALIZER = TypeDeserializer()
_SERIALIZER = TypeSerializer()
_DEVELOPER_GROUPS = {"developers", "admins"}
_ADMIN_GROUPS = {"admins"}
_RESERVATION_STATES = {"HELD", "ACTIVE", "EXPIRED", "CANCELLED", "RELEASED", "SETTLED"}
_CORS_ORIGINS = set(os.environ.get("CORS_ALLOWED_ORIGINS", "http://localhost:5173").split(","))


@dataclass(frozen=True)
class ApiServices:
    ledger: DynamoLedger
    admission: AdmissionService
    db: Any
    budgets_table: str
    reservations_table: str
    samples_table: str
    alerts_table: str
    workflow: WorkflowStarter | None = None
    lifecycle_dispatcher: Any = None


class AuthenticationError(Exception):
    def __init__(self, status: int, code: str, message: str):
        self.status, self.code, self.message = status, code, message
        super().__init__(message)


@lru_cache(maxsize=1)
def _get_services() -> ApiServices:
    """Build SDK clients lazily; actual API calls only occur inside a handler."""
    region = os.environ.get("AWS_REGION", "ap-south-1")
    budgets = _required_env("BUDGETS_TABLE")
    reservations = _required_env("RESERVATIONS_TABLE")
    samples = _required_env("COST_SAMPLES_TABLE")
    alerts = _required_env("ALERTS_TABLE")
    price_cache_table = _required_env("PRICE_CACHE_TABLE")
    db = boto3.client("dynamodb", region_name=region)
    ledger = DynamoLedger(db, budgets, reservations, alerts)
    price_client = boto3.client("pricing", region_name="us-east-1")
    pricing = CachedPricingProvider(
        ResilientPricingProvider(PriceList(price_client)),
        TieredPriceCache(InMemoryPriceCache(), DynamoDBPriceCache(db, price_cache_table)),
    )
    admission = AdmissionService(ledger, pricing)
    workflow_client = boto3.client("stepfunctions", region_name=region)
    workflow = WorkflowStarter(
        workflow_client,
        _required_env("WORKFLOW_STATE_MACHINE_ARN"),
        ledger,
    )
    lifecycle_dispatcher = LambdaLifecycleDispatcher(
        boto3.client("lambda", region_name=region), _required_env("LIFECYCLE_REAPER_ARN")
    )
    return ApiServices(
        ledger,
        admission,
        db,
        budgets,
        reservations,
        samples,
        alerts,
        workflow,
        lifecycle_dispatcher,
    )


@dataclass(frozen=True)
class LambdaLifecycleDispatcher:
    client: Any
    reaper_arn: str

    def dispatch(self, reservation: dict, reason: str, event_id: str) -> None:
        response = self.client.invoke(
            FunctionName=self.reaper_arn,
            InvocationType="Event",
            Payload=json.dumps(
                {
                    "operation": "reap",
                    "reason": reason,
                    "reservation_id": reservation["reservation_id"],
                    "team": reservation["team"],
                    "event_id": event_id,
                },
                sort_keys=True,
            ).encode(),
        )
        if response.get("StatusCode") != 202:
            raise RuntimeError("lifecycle cleanup was not accepted")


def _required_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"missing required environment variable {name}")
    return value


def estimate_handler(event, context):
    return _entrypoint("estimate", event)


def create_request_handler(event, context):
    return _entrypoint("create_request", event)


def get_request_handler(event, context):
    return _entrypoint("get_request", event)


def cancel_request_handler(event, context):
    return _entrypoint("cancel_request", event)


def list_reservations_handler(event, context):
    return _entrypoint("list_reservations", event)


def get_budget_handler(event, context):
    return _entrypoint("get_budget", event)


def put_budget_handler(event, context):
    return _entrypoint("put_budget", event)


def calibration_summary_handler(event, context):
    return _entrypoint("calibration_summary", event)


def list_alerts_handler(event, context):
    return _entrypoint("list_alerts", event)


def _entrypoint(operation: str, event: dict):
    try:
        services = _get_services()
    except (BotoCoreError, ClientError):
        return _error(503, "SERVICE_UNAVAILABLE", "A required backend service is unavailable")
    except (RuntimeError, ValueError):
        return _error(500, "INTERNAL_ERROR", "The API service is not configured")
    return route_request(operation, event, services)


def route_request(operation: str, event: dict, services: ApiServices) -> dict:
    """Dispatch one route; public here to allow deterministic local tests."""
    response = _route_request(operation, event, services)
    origin = _header(event, "origin")
    if origin and origin in _CORS_ORIGINS:
        response.setdefault("headers", {})["Access-Control-Allow-Origin"] = origin
        response["headers"]["Vary"] = "Origin"
    return response


def _route_request(operation: str, event: dict, services: ApiServices) -> dict:
    try:
        identity, groups = authenticated_identity(event)
        if operation in {"put_budget", "calibration_summary", "list_alerts"}:
            _require_admin(groups)
        else:
            _require_developer(groups)
        if operation == "estimate":
            result = services.admission.estimate(_body(event), identity)
            return _response(200, result)
        if operation == "create_request":
            key = _header(event, "Idempotency-Key")
            if not key:
                raise ValueError("Idempotency-Key header is required")
            result = services.admission.admit(
                _body(event),
                identity=identity,
                period=_current_period(),
                idempotency_key=key,
            )
            if services.workflow is None:
                raise RuntimeError("Step Functions workflow is not configured")
            workflow = services.workflow.start(result["reservation"])
            result["workflow"] = workflow
            result["reservation"] = services.ledger.get_reservation(result["reservation_id"])
            result["provisioning_started"] = workflow.get("state") not in {
                "START_REJECTED",
                "START_UNKNOWN",
                "EXECUTION_NAME_CONFLICT",
            }
            if workflow.get("state") == "START_REJECTED":
                return _error(
                    503,
                    "WORKFLOW_START_FAILED",
                    "Workflow start was rejected; the reservation was safely released",
                    {
                        "reservation_id": result["reservation_id"],
                        "released": workflow.get("reservation_released", False),
                    },
                )
            status = result.pop("http_status", 200 if result.get("replayed") else 202)
            return _response(status, result)
        if operation == "get_request":
            return _get_request(event, identity, services)
        if operation == "cancel_request":
            return _cancel_request(event, identity, services)
        if operation == "list_reservations":
            return _list_reservations(event, identity, services)
        if operation == "get_budget":
            return _get_budget(event, identity, services)
        if operation == "put_budget":
            return _put_budget(event, identity, services)
        if operation == "calibration_summary":
            return _calibration_summary(services)
        if operation == "list_alerts":
            return _list_alerts(event, services)
        return _error(404, "NOT_FOUND", "Unknown API operation")
    except AuthenticationError as error:
        return _error(error.status, error.code, error.message)
    except AdmissionOutcomeUnknown as error:
        return _error(
            503,
            "PENDING_UNKNOWN",
            "Reservation outcome is not yet confirmed; retry with the same idempotency key",
            {"reservation_id": error.reservation_id, "reconciliation_required": True},
        )
    except WorkflowStartOutcomeUnknown as error:
        return _error(
            503,
            "PENDING_UNKNOWN",
            "Reservation is durable but workflow start is not yet confirmed; retry with the same idempotency key",
            {
                "reservation_id": error.reservation_id,
                "reconciliation_required": True,
                "workflow": error.result,
            },
        )
    except AdmissionInsufficientBudget as error:
        return _error(409, "INSUFFICIENT_BUDGET", str(error))
    except AdmissionConflict as error:
        return _error(409, "IDEMPOTENCY_CONFLICT", str(error))
    except BudgetAlreadyExists as error:
        return _error(409, "BUDGET_ALREADY_EXISTS", str(error))
    except UnauthorizedTeam:
        return _error(403, "POLICY_DENIED", "Team access is not authorized")
    except AdmissionPricingUnavailable as error:
        return _error(503, "PRICING_UNAVAILABLE", str(error))
    except AdmissionRetryableFailure as error:
        return _error(503, "RETRYABLE_ERROR", str(error))
    except ReservationNotFound:
        return _error(404, "NOT_FOUND", "Request or reservation was not found")
    except InvalidTransition as error:
        return _error(409, "REQUEST_NOT_ACTIVE", str(error))
    except BudgetNotFound:
        return _error(404, "NOT_FOUND", "Budget was not found")
    except (TypeError, ValueError, InvalidOperation, json.JSONDecodeError) as error:
        return _error(400, "VALIDATION_ERROR", str(error))
    except (BotoCoreError, ClientError):
        return _error(503, "SERVICE_UNAVAILABLE", "A required backend service is unavailable")
    except (RuntimeError, KeyError, AttributeError, ArithmeticError, IndexError):
        return _error(500, "INTERNAL_ERROR", "The request could not be completed")


def authenticated_identity(event: dict) -> tuple[VerifiedIdentity, set[str]]:
    """Extract identity only from API Gateway's verified authorizer context."""
    context = event.get("requestContext") or {}
    authorizer = context.get("authorizer") or {}
    jwt_context = authorizer.get("jwt")
    claims = jwt_context.get("claims") if isinstance(jwt_context, dict) else None
    if claims is None:
        claims = authorizer.get("claims")
    if not isinstance(claims, dict):
        raise AuthenticationError(401, "UNAUTHENTICATED", "A valid Cognito ID token is required")
    subject = claims.get("sub")
    team = claims.get("custom:team") or claims.get("team")
    if not isinstance(subject, str) or not subject or not isinstance(team, str) or not team:
        raise AuthenticationError(
            401, "UNAUTHENTICATED", "Trusted subject and team claims are required"
        )
    groups = _groups(claims.get("cognito:groups", claims.get("groups", [])))
    if not groups.intersection(_DEVELOPER_GROUPS):
        raise AuthenticationError(403, "POLICY_DENIED", "Developer access is required")
    return VerifiedIdentity(team=team, subject=subject), groups


def _groups(value) -> set[str]:
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            parsed = [group.strip().strip("'\"") for group in value.strip().strip("[]").split(",")]
        value = parsed
    if not isinstance(value, list) or any(not isinstance(group, str) for group in value):
        return set()
    return set(value)


def _require_admin(groups: set[str]) -> None:
    if not groups.intersection(_ADMIN_GROUPS):
        raise AuthenticationError(403, "POLICY_DENIED", "Administrator access is required")


def _require_developer(groups: set[str]) -> None:
    if not groups.intersection(_DEVELOPER_GROUPS):
        raise AuthenticationError(403, "POLICY_DENIED", "Developer access is required")


def _get_request(event: dict, identity: VerifiedIdentity, services: ApiServices) -> dict:
    reservation_id = _path_parameter(event, "id")
    reservation = services.ledger.get_reservation(reservation_id)
    if reservation.get("team") != identity.team:
        raise UnauthorizedTeam("request belongs to another team")
    return _response(200, {"request": reservation})


def _cancel_request(event: dict, identity: VerifiedIdentity, services: ApiServices) -> dict:
    reservation_id = _path_parameter(event, "id")
    reservation = services.ledger.get_reservation(reservation_id)
    if reservation.get("team") != identity.team:
        raise UnauthorizedTeam("request belongs to another team")
    if reservation["status"] == "ACTIVE":
        reservation = services.ledger.cancel_active(reservation_id)["reservation"]
    elif reservation["status"] not in {"CANCELLED", "EXPIRED"}:
        raise InvalidTransition(
            f"cannot cancel request in {reservation['status']}; only active requests can be cancelled"
        )
    if services.lifecycle_dispatcher is None:
        raise RuntimeError("lifecycle reaper is not configured")
    reason = "expiry" if reservation["status"] == "EXPIRED" else "cancel"
    services.lifecycle_dispatcher.dispatch(
        reservation, reason, lifecycle_event_id(reservation, reason)
    )
    return _response(
        202,
        {
            "request": reservation,
            "cancellation_requested": reservation["status"] == "CANCELLED",
            "cleanup_state": reservation.get("workflow_cleanup_state", "PENDING"),
        },
    )


def _list_reservations(event: dict, identity: VerifiedIdentity, services: ApiServices) -> dict:
    query = event.get("queryStringParameters") or {}
    requested_team = query.get("team") or identity.team
    if requested_team != identity.team:
        raise UnauthorizedTeam("reservations can only be listed for the verified team")
    status = query.get("status")
    if status and status not in _RESERVATION_STATES:
        raise ValueError("unknown reservation status")
    limit = _parse_limit(query.get("limit", "50"))
    start_key = _decode_token(query.get("nextToken"))
    result = services.ledger.list_reservations(
        team=identity.team, status=status, limit=limit, start_key=start_key
    )
    return _response(
        200,
        {"items": result["items"], "nextToken": _encode_token(result.get("last_evaluated_key"))},
    )


def _get_budget(event: dict, identity: VerifiedIdentity, services: ApiServices) -> dict:
    team = _path_parameter(event, "team")
    if team != identity.team:
        raise UnauthorizedTeam("budget access is limited to the verified team")
    period = (event.get("queryStringParameters") or {}).get("period") or _current_period()
    _validate_period(period)
    budget = services.ledger.get_budget(team, period)
    if not budget:
        raise BudgetNotFound(f"budget {team}/{period} was not found")
    return _response(200, {"budget": budget})


def _put_budget(event: dict, identity: VerifiedIdentity, services: ApiServices) -> dict:
    team = _path_parameter(event, "team")
    payload = _body(event)
    period = payload.get("period") or _current_period()
    _validate_period(period)
    if "budget_micro" in payload:
        amount = payload["budget_micro"]
        if isinstance(amount, bool) or not isinstance(amount, int):
            raise TypeError("budget_micro must be an integer")
    elif "budget_usd" in payload:
        value = payload["budget_usd"]
        if not isinstance(value, (str, int, Decimal)) or isinstance(value, bool):
            raise TypeError("budget_usd must be a decimal string or integer")
        amount = usd_to_micro(value)
    else:
        raise ValueError("budget_micro or budget_usd is required")
    if amount <= 0:
        raise ValueError("budget amount must be greater than zero")
    budget = services.ledger.create_budget(team=team, period=period, budget_micro=amount)
    return _response(200, {"budget": budget, "replayed": bool(budget.get("replayed", False))})


def _calibration_summary(services: ApiServices) -> dict:
    policies = MarginPolicies()
    token = None
    order = 0
    while True:
        request = {
            "TableName": services.samples_table,
            "KeyConditionExpression": "resource_class = :class",
            "ExpressionAttributeValues": {":class": {"S": "ec2"}},
            "ScanIndexForward": True,
        }
        if token:
            request["ExclusiveStartKey"] = token
        response = services.db.query(**request)
        for raw in response.get("Items", []):
            item = _deserialize_item(raw)
            if not _valid_calibration_item(item):
                continue
            sample_id = str(
                item.get("settlement_id") or item.get("sample_id") or item["settled_at"]
            )
            actual = int(item["actual_micro"])
            modeled = int(item["modeled_runtime_micro"])
            added = policies.add_settlement(
                "ec2",
                Sample(
                    settlement_id=sample_id,
                    ratio=actual / modeled,
                    reserved_micro=int(item["reserved_micro"]),
                    actual_micro=actual,
                    modeled_runtime_micro=modeled,
                    settled_order=order,
                ),
            )
            if added:
                order += 1
        token = response.get("LastEvaluatedKey")
        if not token:
            break
    names = ("fixed", "rolling_quantile", "split_conformal", "adaptive")
    return _response(
        200,
        {
            "resource_class": "ec2",
            "sample_count": order,
            "policies": [{"policy": name, **policies.summary("ec2", name)} for name in names],
            "source": "CostSamples",
        },
    )


def _valid_calibration_item(item: dict) -> bool:
    values = (
        item.get("actual_micro"),
        item.get("reserved_micro"),
        item.get("modeled_runtime_micro"),
    )
    return (
        isinstance(item.get("settled_at"), str)
        and item.get("settlement_stage") == "FINAL"
        and item.get("attributed") is True
        and bool(item.get("settlement_id"))
        and all(
            isinstance(value, (int, Decimal)) and not isinstance(value, bool) for value in values
        )
        and int(item["actual_micro"]) >= 0
        and int(item["reserved_micro"]) >= 0
        and int(item["modeled_runtime_micro"]) > 0
    )


def _list_alerts(event: dict, services: ApiServices) -> dict:
    query = event.get("queryStringParameters") or {}
    limit = _parse_limit(query.get("limit", "50"))
    token = _decode_token(query.get("nextToken"))
    request = {"TableName": services.alerts_table, "Limit": limit}
    if token:
        request["ExclusiveStartKey"] = _serialize_item(token)
    response = services.db.scan(**request)
    items = [_deserialize_item(item) for item in response.get("Items", [])]
    items.sort(key=lambda item: item.get("created_at", ""), reverse=True)
    last_key = _deserialize_item(response.get("LastEvaluatedKey"))
    return _response(200, {"items": items, "nextToken": _encode_token(last_key)})


def _body(event: dict) -> dict:
    raw = event.get("body")
    if raw is None:
        raise ValueError("request body is required")
    if event.get("isBase64Encoded"):
        try:
            raw = base64.b64decode(raw, validate=True).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError) as error:
            raise ValueError("request body is not valid base64 UTF-8") from error
    try:
        value = json.loads(raw, parse_float=Decimal, parse_int=int)
    except (TypeError, json.JSONDecodeError) as error:
        raise ValueError("request body must be valid JSON") from error
    if not isinstance(value, dict):
        raise TypeError("request body must be a JSON object")
    return value


def _header(event: dict, name: str) -> str | None:
    headers = event.get("headers") or {}
    return next((value for key, value in headers.items() if key.lower() == name.lower()), None)


def _path_parameter(event: dict, name: str) -> str:
    value = (event.get("pathParameters") or {}).get(name)
    if not isinstance(value, str) or not value:
        raise ValueError(f"path parameter {name} is required")
    return value


def _parse_limit(value) -> int:
    try:
        limit = int(value)
    except (TypeError, ValueError) as error:
        raise ValueError("limit must be an integer from 1 to 100") from error
    if not 1 <= limit <= 100:
        raise ValueError("limit must be an integer from 1 to 100")
    return limit


def _validate_period(period) -> None:
    if not isinstance(period, str) or len(period) != 7 or period[4] != "-":
        raise ValueError("period must use YYYY-MM format")
    try:
        datetime.strptime(period, "%Y-%m")
    except ValueError as error:
        raise ValueError("period must use YYYY-MM format") from error


def _current_period() -> str:
    return datetime.now(UTC).strftime("%Y-%m")


def _decode_token(token) -> dict | None:
    if not token:
        return None
    try:
        raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
        encoded = json.loads(raw)
        if not isinstance(encoded, dict):
            raise TypeError("nextToken payload must be an object")
        return {key: _DESERIALIZER.deserialize(value) for key, value in encoded.items()}
    except (binascii.Error, json.JSONDecodeError, TypeError, ValueError, KeyError) as error:
        raise ValueError("nextToken is invalid") from error


def _encode_token(key: dict | None) -> str | None:
    if not key:
        return None
    serialized = _serialize_item(key)
    raw = json.dumps(serialized, sort_keys=True, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _serialize_item(item: dict) -> dict:
    return {key: _SERIALIZER.serialize(value) for key, value in item.items()}


def _deserialize_item(item: dict | None) -> dict:
    if not item:
        return {}
    return {key: _DESERIALIZER.deserialize(value) for key, value in item.items()}


def _response(status: int, body: dict) -> dict:
    return {
        "statusCode": status,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "http://localhost:5173",
            "Vary": "Origin",
        },
        "body": json.dumps(body, default=_json_default, separators=(",", ":")),
    }


def _error(status: int, code: str, message: str, details: dict | None = None) -> dict:
    return _response(
        status, {"error": {"code": code, "message": message, "details": details or {}}}
    )


def _json_default(value):
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else str(value)
    raise TypeError(f"cannot serialize {type(value).__name__}")
