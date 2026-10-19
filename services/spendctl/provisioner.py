"""M6 EC2 provisioning operations; every cloud client is injected or created in the handler."""

from __future__ import annotations

import os
from typing import Any

from botocore.exceptions import BotoCoreError, ClientError

from .observability import emit_emf, structured_log

APPROVED_INSTANCE_TYPES = {"t3.micro", "t3.small"}
REGION = "ap-south-1"
MANAGED_BY = "spend-controller"
PROJECT_TAGS = {"project": "spendctl", "env": "dev"}


class ProvisioningInputError(ValueError):
    """The reserved launch payload or required network configuration is invalid."""


class OwnershipVerificationError(RuntimeError):
    """A discovered resource could not be proven to belong to this reservation."""


def launch_instance(
    event: dict,
    *,
    ec2: Any,
    ami_id: str,
    private_subnet_id: str,
    security_group_id: str,
    failure_injection_token: str | None = None,
    failure_injection_enabled: bool = False,
) -> dict:
    """Launch exactly one private, encrypted gp3 instance with a stable client token."""
    reservation_id, team, instance_type, ebs_gb = _launch_fields(event)
    if not ami_id or not private_subnet_id or not security_group_id:
        raise ProvisioningInputError(
            "approved AMI, private subnet, and security group are required"
        )
    if failure_injection_enabled and (
        event.get("failure_injection") is True
        or (
            failure_injection_token
            and event.get("failure_injection_token") == failure_injection_token
        )
    ):
        return {
            "reservation_id": reservation_id,
            "state": "INJECTED_PRELAUNCH_FAILURE",
            "failure_reason": "controlled failure before any EC2 API call",
            "instance_id": None,
            "volume_ids": [],
        }

    request_tags = {
        **PROJECT_TAGS,
        "managed-by": MANAGED_BY,
        "reservation-id": reservation_id,
        "team": team,
    }
    tag_specifications = [
        {
            "ResourceType": resource_type,
            "Tags": [{"Key": key, "Value": value} for key, value in request_tags.items()],
        }
        for resource_type in ("instance", "volume")
    ]
    response = ec2.run_instances(
        ImageId=ami_id,
        InstanceType=instance_type,
        MinCount=1,
        MaxCount=1,
        ClientToken=reservation_id,
        NetworkInterfaces=[
            {
                "DeviceIndex": 0,
                "SubnetId": private_subnet_id,
                "Groups": [security_group_id],
                "AssociatePublicIpAddress": False,
                "DeleteOnTermination": True,
            }
        ],
        BlockDeviceMappings=[
            {
                "DeviceName": "/dev/xvda",
                "Ebs": {
                    "VolumeSize": ebs_gb,
                    "VolumeType": "gp3",
                    "Encrypted": True,
                    "DeleteOnTermination": True,
                },
            }
        ],
        TagSpecifications=tag_specifications,
        MetadataOptions={
            "HttpEndpoint": "enabled",
            "HttpTokens": "required",
            "HttpPutResponseHopLimit": 1,
        },
    )
    instances = response.get("Instances", [])
    if len(instances) != 1 or not instances[0].get("InstanceId"):
        raise RuntimeError("EC2 did not return exactly one instance ID")
    instance = instances[0]
    volume_ids = sorted(
        item["Ebs"]["VolumeId"]
        for item in instance.get("BlockDeviceMappings", [])
        if item.get("Ebs", {}).get("VolumeId")
    )
    return {
        "reservation_id": reservation_id,
        "instance_id": instance["InstanceId"],
        "volume_ids": volume_ids,
        "state": instance.get("State", {}).get("Name", "pending"),
    }


def poll_instance(instance_id: str, *, ec2: Any) -> dict:
    response = ec2.describe_instances(InstanceIds=[instance_id])
    instances = [
        instance
        for row in response.get("Reservations", [])
        for instance in row.get("Instances", [])
    ]
    if len(instances) != 1:
        return {"state": "unknown", "running": False}
    state = instances[0].get("State", {}).get("Name", "unknown")
    return {"state": state, "running": state == "running"}


def cleanup_reservation(event: dict, *, ec2: Any) -> dict:
    """Idempotently terminate only instances and volumes with matching owner tags."""
    reservation_id, team = _reservation_identity(event)
    filters = [
        {"Name": "tag:managed-by", "Values": [MANAGED_BY]},
        {"Name": "tag:reservation-id", "Values": [reservation_id]},
        {"Name": "tag:team", "Values": [team]},
        {"Name": "tag:project", "Values": [PROJECT_TAGS["project"]]},
        {"Name": "tag:env", "Values": [PROJECT_TAGS["env"]]},
    ]
    described = ec2.describe_instances(Filters=filters)
    instances = [
        instance
        for row in described.get("Reservations", [])
        for instance in row.get("Instances", [])
    ]
    active_ids = sorted(
        instance["InstanceId"]
        for instance in instances
        if instance.get("State", {}).get("Name") not in {"terminated", "shutting-down"}
    )
    if active_ids:
        ec2.terminate_instances(InstanceIds=active_ids)

    volumes_response = ec2.describe_volumes(Filters=filters)
    volumes = volumes_response.get("Volumes", [])
    for volume in volumes:
        if volume.get("State") == "available":
            ec2.delete_volume(VolumeId=volume["VolumeId"])
    pending_instances = any(
        instance.get("State", {}).get("Name") not in {"terminated"} for instance in instances
    )
    pending_volumes = any(volume.get("State") != "deleted" for volume in volumes)
    found = bool(instances or volumes)
    return {
        "reservation_id": reservation_id,
        "had_resources": found,
        "cleanup_state": "PENDING" if pending_instances or pending_volumes else "COMPLETE",
        "instance_ids": sorted(instance["InstanceId"] for instance in instances),
        "volume_ids": sorted(volume["VolumeId"] for volume in volumes),
    }


def terminate_expected_resources(event: dict, *, ec2: Any) -> dict:
    """Terminate only the ledger-bound instance and delete only its tagged volumes."""
    reservation_id, team = _reservation_identity(event)
    instance_id = event.get("instance_id")
    if not isinstance(instance_id, str) or not instance_id.startswith("i-"):
        return {"cleanup_state": "UNKNOWN", "reason": "ledger instance ID is missing"}

    response = ec2.describe_instances(InstanceIds=[instance_id])
    instances = [
        instance
        for row in response.get("Reservations", [])
        for instance in row.get("Instances", [])
    ]
    if not instances:
        if event.get("instance_state") == "terminated" and event.get("terminated_at"):
            instance_terminated = True
        else:
            return {"cleanup_state": "UNKNOWN", "reason": "instance state is not observable"}
    else:
        instance = instances[0]
        _verify_ownership(instance.get("Tags", []), reservation_id, team)
        state = instance.get("State", {}).get("Name", "unknown")
        if state not in {"terminated", "shutting-down"}:
            ec2.terminate_instances(InstanceIds=[instance_id])
            return {"cleanup_state": "PENDING", "instance_id": instance_id}
        instance_terminated = state == "terminated"
        if not instance_terminated:
            return {"cleanup_state": "PENDING", "instance_id": instance_id}

    volume_ids = event.get("volume_ids") or []
    for volume_id in sorted(set(volume_ids)):
        if not isinstance(volume_id, str) or not volume_id.startswith("vol-"):
            raise OwnershipVerificationError("ledger contains an invalid volume ID")
        try:
            volume_response = ec2.describe_volumes(VolumeIds=[volume_id])
        except ClientError as error:
            code = getattr(error, "response", {}).get("Error", {}).get("Code")
            if code == "InvalidVolume.NotFound":
                continue
            raise
        volumes = volume_response.get("Volumes", [])
        if not volumes:
            return {"cleanup_state": "UNKNOWN", "reason": "volume state is not observable"}
        volume = volumes[0]
        _verify_ownership(volume.get("Tags", []), reservation_id, team)
        attachments = volume.get("Attachments", [])
        if any(attachment.get("InstanceId") != instance_id for attachment in attachments):
            raise OwnershipVerificationError("volume is attached to an unexpected instance")
        if volume.get("State") == "available":
            ec2.delete_volume(VolumeId=volume_id)
            return {"cleanup_state": "PENDING", "instance_id": instance_id}
        if volume.get("State") != "deleted":
            return {"cleanup_state": "PENDING", "instance_id": instance_id}

    return {"cleanup_state": "COMPLETE", "instance_id": instance_id}


def inspect_expected_cleanup(event: dict, *, ec2: Any) -> dict:
    """Read-only cleanup plan used by lifecycle dry-run requests."""
    reservation_id, team = _reservation_identity(event)
    instance_id = event.get("instance_id")
    if not isinstance(instance_id, str) or not instance_id.startswith("i-"):
        return {"state": "UNKNOWN", "reason": "ledger instance ID is missing"}
    response = ec2.describe_instances(InstanceIds=[instance_id])
    instances = [i for group in response.get("Reservations", []) for i in group.get("Instances", [])]
    if not instances:
        if event.get("instance_state") != "terminated" or not event.get("terminated_at"):
            return {"state": "UNKNOWN", "reason": "instance state is not observable"}
        instance_state = "terminated"
    else:
        instance = instances[0]
        _verify_ownership(instance.get("Tags", []), reservation_id, team)
        instance_state = instance.get("State", {}).get("Name", "unknown")
    to_delete = []
    for volume_id in sorted(set(event.get("volume_ids") or [])):
        try:
            volume_response = ec2.describe_volumes(VolumeIds=[volume_id])
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") == "InvalidVolume.NotFound":
                continue
            raise
        volumes = volume_response.get("Volumes", [])
        if not volumes:
            return {"state": "UNKNOWN", "reason": "volume state is not observable"}
        volume = volumes[0]
        _verify_ownership(volume.get("Tags", []), reservation_id, team)
        if any(a.get("InstanceId") != instance_id for a in volume.get("Attachments", [])):
            raise OwnershipVerificationError("volume is attached to an unexpected instance")
        if volume.get("State") == "available":
            to_delete.append(volume_id)
    return {
        "state": "DRY_RUN",
        "instance_id": instance_id,
        "instance_state": instance_state,
        "would_terminate": instance_state not in {"terminated", "shutting-down"},
        "would_delete_volumes": to_delete,
    }


def find_reservation_resources(event: dict, *, ec2: Any) -> dict:
    """Read-only tag-scoped lookup for uncertain-start reconciliation."""
    reservation_id, team = _reservation_identity(event)
    filters = [
        {"Name": "tag:managed-by", "Values": [MANAGED_BY]},
        {"Name": "tag:reservation-id", "Values": [reservation_id]},
        {"Name": "tag:team", "Values": [team]},
        {"Name": "tag:project", "Values": [PROJECT_TAGS["project"]]},
        {"Name": "tag:env", "Values": [PROJECT_TAGS["env"]]},
    ]
    instances = _describe_pages(ec2.describe_instances, "Reservations", Filters=filters)
    volumes = _describe_pages(ec2.describe_volumes, "Volumes", Filters=filters)
    flattened_instances = [
        instance for row in instances for instance in row.get("Instances", [])
    ]
    return {"instances": flattened_instances, "volumes": volumes}


def _describe_pages(call, result_key: str, **kwargs) -> list[dict]:
    output = []
    token = None
    while True:
        request = dict(kwargs)
        if token:
            request["NextToken"] = token
        page = call(**request)
        output.extend(page.get(result_key, []))
        token = page.get("NextToken")
        if not token:
            return output


def _verify_ownership(tags: list[dict], reservation_id: str, team: str) -> None:
    values = {tag.get("Key"): tag.get("Value") for tag in tags}
    expected = {
        "managed-by": MANAGED_BY,
        "reservation-id": reservation_id,
        "team": team,
        "project": PROJECT_TAGS["project"],
        "env": PROJECT_TAGS["env"],
    }
    if any(values.get(key) != value for key, value in expected.items()):
        raise OwnershipVerificationError("resource ownership tags do not match reservation")


def provision_handler(event, context):
    """Step Functions Lambda entry point; assumes a reservation-tagged role session."""
    import boto3

    op = event.get("operation")
    region = event.get("region", REGION)
    if region != REGION:
        raise ProvisioningInputError("provisioning is restricted to ap-south-1")
    try:
        credentials = _assume_provisioner_role(event, boto3)
        ec2 = boto3.client("ec2", region_name=REGION, **credentials)
        if op == "launch":
            result = launch_instance(
                event,
                ec2=ec2,
                ami_id=os.environ.get("APPROVED_AMI_ID", ""),
                private_subnet_id=os.environ.get("PRIVATE_SUBNET_ID", ""),
                security_group_id=os.environ.get("SECURITY_GROUP_ID", ""),
                failure_injection_enabled=os.environ.get("SPENDCTL_FAILURE_INJECTION_ENABLED")
                == "true",
                failure_injection_token=os.environ.get("SPENDCTL_FAILURE_INJECTION_TOKEN"),
            )
            emit_emf("Spendctl/Workflow", {"EC2LaunchSuccesses": (1, "Count")}, dimensions={"result": "success"})
            structured_log("M6", "ec2_launch", "success", reservation_id=event.get("reservation_id"),
                           instance_id=result.get("instance_id"))
            return result
        if op == "poll":
            return poll_instance(event["instance_id"], ec2=ec2)
        if op == "cleanup":
            return cleanup_reservation(event, ec2=ec2)
        raise ProvisioningInputError("unsupported provisioning operation")
    except (BotoCoreError, ClientError, TimeoutError, ConnectionError) as error:
        if op == "launch":
            emit_emf("Spendctl/Workflow", {"EC2LaunchFailures": (1, "Count")}, dimensions={"result": "failure"})
        structured_log("M6", f"{op or 'unknown'}_provisioning", "failure",
                       reservation_id=event.get("reservation_id"), error_category=type(error).__name__)
        raise


def _assume_provisioner_role(event: dict, boto3_module: Any) -> dict:
    reservation_id, team = _reservation_identity(event)
    role_arn = os.environ.get("PROVISIONER_ROLE_ARN")
    if not role_arn:
        raise ProvisioningInputError("PROVISIONER_ROLE_ARN is not configured")
    response = boto3_module.client("sts", region_name=REGION).assume_role(
        RoleArn=role_arn,
        RoleSessionName=f"spendctl-{reservation_id[-32:]}",
        Tags=[
            {"Key": "reservation-id", "Value": reservation_id},
            {"Key": "team", "Value": team},
        ],
    )
    return {
        "aws_access_key_id": response["Credentials"]["AccessKeyId"],
        "aws_secret_access_key": response["Credentials"]["SecretAccessKey"],
        "aws_session_token": response["Credentials"]["SessionToken"],
    }


def _reservation_identity(event: dict) -> tuple[str, str]:
    reservation_id = event.get("reservation_id")
    team = event.get("team")
    if not isinstance(reservation_id, str) or not reservation_id.startswith("rsv_"):
        raise ProvisioningInputError("valid reservation_id is required")
    if not isinstance(team, str) or not team or len(team) > 64:
        raise ProvisioningInputError("valid team is required")
    return reservation_id, team


def _launch_fields(event: dict) -> tuple[str, str, str, int]:
    reservation_id, team = _reservation_identity(event)
    resource = event.get("resource")
    if not isinstance(resource, dict):
        raise ProvisioningInputError("resource must be an object")
    instance_type = resource.get("instance_type")
    ebs_gb = resource.get("ebs_gb")
    if instance_type not in APPROVED_INSTANCE_TYPES:
        raise ProvisioningInputError("instance type is not approved")
    if isinstance(ebs_gb, bool) or not isinstance(ebs_gb, int) or not 1 <= ebs_gb <= 30:
        raise ProvisioningInputError("gp3 volume size must be between 1 and 30 GB")
    if isinstance(event.get("planned_hours"), bool) or not isinstance(
        event.get("planned_hours"), int
    ):
        raise ProvisioningInputError("planned_hours must be an integer")
    if not 1 <= event["planned_hours"] <= 24:
        raise ProvisioningInputError("planned_hours must be between 1 and 24")
    if event.get("region", REGION) != REGION:
        raise ProvisioningInputError("provisioning is restricted to ap-south-1")
    return reservation_id, team, instance_type, ebs_gb
