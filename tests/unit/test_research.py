from decimal import Decimal

from research.evaluate import (
    Observation,
    atomic_vs_naive_experiment,
    build_dataset,
    calibration_lag_sensitivity,
    estimation_metrics,
    evaluate_margins,
    render_markdown,
)


def _export(actual=1_250_000):
    reservation = {
        "reservation_id": "rsv-a", "status": "SETTLED", "settlement_stage": "FINAL",
        "resource": {"instance_type": "t3.micro", "ebs_gb": 8},
        "planned_hours": 2, "compute_micro": 900_000, "storage_micro": 100_000,
        "margin": "1.10", "reserved_micro": 1_100_000,
        "price_snapshot_id": "snapshot-a", "price_fetched_at": "2026-01-01T00:00:00Z",
    }
    audit = {"record_type": "BILLING_RECONCILIATION", "reservation_id": "rsv-a",
             "billing_period": "2026-01", "source_revision_order": "001",
             "new_actual_micro": actual}
    return {"reservations": [reservation], "billing_audits": [audit]}


def test_export_join_is_by_reservation_id_and_keeps_provenance():
    report = build_dataset(_export(), provenance="aws_export")
    assert report["classification"] == "AWS_EXPORT_ASSERTED_UNVERIFIED"
    assert report["observation_count"] == 1
    row = report["observations"][0]
    assert row["reservation_id"] == "rsv-a"
    assert row["estimated_compute_micro"] == 900_000
    assert row["actual_micro"] == 1_250_000


def test_dataset_excludes_unsettled_missing_and_invalid_configuration():
    data = _export()
    data["reservations"][0]["settlement_stage"] = "PROVISIONAL"
    report = build_dataset(data)
    assert report["observation_count"] == 0
    assert report["exclusion_counts"]["not_finally_reconciled"] == 1
    data = _export()
    data["reservations"][0]["resource"]["instance_type"] = "m7.large"
    assert build_dataset(data)["exclusion_counts"]["unsupported_resource_configuration"] == 1
    data = _export(actual=-1)
    assert build_dataset(data)["exclusion_counts"]["invalid_financial_field:new_actual_micro must be nonnegative"] == 1


def test_metrics_empty_zero_actual_and_deficit_cases_are_explicit():
    assert estimation_metrics([])["mae_micro"] is None
    row = Observation("r", "t3.micro", "ap-south-1", 8, 1, None, 5, 5,
                      Decimal("1.1"), 11, None, 0, "snap", "time", "2026-01", "FINAL", "fixture")
    metrics = estimation_metrics([row])
    assert metrics["mape_percent"] is None
    assert metrics["mape_excluded_zero_or_negative_actual"] == 1
    assert metrics["negative_actual_count"] == 0
    assert metrics["reserve_deficit_count"] == 0


def test_margin_candidates_round_up_and_holdout_is_labelled():
    row = Observation("r", "t3.micro", "ap-south-1", 8, 1, None, 1, 1,
                      Decimal("1.1"), 3, None, 3, "snap", "time", "2026-01", "FINAL", "fixture")
    result = evaluate_margins([row], [Decimal("1.1")])["candidate_margins"][0]
    assert result["total_reserved_micro"] == 3
    assert result["sample_size"] == 1
    assert result["holdout_sample_size"] == 0
    assert result["holdout"].startswith("exploratory")


def test_concurrency_experiment_is_reproducible_and_reports_atomic_safety():
    left = atomic_vs_naive_experiment(seed=42, concurrency=[1, 8], request_count=50,
                                      budget_micro=30_000_000)
    assert left == atomic_vs_naive_experiment(seed=42, concurrency=[1, 8], request_count=50,
                                              budget_micro=30_000_000)
    assert all(row["atomic_invariant_holds"] for row in left)
    assert left[1]["naive_over_admission_micro"] > 0
    assert left[1]["atomic_transaction_conflicts"] is None
    assert not left[1]["naive_invariant_holds"]
    assert left[1]["atomic_reservation_latency_ms"] is None


def test_lag_sensitivity_is_seeded_and_reports_synthetic_status():
    rows = calibration_lag_sensitivity(seed=11, requests=100, lags=[0, 7])
    assert rows == calibration_lag_sensitivity(seed=11, requests=100, lags=[0, 7])
    assert all(row["classification"] == "SYNTHETIC_NOT_AWS_BILLING" for row in rows)


def test_markdown_report_labels_provenance_and_empty_real_observations():
    dataset = build_dataset({"reservations": [], "billing_audits": []}, provenance="aws_export")
    report = {"dataset": dataset, "metrics": estimation_metrics([]),
              "margin_evaluation": evaluate_margins([], [Decimal("1.10")])}
    markdown = render_markdown(report)
    assert "AWS_EXPORT_ASSERTED_UNVERIFIED" in markdown
    assert "No verified real billing observations" in markdown
    assert "recommendations only" in markdown
