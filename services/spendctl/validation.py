from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Request:
    team: str
    instance_type: str
    ebs_gb: int
    duration_hours: int
    purpose: str


def validate_request(payload: dict) -> Request:
    if not isinstance(payload, dict):
        raise TypeError("request must be an object")
    if "resource" not in payload:
        raise ValueError("request must contain a resource object")
    if not isinstance(payload["resource"], dict):
        raise TypeError("resource must be an object")
    team = payload.get("team")
    resource = payload["resource"]
    itype, gb, hours = (
        resource.get("instance_type"),
        resource.get("ebs_gb"),
        payload.get("duration_hours"),
    )
    if not isinstance(team, str):
        raise TypeError("team must be a string")
    resource_type = resource.get("type")
    if not isinstance(resource_type, str):
        raise TypeError("resource type must be a string")
    if not team or resource_type != "ec2":
        raise ValueError("invalid team or resource type")
    if not isinstance(itype, str):
        raise TypeError("instance_type must be a string")
    if itype not in {"t3.micro", "t3.small"}:
        raise ValueError("instance type is not allowed")
    if type(gb) is not int:
        raise TypeError("ebs_gb must be an integer")
    if not 1 <= gb <= 30:
        raise ValueError("ebs_gb must be an integer from 1 to 30")
    if type(hours) is not int:
        raise TypeError("duration_hours must be an integer")
    if not 1 <= hours <= 24:
        raise ValueError("duration_hours must be an integer from 1 to 24")
    purpose = payload.get("purpose", "")
    if not isinstance(purpose, str):
        raise TypeError("purpose must be a string")
    if len(purpose) > 256:
        raise ValueError("purpose must be a string of at most 256 characters")
    return Request(team, itype, gb, hours, purpose)
