# Project context: real estate tokenization infrastructure

This repo holds the AWS infrastructure (IaC) for the "Tokenización Inmobiliaria" MVP. The architecture covers Amplify front ends, API Gateway with .NET Lambdas, RDS PostgreSQL behind RDS Proxy, EventBridge/SQS, a Step Functions saga, Stellar signing with KMS, and an AI concierge on Bedrock AgentCore.

## Related repositories

- `tokenization-infra` (this repo): infrastructure only. AWS CDK v2 in Python.
- `TokenizacionInmobiliaria` (`../TokenizacionInmobiliaria`): the application repo. It builds the .NET Lambda packages and the AgentCore runtime artifact. This repo references those artifacts by S3 key in the stage config and contains no handler code or placeholders.
- Both repos open together through the multi-root workspace file `../tokenizacion.code-workspace`.

## Specs

The work is split into several specs, built in order.

1. `real-estate-tokenization-iac` (current, approved, ready to implement): the minimal core infrastructure. It covers the VPC, KMS and secrets, Cognito, one Amplify SPA (the investor/seller portal), API Gateway, the five domain Lambdas plus the signer Lambda, RDS with RDS Proxy, DynamoDB, the S3 documents bucket, Step Functions, EventBridge, Scheduler, SQS with DLQs, minimal observability, tests and GitHub Actions CI/CD.
   - Requirements: `.kiro/specs/real-estate-tokenization-iac/requirements.md`
   - Design: `.kiro/specs/real-estate-tokenization-iac/design.md`
   - Tasks: `.kiro/specs/real-estate-tokenization-iac/tasks.md`
2. `tokenization-agentcore-iac` (planned, not yet written): Bedrock AgentCore Runtime, Memory and Gateway, the AgentCore DynamoDB table, a Cognito machine-to-machine client for the Gateway, and the Stellar agent UI Amplify app. It builds on spec 1 through the SSM parameters under `/{project}/{stage}/`.
3. Undecided, to be scoped later: WAF (API and Amplify), GuardDuty, the Kinesis/Firehose/Glue/Athena analytics and audit pipeline, CloudTrail, the CloudWatch dashboard, the backoffice Amplify app and Cognito threat protection.

Treat the spec documents as the source of truth. Do not add deferred items to spec 1.

## Decisions made with the team (spec 1)

- Single AWS account. Only the `dev` stage exists for now, and stage config is parameterized so more stages can be added. Region is configurable and defaults to `us-east-1`.
- NAT gateways are configurable from 1 to 2, and dev uses 1.
- No CloudFormation custom resources. The allowlist is empty and a test enforces it.
- Later specs integrate through SSM parameters, not CloudFormation exports.
- Approved deviations recorded in the design:
  - a separate `{stage}-portal-branch` stack, to break the API/Amplify dependency cycle;
  - Availability Zones fixed in the stage config;
  - the GitHub OIDC bootstrap stack lives in its own `bootstrap_app.py`, with a deploy role (merges to `main`) and a read-only diff role (pull requests);
  - CORS on API Gateway 4XX/5XX responses allows only the portal origin;
  - log groups use default CloudWatch encryption;
  - PKCE is enforced by the SPA library because Cognito cannot require it;
  - cdk-nag v3, with a fallback to v2 if its Python API does not fail synthesis on errors.
- Front ends send Cognito access tokens (not ID tokens), because API Gateway checks scopes only on access tokens.

## Environment and tooling

- AWS credentials come from `AWS_PROFILE`, which `.envrc` (direnv plus devbox) exports. It is an IAM Identity Center (SSO) profile. When calls fail with expired credentials, run `aws sso login --profile "$AWS_PROFILE"`.
- The machine is Linux aarch64. Tooling for spec 1: uv, Python 3.12, and Node only for the CDK CLI.
- Workspace MCP servers are in `.kiro/settings/mcp.json`:
  - `awslabs.aws-documentation-mcp-server`: use it for AWS, CDK and CloudFormation documentation lookups.
  - `bedrock-agentcore-mcp-server`: AgentCore documentation.
  - `awslabs.aws-pricing-mcp-server`: it reads `${AWS_PROFILE}`. Kiro only fills that in when the variable is approved in "Mcp Approved Env Vars" and set in Kiro's own process environment. If pricing calls fail, the variable is probably missing from Kiro's environment.
  - `awslabs.aws-iac-mcp-server` is disabled. Its `guardpycfn` dependency has no aarch64 wheel and fails to build from source.
