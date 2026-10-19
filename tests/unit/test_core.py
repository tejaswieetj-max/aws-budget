import builtins
import importlib.util
from decimal import Decimal
from pathlib import Path

import pytest
from spendctl.calibration import MarginPolicies, Sample
from spendctl.money import multiply_ceil, usd_to_micro
from spendctl.pricing import demo_estimate, estimate
from spendctl.validation import validate_request
from spendctl.workflow_start import (
    ClientError,
    EndpointConnectionError,
    execution_name,
    recover_held_reservation,
    start_or_reconcile,
)


def test_integer_money_rounds_up():
    assert usd_to_micro("0.0000001") == 1
    assert multiply_ceil(1, "1.10") == 2


def test_gp3_contract_1_to_30_and_demo_default_independent():
    body = {
        "team": "alpha",
        "resource": {"type": "ec2", "instance_type": "t3.micro", "ebs_gb": 1},
        "duration_hours": 4,
    }
    assert validate_request(body).ebs_gb == 1
    body["resource"]["ebs_gb"] = 30
    assert validate_request(body).ebs_gb == 30
    body["resource"]["ebs_gb"] = 31
    with pytest.raises(ValueError):
        validate_request(body)


@pytest.mark.parametrize(
    "body",
    [
        [],
        {"team": "alpha", "resource": None},
        {"team": 7, "resource": {"type": "ec2"}},
        {"team": "alpha", "resource": {"type": 42}},
        {"team": "alpha", "resource": {"type": "ec2", "instance_type": 2}},
        {
            "team": "alpha",
            "resource": {"type": "ec2", "instance_type": "t3.micro", "ebs_gb": "8"},
            "duration_hours": 4,
        },
        {
            "team": "alpha",
            "resource": {"type": "ec2", "instance_type": "t3.micro", "ebs_gb": 8},
            "duration_hours": "4",
        },
    ],
)
def test_invalid_request_types_raise_type_error(body):
    with pytest.raises(TypeError):
        validate_request(body)


def _sample(id, order, miss):
    return Sample(
        id, 1.4 if miss else 1.05, 1_100_000, 1_200_000 if miss else 1_000_000, 1_000_000, order
    )


def test_adaptive_update_deduplicates_and_replays_out_of_order():
    a, b = MarginPolicies(0.02), MarginPolicies(0.02)
    a.add_settlement("ec2", _sample("x", 20, True))
    a.add_settlement("ec2", _sample("y", 10, False))
    b.add_settlement("ec2", _sample("y", 10, False))
    b.add_settlement("ec2", _sample("x", 20, True))
    assert a.adaptive["ec2"].alpha == b.adaptive["ec2"].alpha
    assert not a.add_settlement("ec2", _sample("x", 20, True))
    assert a.get("ec2", "adaptive") <= 2


def test_gamma_bounds():
    with pytest.raises(ValueError):
        MarginPolicies(0.06)


def test_split_conformal_requires_frozen_calibration_partition():
    p = MarginPolicies()
    assert p.get("ec2", "split_conformal") == 1.10
    p.set_split_calibration("ec2", [1.1] * 40)
    assert p.get("ec2", "split_conformal") == 1.1


def test_execution_name_and_ambiguous_recovery_decision():
    assert execution_name("rsv_123").startswith("spendctl-rsv_123-")
    assert recover_held_reservation({"id": "r"}, None, lambda _: []) == "RETRY_SAME_EXECUTION_NAME"


class FakeStepFunctions:
    def __init__(self, start_error=None, list_error=None, executions=None, description=None):
        self.start_error = start_error
        self.list_error = list_error
        self.executions = executions or []
        self.description = description or {}
        self.calls = []

    def start_execution(self, **kwargs):
        self.calls.append(("start_execution", kwargs))
        if self.start_error:
            raise self.start_error
        return {"executionArn": "arn:execution:new"}

    def list_executions(self, **kwargs):
        self.calls.append(("list_executions", kwargs))
        if self.list_error:
            raise self.list_error
        return {"executions": self.executions}

    def describe_execution(self, **kwargs):
        self.calls.append(("describe_execution", kwargs))
        return self.description


def _client_error(code):
    return ClientError({"Error": {"Code": code, "Message": code}}, "StartExecution")


def test_successful_start_does_not_reconcile():
    client = FakeStepFunctions()
    result = start_or_reconcile(client, "arn:sm", "r-1", {"reservation_id": "r-1"})
    assert result == {"state": "STARTED", "executionArn": "arn:execution:new"}
    assert [call[0] for call in client.calls] == ["start_execution"]


def test_timeout_with_no_visible_execution_remains_unknown_and_held():
    client = FakeStepFunctions(start_error=TimeoutError("read timed out"))
    result = start_or_reconcile(client, "arn:sm", "r-timeout", {"reservation_id": "r-timeout"})
    assert result["state"] == "START_UNKNOWN"
    assert result["recovery_required"] is True
    assert [call[0] for call in client.calls] == ["start_execution", "list_executions"]
    assert (
        recover_held_reservation({"id": "r-timeout"}, None, lambda _: [])
        == "RETRY_SAME_EXECUTION_NAME"
    )


def test_timeout_with_list_failure_remains_unknown():
    client = FakeStepFunctions(
        start_error=EndpointConnectionError(endpoint_url="https://start.example"),
        list_error=EndpointConnectionError(endpoint_url="https://reconcile.example"),
    )
    result = start_or_reconcile(client, "arn:sm", "r-timeout", {"reservation_id": "r-timeout"})
    assert result["state"] == "START_UNKNOWN"
    assert result["recovery_required"] is True


def test_fallback_botocore_exceptions_preserve_message_and_attributes(monkeypatch):
    original_import = builtins.__import__

    def without_botocore(name, *args, **kwargs):
        if name == "botocore.exceptions":
            raise ModuleNotFoundError("botocore unavailable for fallback test", name="botocore")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", without_botocore)
    module_path = Path(__file__).parents[2] / "services" / "spendctl" / "workflow_start.py"
    spec = importlib.util.spec_from_file_location("workflow_start_fallback_test", module_path)
    fallback = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fallback)

    error = fallback.EndpointConnectionError(
        "start transport error", endpoint_url="https://example"
    )
    assert str(error) == "start transport error"
    assert error.args == ("start transport error",)
    assert error.message == "start transport error"
    assert error.endpoint_url == "https://example"


def test_fallback_does_not_mask_a_missing_botocore_dependency(monkeypatch):
    original_import = builtins.__import__

    def missing_dependency(name, *args, **kwargs):
        if name == "botocore.exceptions":
            raise ModuleNotFoundError("botocore internal dependency missing", name="urllib3")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", missing_dependency)
    module_path = Path(__file__).parents[2] / "services" / "spendctl" / "workflow_start.py"
    spec = importlib.util.spec_from_file_location(
        "workflow_start_missing_dependency_test", module_path
    )
    module = importlib.util.module_from_spec(spec)
    with pytest.raises(ModuleNotFoundError, match="internal dependency"):
        spec.loader.exec_module(module)


def test_duplicate_execution_name_reconciles_matching_input_and_status():
    payload = {"reservation_id": "r-duplicate"}
    name = execution_name("r-duplicate")
    client = FakeStepFunctions(
        start_error=_client_error("ExecutionAlreadyExists"),
        executions=[{"name": name, "executionArn": "arn:execution:existing", "status": "RUNNING"}],
        description={"input": '{"reservation_id": "r-duplicate"}', "status": "RUNNING"},
    )
    result = start_or_reconcile(client, "arn:sm", "r-duplicate", payload)
    assert result == {
        "state": "RUNNING",
        "executionArn": "arn:execution:existing",
        "recovery_required": False,
    }


def test_duplicate_name_with_different_input_needs_manual_reconciliation():
    name = execution_name("r-collision")
    client = FakeStepFunctions(
        start_error=_client_error("ExecutionAlreadyExists"),
        executions=[{"name": name, "executionArn": "arn:execution:existing", "status": "RUNNING"}],
        description={"input": '{"reservation_id": "other"}', "status": "RUNNING"},
    )
    result = start_or_reconcile(client, "arn:sm", "r-collision", {"reservation_id": "r-collision"})
    assert result["state"] == "EXECUTION_NAME_CONFLICT"
    assert (
        recover_held_reservation({"id": "r-collision"}, result, lambda _: [])
        == "MANUAL_RECONCILIATION_REQUIRED"
    )


def test_duplicate_name_not_yet_visible_is_not_released():
    client = FakeStepFunctions(start_error=_client_error("ExecutionAlreadyExists"))
    result = start_or_reconcile(client, "arn:sm", "r-duplicate", {"reservation_id": "r-duplicate"})
    assert result["state"] == "START_UNKNOWN"
    assert result["duplicate_name_unresolved"] is True


@pytest.mark.parametrize(
    ("execution", "resources", "expected"),
    [
        ({"status": "FAILED"}, [], "RELEASE_IF_STILL_HELD"),
        ({"status": "FAILED"}, [{"instance_id": "i-created"}], "CLEANUP_THEN_RECONCILE"),
        ({"state": "START_UNKNOWN"}, [], "RETRY_SAME_EXECUTION_NAME"),
    ],
)
def test_recovery_requires_execution_and_resource_reconciliation(execution, resources, expected):
    result = recover_held_reservation({"id": "r-recover"}, execution, lambda _: resources)
    assert result == expected


def test_unexpected_programming_error_is_not_swallowed():
    client = FakeStepFunctions(start_error=RuntimeError("bug"))
    with pytest.raises(RuntimeError, match="bug"):
        start_or_reconcile(client, "arn:sm", "r-bug", {"reservation_id": "r-bug"})


def test_estimator_uses_730_hours_and_labels_fallback():
    result = estimate(Decimal("0.01"), Decimal("0.08"), 8, 4)
    assert result["total_micro"] == result["compute_micro"] + result["storage_micro"]
    assert result["source"] == "unspecified"
    assert demo_estimate("t3.micro", 8, 4)["source"] == "fixture_unverified"
