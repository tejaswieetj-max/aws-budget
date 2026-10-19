import pytest
from spendctl.provisioner import cleanup_reservation, launch_instance, poll_instance

EVENT = {
    "reservation_id": "rsv_0123456789abcdef",
    "team": "alpha",
    "region": "ap-south-1",
    "resource": {"type": "ec2", "instance_type": "t3.micro", "ebs_gb": 8},
    "planned_hours": 2,
}


class FakeEC2:
    def __init__(self):
        self.calls = []
        self.instances = []
        self.volumes = []

    def run_instances(self, **kwargs):
        self.calls.append(("run_instances", kwargs))
        return {
            "Instances": [
                {
                    "InstanceId": "i-123",
                    "State": {"Name": "pending"},
                    "BlockDeviceMappings": [{"Ebs": {"VolumeId": "vol-123"}}],
                }
            ]
        }

    def describe_instances(self, **kwargs):
        self.calls.append(("describe_instances", kwargs))
        return (
            {"Reservations": [{"Instances": self.instances}]}
            if self.instances
            else {"Reservations": []}
        )

    def describe_volumes(self, **kwargs):
        self.calls.append(("describe_volumes", kwargs))
        return {"Volumes": self.volumes}

    def terminate_instances(self, **kwargs):
        self.calls.append(("terminate_instances", kwargs))
        return {}

    def delete_volume(self, **kwargs):
        self.calls.append(("delete_volume", kwargs))
        return {}


def test_launch_uses_client_token_private_network_and_required_tags():
    ec2 = FakeEC2()
    result = launch_instance(
        EVENT,
        ec2=ec2,
        ami_id="ami-approved",
        private_subnet_id="subnet-private",
        security_group_id="sg-no-ingress",
    )
    assert result == {
        "reservation_id": EVENT["reservation_id"],
        "instance_id": "i-123",
        "volume_ids": ["vol-123"],
        "state": "pending",
    }
    request = ec2.calls[0][1]
    assert request["ClientToken"] == EVENT["reservation_id"]
    assert (request["MinCount"], request["MaxCount"]) == (1, 1)
    assert request["NetworkInterfaces"][0]["AssociatePublicIpAddress"] is False
    assert request["NetworkInterfaces"][0]["SubnetId"] == "subnet-private"
    assert request["MetadataOptions"]["HttpTokens"] == "required"
    assert "KeyName" not in request
    ebs = request["BlockDeviceMappings"][0]["Ebs"]
    assert ebs == {
        "VolumeSize": 8,
        "VolumeType": "gp3",
        "Encrypted": True,
        "DeleteOnTermination": True,
    }
    for spec in request["TagSpecifications"]:
        tags = {tag["Key"]: tag["Value"] for tag in spec["Tags"]}
        assert tags["managed-by"] == "spend-controller"
        assert tags["reservation-id"] == EVENT["reservation_id"]
        assert tags["team"] == "alpha"
        assert tags["project"] == "spendctl" and tags["env"] == "dev"


@pytest.mark.parametrize(
    "changes",
    [
        {"resource": {"type": "ec2", "instance_type": "m7.large", "ebs_gb": 8}},
        {"resource": {"type": "ec2", "instance_type": "t3.micro", "ebs_gb": 31}},
        {"region": "us-east-1"},
    ],
)
def test_launch_rejects_unsupported_configuration_before_ec2_call(changes):
    ec2 = FakeEC2()
    event = {**EVENT, **changes}
    with pytest.raises(ValueError):
        launch_instance(
            event,
            ec2=ec2,
            ami_id="ami-approved",
            private_subnet_id="subnet-private",
            security_group_id="sg-no-ingress",
        )
    assert ec2.calls == []


def test_failure_injection_is_explicit_and_occurs_before_launch():
    ec2 = FakeEC2()
    result = launch_instance(
        {**EVENT, "failure_injection": True},
        ec2=ec2,
        ami_id="ami-approved",
        private_subnet_id="subnet-private",
        security_group_id="sg-no-ingress",
        failure_injection_enabled=True,
    )
    assert result["state"] == "INJECTED_PRELAUNCH_FAILURE"
    assert ec2.calls == []


def test_handler_assumes_reservation_tagged_provisioner_role(monkeypatch):
    import boto3
    from spendctl import provisioner

    calls = []

    class FakeSTS:
        def assume_role(self, **kwargs):
            calls.append(("assume_role", kwargs))
            return {
                "Credentials": {
                    "AccessKeyId": "access",
                    "SecretAccessKey": "secret",
                    "SessionToken": "token",
                }
            }

    ec2 = FakeEC2()

    def fake_client(service, **kwargs):
        return FakeSTS() if service == "sts" else ec2

    monkeypatch.setattr(boto3, "client", fake_client)
    monkeypatch.setenv("PROVISIONER_ROLE_ARN", "arn:aws:iam::123456789012:role/provisioner")
    monkeypatch.setenv("APPROVED_AMI_ID", "ami-approved")
    monkeypatch.setenv("PRIVATE_SUBNET_ID", "subnet-private")
    monkeypatch.setenv("SECURITY_GROUP_ID", "sg-no-ingress")
    result = provisioner.provision_handler({**EVENT, "operation": "launch"}, None)
    assert result["instance_id"] == "i-123"
    sts_request = calls[0][1]
    assert sts_request["RoleArn"].endswith(":role/provisioner")
    assert {tag["Key"]: tag["Value"] for tag in sts_request["Tags"]} == {
        "reservation-id": EVENT["reservation_id"],
        "team": "alpha",
    }
    assert ec2.calls[0][1]["ClientToken"] == EVENT["reservation_id"]


def test_poll_and_cleanup_are_tag_scoped_and_idempotent():
    ec2 = FakeEC2()
    ec2.instances = [{"InstanceId": "i-123", "State": {"Name": "running"}}]
    assert poll_instance("i-123", ec2=ec2) == {"state": "running", "running": True}
    ec2.volumes = [{"VolumeId": "vol-123", "State": "available"}]
    result = cleanup_reservation(
        {"reservation_id": EVENT["reservation_id"], "team": "alpha"}, ec2=ec2
    )
    assert result["had_resources"] is True
    assert result["cleanup_state"] == "PENDING"
    filters = next(
        call[1]["Filters"]
        for call in ec2.calls
        if call[0] == "describe_instances" and "Filters" in call[1]
    )
    assert {item["Name"]: item["Values"][0] for item in filters} == {
        "tag:managed-by": "spend-controller",
        "tag:reservation-id": EVENT["reservation_id"],
        "tag:team": "alpha",
        "tag:project": "spendctl",
        "tag:env": "dev",
    }
    assert ("terminate_instances", {"InstanceIds": ["i-123"]}) in ec2.calls
    assert ("delete_volume", {"VolumeId": "vol-123"}) in ec2.calls
