# Decisions and interpretation

## Frozen contracts

The repository began empty. The uploaded text available in this task is the master project prompt; a separate Team Build Guide was not attached. The API paths/statuses/error envelope, reservation states, EventBridge event names, table semantics, and M0–M12 boundaries in that prompt are treated as frozen. Contract alterations must be recorded in `docs/contract-changes.md` before changing implementation.

## M5 HTTP API v2

M5 uses API Gateway HTTP API v2 as required by the project specification. Its
JWT authorizer uses the Cognito user-pool issuer and public app-client ID as the
issuer and audience. Lambda payload format 2.0 claims are read only from
`requestContext.authorizer.jwt.claims`; malformed group strings such as
`[admins]` are normalized by the handler adapter. The default stage throttles
at 50 requests/second with burst 100 and accepts context overrides for isolated
load testing. The CloudFront CORS origin is a required deployment parameter;
localhost remains allowed for local development. Access logs include the API
request ID. These infrastructure changes leave route paths and handler
response shapes unchanged.

## gp3 sizing

The API and v1 policy accept gp3 sizes from 1 through 30 GB. The 8 GB figure in cost-safety examples is the default demo request only. Validation enforces the former and demo configuration uses the latter.

## Adaptive update and settlement ordering

For each resource class, alpha starts at 0.05; target alpha is 0.05 and gamma is configurable in [0.01, 0.05]. For each eligible final settlement, `miss=1` iff `actual_micro > reserved_micro`; otherwise `miss=0`. Then `alpha_next = clamp(alpha_current + gamma * (0.05 - miss), 0.001, 0.5)`. The empirical reserve multiplier is the nearest-rank empirical quantile of valid `actual / modeled_runtime_cost` ratios at `1-alpha_next`, clamped to [1.0, 2.0], with fixed 1.10 cold-start fallback until 10 valid samples.

Settlement records are deduplicated by immutable settlement version/id and sorted by `(settled_at, settlement_id)`. A delayed record causes the alpha state to be replayed from the initial alpha in this canonical order, so delivery order cannot change the result. A later billing correction is a new version and is replayed; it does not mutate the prior record. The ledger applies only a new settlement version once. Missing or unattributed billing is ineligible for calibration. This adaptive heuristic does not provide a formal coverage guarantee: exchangeability/stationarity, representative labeled data, stable attribution, and a predeclared evaluation protocol would be needed for statistical claims.

## Billing and cost model

All backend amounts are integer micro-USD and round upward. The estimator's gp3 monthly price uses a 730-hour month. Pricing, data transfer, taxes, credits, refunds, discounts, and T3 CPU-credit behavior need live verification; fixture/fallback values are never called live. No real billing results are supplied in this repo.

PriceCache stores each provider SKU under `sku_key` for seven days and retains
the fetch timestamp with the unit rate. The estimate snapshot ID identifies the
configuration and rates used; the reservation stores both that ID and the
fetched-at timestamp. When Price List lookup fails, an explicit
`fallback_unverified` rate table is used and logged. The fallback is never
labelled live and remains an unverified budgeting fallback.

Confirmed workflow-failure runtime cost uses the persisted snapshot's compute
and gp3 rates with the recorded launch and cleanup-completion times. Created
instances have a 60-second compute billing minimum; EBS is prorated by elapsed
seconds using the existing 730-hour month assumption. This is a local estimate
until M8 billing reconciliation supplies attributable actual cost.

M1 hosted UI keeps localhost callback/logout URLs in every environment; an
optional `cloudfront_url` CDK context value appends production callback/logout
URLs. The hosted UI domain prefix is a deployment parameter. ProvisionerRole's
permissions boundary permits only EC2 actions in `ap-south-1`; the role's
resource policy remains the narrower operation/tag policy. Caller permission
for `sts:TagSession` is intentionally left for the M1 verification to prove.

M8 uses an S3 manifest notification to a dedicated reconciler Lambda. The
manual Data Export and Glue table are still separately configured; the
`EnableBillingReconciliation` stack parameter defaults to false. A validated
manifest and a 60-day period-age guard are operational safeguards, not proof of
final completeness. The handler fails closed on a field/schema mismatch and
only adjusts reservations with attributable positive usage rows.

CUR amounts use `line_item_net_unblended_cost` when populated, else
`line_item_unblended_cost`. Parse with `Decimal`, multiply USD by 1,000,000,
then round to integer micro-USD with `ROUND_HALF_EVEN`, including signed values.
Only `Usage` for AmazonEC2/AmazonEBS is in scope. Credits, refunds, discounts,
taxes, fees, non-usage and unknown services remain excluded/unattributed; no
team budget is refunded on a negative adjustment or absent row. The ledger
applies only `new attributable target - previously booked target` and writes
the settlement and audit record atomically. A later export revision creates a
new settlement version. `createTimestamp` is used as the monotonic ordering
field; the ledger rejects an older export revision after a newer one has been
applied. Its presence and ordering semantics are local fixture assumptions
pending account verification.

## Crash-safe workflow start

Persist the deterministic Standard Workflow execution name on the confirmed `HELD` reservation before calling `StartExecution`. The name is derived from the reservation ID, and the JSON input is built only from the durable reservation. An ambiguous timeout or failed `ListExecutions`/`DescribeExecution` remains `UNKNOWN`; do not release or start a differently named execution. Retrying the same admission key reuses the same name and input. A duplicate name is accepted only after its stored input is checked; a mismatch remains unresolved. A conclusive Step Functions rejection followed by successful reconciliation with no execution is recorded as start `FAILED` and permits an idempotent hold release. A provisioning failure after an execution started is recorded separately from start failure.

The Standard workflow invokes one provisioner Lambda. That function assumes `ProvisionerRole` with reservation and team session tags; the state-machine role can invoke only the provisioner and reservation callbacks and has no EC2 permissions. Launch uses an EC2 client token equal to the reservation ID, required project/ownership tags on instance and volume, gp3 encrypted root storage with delete-on-termination, IMDSv2, no key pair, and an explicitly non-public network interface. The subnet and no-ingress security group are required stack parameters. The state machine waits and polls up to six minutes for the instance to become running, records instance/volume IDs, and activates the reservation.

Failure cleanup is tag-scoped and polled until complete or a bounded timeout. Only a controlled pre-launch failure with durable `NOT_CREATED` evidence can release the hold after workflow execution. If an instance/volume may have existed, cleanup or spend is uncertain, so the reservation stays held for later reconciliation rather than incorrectly refunding possible spend. The M7 expiry schedule and M8 actual-cost settlement are not implemented by M6. IAM condition-key behavior and configured subnet/security-group restrictions must be verified in an approved AWS environment; synthesis is not proof of enforcement.

## M2 DynamoDB ledger state and retry decisions

### Budget state machine

- `OPEN -> CLOSED` is allowed only when `reserved_micro == 0` and the transactionally maintained `open_reservations == 0`. The latter counts reservations from `reserve` until release or first final settlement, including provisional settlements.
- `CLOSED` is terminal for admission and budget lifecycle. Initial reservations and first final settlements require `OPEN`; a later versioned accounting correction may adjust a closed budget's available/spent balances in the same transaction while leaving its status `CLOSED`. This is required for late CUR revisions and is not permission to reopen or admit against that budget.
- A repeated close of an already closed budget is an idempotent replay. A closed budget is never reset by seeding.

### Reservation state machine

- `HELD -> ACTIVE` records launch and expiry.
- `HELD -> RELEASED` is allowed only when workflow-start state is `NOT_STARTED` or definitively `FAILED`.
- `ACTIVE -> EXPIRED` and `ACTIVE -> CANCELLED` are allowed lifecycle transitions.
- `EXPIRED` or `CANCELLED` can receive one provisional settlement. The status remains unchanged while `settlement_stage=PROVISIONAL`.
- `EXPIRED` or `CANCELLED` with a provisional settlement can move to `SETTLED` through final settlement. A higher, sequential settlement version corrects the actual cost while the public reservation status remains `SETTLED`.
- `RELEASED` and public status `SETTLED` are terminal status states. Settlement corrections are versioned accounting adjustments, not status transitions.
- Repeating an already-applied transition returns the durable current reservation. Other transitions fail with `InvalidTransition`.

### Workflow-start substate

`NOT_STARTED -> STARTING -> STARTED | FAILED | UNKNOWN`; `UNKNOWN -> STARTING | STARTED | FAILED | UNKNOWN`; `STARTED` and `FAILED` are terminal. `FAILED` means the Step Functions start was definitively rejected and no execution exists; a failed execution after `STARTED` is not represented as a failed start. `UNKNOWN` never authorizes release. A timeout or unavailable reconciliation yields an unknown outcome, not a failed start. M6 must reconcile execution and tagged resources and add cleanup evidence before it can safely release a hold after an execution had started; M2 deliberately leaves that reservation held otherwise.

### Keys, idempotency, and ambiguous outcomes

Reservations use `pk=RES#<reservation_id>, sk=META`; durable idempotency rows use `pk=IDEMP#<sha256(canonical [team,key])>, sk=KEY`, with the original team and key stored as attributes. Hashing the canonical pair avoids delimiter collisions between team and key. Settlement ID and version records use their own `SETTLEID#...` and `SETTLEVER#...` partitions. A repeated reservation key with the same semantic request fingerprint returns its original reservation; a different fingerprint raises `DuplicateIdempotencyKey`. A reservation ID collision is separately rejected. Only idempotency records receive a 365-day DynamoDB TTL; reservation, settlement, expiry, and cleanup records do not expire automatically. Retrying an ambiguous operation requires the same stable IDs, key, amount, and request. If a write may have committed and a consistent read cannot resolve it, the adapter raises `AmbiguousLedgerOutcome`; callers must reconcile before release or issuing a different request.

The status/expiry GSI requires both index keys on each reservation. A `HELD` row carries a planned expiry derived from reservation time and planned hours so status queries include it; successful activation replaces this with launch time plus planned hours. Lifecycle expiry processing must still transition only `ACTIVE` rows. This index attribute is scheduling/query metadata, not DynamoDB TTL.

All balance mutations are DynamoDB transactions coupling the budget update, reservation state, and durable settlement/idempotency rows as applicable. The budget item is read and checked against `available_micro + reserved_micro + spent_micro = budget_micro`; transaction conditions serialize concurrent balance changes and prevent insufficient reserves or duplicate state changes. `open_reservations` changes in the same transactions as reserve, release, and first final settlement, avoiding an eventually consistent GSI check in the budget close race. Final overrun corrections may make available funds negative, preserving the invariant and writing a versioned overrun alert.

A Moto test proves local adapter behavior only. Conditional transaction semantics, GSI propagation, TTL behavior, IAM, and contention still require verification against a dedicated real DynamoDB integration environment before deployment.

## M3 pricing provider and estimate semantics

The pricing boundary is a `PricingProvider` returning `Decimal` USD unit prices for
Mumbai (`ap-south-1`) Linux/shared On-Demand `t3.micro`/`t3.small` and gp3
GB-month storage. The AWS Price List API client is called in its endpoint region
(`us-east-1`) and all returned products are revalidated locally. Both product
queries paginate through every `NextToken`; no result and conflicting dimensions
are explicit pricing errors. `PriceList` has not been run against AWS in this
workspace, so its source is labelled `aws_pricelist_unverified`.

The estimator reports compute and gp3 storage separately in integer micro-USD.
Each component rounds upward independently, making the estimate conservatively
sum to `compute_micro + storage_micro`. It assumes 730 hours per storage month.
Modeled charges are Linux shared On-Demand instance hours and gp3 provisioned
capacity by GB-month, prorated over requested hours. It excludes taxes, EBS
snapshots, provisioned IOPS/throughput above gp3 included baselines, data
transfer, public IPv4, credits, negotiated discounts, Savings Plans/Reserved
Instances, T3 CPU-credit behavior, and any resources not in the request.
The fixture provider uses illustrative values and labels results
`fixture_unverified`; neither fixture nor cache is live-price evidence. The
seven-day cache interface currently has an in-memory implementation only; the
existing DynamoDB `PriceCache` table is not wired into the service.

## M4 admission service boundary

Admission accepts a `VerifiedIdentity` produced by a future verified-auth
adapter; it requires the request's team to match that identity and uses the
verified subject as ledger requester. Request JSON alone never authorizes a
team. The service applies fixed `1.10` reserve margin by default, derives a
stable reservation id from team/subject/idempotency key, and calls only the M2
transactional `reserve` operation. The M5 adapter starts M6 only after M4
returns a durable reservation. A confirmed reservation is returned as admitted;
if a ledger write or reconciliation read
is ambiguous, `AdmissionOutcomeUnknown` carries the reservation id and callers
must retry `reconcile` with the same identity, period, key, and request. Such an
outcome is not an admission confirmation and must not trigger provisioning or
release. M5 must map this internal pending outcome into the eventual public
response contract without changing the frozen route/error shapes silently.

Admission replay first reads the deterministic reservation id, so an already
committed request returns the durable estimate even if prices later change.
Reusing the same key with a changed team, subject, period, resource, runtime,
purpose, or volume is a conflict. Provider values, identity claims, and all
ledger behavior are locally tested with deterministic fixtures/Moto only; no
AWS identity or DynamoDB service behavior is verified here.

## M5 API adapter

The REST API uses an API Gateway Cognito User Pool authorizer; Lambda trusts only
the authorizer claim context, requiring `sub`, `custom:team`, and a Cognito group
claim. The Cognito app client can read `custom:team` for the ID token but is not
granted client write access to that membership attribute; assignment must use a
trusted administrative path. The handler additionally enforces developer/admin
group access and team ownership. The gateway handles token signature, issuer, and audience checks;
these have not been exercised live. Every route has its own Python 3.12 Lambda,
256 MB/10 second defaults, explicit CloudWatch log group, and a dedicated role
with only the DynamoDB operations/table ARNs that route uses. `pricing:GetProducts`
is granted only to estimate and request-creation roles. The create-request role's
workflow-start and reconciliation permissions are listed under M6 below.

M5 request creation starts M6 after M4 confirms the durable hold. The starter
persists execution identity before the Step Functions call and records the ARN.
Cancellation now conditionally records `ACTIVE -> CANCELLED` and asynchronously
dispatches M7 cleanup; it returns 202 with cleanup pending. The hold remains
reserved until tagged termination and volume cleanup are confirmed, then the
entire reserve becomes provisional spend pending M8 billing corrections. A
HELD reservation or any request with an uncertain start cannot be cancelled.
Unknown admission or start outcomes map to 503
`PENDING_UNKNOWN` with the reservation id and reconciliation instruction.
Calibration
summaries use only final, attributed, identified persisted `CostSamples`; alerts are read from
the Alerts table. Neither endpoint fabricates data. The table query/scan and
Gateway responses are tested with Moto or simulated API Gateway events only.

M6 CDK synthesis builds a Standard state machine, a dedicated state-machine
invoke role, separate reservation callback Lambda roles, and a provisioner
Lambda role that may assume the restricted EC2 ProvisionerRole. The M5 create
request role has `StartExecution` and `ListExecutions` on the named state machine
plus `DescribeExecution` on that machine's execution ARN prefix, which are needed
to reconcile deterministic start names. Provisioner inputs are checked against
v1 SKU/size/runtime policy. Launch, status polling,
tagged cleanup, activation, duplicate starts and ambiguous outcomes are locally
tested or template-inspected only. The state machine does not create an expiry
schedule; that integration belongs to M7. Live IAM evaluation, private subnet
routing, no-ingress security-group configuration, SSM AMI resolution and EC2
lifecycle behavior remain unverified.

## M7 lifecycle, cancellation and expiry

After the M6 activation callback durably transitions the reservation to ACTIVE,
it creates a deterministic one-time EventBridge Scheduler schedule at the
ledger expiry, with a constrained Scheduler role targeting the reaper Lambda.
An EventBridge rule also invokes the reaper every five minutes to recover missed
schedules and retry CANCELLED/EXPIRED cleanup. The status/expiry GSI is queried
for due ACTIVE rows; DynamoDB TTL is not used for scheduling. Cancellation first
uses the conditional M2 `ACTIVE -> CANCELLED` transition and dispatches the
reaper asynchronously. Duplicate API requests and schedule/sweeper deliveries
reuse reservation-scoped event IDs; EC2 termination/deletion is repeated only
for the exact ledger instance and recorded volumes, and the provisional
settlement ID is stable.

Before termination, the reaper reads the reservation-bound instance ID and
verifies project, environment, managed-by, reservation-id and team tags. It
deletes only ledger-bound volumes after the owned instance is terminated and
the volume is detached/available. Missing or contradictory evidence creates a
stable alert and leaves the hold uncommitted; no unknown resource is deleted.
Once cleanup is confirmed, the M7 provisional settlement moves the entire
reserved amount to provisional spend, preserving
`available_micro + reserved_micro + spent_micro = budget_micro`. It does not
claim actual billing; M8 must correct the provisional amount. Cleanup failure
or unknown workflow state never releases funds.

The safety sweeper retries HELD STARTING/UNKNOWN/STARTED workflows using the
same execution name only after a tag-scoped resource check. If a resource is
already present while execution state is unclear, it alerts and stops. Terminal
failed executions with possible resources receive tag-scoped cleanup but remain
held unless durable pre-launch `NOT_CREATED` evidence authorizes release. EC2
state-change events update instance timestamps monotonically, ignoring older
events. A CloudTrail RunInstances detector persists and publishes bypass alerts;
a daily read-only audit flags unmatched tagged instances/volumes and never
deletes them. Scheduler delivery, CloudTrail availability, IAM condition keys,
and resource behavior remain live-verification assumptions.

Expiry publication is marked in the reservation only after EventBridge accepts
the stable event. The periodic sweeper retries publication after cleanup and
provisional settlement, so an event delivery error cannot strand the expiry
notification. Cancellation does not delete the one-time expiry schedule; it
may invoke the reaper once more at the original expiry, where terminal-state
and settlement checks make the replay harmless. The schedule self-deletes after
that invocation. A dedicated DLQ is not configured; periodic reconciliation
and Lambda asynchronous retry behavior are the recovery mechanisms.
# M4 invariant audit and experiment

The five-minute audit derives held dollars from HELD/ACTIVE/CANCELLED/EXPIRED
reservation rows with no settlement stage; PROVISIONAL rows contribute their
provisional amount to spent while remaining open; FINAL rows contribute the
final actual amount and are closed. It reads all scanned items consistently,
then rescans once after a short delay before raising an alert because DynamoDB
does not offer a cross-table snapshot. Alert IDs are stable per team, budget
period and five-minute window. An audit EventBridge event is emitted for each
observed discrepancy, including when a prior Alert write was de-duplicated.

The intentionally unsafe GetItem/check/unconditional-Update implementation is
only in an opt-in test stack (`-c enable_naive=true`) and requires both
`ADMISSION_MODE=naive` and `ALLOW_NAIVE_ADMISSION=true`. It is not wired to the
API or standard stacks. It exists solely as a counterexample; its arithmetic
can violate the ledger invariant under concurrency.
