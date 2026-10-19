import json

import pytest
from spendctl.observability import emit_emf, structured_log


def test_emf_uses_only_bounded_dimensions(capsys):
    emit_emf("Spendctl/Test", {"Count": (1, "Count")}, dimensions={"result": "success"})
    body = json.loads(capsys.readouterr().out)
    assert body["result"] == "success"
    assert body["_aws"]["CloudWatchMetrics"][0]["Dimensions"] == [["result"]]


def test_emf_rejects_high_cardinality_identifiers(capsys):
    with pytest.raises(ValueError, match="dimension"):
        emit_emf("Spendctl/Test", {"Count": (1, "Count")}, dimensions={"reservation_id": "rsv-1"})
    assert capsys.readouterr().out == ""


def test_structured_log_has_context_and_no_metric_dimensions(capsys):
    structured_log("M6", "start_execution", "unknown", reservation_id="rsv-1")
    body = json.loads(capsys.readouterr().out)
    assert body["module"] == "M6"
    assert body["reservation_id"] == "rsv-1"
    assert "_aws" not in body
