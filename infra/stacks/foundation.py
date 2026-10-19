"""Initial CDK stack topology. Resource-level acceptance work remains documented per module."""

import json
from pathlib import Path

from aws_cdk import CfnOutput, CfnParameter, Duration, RemovalPolicy, Stack, Tags
from aws_cdk import aws_apigateway as apigateway
from aws_cdk import aws_apigatewayv2 as apigatewayv2
from aws_cdk import aws_apigatewayv2_authorizers as apigatewayv2_authorizers
from aws_cdk import aws_apigatewayv2_integrations as apigatewayv2_integrations
from aws_cdk import aws_cognito as cognito
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_events as events
from aws_cdk import aws_events_targets as event_targets
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from aws_cdk import aws_scheduler as scheduler
from aws_cdk import aws_sqs as sqs
from aws_cdk import aws_ssm as ssm
from aws_cdk import aws_stepfunctions as stepfunctions
from constructs import Construct


class IdentityStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs):
        super().__init__(scope, construct_id, **kwargs)
        self.pool = cognito.UserPool(
            self,
            "Developers",
            user_pool_name="spendctl-dev-users",
            self_sign_up_enabled=False,
            sign_in_aliases=cognito.SignInAliases(email=True),
            standard_attributes=cognito.StandardAttributes(
                email=cognito.StandardAttribute(required=True, mutable=True)
            ),
            custom_attributes={"team": cognito.StringAttribute(mutable=True)},
        )
        self.pool.add_group("developers", group_name="developers")
        self.pool.add_group("admins", group_name="admins")
        client_read_attributes = (
            cognito.ClientAttributes()
            .with_standard_attributes(email=True)
            .with_custom_attributes("team")
        )
        client_write_attributes = cognito.ClientAttributes().with_standard_attributes(email=True)
        self.client = self.pool.add_client(
            "WebClient",
            generate_secret=False,
            auth_flows=cognito.AuthFlow(user_srp=True),
            read_attributes=client_read_attributes,
            write_attributes=client_write_attributes,
            o_auth=cognito.OAuthSettings(
                flows=cognito.OAuthFlows(authorization_code_grant=True),
                callback_urls=["http://localhost:5173/callback"],
                scopes=[
                    cognito.OAuthScope.OPENID,
                    cognito.OAuthScope.EMAIL,
                    cognito.OAuthScope.PROFILE,
                ],
            ),
        )
        CfnOutput(self, "UserPoolId", value=self.pool.user_pool_id)


class DataStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs):
        super().__init__(scope, construct_id, **kwargs)

        def table(name, partition, sort=None, pitr=False, ttl_attribute=None):
            return dynamodb.Table(
                self,
                name,
                table_name=f"spendctl-dev-{name.lower()}",
                partition_key=dynamodb.Attribute(
                    name=partition, type=dynamodb.AttributeType.STRING
                ),
                sort_key=dynamodb.Attribute(name=sort, type=dynamodb.AttributeType.STRING)
                if sort
                else None,
                billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
                point_in_time_recovery_specification=dynamodb.PointInTimeRecoverySpecification(
                    point_in_time_recovery_enabled=pitr
                ),
                encryption=dynamodb.TableEncryption.AWS_MANAGED,
                removal_policy=RemovalPolicy.RETAIN,
                time_to_live_attribute=ttl_attribute,
            )

        self.budgets = table("Budgets", "team", "period", True)
        self.reservations = table("Reservations", "pk", "sk", True, "ttl")
        self.reservations.add_global_secondary_index(
            index_name="team-created-index",
            partition_key=dynamodb.Attribute(name="team", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="created_at", type=dynamodb.AttributeType.STRING),
            projection_type=dynamodb.ProjectionType.ALL,
        )
        self.reservations.add_global_secondary_index(
            index_name="status-expiry-index",
            partition_key=dynamodb.Attribute(name="status", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="expires_at", type=dynamodb.AttributeType.STRING),
            projection_type=dynamodb.ProjectionType.ALL,
        )
        self.prices = table("PriceCache", "sku_key")
        self.samples = table("CostSamples", "resource_class", "settled_at")
        self.alerts = table("Alerts", "alert_id")


class ApiStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        user_pool,
        user_pool_client_id,
        data_stack,
        state_machine_arn,
        lifecycle_reaper_arn,
        **kwargs,
    ):
        super().__init__(scope, construct_id, **kwargs)
        source = str(Path(__file__).resolve().parents[2] / "services")
        table_env = {
            "BUDGETS_TABLE": data_stack.budgets.table_name,
            "RESERVATIONS_TABLE": data_stack.reservations.table_name,
            "COST_SAMPLES_TABLE": data_stack.samples.table_name,
            "ALERTS_TABLE": data_stack.alerts.table_name,
            "WORKFLOW_STATE_MACHINE_ARN": state_machine_arn,
            "LIFECYCLE_REAPER_ARN": lifecycle_reaper_arn,
            "CORS_ALLOWED_ORIGINS": "http://localhost:5173",
        }
        functions = {}

        def make_function(operation, handler, actions=(), resources=(), pricing=False):
            name = f"spendctl-dev-m5-{operation.replace('_', '-')}"
            log_group = logs.LogGroup(
                self,
                f"{operation}LogGroup",
                log_group_name=f"/aws/lambda/{name}",
                retention=logs.RetentionDays.ONE_MONTH,
                removal_policy=RemovalPolicy.RETAIN,
            )
            role = iam.Role(
                self,
                f"{operation}Role",
                role_name=f"spendctl-dev-m5-{operation.replace('_', '-')}-role",
                assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            )
            role.add_to_policy(
                iam.PolicyStatement(
                    actions=["logs:CreateLogStream", "logs:PutLogEvents"],
                    resources=[f"{log_group.log_group_arn}:*"],
                )
            )
            if actions:
                role.add_to_policy(
                    iam.PolicyStatement(actions=list(actions), resources=list(resources))
                )
            if pricing:
                role.add_to_policy(
                    iam.PolicyStatement(actions=["pricing:GetProducts"], resources=["*"])
                )
            function = lambda_.Function(
                self,
                f"{operation}Function",
                function_name=name,
                runtime=lambda_.Runtime.PYTHON_3_12,
                handler=f"spendctl.api.{handler}",
                code=lambda_.Code.from_asset(source),
                role=role,
                memory_size=256,
                timeout=Duration.seconds(10),
                environment=table_env,
            )
            functions[operation] = function
            return function

        budgets_arn = data_stack.budgets.table_arn
        reservations_arn = data_stack.reservations.table_arn
        samples_arn = data_stack.samples.table_arn
        alerts_arn = data_stack.alerts.table_arn
        make_function("estimate", "estimate_handler", pricing=True)
        create_request = make_function(
            "create_request",
            "create_request_handler",
            ["dynamodb:GetItem", "dynamodb:TransactWriteItems"],
            [budgets_arn, reservations_arn],
            pricing=True,
        )
        create_request.role.add_to_policy(
            iam.PolicyStatement(
                actions=["states:StartExecution", "states:ListExecutions"],
                resources=[state_machine_arn],
            )
        )
        create_request.role.add_to_policy(
            iam.PolicyStatement(
                actions=["states:DescribeExecution"],
                resources=[
                    f"arn:{self.partition}:states:{self.region}:{self.account}:execution:spendctl-dev-provisioning:*"
                ],
            )
        )
        make_function(
            "get_request", "get_request_handler", ["dynamodb:GetItem"], [reservations_arn]
        )
        cancel_request = make_function(
            "cancel_request", "cancel_request_handler", ["dynamodb:GetItem"], [reservations_arn]
        )
        cancel_request.role.add_to_policy(
            iam.PolicyStatement(actions=["lambda:InvokeFunction"], resources=[lifecycle_reaper_arn])
        )
        make_function(
            "list_reservations",
            "list_reservations_handler",
            ["dynamodb:Query"],
            [
                reservations_arn,
                f"{reservations_arn}/index/team-created-index",
                f"{reservations_arn}/index/status-expiry-index",
            ],
        )
        make_function("get_budget", "get_budget_handler", ["dynamodb:GetItem"], [budgets_arn])
        make_function(
            "put_budget",
            "put_budget_handler",
            ["dynamodb:GetItem", "dynamodb:PutItem"],
            [budgets_arn],
        )
        make_function(
            "calibration_summary", "calibration_summary_handler", ["dynamodb:Query"], [samples_arn]
        )
        make_function("list_alerts", "list_alerts_handler", ["dynamodb:Scan"], [alerts_arn])

        cloudfront_origin = CfnParameter(
            self,
            "CloudFrontOrigin",
            type="String",
            description="Deployed frontend origin allowed by API CORS, including scheme and hostname",
        )
        table_env["CORS_ALLOWED_ORIGINS"] = (
            f"http://localhost:5173,{cloudfront_origin.value_as_string}"
        )
        authorizer = apigatewayv2_authorizers.HttpJwtAuthorizer(
            "SpendctlCognitoAuthorizer",
            user_pool.user_pool_provider_url,
            jwt_audience=[user_pool_client_id],
            authorizer_name="spendctl-cognito-id-token",
        )
        access_log_group = logs.LogGroup(
            self,
            "ApiAccessLogGroup",
            log_group_name="/aws/apigateway/spendctl-dev-api",
            retention=logs.RetentionDays.ONE_MONTH,
            removal_policy=RemovalPolicy.RETAIN,
        )
        rate_limit = self.node.try_get_context("api_throttle_rate") or 50
        burst_limit = self.node.try_get_context("api_throttle_burst") or 100
        api = apigatewayv2.HttpApi(
            self,
            "SpendctlApi",
            api_name="spendctl-dev-api",
            description="Authenticated AWS spend admission API",
            create_default_stage=False,
            cors_preflight=apigatewayv2.CorsPreflightOptions(
                allow_origins=["http://localhost:5173", cloudfront_origin.value_as_string],
                allow_methods=[
                    apigatewayv2.CorsHttpMethod.GET,
                    apigatewayv2.CorsHttpMethod.POST,
                    apigatewayv2.CorsHttpMethod.PUT,
                    apigatewayv2.CorsHttpMethod.OPTIONS,
                ],
                allow_headers=["Authorization", "Content-Type", "Idempotency-Key"],
                max_age=Duration.hours(1),
            ),
        )
        apigatewayv2.CfnStage(
            self,
            "DefaultStage",
            api_id=api.api_id,
            stage_name="$default",
            auto_deploy=True,
            default_route_settings=apigatewayv2.CfnStage.RouteSettingsProperty(
                throttling_rate_limit=float(rate_limit),
                throttling_burst_limit=float(burst_limit),
            ),
            access_log_settings=apigatewayv2.CfnStage.AccessLogSettingsProperty(
                destination_arn=access_log_group.log_group_arn,
                format=apigateway.AccessLogFormat.custom(
                    json.dumps(
                        {
                            "requestId": "$context.requestId",
                            "routeKey": "$context.routeKey",
                            "status": "$context.status",
                            "sourceIp": "$context.identity.sourceIp",
                        },
                        separators=(",", ":"),
                    )
                ).to_string(),
            ),
        )

        def add_route(path, method, operation):
            api.add_routes(
                path=path,
                methods=[getattr(apigatewayv2.HttpMethod, method)],
                integration=apigatewayv2_integrations.HttpLambdaIntegration(
                    f"{operation}Integration",
                    functions[operation],
                    payload_format_version=apigatewayv2.PayloadFormatVersion.VERSION_2_0,
                ),
                authorizer=authorizer,
            )

        add_route("/v1/estimate", "POST", "estimate")
        add_route("/v1/requests", "POST", "create_request")
        add_route("/v1/requests/{id}", "GET", "get_request")
        add_route("/v1/requests/{id}/cancel", "POST", "cancel_request")
        add_route("/v1/reservations", "GET", "list_reservations")
        add_route("/v1/budgets/{team}", "GET", "get_budget")
        add_route("/v1/budgets/{team}", "PUT", "put_budget")
        add_route("/v1/calibration/summary", "GET", "calibration_summary")
        add_route("/v1/alerts", "GET", "list_alerts")

        CfnOutput(self, "ApiUrl", value=f"{api.api_endpoint}/v1")


class OrchestrationStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        data_stack,
        lifecycle_stack,
        **kwargs,
    ):
        super().__init__(scope, construct_id, **kwargs)
        Tags.of(self).add("project", "spendctl")
        Tags.of(self).add("env", "dev")
        source = str(Path(__file__).resolve().parents[2] / "services")
        private_subnet = CfnParameter(
            self,
            "PrivateSubnetId",
            type="AWS::EC2::Subnet::Id",
            description="Subnet verified as private and without a NAT gateway route",
        )
        security_group = CfnParameter(
            self,
            "ProvisioningSecurityGroupId",
            type="AWS::EC2::SecurityGroup::Id",
            description="Security group with no ingress rules",
        )
        ami_id = ssm.StringParameter.value_for_string_parameter(
            self, "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64"
        )

        def make_lambda(operation, handler, actions, resources, environment=None):
            name = f"spendctl-dev-m6-{operation.replace('_', '-')}"
            log_group = logs.LogGroup(
                self,
                f"{operation}LogGroup",
                log_group_name=f"/aws/lambda/{name}",
                retention=logs.RetentionDays.ONE_MONTH,
                removal_policy=RemovalPolicy.RETAIN,
            )
            role = iam.Role(
                self,
                f"{operation}Role",
                role_name=f"{name}-role",
                assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            )
            role.add_to_policy(
                iam.PolicyStatement(
                    actions=["logs:CreateLogStream", "logs:PutLogEvents"],
                    resources=[f"{log_group.log_group_arn}:*"],
                )
            )
            if actions:
                role.add_to_policy(iam.PolicyStatement(actions=actions, resources=resources))
            function = lambda_.Function(
                self,
                f"{operation}Function",
                function_name=name,
                runtime=lambda_.Runtime.PYTHON_3_12,
                handler=handler,
                code=lambda_.Code.from_asset(source),
                role=role,
                memory_size=256,
                timeout=Duration.seconds(10),
                environment=environment or {},
            )
            return function

        provisioner_name = "spendctl-dev-m6-provisioner"
        provisioner_log = logs.LogGroup(
            self,
            "ProvisionerLogGroup",
            log_group_name=f"/aws/lambda/{provisioner_name}",
            retention=logs.RetentionDays.ONE_MONTH,
            removal_policy=RemovalPolicy.RETAIN,
        )
        provisioner_role = iam.Role(
            self,
            "ProvisionerLambdaRole",
            role_name=f"{provisioner_name}-role",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
        )
        provisioner_role.add_to_policy(
            iam.PolicyStatement(
                actions=["logs:CreateLogStream", "logs:PutLogEvents"],
                resources=[f"{provisioner_log.log_group_arn}:*"],
            )
        )
        provisioner_policy_role = iam.Role(
            self,
            "ProvisionerRole",
            role_name="spendctl-dev-provisioner-role",
            assumed_by=iam.ArnPrincipal(provisioner_role.role_arn),
            description="Reservation-tagged EC2 launch and cleanup role assumed only by the M6 provisioner Lambda",
        )
        provisioner_policy_role.assume_role_policy.add_statements(
            iam.PolicyStatement(
                principals=[iam.ArnPrincipal(provisioner_role.role_arn)],
                actions=["sts:TagSession"],
                conditions={
                    "StringLike": {"aws:RequestTag/reservation-id": "rsv_*"},
                    "Null": {"aws:RequestTag/team": "false"},
                    "ForAllValues:StringEquals": {"aws:TagKeys": ["reservation-id", "team"]},
                },
            )
        )
        provisioner_policy_role.assume_role_policy.add_statements(
            iam.PolicyStatement(
                principals=[iam.ArnPrincipal(lifecycle_stack.reaper_role.role_arn)],
                actions=["sts:AssumeRole", "sts:TagSession"],
                conditions={
                    "StringLike": {"aws:RequestTag/reservation-id": "rsv_*"},
                    "Null": {"aws:RequestTag/team": "false"},
                    "ForAllValues:StringEquals": {"aws:TagKeys": ["reservation-id", "team"]},
                },
            )
        )
        provisioner_role.add_to_policy(
            iam.PolicyStatement(
                actions=["sts:AssumeRole"], resources=[provisioner_policy_role.role_arn]
            )
        )
        ec2_conditions = {
            "StringEquals": {
                "ec2:Region": "ap-south-1",
                "ec2:InstanceType": ["t3.micro", "t3.small"],
                "ec2:Subnet": private_subnet.value_as_string,
                "ec2:SecurityGroup": security_group.value_as_string,
                "ec2:VolumeType": "gp3",
                "aws:RequestTag/reservation-id": "${aws:PrincipalTag/reservation-id}",
                "aws:RequestTag/team": "${aws:PrincipalTag/team}",
                "aws:RequestTag/project": "spendctl",
                "aws:RequestTag/env": "dev",
                "aws:RequestTag/managed-by": "spend-controller",
            },
            "Bool": {"ec2:AssociatePublicIpAddress": "false"},
            "NumericGreaterThanEquals": {"ec2:VolumeSize": "1"},
            "NumericLessThanEquals": {"ec2:VolumeSize": "30"},
            "Null": {
                "aws:RequestTag/reservation-id": "false",
                "aws:RequestTag/team": "false",
            },
        }
        provisioner_policy_role.add_to_policy(
            iam.PolicyStatement(
                actions=["ec2:RunInstances"], resources=["*"], conditions=ec2_conditions
            )
        )
        provisioner_policy_role.add_to_policy(
            iam.PolicyStatement(
                actions=["ec2:CreateTags"],
                resources=[
                    f"arn:{self.partition}:ec2:{self.region}:{self.account}:instance/*",
                    f"arn:{self.partition}:ec2:{self.region}:{self.account}:volume/*",
                ],
                conditions={
                    "StringEquals": {
                        "ec2:CreateAction": "RunInstances",
                        "aws:RequestTag/reservation-id": "${aws:PrincipalTag/reservation-id}",
                        "aws:RequestTag/team": "${aws:PrincipalTag/team}",
                        "aws:RequestTag/project": "spendctl",
                        "aws:RequestTag/env": "dev",
                        "aws:RequestTag/managed-by": "spend-controller",
                    },
                    "Null": {
                        "aws:RequestTag/reservation-id": "false",
                        "aws:RequestTag/team": "false",
                    },
                },
            )
        )
        provisioner_policy_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "ec2:DescribeInstances",
                    "ec2:DescribeInstanceStatus",
                    "ec2:DescribeVolumes",
                ],
                resources=["*"],
            )
        )
        owner_conditions = {
            "StringEquals": {
                "ec2:ResourceTag/project": "spendctl",
                "ec2:ResourceTag/env": "dev",
                "ec2:ResourceTag/managed-by": "spend-controller",
                "ec2:ResourceTag/reservation-id": "${aws:PrincipalTag/reservation-id}",
                "ec2:ResourceTag/team": "${aws:PrincipalTag/team}",
            }
        }
        provisioner_policy_role.add_to_policy(
            iam.PolicyStatement(
                actions=["ec2:TerminateInstances"],
                resources=[f"arn:{self.partition}:ec2:{self.region}:{self.account}:instance/*"],
                conditions=owner_conditions,
            )
        )
        provisioner_policy_role.add_to_policy(
            iam.PolicyStatement(
                actions=["ec2:DeleteVolume"],
                resources=[f"arn:{self.partition}:ec2:{self.region}:{self.account}:volume/*"],
                conditions=owner_conditions,
            )
        )
        provisioner = lambda_.Function(
            self,
            "ProvisionerFunction",
            function_name=provisioner_name,
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="spendctl.provisioner.provision_handler",
            code=lambda_.Code.from_asset(source),
            role=provisioner_role,
            memory_size=256,
            timeout=Duration.seconds(10),
            environment={
                "PROVISIONER_ROLE_ARN": provisioner_policy_role.role_arn,
                "APPROVED_AMI_ID": ami_id,
                "PRIVATE_SUBNET_ID": private_subnet.value_as_string,
                "SECURITY_GROUP_ID": security_group.value_as_string,
                "SPENDCTL_FAILURE_INJECTION_ENABLED": "false",
            },
        )

        reservations_arn = data_stack.reservations.table_arn
        activate = make_lambda(
            "activate_reservation",
            "spendctl.workflow_handlers.activate_handler",
            ["dynamodb:GetItem", "dynamodb:UpdateItem"],
            [reservations_arn],
            {
                "BUDGETS_TABLE": data_stack.budgets.table_name,
                "RESERVATIONS_TABLE": data_stack.reservations.table_name,
                "EXPIRY_SCHEDULE_GROUP": lifecycle_stack.schedule_group_name,
                "LIFECYCLE_REAPER_ARN": lifecycle_stack.reaper.function_arn,
                "SCHEDULER_TARGET_ROLE_ARN": lifecycle_stack.scheduler_role.role_arn,
                "EVENT_BUS_NAME": lifecycle_stack.event_bus.event_bus_name,
            },
        )
        schedule_arn = (
            f"arn:{self.partition}:scheduler:{self.region}:{self.account}:schedule/"
            f"{lifecycle_stack.schedule_group_name}/*"
        )
        activate.role.add_to_policy(
            iam.PolicyStatement(
                actions=["scheduler:CreateSchedule", "scheduler:GetSchedule"],
                resources=[schedule_arn],
            )
        )
        activate.role.add_to_policy(
            iam.PolicyStatement(
                actions=["iam:PassRole"],
                resources=[lifecycle_stack.scheduler_role.role_arn],
                conditions={"StringEquals": {"iam:PassedToService": "scheduler.amazonaws.com"}},
            )
        )
        activate.role.add_to_policy(
            iam.PolicyStatement(
                actions=["events:PutEvents"], resources=[lifecycle_stack.event_bus.event_bus_arn]
            )
        )
        record_resources = make_lambda(
            "record_resources",
            "spendctl.workflow_handlers.record_resources_handler",
            ["dynamodb:GetItem", "dynamodb:UpdateItem"],
            [reservations_arn],
            {
                "BUDGETS_TABLE": data_stack.budgets.table_name,
                "RESERVATIONS_TABLE": data_stack.reservations.table_name,
            },
        )
        record_outcome = make_lambda(
            "record_outcome",
            "spendctl.workflow_handlers.record_workflow_outcome_handler",
            ["dynamodb:GetItem", "dynamodb:UpdateItem", "dynamodb:TransactWriteItems"],
            [reservations_arn, data_stack.budgets.table_arn],
            {
                "BUDGETS_TABLE": data_stack.budgets.table_name,
                "RESERVATIONS_TABLE": data_stack.reservations.table_name,
                "ALERTS_TABLE": data_stack.alerts.table_name,
            },
        )
        record_outcome.role.add_to_policy(
            iam.PolicyStatement(actions=["dynamodb:PutItem"], resources=[data_stack.alerts.table_arn])
        )
        release_prelaunch = make_lambda(
            "release_prelaunch_failure",
            "spendctl.workflow_handlers.release_prelaunch_failure_handler",
            ["dynamodb:GetItem", "dynamodb:TransactWriteItems"],
            [data_stack.budgets.table_arn, reservations_arn],
            {
                "BUDGETS_TABLE": data_stack.budgets.table_name,
                "RESERVATIONS_TABLE": data_stack.reservations.table_name,
            },
        )

        sfn_role = iam.Role(
            self,
            "WorkflowExecutionRole",
            role_name="spendctl-dev-m6-workflow-role",
            assumed_by=iam.ServicePrincipal("states.amazonaws.com"),
            description="Step Functions may invoke only the M6 provisioner and reservation callbacks",
        )
        sfn_role.add_to_policy(
            iam.PolicyStatement(
                actions=["lambda:InvokeFunction"],
                resources=[
                    provisioner.function_arn,
                    activate.function_arn,
                    record_resources.function_arn,
                    record_outcome.function_arn,
                    release_prelaunch.function_arn,
                ],
            )
        )
        retry_lambda = [
            {
                "ErrorEquals": [
                    "Lambda.ServiceException",
                    "Lambda.AWSLambdaException",
                    "Lambda.SdkClientException",
                    "Lambda.TooManyRequestsException",
                ],
                "IntervalSeconds": 2,
                "MaxAttempts": 4,
                "BackoffRate": 2.0,
            }
        ]
        definition = {
            "Comment": "spendctl M6 Standard provisioning workflow",
            "StartAt": "LaunchInstance",
            "States": {
                "LaunchInstance": {
                    "Type": "Task",
                    "Resource": "arn:aws:states:::lambda:invoke",
                    "Parameters": {
                        "FunctionName": "${ProvisionerArn}",
                        "Payload": {
                            "operation": "launch",
                            "reservation_id.$": "$.reservation_id",
                            "team.$": "$.team",
                            "resource.$": "$.resource",
                            "planned_hours.$": "$.planned_hours",
                            "region.$": "$.region",
                            "failure_injection.$": "$.failure_injection",
                        },
                    },
                    "ResultSelector": {
                        "state.$": "$.Payload.state",
                        "instance_id.$": "$.Payload.instance_id",
                        "volume_ids.$": "$.Payload.volume_ids",
                    },
                    "ResultPath": "$.launch",
                    "Retry": retry_lambda,
                    "Catch": [
                        {
                            "ErrorEquals": ["States.ALL"],
                            "ResultPath": "$.failure",
                            "Next": "InitializeCleanupCount",
                        }
                    ],
                    "Next": "CheckLaunchResult",
                },
                "CheckLaunchResult": {
                    "Type": "Choice",
                    "Choices": [
                        {
                            "Variable": "$.launch.state",
                            "StringEquals": "INJECTED_PRELAUNCH_FAILURE",
                            "Next": "ReleasePrelaunchFailure",
                        }
                    ],
                    "Default": "RecordResources",
                },
                "RecordResources": {
                    "Type": "Task",
                    "Resource": "arn:aws:states:::lambda:invoke",
                    "Parameters": {
                        "FunctionName": "${RecordResourcesArn}",
                        "Payload": {
                            "reservation_id.$": "$.reservation_id",
                            "instance_id.$": "$.launch.instance_id",
                            "volume_ids.$": "$.launch.volume_ids",
                            "execution_arn.$": "$$.Execution.Id",
                            "launched_at.$": "$$.State.EnteredTime",
                        },
                    },
                    "ResultPath": "$.recorded_resources",
                    "Retry": retry_lambda,
                    "Catch": [
                        {
                            "ErrorEquals": ["States.ALL"],
                            "ResultPath": "$.failure",
                            "Next": "InitializeCleanupCount",
                        }
                    ],
                    "Next": "InitializePollCount",
                },
                "InitializePollCount": {
                    "Type": "Pass",
                    "Result": {"attempt": 0},
                    "ResultPath": "$.poll_counter",
                    "Next": "PollInstance",
                },
                "PollInstance": {
                    "Type": "Task",
                    "Resource": "arn:aws:states:::lambda:invoke",
                    "Parameters": {
                        "FunctionName": "${ProvisionerArn}",
                        "Payload": {
                            "operation": "poll",
                            "reservation_id.$": "$.reservation_id",
                            "team.$": "$.team",
                            "instance_id.$": "$.launch.instance_id",
                            "region.$": "$.region",
                        },
                    },
                    "ResultSelector": {
                        "state.$": "$.Payload.state",
                        "running.$": "$.Payload.running",
                    },
                    "ResultPath": "$.poll",
                    "Retry": retry_lambda,
                    "Catch": [
                        {
                            "ErrorEquals": ["States.ALL"],
                            "ResultPath": "$.failure",
                            "Next": "InitializeCleanupCount",
                        }
                    ],
                    "Next": "CheckPollResult",
                },
                "CheckPollResult": {
                    "Type": "Choice",
                    "Choices": [
                        {
                            "Variable": "$.poll.running",
                            "BooleanEquals": True,
                            "Next": "ActivateReservation",
                        },
                        {
                            "Variable": "$.poll.state",
                            "StringEquals": "terminated",
                            "Next": "InitializeCleanupCount",
                        },
                        {
                            "Variable": "$.poll.state",
                            "StringEquals": "stopped",
                            "Next": "InitializeCleanupCount",
                        },
                        {
                            "Variable": "$.poll_counter.attempt",
                            "NumericGreaterThanEquals": 36,
                            "Next": "InitializeCleanupCount",
                        },
                    ],
                    "Default": "WaitBeforePoll",
                },
                "WaitBeforePoll": {"Type": "Wait", "Seconds": 10, "Next": "IncrementPollCount"},
                "IncrementPollCount": {
                    "Type": "Pass",
                    "Parameters": {"attempt.$": "States.MathAdd($.poll_counter.attempt, 1)"},
                    "ResultPath": "$.poll_counter",
                    "Next": "PollInstance",
                },
                "ActivateReservation": {
                    "Type": "Task",
                    "Resource": "arn:aws:states:::lambda:invoke",
                    "Parameters": {
                        "FunctionName": "${ActivateArn}",
                        "Payload": {
                            "reservation_id.$": "$.reservation_id",
                            "instance_id.$": "$.launch.instance_id",
                            "volume_ids.$": "$.launch.volume_ids",
                            "execution_arn.$": "$$.Execution.Id",
                            "launched_at.$": "$$.State.EnteredTime",
                        },
                    },
                    "ResultPath": "$.activation",
                    "Retry": retry_lambda,
                    "Catch": [
                        {
                            "ErrorEquals": ["States.ALL"],
                            "ResultPath": "$.failure",
                            "Next": "InitializeCleanupCount",
                        }
                    ],
                    "Next": "RecordSuccess",
                },
                "RecordSuccess": {
                    "Type": "Task",
                    "Resource": "arn:aws:states:::lambda:invoke",
                    "Parameters": {
                        "FunctionName": "${RecordOutcomeArn}",
                        "Payload": {
                            "reservation_id.$": "$.reservation_id",
                            "execution_status": "SUCCEEDED",
                        },
                    },
                    "Retry": retry_lambda,
                    "Catch": [
                        {
                            "ErrorEquals": ["States.ALL"],
                            "ResultPath": "$.status_failure",
                            "Next": "RecordUnknownOutcome",
                        }
                    ],
                    "End": True,
                },
                "RecordUnknownOutcome": {
                    "Type": "Task",
                    "Resource": "arn:aws:states:::lambda:invoke",
                    "Parameters": {
                        "FunctionName": "${RecordOutcomeArn}",
                        "Payload": {
                            "reservation_id.$": "$.reservation_id",
                            "execution_status": "UNKNOWN",
                        },
                    },
                    "Retry": retry_lambda,
                    "End": True,
                },
                "InitializeCleanupCount": {
                    "Type": "Pass",
                    "Result": {"attempt": 0},
                    "ResultPath": "$.cleanup_counter",
                    "Next": "CleanupFailedLaunch",
                },
                "CleanupFailedLaunch": {
                    "Type": "Task",
                    "Resource": "arn:aws:states:::lambda:invoke",
                    "Parameters": {
                        "FunctionName": "${ProvisionerArn}",
                        "Payload": {
                            "operation": "cleanup",
                            "reservation_id.$": "$.reservation_id",
                            "team.$": "$.team",
                            "region.$": "$.region",
                        },
                    },
                    "ResultSelector": {
                        "cleanup_state.$": "$.Payload.cleanup_state",
                        "had_resources.$": "$.Payload.had_resources",
                    },
                    "ResultPath": "$.cleanup",
                    "Retry": retry_lambda,
                    "Catch": [
                        {
                            "ErrorEquals": ["States.ALL"],
                            "ResultPath": "$.cleanup_failure",
                            "Next": "RecordCleanupUnknown",
                        }
                    ],
                    "Next": "CheckCleanupResult",
                },
                "CheckCleanupResult": {
                    "Type": "Choice",
                    "Choices": [
                        {
                            "Variable": "$.cleanup.cleanup_state",
                            "StringEquals": "COMPLETE",
                            "Next": "RecordFailure",
                        },
                        {
                            "Variable": "$.cleanup_counter.attempt",
                            "NumericGreaterThanEquals": 36,
                            "Next": "RecordCleanupUnknown",
                        },
                    ],
                    "Default": "WaitBeforeCleanupPoll",
                },
                "WaitBeforeCleanupPoll": {
                    "Type": "Wait",
                    "Seconds": 10,
                    "Next": "IncrementCleanupCount",
                },
                "IncrementCleanupCount": {
                    "Type": "Pass",
                    "Parameters": {"attempt.$": "States.MathAdd($.cleanup_counter.attempt, 1)"},
                    "ResultPath": "$.cleanup_counter",
                    "Next": "CleanupFailedLaunch",
                },
                "RecordFailure": {
                    "Type": "Task",
                    "Resource": "arn:aws:states:::lambda:invoke",
                    "Parameters": {
                        "FunctionName": "${RecordOutcomeArn}",
                        "Payload": {
                            "reservation_id.$": "$.reservation_id",
                            "execution_status": "FAILED",
                            "cleanup_state.$": "$.cleanup.cleanup_state",
                            "had_resources.$": "$.cleanup.had_resources",
                        },
                    },
                    "Retry": retry_lambda,
                    "Next": "ProvisioningFailed",
                },
                "RecordCleanupUnknown": {
                    "Type": "Task",
                    "Resource": "arn:aws:states:::lambda:invoke",
                    "Parameters": {
                        "FunctionName": "${RecordOutcomeArn}",
                        "Payload": {
                            "reservation_id.$": "$.reservation_id",
                            "execution_status": "UNKNOWN",
                            "cleanup_state": "UNKNOWN",
                        },
                    },
                    "Retry": retry_lambda,
                    "Next": "ProvisioningFailed",
                },
                "ReleasePrelaunchFailure": {
                    "Type": "Task",
                    "Resource": "arn:aws:states:::lambda:invoke",
                    "Parameters": {
                        "FunctionName": "${ReleasePrelaunchArn}",
                        "Payload": {"reservation_id.$": "$.reservation_id"},
                    },
                    "Retry": retry_lambda,
                    "Next": "InjectedFailure",
                },
                "ProvisioningFailed": {
                    "Type": "Fail",
                    "Error": "ProvisioningFailed",
                    "Cause": "Provisioning failed or cleanup requires reconciliation",
                },
                "InjectedFailure": {
                    "Type": "Fail",
                    "Error": "InjectedProvisioningFailure",
                    "Cause": "Pre-launch failure injection; reservation safely released",
                },
            },
        }
        state_machine = stepfunctions.CfnStateMachine(
            self,
            "ProvisioningStateMachine",
            state_machine_name="spendctl-dev-provisioning",
            state_machine_type="STANDARD",
            role_arn=sfn_role.role_arn,
            definition_string=json.dumps(definition),
            definition_substitutions={
                "ProvisionerArn": provisioner.function_arn,
                "ActivateArn": activate.function_arn,
                "RecordResourcesArn": record_resources.function_arn,
                "RecordOutcomeArn": record_outcome.function_arn,
                "ReleasePrelaunchArn": release_prelaunch.function_arn,
            },
        )
        Tags.of(state_machine).add("project", "spendctl")
        Tags.of(state_machine).add("env", "dev")
        self.state_machine_arn = state_machine.attr_arn
        CfnOutput(self, "StateMachineArn", value=self.state_machine_arn)


class LifecycleStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, *, data_stack, **kwargs):
        super().__init__(scope, construct_id, **kwargs)
        Tags.of(self).add("project", "spendctl")
        Tags.of(self).add("env", "dev")
        source = str(Path(__file__).resolve().parents[2] / "services")
        self.event_bus = events.EventBus(self, "SpendctlEventBus", event_bus_name="spendctl-bus")
        self.schedule_group_name = "spendctl-expiry"
        self.schedule_group = scheduler.CfnScheduleGroup(
            self, "ExpiryScheduleGroup", name=self.schedule_group_name
        )
        machine_arn = (
            f"arn:{self.partition}:states:{self.region}:{self.account}:"
            "stateMachine:spendctl-dev-provisioning"
        )
        execution_arn = (
            f"arn:{self.partition}:states:{self.region}:{self.account}:"
            "execution:spendctl-dev-provisioning:*"
        )
        provisioner_role_arn = (
            f"arn:{self.partition}:iam::{self.account}:role/spendctl-dev-provisioner-role"
        )

        def function(name, handler, actions, resources, environment, timeout_seconds=10):
            function_name = f"spendctl-dev-m7-{name.replace('_', '-')}"
            log_group = logs.LogGroup(
                self,
                f"{name}LogGroup",
                log_group_name=f"/aws/lambda/{function_name}",
                retention=logs.RetentionDays.ONE_MONTH,
                removal_policy=RemovalPolicy.RETAIN,
            )
            role = iam.Role(
                self,
                f"{name}Role",
                role_name=f"{function_name}-role",
                assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            )
            role.add_to_policy(
                iam.PolicyStatement(
                    actions=["logs:CreateLogStream", "logs:PutLogEvents"],
                    resources=[f"{log_group.log_group_arn}:*"],
                )
            )
            if actions:
                role.add_to_policy(iam.PolicyStatement(actions=actions, resources=resources))
            fn = lambda_.Function(
                self,
                f"{name}Function",
                function_name=function_name,
                runtime=lambda_.Runtime.PYTHON_3_12,
                handler=f"spendctl.lifecycle_handlers.{handler}",
                code=lambda_.Code.from_asset(source),
                role=role,
                memory_size=256,
                timeout=Duration.seconds(timeout_seconds),
                environment=environment,
            )
            return fn, role

        table_env = {
            "BUDGETS_TABLE": data_stack.budgets.table_name,
            "RESERVATIONS_TABLE": data_stack.reservations.table_name,
            "ALERTS_TABLE": data_stack.alerts.table_name,
            "EVENT_BUS_NAME": self.event_bus.event_bus_name,
        }
        reservations_arn = data_stack.reservations.table_arn
        index_arn = f"{reservations_arn}/index/status-expiry-index"
        alerts_arn = data_stack.alerts.table_arn
        budgets_arn = data_stack.budgets.table_arn

        self.reaper, self.reaper_role = function(
            "reaper",
            "reaper_handler",
            [],
            [],
            {
                **table_env,
                "WORKFLOW_STATE_MACHINE_ARN": machine_arn,
                "PROVISIONER_ROLE_ARN": provisioner_role_arn,
            },
            timeout_seconds=30,
        )
        self.reaper_role.add_to_policy(
            iam.PolicyStatement(
                actions=["dynamodb:GetItem", "dynamodb:UpdateItem", "dynamodb:TransactWriteItems"],
                resources=[reservations_arn, budgets_arn],
            )
        )
        self.reaper_role.add_to_policy(
            iam.PolicyStatement(actions=["dynamodb:Query"], resources=[reservations_arn, index_arn])
        )
        self.reaper_role.add_to_policy(
            iam.PolicyStatement(actions=["dynamodb:PutItem"], resources=[alerts_arn])
        )
        self.reaper_role.add_to_policy(
            iam.PolicyStatement(
                actions=["sts:AssumeRole", "sts:TagSession"], resources=[provisioner_role_arn]
            )
        )
        self.reaper_role.add_to_policy(
            iam.PolicyStatement(
                actions=["states:StartExecution", "states:ListExecutions"], resources=[machine_arn]
            )
        )
        self.reaper_role.add_to_policy(
            iam.PolicyStatement(actions=["states:DescribeExecution"], resources=[execution_arn])
        )
        self.reaper_role.add_to_policy(
            iam.PolicyStatement(
                actions=["events:PutEvents"], resources=[self.event_bus.event_bus_arn]
            )
        )

        self.state_listener, self.state_listener_role = function(
            "state_listener",
            "state_change_handler",
            [],
            [],
            table_env,
        )
        self.state_listener_role.add_to_policy(
            iam.PolicyStatement(actions=["ec2:DescribeInstances"], resources=["*"])
        )
        self.state_listener_role.add_to_policy(
            iam.PolicyStatement(
                actions=["dynamodb:GetItem", "dynamodb:UpdateItem"], resources=[reservations_arn]
            )
        )
        self.state_listener_role.add_to_policy(
            iam.PolicyStatement(actions=["dynamodb:PutItem"], resources=[alerts_arn])
        )
        self.bypass_detector, self.bypass_detector_role = function(
            "bypass_detector",
            "bypass_handler",
            ["dynamodb:PutItem"],
            [alerts_arn],
            {**table_env, "PROVISIONER_ROLE_ARN": provisioner_role_arn},
        )
        self.bypass_detector_role.add_to_policy(
            iam.PolicyStatement(actions=["dynamodb:GetItem"], resources=[reservations_arn])
        )
        self.bypass_detector_role.add_to_policy(
            iam.PolicyStatement(
                actions=["events:PutEvents"], resources=[self.event_bus.event_bus_arn]
            )
        )
        self.orphan_auditor, self.orphan_auditor_role = function(
            "orphan_auditor",
            "orphan_audit_handler",
            [],
            [],
            table_env,
            timeout_seconds=30,
        )
        self.orphan_auditor_role.add_to_policy(
            iam.PolicyStatement(
                actions=["ec2:DescribeInstances", "ec2:DescribeVolumes"], resources=["*"]
            )
        )
        self.orphan_auditor_role.add_to_policy(
            iam.PolicyStatement(actions=["dynamodb:GetItem"], resources=[reservations_arn])
        )
        self.orphan_auditor_role.add_to_policy(
            iam.PolicyStatement(actions=["dynamodb:PutItem"], resources=[alerts_arn])
        )

        invariant_name = "spendctl-dev-m4-invariant-checker"
        invariant_log = logs.LogGroup(
            self,
            "InvariantCheckerLogGroup",
            log_group_name=f"/aws/lambda/{invariant_name}",
            retention=logs.RetentionDays.ONE_MONTH,
            removal_policy=RemovalPolicy.RETAIN,
        )
        self.invariant_checker_role = iam.Role(
            self,
            "InvariantCheckerRole",
            role_name=f"{invariant_name}-role",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
        )
        self.invariant_checker_role.add_to_policy(
            iam.PolicyStatement(
                actions=["logs:CreateLogStream", "logs:PutLogEvents"],
                resources=[f"{invariant_log.log_group_arn}:*"],
            )
        )
        self.invariant_checker_role.add_to_policy(
            iam.PolicyStatement(
                actions=["dynamodb:Scan"], resources=[budgets_arn, reservations_arn]
            )
        )
        self.invariant_checker_role.add_to_policy(
            iam.PolicyStatement(actions=["dynamodb:PutItem"], resources=[alerts_arn])
        )
        self.invariant_checker_role.add_to_policy(
            iam.PolicyStatement(
                actions=["events:PutEvents"], resources=[self.event_bus.event_bus_arn]
            )
        )
        self.invariant_checker = lambda_.Function(
            self,
            "InvariantCheckerFunction",
            function_name=invariant_name,
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="spendctl.invariant_handlers.invariant_checker_handler",
            code=lambda_.Code.from_asset(source),
            role=self.invariant_checker_role,
            memory_size=256,
            timeout=Duration.seconds(10),
            environment={
                "BUDGETS_TABLE": data_stack.budgets.table_name,
                "RESERVATIONS_TABLE": data_stack.reservations.table_name,
                "ALERTS_TABLE": data_stack.alerts.table_name,
                "EVENT_BUS_NAME": self.event_bus.event_bus_name,
            },
        )
        self.invariant_dlq = sqs.Queue(
            self,
            "InvariantCheckerDlq",
            queue_name="spendctl-dev-invariant-checker-dlq",
            encryption=sqs.QueueEncryption.SQS_MANAGED,
            retention_period=Duration.days(14),
            removal_policy=RemovalPolicy.RETAIN,
        )
        schedule_trust = iam.ServicePrincipal(
            "scheduler.amazonaws.com",
            conditions={
                "StringEquals": {"aws:SourceAccount": self.account},
                "ArnLike": {"aws:SourceArn": self.schedule_group.attr_arn},
            },
        )
        invariant_schedule_role = iam.Role(
            self,
            "InvariantCheckerScheduleRole",
            role_name="spendctl-dev-m4-invariant-schedule-role",
            assumed_by=schedule_trust,
        )
        invariant_schedule_role.add_to_policy(
            iam.PolicyStatement(
                actions=["lambda:InvokeFunction"],
                resources=[self.invariant_checker.function_arn],
            )
        )
        invariant_schedule_role.add_to_policy(
            iam.PolicyStatement(
                actions=["sqs:SendMessage"], resources=[self.invariant_dlq.queue_arn]
            )
        )
        self.invariant_schedule = scheduler.CfnSchedule(
            self,
            "InvariantCheckerSchedule",
            name="spendctl-dev-invariant-check-5m",
            group_name=self.schedule_group_name,
            schedule_expression="rate(5 minutes)",
            flexible_time_window=scheduler.CfnSchedule.FlexibleTimeWindowProperty(mode="OFF"),
            target=scheduler.CfnSchedule.TargetProperty(
                arn=self.invariant_checker.function_arn,
                role_arn=invariant_schedule_role.role_arn,
                input='{"operation":"check_invariants"}',
                dead_letter_config=scheduler.CfnSchedule.DeadLetterConfigProperty(
                    arn=self.invariant_dlq.queue_arn
                ),
                retry_policy=scheduler.CfnSchedule.RetryPolicyProperty(
                    maximum_event_age_in_seconds=3600,
                    maximum_retry_attempts=3,
                ),
            ),
            state="ENABLED",
        )

        self.scheduler_role = iam.Role(
            self,
            "ExpiryScheduleTargetRole",
            role_name="spendctl-dev-m7-scheduler-target-role",
            assumed_by=schedule_trust,
        )
        self.scheduler_role.add_to_policy(
            iam.PolicyStatement(
                actions=["lambda:InvokeFunction"], resources=[self.reaper.function_arn]
            )
        )

        events.Rule(
            self,
            "LifecycleSafetySweeper",
            rule_name="spendctl-dev-lifecycle-sweeper",
            schedule=events.Schedule.rate(Duration.minutes(5)),
            targets=[
                event_targets.LambdaFunction(
                    self.reaper,
                    event=events.RuleTargetInput.from_object({"operation": "sweep"}),
                )
            ],
        )
        events.Rule(
            self,
            "InstanceStateChangeListener",
            rule_name="spendctl-dev-instance-state-listener",
            event_pattern=events.EventPattern(
                source=["aws.ec2"],
                detail_type=["EC2 Instance State-change Notification"],
            ),
            targets=[event_targets.LambdaFunction(self.state_listener)],
        )
        events.Rule(
            self,
            "RunInstancesBypassDetector",
            rule_name="spendctl-dev-runinstances-bypass-detector",
            event_pattern=events.EventPattern(
                source=["aws.ec2"],
                detail_type=["AWS API Call via CloudTrail"],
                detail={"eventSource": ["ec2.amazonaws.com"], "eventName": ["RunInstances"]},
            ),
            targets=[event_targets.LambdaFunction(self.bypass_detector)],
        )
        events.Rule(
            self,
            "DailyOrphanAudit",
            rule_name="spendctl-dev-daily-orphan-audit",
            schedule=events.Schedule.rate(Duration.days(1)),
            targets=[event_targets.LambdaFunction(self.orphan_auditor)],
        )
        CfnOutput(self, "EventBusName", value=self.event_bus.event_bus_name)
        CfnOutput(self, "ReaperArn", value=self.reaper.function_arn)


class BillingStack(Stack):
    pass


class WebStack(Stack):
    pass


class ObservabilityStack(Stack):
    pass


class NaiveAdmissionExperimentStack(Stack):
    """Opt-in, non-API stack used only to compare unsafe admission concurrency."""

    def __init__(self, scope: Construct, construct_id: str, *, data_stack, **kwargs):
        super().__init__(scope, construct_id, **kwargs)
        Tags.of(self).add("project", "spendctl")
        Tags.of(self).add("env", "dev")
        function_name = "spendctl-test-naive-admission"
        log_group = logs.LogGroup(
            self,
            "NaiveAdmissionLogGroup",
            log_group_name=f"/aws/lambda/{function_name}",
            retention=logs.RetentionDays.ONE_WEEK,
            removal_policy=RemovalPolicy.RETAIN,
        )
        role = iam.Role(
            self,
            "NaiveAdmissionRole",
            role_name=f"{function_name}-role",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
        )
        role.add_to_policy(
            iam.PolicyStatement(
                actions=["logs:CreateLogStream", "logs:PutLogEvents"],
                resources=[f"{log_group.log_group_arn}:*"],
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                actions=["dynamodb:GetItem", "dynamodb:UpdateItem"],
                resources=[data_stack.budgets.table_arn],
            )
        )
        lambda_.Function(
            self,
            "NaiveAdmissionFunction",
            function_name=function_name,
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="spendctl.naive_admission.naive_admission_handler",
            code=lambda_.Code.from_asset(str(Path(__file__).resolve().parents[2] / "services")),
            role=role,
            memory_size=256,
            timeout=Duration.seconds(10),
            environment={
                "BUDGETS_TABLE": data_stack.budgets.table_name,
                "ADMISSION_MODE": "naive",
                "ALLOW_NAIVE_ADMISSION": "true",
            },
        )
