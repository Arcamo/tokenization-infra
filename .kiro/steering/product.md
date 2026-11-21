---
inclusion: always
---

# Product: real estate tokenization infrastructure

This repo holds the AWS infrastructure (IaC) for the "Tokenización Inmobiliaria" MVP. Investors and sellers use a web portal (an Amplify Hosting app created by hand) to tokenize real estate on the Stellar network. The backend is a set of .NET Lambdas behind API Gateway, with a Step Functions saga, KYC providers, and Stellar transactions signed by a KMS Ed25519 key.

## Scope of this repo

- Infrastructure only: AWS CDK v2 in Python.
- No application code. Business logic, Stellar transaction building, KYC integration code and front-end code are out of scope. Do not add handler code or placeholder Lambdas.
- No Amplify resources. Every Amplify Hosting app (the portal now, the Stellar agent UI and backoffice later) is created and configured by hand: app, branch, SPA rewrite rule, repository, custom domain and environment variables. The IaC reads the portal origin from the stage config (`portal.origin`) and publishes what the front end needs as SSM parameters.
- Lambda zips and the AgentCore runtime artifact are built by the application repo, `../TokenizacionInmobiliaria`. This repo references them by S3 bucket, key and optional version in the stage config (Artifact_References).
- Both repos open together through `../tokenizacion.code-workspace`.

## Specs (source of truth)

The spec documents decide what gets built. When steering and a spec disagree, the spec wins; update the steering.

1. `real-estate-tokenization-iac` (approved, ready to implement): the core infrastructure. VPC, KMS and secrets, Cognito (including the portal's app client), API Gateway, five domain Lambdas plus the signer Lambda, RDS PostgreSQL behind RDS Proxy, DynamoDB, the S3 documents bucket, Step Functions, EventBridge, Scheduler, SQS with DLQs, minimal observability, tests, and GitHub Actions CI/CD.
   - `.kiro/specs/real-estate-tokenization-iac/requirements.md`
   - `.kiro/specs/real-estate-tokenization-iac/design.md`
   - `.kiro/specs/real-estate-tokenization-iac/tasks.md`
2. `tokenization-agentcore-iac` (planned, not written): Bedrock AgentCore Runtime, Memory and Gateway, the AgentCore DynamoDB table, a Cognito machine-to-machine client for the Gateway, and the Cognito settings for the Stellar agent UI (its Amplify app is made by hand). It reads spec 1 resources through SSM parameters under `/{project}/{stage}/`.
3. Undecided, scoped later: WAF (API and Amplify), GuardDuty, the Kinesis/Firehose/Glue/Athena analytics and audit pipeline, CloudTrail, the CloudWatch dashboard, Cognito and API settings for the backoffice front end (its Amplify app is made by hand), Cognito threat protection.

Do not add deferred items (spec 2 or 3) to spec 1. If a task seems to need one, stop and ask.

## Decisions made with the team (spec 1)

- One AWS account. Only the `dev` stage exists; adding a stage means adding a YAML file, not changing stack code. Region is configurable, default `us-east-1`.
- NAT gateways: configurable 1 to 2, dev uses 1. Zero is not allowed because Lambdas need egress to Stellar, the Anchor and KYC providers.
- No CloudFormation custom resources. The allowlist is empty and a test enforces it.
- Later specs integrate through SSM parameters, never CloudFormation exports.
- Amplify apps are created by hand (D4, O8). There is no portal branch stack. Order: create the Amplify app and branch, put its origin in `portal.origin`, deploy, then set the Amplify environment variables from the SSM parameters (`identity/user-pool-id`, `identity/portal-client-id`, `identity/hosted-ui-domain`, `api/base-url`) and the region. If the portal domain changes, update `portal.origin` and redeploy, or sign-in redirects and CORS break.
- Front ends send Cognito access tokens, not ID tokens, because API Gateway checks scopes only on access tokens.
- Approved deviations, recorded in the design (D1 to D11, O4 to O8):
  - Availability Zones are fixed in the stage config;
  - the GitHub OIDC bootstrap stack lives in its own `bootstrap_app.py`, with a deploy role (merges to `main`) and a read-only diff role (pull requests);
  - CORS on API Gateway 4XX/5XX responses allows only the portal origin;
  - log groups use default CloudWatch encryption;
  - PKCE is enforced by the SPA library, because Cognito cannot require it;
  - cdk-nag v3, falling back to v2 only if v3's Python API does not fail synthesis on errors.

Open items still to confirm are listed under "Risks and Open Items" in the design (O1 access tokens with the front-end team, O2 OIDC subject format, O3 runtime scheduler permissions, O9 account-wide API Gateway role, O10 Lambda timeouts above 29 s, O11 export changes need two deploys).
