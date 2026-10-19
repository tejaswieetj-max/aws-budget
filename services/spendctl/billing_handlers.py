"""AWS adapters for CUR manifest-triggered Athena reconciliation."""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from datetime import UTC, datetime
from functools import partial
from urllib.parse import unquote_plus

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from .billing import BillingReconciler, InvalidBillingRecord, normalize_record, validate_manifest
from .ledger import DynamoLedger

_REQUIRED_CUR_COLUMNS = (
    "identity_line_item_id",
    "bill_billing_period_start_date",
    "line_item_usage_start_date",
    "line_item_usage_end_date",
    "product_product_code",
    "line_item_line_item_type",
)
_OPTIONAL_CUR_COLUMNS = (
    "line_item_usage_account_id",
    "line_item_usage_type",
    "line_item_operation",
    "line_item_resource_id",
    "line_item_currency_code",
    "line_item_unblended_cost",
    "line_item_net_unblended_cost",
    "resource_tags_user_reservation_id",
    "resource_tags_user_team",
)


def billing_manifest_handler(event, context):
    """Reconcile only a validated manifest after the safety delay and opt-in."""
    s3 = boto3.client("s3", region_name=_region())
    athena = boto3.client("athena", region_name=_region())
    manifest_results = []
    for record in event.get("Records", []):
        bucket = record.get("s3", {}).get("bucket", {}).get("name")
        key = unquote_plus(record.get("s3", {}).get("object", {}).get("key", ""))
        if bucket != _required("CUR_EXPORT_BUCKET") or not key.lower().endswith("manifest.json"):
            continue
        if not key.startswith(_required("CUR_EXPORT_PREFIX")):
            continue
        if ".." in key.split("/"):
            raise InvalidBillingRecord("invalid S3 manifest key")
        try:
            response = s3.get_object(Bucket=bucket, Key=key)
        except (BotoCoreError, ClientError) as error:
            _emit_event(
                "BillingIngestionFailed", {"object_key": key, "reason": type(error).__name__}
            )
            raise
        try:
            manifest = json.loads(response["Body"].read())
            period, revision, revision_order = validate_manifest(manifest)
        except (InvalidBillingRecord, json.JSONDecodeError, UnicodeDecodeError) as error:
            _emit_event(
                "BillingSchemaInvalid",
                {"object_key": key, "reason": type(error).__name__},
            )
            raise
        if not _enabled() or not _old_enough(period):
            manifest_results.append({"state": "DEFERRED", "period": period, "revision": revision})
            continue
        rows = _query_rows(athena, period)
        object_id = f"s3://{bucket}/{key}#{revision}"
        normalized = [normalize_record(row, object_id=object_id) for row in rows]
        ledger = _ledger()
        result = BillingReconciler(ledger, emit=partial(_emit_event, ledger=ledger)).reconcile(
            normalized,
            billing_period=period,
            source_revision=revision,
            source_revision_order=revision_order,
            complete=True,
        )
        _metric("BillingRecordsProcessed", result.processed)
        _metric("BillingRecordsUnattributed", result.unattributed)
        _metric("BillingRecordsExcluded", result.excluded)
        _metric("BillingDuplicateRecords", result.duplicates)
        _metric(
            "BillingAdjustmentMicroUsd",
            sum(int(row.get("adjustment_micro") or 0) for row in result.adjustments),
            unit="None",
        )
        candidates = result.processed + result.unattributed
        _metric(
            "BillingAttributionRate",
            result.processed / candidates if candidates else 0,
            unit="None",
        )
        manifest_results.append(
            {"state": "RECONCILED", "period": period, "revision": revision, **result.__dict__}
        )
    return {"state": "OK", "manifests": manifest_results}


def _query_rows(client, period: str) -> list[dict]:
    table = _required("CUR_TABLE")
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", table):
        raise ValueError("CUR_TABLE must be a Glue table name in spendctl_billing")
    database, table_name = "spendctl_billing", table
    try:
        glue_table = boto3.client("glue", region_name=_region()).get_table(
            DatabaseName=database, Name=table_name
        )["Table"]
    except (BotoCoreError, ClientError) as error:
        _emit_event("BillingIngestionFailed", {"table": table_name, "reason": type(error).__name__})
        raise
    available = {
        str(column["Name"]).lower()
        for column in glue_table.get("StorageDescriptor", {}).get("Columns", [])
    }
    missing = set(_REQUIRED_CUR_COLUMNS) - available
    if not ({"line_item_unblended_cost", "line_item_net_unblended_cost"} & available):
        missing.add("line_item_unblended_cost or line_item_net_unblended_cost")
    if missing:
        _emit_event("BillingSchemaInvalid", {"missing_columns": sorted(missing)})
        raise InvalidBillingRecord(f"CUR table missing required columns: {sorted(missing)}")
    selected = [name for name in _REQUIRED_CUR_COLUMNS + _OPTIONAL_CUR_COLUMNS if name in available]
    columns = ", ".join(f'"{column}"' for column in selected)
    sql_table = f'"{database}"."{table_name}"'
    predicates = [
        f"date_format(CAST(bill_billing_period_start_date AS timestamp), '%Y-%m') = '{period}'"
    ]
    partition_keys = {
        str(key["Name"]).lower()
        for key in glue_table.get("PartitionKeys", [])
        if re.fullmatch(r"[a-z_][a-z0-9_]*", str(key.get("Name", "")).lower())
    }
    if "bill_billing_period" in partition_keys:
        predicates.append(f"\"bill_billing_period\" = '{period}'")
    elif "billing_period" in partition_keys:
        predicates.append(f"\"billing_period\" = '{period}'")
    elif {"year", "month"}.issubset(partition_keys):
        year, month = period.split("-")
        predicates.extend([f"\"year\" = '{year}'", f"\"month\" = '{month}'"])
    sql = f"SELECT {columns} FROM {sql_table} WHERE " + " AND ".join(predicates)
    try:
        response = client.start_query_execution(
            QueryString=sql,
            WorkGroup=_required("ATHENA_WORKGROUP"),
        )
    except (BotoCoreError, ClientError) as error:
        _emit_event("BillingIngestionFailed", {"period": period, "reason": type(error).__name__})
        raise
    query_id = response["QueryExecutionId"]
    deadline = time.monotonic() + int(os.environ.get("ATHENA_QUERY_TIMEOUT_SECONDS", "240"))
    while time.monotonic() < deadline:
        state = client.get_query_execution(QueryExecutionId=query_id)["QueryExecution"]["Status"][
            "State"
        ]
        if state == "SUCCEEDED":
            break
        if state in {"FAILED", "CANCELLED"}:
            reason = client.get_query_execution(QueryExecutionId=query_id)["QueryExecution"][
                "Status"
            ].get("StateChangeReason", "unknown")
            _emit_event("BillingIngestionFailed", {"query_id": query_id, "reason": reason[:300]})
            raise RuntimeError(f"Athena query {state}: {reason}")
        time.sleep(2)
    else:
        raise TimeoutError(f"Athena query did not complete: {query_id}")
    pages = client.get_paginator("get_query_results").paginate(QueryExecutionId=query_id)
    rows = []
    names = None
    for page in pages:
        result_set = page["ResultSet"]
        names = names or [
            column["Name"] for column in result_set["ResultSetMetadata"]["ColumnInfo"]
        ]
        for row in result_set["Rows"]:
            values = [cell.get("VarCharValue") for cell in row["Data"]]
            if values == names:
                continue
            values += [None] * (len(names) - len(values))
            rows.append(dict(zip(names, values, strict=True)))
    if (
        names is None
        or any(name not in names for name in _REQUIRED_CUR_COLUMNS)
        or not ({"line_item_unblended_cost", "line_item_net_unblended_cost"} & set(names))
    ):
        _emit_event("BillingSchemaInvalid", {"columns": names or []})
        raise InvalidBillingRecord("Athena result does not match configured CUR 2.0 fields")
    return rows


def _old_enough(period: str) -> bool:
    year, month = map(int, period.split("-"))
    period_end_month = month + 1
    if period_end_month == 13:
        year, period_end_month = year + 1, 1
    period_end = datetime(year, period_end_month, 1, tzinfo=UTC)
    return (datetime.now(UTC) - period_end).days >= int(
        os.environ.get("CUR_FINALIZATION_DELAY_DAYS", "60")
    )


def _enabled() -> bool:
    return os.environ.get("BILLING_RECONCILIATION_ENABLED", "false").lower() == "true"


def _ledger() -> DynamoLedger:
    dynamodb = boto3.client("dynamodb", region_name=_region())
    return DynamoLedger(
        dynamodb,
        _required("BUDGETS_TABLE"),
        _required("RESERVATIONS_TABLE"),
        _required("ALERTS_TABLE"),
    )


def _emit_event(name: str, detail: dict, *, ledger=None) -> None:
    # Stable alert/event keys are kept in the details for downstream dedupe.
    event_id = (
        detail.get("settlement_id")
        or detail.get("reconciliation_id")
        or detail.get("record_id")
        or detail.get("query_id")
        or hashlib.sha256(json.dumps(detail, sort_keys=True, default=str).encode()).hexdigest()
    )
    print(json.dumps({"event": name, "event_id": event_id, **detail}, sort_keys=True, default=str))
    alert_types = {
        "BillingAttributionAmbiguous",
        "BillingSchemaInvalid",
        "BillingIngestionFailed",
        "BillingLedgerConflict",
    }
    if name in alert_types:
        _metric("BillingReconciliationFailures", 1)
        ledger = ledger or _ledger()
        alert_id = f"BILLING#{name}#{hashlib.sha256(str(event_id).encode()).hexdigest()}"
        ledger.record_alert(alert_id=alert_id, alert_type=name, details=detail)
    if name == "BillingStaleRevision":
        _metric("BillingStaleRevisions", 1)
    bus = os.environ.get("EVENT_BUS_NAME")
    if bus:
        response = boto3.client("events", region_name=_region()).put_events(
            Entries=[
                {
                    "EventBusName": bus,
                    "Source": "spendctl.billing",
                    "DetailType": name,
                    "Detail": json.dumps(
                        {
                            "event_id": event_id,
                            "schema_version": 1,
                            "occurred_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                            **detail,
                        },
                        sort_keys=True,
                        default=str,
                    ),
                }
            ]
        )
        if response.get("FailedEntryCount", 0):
            raise RuntimeError(f"failed to publish {name}")


def _metric(name: str, value: float, *, unit: str = "Count") -> None:
    print(
        json.dumps(
            {
                "_aws": {
                    "Timestamp": int(time.time() * 1000),
                    "CloudWatchMetrics": [
                        {
                            "Namespace": "Spendctl/Billing",
                            "Dimensions": [[]],
                            "Metrics": [{"Name": name, "Unit": unit}],
                        }
                    ],
                },
                name: value,
            },
            sort_keys=True,
        )
    )


def _required(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise ValueError(f"missing environment setting {name}")
    return value


def _region() -> str:
    return os.environ.get("AWS_REGION", "ap-south-1")
