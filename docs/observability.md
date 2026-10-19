# M11 observability

## EMF metrics

Application functions write CloudWatch Embedded Metric Format to standard output. Admission emits `AdmissionDecision` and `AdmissionLatencyMs` with only `result=admitted|denied|error`, plus replay, insufficient-budget and transaction-cancellation counters. Successful admission emits a dimensionless `BudgetUtilizationPercent` sample; 50%, 80% and 100% alarms use the maximum sample observed in a five-minute period. Team IDs are deliberately not metric dimensions.

Workflow start emits attempts, successes, failures and unknown outcomes. Lifecycle emits cleanup attempts/failures, unresolved-resource counts and expiry/cancellation outcomes. Pricing emits bounded cache hit/miss, lookup-failure and fallback-usage metrics. M8 emits reconciled record, unattributed/excluded/duplicate record, stale revision, attribution rate, adjustment, and reconciliation-failure metrics. API Gateway contributes its native 5xx and latency metrics; Lambda `Errors` and Step Functions execution-failure metrics are graphed. No reservation, request, user, or team ID appears in a metric dimension.

`spendctl.observability` provides the JSON log envelope and validates its bounded dimension allowlist. Existing handlers may include reservation IDs in structured log fields for operational correlation; do not log tokens or credentials. API access logs contain request ID, route, status and source IP; review retention and privacy settings before deployment.

## Dashboard, alarms and limits

`ObservabilityStack` synthesizes a dashboard for admission, workflow, cleanup, ledger invariant, billing, price, budget and API signals. Alarms cover invariant violations, repeated provisioning/cleanup failures, unknown starts, reconciliation failures, API 5xx, invariant DLQ depth and 50/80/100% budget utilization. A dedicated SNS topic is the alarm action target; it intentionally has no email/phone subscription configured. No recipient is invented.

Existing Lambda log groups retain logs for one month. The invariant scheduler has a DLQ. Tracing is not activated in CDK; API Gateway access logs and correlation fields are present, but no claim is made that X-Ray traces are active. CloudWatch metrics, dashboards, alarm state changes, SNS delivery, log retention, metric extraction and alarm thresholds require live AWS verification.

CUR delivery-gap alarms are intentionally absent. A reliable delivery-lag baseline requires real export delivery observations; the 60-day reconciliation gate does not establish a cadence guarantee. Budget threshold EMF is emitted on new reservations only and is dimensionless across teams; thresholds provide an account-level signal, not team-specific notices. Existing personal AWS Budgets setup at 50/80/100 remains a manual account action in `docs/aws-setup.md`.
