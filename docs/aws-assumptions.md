# AWS assumptions and verification status

No AWS account was queried and no AWS resources were created. The following remain assumptions, not verified facts:

- Price List API supports the selected product queries and fields; actual ap-south-1 instance and gp3 rates and query dimensions.
- CUR 2.0/Data Exports availability, hourly/resource ID configuration, exported tag columns, line-item schema, and Athena/Glue partition behavior.
- IAM authorization evaluation for `RunInstances`, `CreateTags`, AMIs, volumes, networking resources, pass-role and reservation-bound STS tags.
- M6 `ProvisionerRole` IAM condition-key enforcement for instance type, region, configured subnet/security group, gp3 volume size/type, request tags and reservation/team principal tags; no live positive/negative IAM evaluation has been run.
- The configured subnet has no NAT route, the configured security group has no ingress, the regional SSM Amazon Linux 2023 parameter resolves to an approved AMI, and EC2 applies the requested instance/volume tag specifications and cleanup semantics.
- Regional availability/quotas for all required services, credits, and budget notification state.
- CDK synthesis/deployment, Cognito callback and hosted UI URLs, CloudFront OAC, alarm delivery and SNS confirmation.
- Any live pricing, billing, latency, or cleanup behavior.

Each item must be verified with actual account queries and evidence recorded in `docs/verify-log.md`. AWS deployment, EC2 launches, billing configuration, account budget changes, and destructive cleanup require explicit approval.
