#!/usr/bin/env python3
"""CDK entry point. Synthesis needs no AWS credentials; deployment is separate."""

from aws_cdk import App, Environment, Tags
from stacks.foundation import (
    ApiStack,
    BillingStack,
    DataStack,
    IdentityStack,
    LifecycleStack,
    NaiveAdmissionExperimentStack,
    ObservabilityStack,
    OrchestrationStack,
    WebStack,
)

app = App()
Tags.of(app).add("project", "spendctl")
Tags.of(app).add("env", "dev")
env = Environment(
    account=app.node.try_get_context("account"),
    region=app.node.try_get_context("region") or "ap-south-1",
)
identity = IdentityStack(
    app, "IdentityStack", cloudfront_url=app.node.try_get_context("cloudfront_url"), env=env
)
data = DataStack(app, "DataStack", env=env)
lifecycle = LifecycleStack(app, "LifecycleStack", data_stack=data, env=env)
orchestration = OrchestrationStack(
    app, "OrchestrationStack", data_stack=data, lifecycle_stack=lifecycle, env=env
)
api = ApiStack(
    app,
    "ApiStack",
    user_pool=identity.pool,
    user_pool_client_id=identity.client.user_pool_client_id,
    data_stack=data,
    state_machine_arn=orchestration.state_machine_arn,
    lifecycle_reaper_arn=lifecycle.reaper.function_arn,
    env=env,
)
billing = BillingStack(app, "BillingStack", env=env)
web = WebStack(app, "WebStack", env=env)
observability = ObservabilityStack(app, "ObservabilityStack", env=env)
if str(app.node.try_get_context("enable_naive")).lower() == "true":
    NaiveAdmissionExperimentStack(app, "NaiveAdmissionExperimentStack", data_stack=data, env=env)
for stack in [identity, data, api, orchestration, lifecycle, billing, web, observability]:
    stack.add_dependency(data) if stack is not data else None
api.add_dependency(identity)
api.add_dependency(orchestration)
orchestration.add_dependency(lifecycle)
app.synth()
