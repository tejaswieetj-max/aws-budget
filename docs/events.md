# Internal EventBridge event contract

Event bus name is `spendctl-bus`; event detail includes a stable `event_id`, `reservation_id`, team, occurrence time, and schema version. Consumers persist/deduplicate `event_id` before financial side effects.

| Event | Producer | Consumer | Retry/idempotency |
|---|---|---|---|
| ReservationActivated | Workflow/lifecycle | Scheduler, UI projection | Retry with stable event id; activation conditional on HELD |
| ReservationExpired | Reaper | Settlement/reconciliation | At-least-once; unique event id and reservation transition |
| ReservationSettled | Reconciliation | Calibration, UI | Settlement version is idempotency key |
| BypassDetected | CloudTrail rule/auditor | Alerts/SNS | Alert idempotent; no automatic termination |
| InvariantViolation | Checker | Alerts/SNS | Stable check-window id; repeat alerts deduplicated |

Live event delivery and target retry/DLQ behavior have not been verified.
