"""Offline, non-mutating contract smoke checks."""

from spendctl.calibration import MarginPolicies
from spendctl.validation import validate_request

sample = {
    "team": "alpha",
    "resource": {"type": "ec2", "instance_type": "t3.micro", "ebs_gb": 8},
    "duration_hours": 4,
    "purpose": "demo",
}
assert validate_request(sample).ebs_gb == 8
assert MarginPolicies().get("ec2") == 1.10
print("Local smoke checks passed; no AWS APIs were called.")
