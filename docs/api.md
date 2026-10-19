# API contract

All routes require a Cognito ID token and backend authorization. Request creation requires `Idempotency-Key`. Responses preserve the frozen contract: 202 admitted, 200 replay, 409 `INSUFFICIENT_BUDGET`, 403 `POLICY_DENIED`, and 400 validation error. Errors use `{ "error": { "code": "...", "message": "...", "details": {} } }`.

| Method | Route | Access |
|---|---|---|
| POST | `/v1/estimate` | Developer |
| POST | `/v1/requests` | Developer |
| GET | `/v1/requests/{id}` | Developer |
| POST | `/v1/requests/{id}/cancel` | Developer |
| GET | `/v1/reservations?team=&status=` | Developer; team scoped |
| GET | `/v1/budgets/{team}` | Developer; team scoped |
| PUT | `/v1/budgets/{team}` | Admin |
| GET | `/v1/calibration/summary` | Admin |
| GET | `/v1/alerts` | Admin |

Demo request default is gp3 8 GB. V1 validation permits gp3 1–30 GB and 1–24 hours, with t3.micro or t3.small.

## M5 handler and response details

API Gateway HTTP API v2 uses a JWT authorizer for every route, with the Cognito
pool issuer and web-client ID as the issuer and audience. The authorizer
validates the signed ID token against the configured pool; Lambda handlers accept
identity only from `requestContext.authorizer` claims (`sub`, `custom:team`, and
`cognito:groups`). A request body's `team` is checked against the trusted team
claim and never grants access. Members of `developers` or `admins` may use
developer routes; `PUT /v1/budgets/{team}`, `GET /v1/calibration/summary`, and
`GET /v1/alerts` require the `admins` group. Budget reads and reservation reads
are restricted to the caller's team. Admin budget creation may target a team.

- `POST /v1/estimate` returns 200 with the componentized M3 estimate.
  The estimate includes integer `compute_micro`, `storage_micro`, and
  `total_micro`, plus `price_source`, `price_snapshot_id`, and
  The `source` value `fallback_unverified` means the service used its
  explicitly labelled fallback rate table after a Price List lookup failure;
  it is not a live-price result.
- `POST /v1/requests` returns 202 for a new durable reservation or 200 for a
  replay, with the M4 admission result, reservation, and M6 workflow result.
  Only after M2 durably confirms the reservation does the handler persist the
  deterministic execution name and call Step Functions Standard `StartExecution`.
  Successful responses add `workflow.state`, `workflow.execution_name`, and
  `workflow.executionArn`. A conclusive start rejection returns 503
  `WORKFLOW_START_FAILED` after the safe release path. An ambiguous start returns
  503 `PENDING_UNKNOWN`; the reservation remains held and callers must retry with
  the same key so M6 can reconcile the same execution name and input.
- `GET /v1/requests/{id}` returns `{ "request": <reservation> }` for the caller's
  team or the standard 404 error envelope.
- `POST /v1/requests/{id}/cancel` is team-scoped and accepts only ACTIVE
  reservations. It conditionally transitions ACTIVE to CANCELLED, then
  asynchronously dispatches the tag-verified reaper and returns 202 with the
  updated request and `cleanup_state: PENDING`. Repeating the request is
  idempotent and dispatches the same stable lifecycle event identity. HELD,
  RELEASED, and SETTLED requests return 409 `REQUEST_NOT_ACTIVE`; a HELD request
  with an uncertain workflow is never cancelled or released by this route.
  Cancellation does not immediately release the budget hold. The reaper only
  moves the full reserved amount to provisional spend after termination and
  volume cleanup are verified; M8 billing reconciliation may later correct it.
- `GET /v1/reservations` defaults `team` to the trusted claim; supplying another
  team is denied. Optional `status`, `limit` (1–100), and opaque `nextToken`
  query parameters are supported. Response is `{ "items": [...], "nextToken":
  "..." | null }`.
- `GET /v1/budgets/{team}` returns `{ "budget": ... }` for the caller's team;
  optional `period` defaults to the current UTC month.
- `PUT /v1/budgets/{team}` is admin-only and accepts `{ "period": "YYYY-MM",
  "budget_micro": 123 }` or `{ "period": "YYYY-MM", "budget_usd": "12.34" }`.
  It creates through the M2 ledger and never resets an existing budget.
- `GET /v1/calibration/summary` is admin-only and derives fixed, rolling,
  split-conformal, and adaptive summaries from the `CostSamples` table. Empty
  samples are reported as cold start; only final, attributed, identified
  settlements with valid monetary fields are included; no sample values are fabricated.
- `GET /v1/alerts` is admin-only and reads the Alerts table, with optional
  `limit` and opaque `nextToken` pagination.

All handler errors use the frozen `{ "error": { "code": "...", "message":
"...", "details": {} } }` envelope. M5 uses 400 `VALIDATION_ERROR`, 401
`UNAUTHENTICATED`, 403 `POLICY_DENIED`, 404 `NOT_FOUND`, 409
`INSUFFICIENT_BUDGET` or `IDEMPOTENCY_CONFLICT`, and 503 for `PENDING_UNKNOWN`,
`WORKFLOW_START_FAILED`, `PRICING_UNAVAILABLE`, `RETRYABLE_ERROR`, or backend unavailability. Pending
unknown includes the reservation id and `reconciliation_required: true`; clients
must retry with the same idempotency key. For an ambiguous M4 ledger outcome, no
workflow is started because the hold is not confirmed. For an ambiguous M6 start,
the reservation is confirmed, but execution identity/status is unresolved; retry
only reconciles the deterministic execution and never releases the hold. API
HTTP API JWT-authorizer rejections occur before Lambda and use API Gateway's
native unauthorized response; HTTP API v2 does not expose REST API
`GatewayResponse` templates. Handler-generated 401/403 responses retain the
documented error envelope. This transport-level unauthorized response
difference is recorded in `docs/contract-changes.md` and must be checked by a
live API test.

## Local and live verification boundary

Handler tests use simulated API Gateway authorizer claims and Moto/fake service
responses. They do not verify JWT signatures, Cognito issuer/audience behavior,
API Gateway routing/CORS/throttling, IAM evaluation, or real DynamoDB behavior.
M6's Step Functions definition and EC2 request shape and M7 EventBridge/Scheduler
rules are locally synthesized and mock-tested only; no state machine, Lambda,
IAM policy evaluation, schedule delivery, CloudTrail integration, subnet,
security group, AMI resolution, EC2 launch/termination, volume deletion, or
orphan audit has been verified in AWS.
The HTTP API sends Lambda payload format 2.0 events. Handlers read claims from
`requestContext.authorizer.jwt.claims`, body/base64 fields, and HTTP API v2 path,
query and headers. The `$default` stage uses rate 50/burst 100 by default;
`api_throttle_rate` and `api_throttle_burst` CDK context values override these
for isolated load tests. CORS permits localhost and the required CloudFront
origin parameter. Access logs include the API request ID. Each route retains a
separate Lambda role. No deployment or live AWS authentication/API test was
performed.
