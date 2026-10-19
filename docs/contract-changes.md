# Contract changes

No frozen HTTP API, response shape, event name, tag, public reservation status, or module boundary has been changed.

| Date | Contract area | Change | Rationale | Approval / status |
|---|---|---|---|---|
| 2026-10-19 | DynamoDB schema implementation (M2) | Add `team-created-index` (`team`, `created_at`) and `status-expiry-index` (`status`, `expires_at`) to Reservations and enable TTL on the `ttl` attribute. Add internal `record_type`, budget `status` and `open_reservations`, workflow-start state, settlement stage/version, and idempotency/settlement records in the existing `pk`/`sk` Reservations table. | Required to support the specified queries, durable idempotency, atomic budget closure and settlement without changing the public API. TTL is populated only on idempotency rows. | Implementation change documented; deployment/migration not performed. Existing deployed tables would need approved GSI/TTL changes and a backfill of budget status/open reservation counts before use. |
| 2026-10-19 | M5 API response additions | Add explicit `PENDING_UNKNOWN`/`PRICING_UNAVAILABLE`/`RETRYABLE_ERROR` 503 errors, idempotency conflict 409, and `CANCELLATION_UNSUPPORTED` 501 using the frozen error envelope. Cancellation remains read-only; no successful cancellation is claimed. Add response body details and pagination semantics in `docs/api.md`. | The existing route list did not define these failure cases or response bodies. Keeping the same error envelope while documenting additive statuses/codes lets clients distinguish safe pending outcomes and unsupported cleanup. | Documented additive contract detail; no API Gateway deployment or client compatibility verification performed. |
| 2026-10-19 | M6 request response and workflow metadata | Preserve route, status codes for admitted/replayed requests, error envelope, tags, and reservation statuses. Add a `workflow` object to successful request responses; add 503 `WORKFLOW_START_FAILED` for conclusive start rejection after safe release. Persist `workflow_execution_name`, ARN/status, resource IDs and cleanup state in reservation records. A pre-launch failure may release only with durable `NOT_CREATED` evidence. | M6 must report Step Functions identity and distinguish unknown start from confirmed admission. The extra ledger attributes support reconciliation without changing keys or balances. A partial/uncertain launch cannot be released safely until cleanup and billing evidence are reconciled. | Additive fields/codes documented; no live compatibility, IAM, Step Functions, or EC2 verification performed. M7 expiry schedules and M8 settlement remain deferred. |
| 2026-10-20 | M7 cancellation response and lifecycle records | Enable `POST /v1/requests/{id}/cancel` for ACTIVE requests: return 202 with updated request, `cancellation_requested`, and cleanup status; return 409 `REQUEST_NOT_ACTIVE` for ineligible states. Persist internal instance state timestamps, cleanup state, expiry-event publication marker, and termination time. Use existing event names `ReservationActivated`, `ReservationExpired`, and `BypassDetected`; no route, tag, public reservation status, or event name is changed. | Cancellation must record `ACTIVE -> CANCELLED` before asynchronous cleanup. Funds remain reserved until ownership is verified and cleanup completes, then the full reserve is provisionally settled pending M8 actual billing. | Additive response/error behavior and internal fields documented; no deployment or live API, scheduler, EventBridge, CloudTrail, IAM, EC2, or EBS verification performed. |
| 2026-10-20 | M5 API Gateway v2 authorization rejection | Replace REST API Gateway with the required HTTP API v2 and Cognito JWT authorizer. Authorizer rejections occur before Lambda and use API Gateway's native unauthorized response rather than the REST `GatewayResponse` JSON envelope. Handler-generated error envelopes and all route contracts remain unchanged. | HTTP API v2 does not provide REST API `GatewayResponse` templates. This transport-level behavior is documented rather than claiming a configurable envelope that the stack does not provide. | Necessary transport-level deviation documented; verify exact unauthorized response on a deployed HTTP API before client integration. No AWS deployment or live token test performed. |

The ledger persists budget `status=OPEN|CLOSED` and transactionally maintained `open_reservations`. Existing budget rows without these fields are rejected by the M2 ledger and require an explicit migration/backfill; no implicit initialization or balance reset is performed. Reservations must include `planned_hours` (1–24), matching the v1 request policy. Internal reservation status remains `HELD`, `ACTIVE`, `EXPIRED`, `CANCELLED`, `RELEASED`, or `SETTLED`.

M3/M4 implementation adds internal Python provider and admission service interfaces only. It does not add/change an HTTP route, public response shape, event, tag, DynamoDB key, or reservation state. Ambiguous admission is represented internally by `AdmissionOutcomeUnknown`; the future API adapter must map it deliberately without reporting an unconfirmed hold as admitted. PriceCache uses the frozen `sku_key` and adds TTL/rate/fetched-at attributes; reservations carry additive pricing provenance fields.

M5 restricts the Cognito app client's write attributes so `custom:team` remains an administrative membership claim. This changes the app-client permission configuration only; no deployed pool was modified. The API's additive 501/503 statuses and error codes are listed above, with the original error envelope preserved.

## M9–M11 additions

No API route, public request/response shape, DynamoDB key, event name, lifecycle state, or ledger semantic changed for these modules. M9 adds an offline research input/report format only. M10 calls the existing nine routes with their documented shapes. M11 adds EMF metric names and CloudFormation outputs for the WebStack URL/configuration and the dashboard/SNS topic. `WebStack` consumes the existing Cognito identifiers and API URL from stack references; `ObservabilityStack` references the existing API, Step Functions, Lifecycle and Billing resources. The frontend build configuration uses `VITE_API_BASE_URL`, `VITE_COGNITO_HOSTED_UI_DOMAIN`, `VITE_COGNITO_CLIENT_ID`, `VITE_COGNITO_REDIRECT_URI` and `VITE_COGNITO_LOGOUT_URI`; these are public browser configuration, never secrets. No external identity provider or API behavior was live-verified.

## M3 persistent price provenance (Task 4)

PriceCache enables its `ttl` attribute for seven-day expiration and stores
`rate_usd`/`fetched_at` with the frozen `sku_key`, behind the process-local
cache. Reservations gain additive `price_fetched_at` beside snapshot ID/source.
The estimate response identifies its snapshot and `source`, including explicit
`fallback_unverified` when fallback rates are selected. No table
key, API route, response envelope, event name, or resource tag changed.

## M1 permissions boundary and hosted UI parameters (Task 5)

ProvisionerRole gains a managed permissions boundary allowing EC2 actions only
when `aws:RequestedRegion=ap-south-1`; its inline resource/tag restrictions
remain the effective narrower policy. IdentityStack now requires a hosted UI
domain-prefix parameter and accepts optional `cloudfront_url` context for
callback/logout URLs while keeping localhost URLs. New CloudFormation outputs
expose pool ID, app-client ID and hosted UI domain. No public API or table
contract changed. No caller `sts:TagSession` permission was added pending the
human-run verification script.

## M8 billing infrastructure groundwork (Task 6)

BillingStack now synthesizes private encrypted export/result buckets, a Glue
database and a scan-limited Athena workgroup. It does not create an AWS Billing
Data Export or alter account billing settings. These are additive resources;
no API, ledger arithmetic, event or tag contract changed.

## Confirmed workflow-failure settlement (Task 3)

Added a version-1 failure settlement to the existing ledger semantics. A
reservation can move to RELEASED after workflow FAILED/TIMED_OUT/ABORTED only
when durable `workflow_cleanup_state=COMPLETE`, a started execution, and a
known `workflow_launch_state` (`CREATED` or `NOT_CREATED`) exist. A created
launch must also have persisted instance identity and launch time. The same DynamoDB
transaction reduces reserved by the full hold, increases spent by the measured
runtime actual, returns the unused hold to available, decrements the open count,
and persists `settlement_stage=FINAL`, `final_actual_micro`, settlement ID and
version. Replays with the same settlement ID/amount are no-ops. This adds fields
to the existing reservation record, without changing table keys or API routes.
Partial/unknown cleanup never releases funds and creates a durable alert. Runtime
actual is prorated from recorded compute and gp3 snapshot rates, with a 60
second minimum for a created EC2 instance; no resource means zero. This is a
local accounting estimate, not CUR actual cost.
# M8 internal persistence extension (2026-10-20)

No public API route, response, event name, ownership tag, reservation status,
or budget invariant changed. The existing Reservations table now contains two
additional internal record types, using the existing `pk`/`sk` key schema:

- `RESOURCE#<resource-id>` / `MAP` stores the exact resource-to-reservation
  mapping and team, transactionally with workflow resource identity updates.
- `BILLING#<reconciliation-id>` / `RECONCILIATION` stores the billing period,
  source revision, input digest, previous/new actual, incremental adjustment,
  settlement identity/version, and attributed line-item identities. It is
  written in the same transaction as final settlement and budget adjustment.
- The reservation item stores `billing_source_revision_order` as the applied
  manifest timestamp. A conditional comparison prevents an older export from
  overwriting a newer reconciled target.
- A later settlement correction can update `available_micro` and `spent_micro`
  on a `CLOSED` budget without changing its status. Admission and first final
  settlement remain disallowed for a closed budget. This is needed for a
  versioned CUR correction after a reservation has already reached a terminal
  financial state; deployments require no data migration.

This is necessary because the current table has no resource-ID lookup index and
the billing pipeline requires durable exact attribution plus atomic audit.
Existing table keys, GSIs, public reservation semantics and lifecycle states
are unchanged. Existing deployments need no key/index migration; old
reservations without `RESOURCE#…` rows are not retroactively attributed by
guess. No migration/backfill is performed automatically. If live schema or
transaction validation finds that records exceed DynamoDB's transaction size,
the pipeline must fail closed; do not split the financial write from its audit.
