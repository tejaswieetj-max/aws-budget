"""Initial CDK stack topology. Resource-level acceptance work remains documented per module."""

import json
from pathlib import Path

from aws_cdk import CfnOutput, CfnParameter, Duration, RemovalPolicy, Stack, Tags
from aws_cdk import aws_apigateway as apigateway
from aws_cdk import aws_cognito as cognito
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
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
        data_stack,
        state_machine_arn,
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
            "CORS_ALLOWED_ORIGIN": "http://localhost:5173",
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
        make_function(
            "cancel_request", "cancel_request_handler", ["dynamodb:GetItem"], [reservations_arn]
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

        authorizer = apigateway.CognitoUserPoolsAuthorizer(
            self,
            "SpendctlCognitoAuthorizer",
            cognito_user_pools=[user_pool],
            authorizer_name="spendctl-cognito-id-token",
        )
        api = apigateway.RestApi(
            self,
            "SpendctlApi",
            rest_api_name="spendctl-dev-api",
            description="Authenticated AWS spend admission API",
            endpoint_types=[apigateway.EndpointType.REGIONAL],
            deploy_options=apigateway.StageOptions(
                stage_name="v1",
                throttling_rate_limit=20,
                throttling_burst_limit=40,
            ),
            default_cors_preflight_options=apigateway.CorsOptions(
                allow_origins=["http://localhost:5173"],
                allow_methods=["GET", "POST", "PUT", "OPTIONS"],
                allow_headers=["Authorization", "Content-Type", "Idempotency-Key"],
                max_age=Duration.hours(1),
            ),
        )
        api.add_gateway_response(
            "UnauthorizedResponse",
            type=apigateway.ResponseType.UNAUTHORIZED,
            response_headers={
                "Access-Control-Allow-Origin": "'http://localhost:5173'",
                "Content-Type": "'application/json'",
            },
            templates={
                "application/json": '{"error":{"code":"UNAUTHENTICATED","message":"A valid Cognito ID token is required","details":{}}}'
            },
        )
        api.add_gateway_response(
            "AccessDeniedResponse",
            type=apigateway.ResponseType.ACCESS_DENIED,
            response_headers={
                "Access-Control-Allow-Origin": "'http://localhost:5173'",
                "Content-Type": "'application/json'",
            },
            templates={
                "application/json": '{"error":{"code":"POLICY_DENIED","message":"The requested operation is not authorized","details":{}}}'
            },
        )

        def add_method(resource, method, operation):
            resource.add_method(
                method,
                apigateway.LambdaIntegration(functions[operation]),
                authorization_type=apigateway.AuthorizationType.COGNITO,
                authorizer=authorizer,
            )

        v1 = api.root.add_resource("v1")
        add_method(v1.add_resource("estimate"), "POST", "estimate")
        requests = v1.add_resource("requests")
        add_method(requests, "POST", "create_request")
        request_id = requests.add_resource("{id}")
        add_method(request_id, "GET", "get_request")
        add_method(request_id.add_resource("cancel"), "POST", "cancel_request")
        add_method(v1.add_resource("reservations"), "GET", "list_reservations")
        budgets = v1.add_resource("budgets")
        budget = budgets.add_resource("{team}")
        add_method(budget, "GET", "get_budget")
        add_method(budget, "PUT", "put_budget")
        add_method(
            v1.add_resource("calibration").add_resource("summary"), "GET", "calibration_summary"
        )
        add_method(v1.add_resource("alerts"), "GET", "list_alerts")

        CfnOutput(self, "ApiUrl", value=api.url)


class OrchestrationStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, *, data_stack, **kwargs):
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
            },
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
            ["dynamodb:GetItem", "dynamodb:UpdateItem"],
            [reservations_arn],
            {
                "BUDGETS_TABLE": data_stack.budgets.table_name,
                "RESERVATIONS_TABLE": data_stack.reservations.table_name,
            },
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
    pass


class BillingStack(Stack):
    pass


class WebStack(Stack):
    pass


class ObservabilityStack(Stack):
    pass
