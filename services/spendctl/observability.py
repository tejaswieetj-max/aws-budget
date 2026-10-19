"""Structured application logs and low-cardinality CloudWatch EMF metrics."""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from typing import Any

_ALLOWED_DIMENSIONS = {"result", "operation", "component", "outcome"}
_ALLOWED_VALUES = {
    "admitted", "denied", "error", "replay", "pending_unknown", "success", "failure",
    "unknown", "cleanup", "provisioning", "billing", "pricing", "api", "expiry",
    "cancel", "invariant", "schema", "query", "cache_hit", "cache_miss", "fallback",
}


def structured_log(module: str, operation: str, outcome: str, **fields: Any) -> None:
    """Emit a JSON log with a stable envelope; callers must pass non-secret context."""
    record = {
        "timestamp": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "service": "spendctl",
        "module": module,
        "operation": operation,
        "outcome": outcome,
        **fields,
    }
    print(json.dumps(record, sort_keys=True, separators=(",", ":"), default=str))


def emit_emf(namespace: str, metrics: dict[str, tuple[float, str]], *, dimensions: dict[str, str] | None = None) -> None:
    """Emit EMF with bounded dimensions only; identifiers belong in structured logs."""
    dims = dimensions or {}
    if any(key not in _ALLOWED_DIMENSIONS for key in dims):
        raise ValueError("metric dimensions must use the bounded spendctl dimension allowlist")
    if any(value not in _ALLOWED_VALUES for value in dims.values()):
        raise ValueError("metric dimension values must use the bounded spendctl value allowlist")
    payload: dict[str, Any] = {
        "_aws": {
            "Timestamp": int(time.time() * 1000),
            "CloudWatchMetrics": [{
                "Namespace": namespace,
                "Dimensions": [list(dims)] if dims else [[]],
                "Metrics": [{"Name": name, "Unit": unit} for name, (_, unit) in metrics.items()],
            }],
        },
        **dims,
        **{name: value for name, (value, _) in metrics.items()},
    }
    print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
