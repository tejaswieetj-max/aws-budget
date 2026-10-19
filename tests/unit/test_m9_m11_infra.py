import sys
from pathlib import Path

from aws_cdk import App, Duration, Stack, assertions
from aws_cdk import aws_apigatewayv2 as apigatewayv2
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_sqs as sqs

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from infra.stacks.foundation import ObservabilityStack, WebStack


def test_web_stack_synthesizes_private_cloudfront_host_and_config_outputs():
    app = App()
    web = WebStack(app, "WebStack", user_pool_id="pool-id", user_pool_client_id="client-id",
                   api_url="https://api.example/v1")
    template = assertions.Template.from_stack(web)
    template.resource_count_is("AWS::CloudFront::Distribution", 1)
    template.resource_count_is("AWS::S3::Bucket", 1)
    template.has_resource_properties("AWS::S3::Bucket", {
        "PublicAccessBlockConfiguration": {
            "BlockPublicAcls": True, "BlockPublicPolicy": True,
            "IgnorePublicAcls": True, "RestrictPublicBuckets": True,
        },
        "BucketEncryption": {"ServerSideEncryptionConfiguration": [{
            "ServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"},
        }]},
    })
    template.has_output("ApiBaseUrl", {"Value": "https://api.example/v1"})
    template.has_output("FrontendAssetsSynthesized", {"Value": "false"})
    bucket_policies = template.find_resources("AWS::S3::BucketPolicy")
    assert bucket_policies
    policy_text = str(bucket_policies)
    assert "cloudfront.amazonaws.com" in policy_text
    assert "AWS:SourceArn" in policy_text


def test_observability_synthesizes_dashboard_alarms_and_no_broad_execution_role():
    app = App()
    source = Stack(app, "Sources")
    api = apigatewayv2.HttpApi(source, "Api")
    route_function = lambda_.Function(
        source, "RouteFunction", runtime=lambda_.Runtime.PYTHON_3_12, handler="index.handler",
        code=lambda_.Code.from_inline("def handler(event, context): return {}"),
        timeout=Duration.seconds(10), memory_size=256,
    )
    dlq = sqs.Queue(source, "InvariantDlq")
    api_stack = type("ApiStackRef", (), {"api": api, "api_functions": {"route": route_function}})()
    orchestration = type("OrchestrationRef", (), {"state_machine_arn": "arn:aws:states:ap-south-1:123456789012:stateMachine:test"})()
    lifecycle = type("LifecycleRef", (), {"invariant_dlq": dlq})()
    billing = type("BillingRef", (), {"reconciler": route_function})()
    observability = ObservabilityStack(
        app, "ObservabilityStack", api_stack=api_stack, orchestration_stack=orchestration,
        lifecycle_stack=lifecycle, billing_stack=billing,
    )
    template = assertions.Template.from_stack(observability)
    template.resource_count_is("AWS::CloudWatch::Dashboard", 1)
    template.resource_count_is("AWS::CloudWatch::Alarm", 11)
    template.resource_count_is("AWS::SNS::Topic", 1)
    template.resource_count_is("AWS::IAM::Role", 0)
    template.resource_count_is("AWS::SNS::Subscription", 0)
    body = template.to_json()
    serialized = str(body)
    for metric in ("Spendctl/Workflow", "Spendctl/Lifecycle", "Spendctl/Billing",
                   "Spendctl/Pricing", "Spendctl/Budget", "InvariantViolation"):
        assert metric in serialized
