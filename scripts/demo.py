"""Offline demo of request validation and policy default; does not launch cloud resources."""

from spendctl.calibration import MarginPolicies
from spendctl.validation import validate_request

body = {
    "team": "alpha",
    "resource": {"type": "ec2", "instance_type": "t3.micro", "ebs_gb": 8},
    "duration_hours": 4,
    "purpose": "demo",
}
req = validate_request(body)
print(
    f"Validated demo request: team={req.team}, type={req.instance_type}, gp3={req.ebs_gb}GB, hours={req.duration_hours}"
)
print(f"Fixed-policy default reserve multiplier: {MarginPolicies().get('ec2'):.2f}")
print("Offline only: this demo does not authenticate, reserve a budget, or launch EC2.")
