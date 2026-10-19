# Internal EventBridge event contract

Event bus name is `spendctl-bus`; event detail includes a stable `event_id`, `reservation_id`, team, occurrence time, and schema version. Consumers persist/deduplicate `event_id` before financial side effects.

| Event | Producer | Consumer | Retry/idempotency |
|---|---|---|---|
| ReservationActivated | M6 activation callback | Scheduler integration, UI projection | `event_id=sha256(reservation_id, launched_at)`; activation and schedule name are stable on retry |
| ReservationExpired | M7 reaper | Settlement/reconciliation | Stable `sha256(f"{reservation_id}:expired:{expired_at or expires_at}")`; emitted only after verified termination/volume cleanup and provisional settlement |
| ReservationSettled | M8 reconciliation | Calibration, UI | Settlement version is idempotency key; M8 is not implemented in this milestone |
| BypassDetected | M7 CloudTrail RunInstances rule | Alerts table; downstream SNS is not wired | CloudTrail event ID is the stable key; alert write is conditional and event publication may be retried; no automatic termination |
| InvariantViolation | Checker | Alerts/SNS | Stable check-window id; repeat alerts deduplicated |

`ReservationActivated` and `ReservationExpired` detail fields include `event_id`,
`schema_version`, `reservation_id`, `team`, and `occurred_at`; activation also
includes `expires_at`. `BypassDetected` includes the CloudTrail event ID and
occurrence time. EventBridge delivery is at-least-once. Lifecycle effects are
idempotent on reservation ID and conditional ledger state, and consumers should
persist the event ID before financial side effects. The five-minute sweeper
recovers missed expiry schedules; CloudTrail delivery, target retry policy, and
dead-letter behavior require live verification. No DLQ is configured yet.

Live event delivery and target retry/DLQ behavior have not been verified.
M7 records successful expiry-event publication after `PutEvents` accepts the
stable event; if publication fails, the five-minute sweeper retries it. There
is no dedicated DLQ in this iteration.
