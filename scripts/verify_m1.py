"""Human-run M1 IAM boundary check. Every EC2 request has DryRun=True; this script never launches."""

from __future__ import annotations

import os
import uuid

import boto3
from botocore.exceptions import ClientError


def _required(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"set {name} before running this verification")
    return value


def _base_request(reservation_id: str, team: str) -> dict:
    tags = [
        {"Key": "managed-by", "Value": "spend-controller"},
        {"Key": "reservation-id", "Value": reservation_id},
        {"Key": "team", "Value": team},
        {"Key": "project", "Value": "spendctl"},
        {"Key": "env", "Value": "dev"},
    ]
    return {
        "ImageId": _required("SPENDCTL_AMI_ID"),
        "InstanceType": "t3.micro",
        "MinCount": 1,
        "MaxCount": 1,
        "ClientToken": str(uuid.uuid4()),
        "NetworkInterfaces": [
            {
                "DeviceIndex": 0,
                "SubnetId": _required("SPENDCTL_PRIVATE_SUBNET_ID"),
                "Groups": [_required("SPENDCTL_SECURITY_GROUP_ID")],
                "AssociatePublicIpAddress": False,
                "DeleteOnTermination": True,
            }
        ],
        "BlockDeviceMappings": [
            {
                "DeviceName": "/dev/xvda",
                "Ebs": {
                    "VolumeSize": 8,
                    "VolumeType": "gp3",
                    "Encrypted": True,
                    "DeleteOnTermination": True,
                },
            }
        ],
        "MetadataOptions": {
            "HttpEndpoint": "enabled",
            "HttpTokens": "required",
            "HttpPutResponseHopLimit": 1,
        },
        "TagSpecifications": [
            {
                "ResourceType": kind,
                "Tags": [
                    tag
                    for tag in tags
                    if not (tag["Key"] == "reservation-id" and reservation_id == "")
                ],
            }
            for kind in ("instance", "volume")
        ],
        "DryRun": True,
    }


def _expect(ec2, label: str, request: dict, expected: str) -> None:
    try:
        ec2.run_instances(**request)
    except ClientError as error:
        actual = error.response.get("Error", {}).get("Code")
        print(f"{label}: {actual} (expected {expected})")
        if actual != expected:
            raise
    else:
        raise AssertionError(f"{label}: DryRun unexpectedly succeeded without an error")


def main() -> None:
    region = "ap-south-1"
    role_arn = _required("SPENDCTL_PROVISIONER_ROLE_ARN")
    team = _required("SPENDCTL_TEST_TEAM")
    reservation_id = f"rsv_verify_{uuid.uuid4().hex[:16]}"
    sts = boto3.client("sts", region_name=region)
    assumed = sts.assume_role(
        RoleArn=role_arn,
        RoleSessionName="spendctl-m1-dryrun-verification",
        Tags=[{"Key": "reservation-id", "Value": reservation_id}, {"Key": "team", "Value": team}],
    )["Credentials"]
    ec2 = boto3.client(
        "ec2",
        region_name=region,
        aws_access_key_id=assumed["AccessKeyId"],
        aws_secret_access_key=assumed["SecretAccessKey"],
        aws_session_token=assumed["SessionToken"],
    )
    valid = _base_request(reservation_id, team)
    _expect(ec2, "valid launch", valid, "DryRunOperation")

    missing_tag = _base_request("", team)
    _expect(ec2, "missing reservation-id tag", missing_tag, "UnauthorizedOperation")

    unlisted = _base_request(reservation_id, team)
    unlisted["InstanceType"] = "m7i.large"
    _expect(ec2, "unlisted instance type", unlisted, "UnauthorizedOperation")

    mismatch = _base_request("rsv_other_reservation", team)
    _expect(ec2, "mismatched reservation-id", mismatch, "UnauthorizedOperation")


if __name__ == "__main__":
    main()
