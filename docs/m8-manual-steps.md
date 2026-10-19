# M8 CUR 2.0 billing reconciliation

## Implemented local path

`BillingStack` keeps the CUR export bucket private and encrypted, stores Athena
results in its separate private encrypted bucket, creates the Glue database and
scan-limited Athena workgroup, and adds an S3 manifest notification to a
Python 3.12 reconciliation Lambda. The Data Export itself and its Glue table
remain manually configured. Reconciliation is disabled by default and requires
the `EnableBillingReconciliation=true` stack parameter. The Lambda also waits
until a billing period ended at least 60 days earlier. These gates reduce risk;
they do not prove that an export is complete or final.
The reconciler uses 512 MB and a five-minute timeout to poll Athena; M0–M5 API
Lambdas keep the 256 MB/10-second defaults. Set `CurExportPrefix` to the exact
dedicated CUR object prefix so both the trigger and S3 read policy stay scoped.

The handler validates a Data Export manifest, queries the configured Glue table
for the manifest's billing period, normalizes rows, attributes them by an exact
`reservation-id` tag or an exact resource-ID mapping, then asks the existing
DynamoDB ledger to apply a versioned correction. The query uses a fixed field
list and a validated period literal. Athena output is directed through the
enforced `spendctl-dev-billing` workgroup, whose query scan cap is 1 GiB.

## Record and money semantics

The parser currently expects these CUR 2.0 fields: `identity_line_item_id`,
`bill_billing_period_start_date`, `line_item_usage_start_date`,
`line_item_usage_end_date`, `line_item_usage_account_id`,
`product_product_code`, `line_item_usage_type`, `line_item_operation`,
`line_item_resource_id`, `line_item_line_item_type`,
`line_item_currency_code`, `line_item_unblended_cost`, optional
`line_item_net_unblended_cost`, and optional resource tag columns
`resource_tags_user_reservation_id` / `resource_tags_user_team`. These are
schema assumptions covered by fixtures, not live schema verification. Missing
required columns fail closed and emit `BillingSchemaInvalid`.

All input amounts are parsed with `Decimal`. USD is multiplied by 1,000,000 and
rounded to integer micro-USD with `ROUND_HALF_EVEN`; positive and negative
amounts use the same conversion. The net unblended cost is selected when
present, otherwise unblended cost is selected. Only positive `Usage` rows for
`AmazonEC2` and `AmazonEBS` enter reservation totals. Credits, refunds,
discounts, taxes, fees, non-usage records, non-USD records, and other services
are not applied to a reservation. Negative adjustments are excluded from
per-reservation cost; they do not increase a team's available budget. This
avoids charging/refunding an individual team on ambiguous shared adjustments.

The reconciliation is a target amount for attributable usage in one billing
period. It applies `new target - previous final target` in the same DynamoDB
transaction that writes the settlement version and `BILLING#<reconciliation>`
audit row. The audit records period, source revision, input digest, prior/new
actual, delta, settlement ID/version and line-item identities. Replaying the
same snapshot is a no-op. A changed revision gets a new version and applies
only the difference. An overrun may make available funds negative while the
ledger invariant remains true and the existing overrun alert is recorded.

The resource mappings use `RESOURCE#<resource-id>` records in the existing
Reservations table, written atomically with M6 workflow resource outcomes.
This preserves resource identity beyond later lifecycle cleanup. A resource
tag is checked against that mapping when both exist; conflicts and team-tag
mismatches remain unattributed and emit `BillingAttributionAmbiguous`. No
approximate timestamp, team-only, or instance-type attribution is allowed.

No CUR rows are not evidence of zero cost. The current handler only reconciles
reservations with positive attributable usage rows; it never refunds a
reservation for absent rows. This intentionally leaves true-zero usage and
records whose only charges are credits/discounts unresolved rather than
inferring a refund.

For additional safety, the current implementation leaves attribution unresolved
when lifecycle timestamps are missing or a reservation runtime spans billing
periods. The EC2/EBS charges then remain out of CUR correction and the
provisional/final M6/M7 accounting stays unchanged. Supporting cross-period
reservations requires aggregating complete snapshots for all covered periods
before one target can be applied; it is an explicit implementation limitation.

## Revision, deduplication, and recovery

The event identity is the CUR manifest's assembly ID/version and billing
period. Manifest `assemblyId` (or `assemblyVersion`) is the revision identity;
manifest `createTimestamp` is the source order. A stale timestamp is
rejected by a conditional ledger update, so S3 notifications arriving out of
order cannot roll a newer amount back. This manifest field and its ordering
meaning remain a VERIFY assumption. Each line item uses
`identity_line_item_id` plus manifest object identity. Duplicate identical
line items are ignored; the same identity with different content fails the
whole batch. Reconciliation IDs include
reservation, period, source revision and digest. Ledger settlement, budget
delta, settlement version, and audit row are one DynamoDB transaction. If the
transaction outcome is ambiguous, retry with the same manifest/revision; do
not manufacture a new ID. Conflicting ledger state raises an error and emits a
`BillingLedgerConflict` event. Query/schema/ingestion failures are visible and
leave balances unchanged.

Athena's manifest event is assumed to represent a delivered export assembly;
the 60-day finalization delay is an operational guard, not a guarantee. The
manifest shape, table view of revisions, tag columns, and assembly semantics
must be verified against an actual account export before enabling the gate.
The query currently filters the billing-period column and selects only needed
fields. Partition pruning is not assumed because partition columns vary by
export configuration. If the actual Glue table exposes a verified partition
column, add it before using this for larger exports; the 1 GiB workgroup limit
will stop oversized scans.

## Required IAM

The reconciler role can read CUR objects from the export bucket, write/query
results only in the result bucket, start queries in the one Billing workgroup,
read the `spendctl_billing` Glue catalog, read/update the Budgets and
Reservations tables and write overrun alerts there, publish billing events to
the lifecycle event bus, and write its own CloudWatch log group. Athena query
execution read APIs require wildcard resource scope in the current IAM service
authorization model; query start is scoped to the named workgroup. Live IAM
evaluation remains unverified.

## Human-run setup and verification — do not run as part of local tests

1. **VERIFY:** In AWS Billing in `us-east-1`, create a CUR 2.0 Data Export to
   the deployed `CurExportsBucketName` and `CurExportPrefix`, with hourly Parquet output. Record its
   export name, prefix, delivery principal/policy, manifest sample and Glue
   table name. The bucket notification and role policy are created by CDK; the
   billing console export is not.
2. **VERIFY:** Activate `reservation-id` and `team` cost allocation tags and
   record when each first appears in exported rows. Tag activation can lag.
3. **VERIFY:** Inspect the actual manifest and Glue table schema in Athena.
   Compare every field in `_REQUIRED_CUR_COLUMNS` and the optional field list
   in `billing_handlers.py` with the real schema. Confirm whether
   the table represents the current export revision or contains multiple
   revisions, and identify partition columns. Do not set the opt-in parameter
   until this passes.
   In the Athena console, select the synthesized `spendctl-dev-billing`
   workgroup and run `DESCRIBE spendctl_billing.<configured_table>;`, then a
   bounded representative `SELECT ... LIMIT 20` including the candidate line
   item ID, cost, resource ID and tag columns. Confirm the scanned bytes stay
   under the workgroup cap. Redact account/resource identifiers in saved
   evidence; do not use the table scan as proof of completeness.
4. **VERIFY:** Confirm the manifest is emitted after all data files are
   readable, the role can query only the intended table/workgroup, and a
   representative query stays below the 1 GiB scan cap.
5. **VERIFY:** With separate approval, compare a known tagged EC2/EBS bill to
   the normalized per-reservation amount, validate credits/refunds treatment,
   verify revised exports apply deltas once, and inspect the ledger invariant
   and `BudgetOverrun`/billing events.
6. **VERIFY:** Review expected costs before enabling: S3 storage and requests,
   Athena scans (capped per query but billed), Glue catalog use, Lambda
   invocations, and CloudWatch/EventBridge activity. No Data Export fee or
   account-specific billing estimate is claimed here.

## Local recovery

For `BillingSchemaInvalid`, keep reconciliation disabled, inspect the redacted
Glue schema and update the explicit field mapping/tests. For query/ingestion
failures, retry the same manifest and revision after the cause is fixed. For
ambiguous attribution, repair or verify the resource history/tag, then replay
the same export. For a ledger conflict, inspect the settlement version and
`BILLING#…` audit item before retrying. Never delete audit rows or manually
change budget balances. Missing or delayed data is not grounds for release or
refund.
