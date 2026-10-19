# Decisions and interpretation

## Frozen contracts

The repository began empty. The uploaded text available in this task is the master project prompt; a separate Team Build Guide was not attached. The API paths/statuses/error envelope, reservation states, EventBridge event names, table semantics, and M0–M12 boundaries in that prompt are treated as frozen. Contract alterations must be recorded in `docs/contract-changes.md` before changing implementation.

## gp3 sizing

The API and v1 policy accept gp3 sizes from 1 through 30 GB. The 8 GB figure in cost-safety examples is the default demo request only. Validation enforces the former and demo configuration uses the latter.

## Adaptive update and settlement ordering

For each resource class, alpha starts at 0.05; target alpha is 0.05 and gamma is configurable in [0.01, 0.05]. For each eligible final settlement, `miss=1` iff `actual_micro > reserved_micro`; otherwise `miss=0`. Then `alpha_next = clamp(alpha_current + gamma * (0.05 - miss), 0.001, 0.5)`. The empirical reserve multiplier is the nearest-rank empirical quantile of valid `actual / modeled_runtime_cost` ratios at `1-alpha_next`, clamped to [1.0, 2.0], with fixed 1.10 cold-start fallback until 10 valid samples.

Settlement records are deduplicated by immutable settlement version/id and sorted by `(settled_at, settlement_id)`. A delayed record causes the alpha state to be replayed from the initial alpha in this canonical order, so delivery order cannot change the result. A later billing correction is a new version and is replayed; it does not mutate the prior record. The ledger applies only a new settlement version once. Missing or unattributed billing is ineligible for calibration. This adaptive heuristic does not provide a formal coverage guarantee: exchangeability/stationarity, representative labeled data, stable attribution, and a predeclared evaluation protocol would be needed for statistical claims.

## Billing and cost model

All backend amounts are integer micro-USD and round upward. The estimator's gp3 monthly price uses a 730-hour month. Pricing, data transfer, taxes, credits, refunds, discounts, and T3 CPU-credit behavior need live verification; fixture/fallback values are never called live. No real billing results are supplied in this repo.

## Crash-safe workflow start

Persist the deterministic Standard Workflow execution name on the confirmed `HELD` reservation before calling `StartExecution`. The name is derived from the reservation ID, and the JSON input is built only from the durable reservation. An ambiguous timeout or failed `ListExecutions`/`DescribeExecution` remains `UNKNOWN`; do not release or start a differently named execution. Retrying the same admission key reuses the same name and input. A duplicate name is accepted only after its stored input is checked; a mismatch remains unresolved. A conclusive Step Functions rejection followed by successful reconciliation with no execution is recorded as start `FAILED` and permits an idempotent hold release. A provisioning failure after an execution started is recorded separately from start failure.

The Standard workflow invokes one provisioner Lambda. That function assumes `ProvisionerRole` with reservation and team session tags; the state-machine role can invoke only the provisioner and reservation callbacks and has no EC2 permissions. Launch uses an EC2 client token equal to the reservation ID, required project/ownership tags on instance and volume, gp3 encrypted root storage with delete-on-termination, IMDSv2, no key pair, and an explicitly non-public network interface. The subnet and no-ingress security group are required stack parameters. The state machine waits and polls up to six minutes for the instance to become running, records instance/volume IDs, and activates the reservation.

Failure cleanup is tag-scoped and polled until complete or a bounded timeout. Only a controlled pre-launch failure with durable `NOT_CREATED` evidence can release the hold after workflow execution. If an instance/volume may have existed, cleanup or spend is uncertain, so the reservation stays held for later reconciliation rather than incorrectly refunding possible spend. The M7 expiry schedule and M8 actual-cost settlement are not implemented by M6. IAM condition-key behavior and configured subnet/security-group restrictions must be verified in an approved AWS environment; synthesis is not proof of enforcement.

## M2 DynamoDB ledger state and retry decisions

### Budget state machine

- `OPEN -> CLOSED` is allowed only when `reserved_micro == 0` and the transactionally maintained `open_reservations == 0`. The latter counts reservations from `reserve` until release or first final settlement, including provisional settlements.
- `CLOSED` is terminal. Reservation and settlement transactions condition on `status = OPEN`, so a concurrent close and balance mutation cannot both commit.
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
Cancellation is read-only and returns `CANCELLATION_UNSUPPORTED` (501) until M7
cleanup is implemented. Unknown admission or start outcomes map to 503
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
