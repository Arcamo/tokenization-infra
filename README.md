# Tokenization infrastructure

AWS infrastructure (AWS CDK v2, Python) for the "Tokenización Inmobiliaria" MVP: VPC, KMS and secrets, Cognito, API Gateway, five .NET domain Lambdas plus a KMS signer Lambda, RDS PostgreSQL behind RDS Proxy, DynamoDB, S3, Step Functions, EventBridge, Scheduler, SQS with DLQs, minimal observability, and GitHub Actions CI/CD.

This repository holds infrastructure only. Lambda zips are built by the application repository (`../TokenizacionInmobiliaria`) and referenced by S3 location. Amplify Hosting apps are created by hand; this repository creates no Amplify resources.

Specs: `.kiro/specs/real-estate-tokenization-iac/` (`requirements.md`, `design.md`, `tasks.md`). Decision ids (D1 to D11) and open items (O1 to O11) below refer to `design.md`.

Stacks, named `{stage}-<name>`: `security`, `network`, `identity`, `data`, `messaging`, `api-compute`, `observability`. The `{stage}-cicd-bootstrap` stack is deployed separately (see [Deploy the Bootstrap_Stack](#3-deploy-the-bootstrap_stack)).

## Prerequisites

- [devbox](https://www.jetify.com/devbox) with direnv. `devbox.json` and `.envrc` provide the shell: Python 3.12, uv and Node.js 24 LTS (Node is only for the CDK CLI). Run every command inside this shell.
- AWS credentials through `AWS_PROFILE`, exported by `.envrc`. It is an IAM Identity Center (SSO) profile. When credentials expire, run `aws sso login --profile "$AWS_PROFILE"`.
- The GitHub CLI (`gh`), authenticated with admin rights on the repository, for the OIDC and environment steps.
- Synthesis and tests need no AWS credentials and no Docker.

Install dependencies:

```bash
uv sync          # Python dependencies and the venv, from uv.lock
npm ci           # the pinned CDK CLI (aws-cdk 2.1148.1)
```

Pins (do not change without agreement): Python 3.12, `aws-cdk-lib==2.277.0`, `constructs>=10,<11`, `cdk-nag==3.0.2`, `pydantic>=2,<3`. Add Python dependencies with `uv add`, never with `pip`.

### JSII cache error

If the CDK CLI or synthesis fails with a JSII runtime package cache error, either run

```bash
chmod g-w,o-w ~/.cache/aws
```

or disable the cache for the shell:

```bash
export JSII_RUNTIME_PACKAGE_CACHE=disabled
```

## Setup and first deployment

Do these steps in order. Steps 1 to 5 are one-time per account and stage.

### 1. Fill in the Stage_Config placeholders

`config/stages/dev.yaml` ships with placeholders. Replace them before any real deployment:

- `account`: the 12-digit AWS account id (placeholder `000000000000`). The deploy workflow compares it with the caller identity and stops on a mismatch.
- `observability.alarm_emails`: real recipients (placeholder `ops@example.com`).
- `cicd.github_owner`: the GitHub organization or user that owns this repository (placeholder `example-owner`).
- `portal.origin`: the Amplify origin (see [Portal_App manual setup](#portal_app-manual-setup)).
- Every Artifact_Reference bucket and key (see [Artifact_References](#artifact_references)).

### 2. CDK bootstrap

Bootstrap the target account and region once, using the qualifier in `cicd.cdk_qualifier` (default `hnb659fds`, the CDK default):

```bash
npx cdk bootstrap aws://<account>/<region>
```

The Deploy_Role and Diff_Role only assume the CDK bootstrap roles `cdk-<qualifier>-{deploy,file-publishing,image-publishing,lookup}-role-<account>-<region>`. If you bootstrapped with a custom qualifier, set `cicd.cdk_qualifier` to match.

### 3. Deploy the Bootstrap_Stack

The Bootstrap_Stack (`{stage}-cicd-bootstrap`) holds the GitHub OIDC provider, the Deploy_Role (`{stage}-github-deploy`) and the Diff_Role (`{stage}-github-diff`). It lives in its own CDK app so the pipeline's `cdk deploy --all` never deploys it. An administrator deploys it once:

```bash
npx cdk --app "uv run --frozen python bootstrap_app.py" deploy -c stage=dev
```

(`cdk.json` sets the default `stage=dev`, so `-c stage=dev` is optional for `dev`.)

The stack prints two outputs: `DeployRoleArn` and `DiffRoleArn`. An AWS account allows one GitHub OIDC provider. If the account already has one, set `cicd.create_oidc_provider: false` in the stage file to import it instead of creating it.

To read the outputs later:

```bash
aws cloudformation describe-stacks --stack-name dev-cicd-bootstrap \
  --query "Stacks[0].Outputs" --output table
```

### 4. Set the OIDC subject claim template (open item O2)

The trust policies expect subjects that include the environment and the branch ref. GitHub's default subject has neither, so set the repository's claim template once:

```bash
gh api --method PUT repos/<owner>/<repo>/actions/oidc/customization/sub --input - <<'JSON'
{ "use_default": false, "include_claim_keys": ["repo", "context", "ref"] }
JSON
```

The subjects become:

- Deploy_Role: `repo:<owner>/<repo>:environment:<stage>:ref:refs/heads/<branch>` (exact match).
- Diff_Role: `repo:<owner>/<repo>:pull_request:ref:refs/pull/*` (wildcard).

Confirm the format against a real token on the first workflow run. If it differs, only `src/tokenization_infra/core/oidc.py` changes.

### 5. GitHub environment, branch rule and repository variables

Create a GitHub environment named after the stage and restrict it to the `main` branch:

```bash
gh api --method PUT repos/<owner>/<repo>/environments/dev --input - <<'JSON'
{ "deployment_branch_policy": { "protected_branches": false, "custom_branch_policies": true } }
JSON
gh api --method POST repos/<owner>/<repo>/environments/dev/deployment-branch-policies \
  -f name=main -f type=branch
```

Set the two repository variables from the Bootstrap_Stack outputs (variables, not secrets; no AWS keys are stored):

```bash
gh variable set AWS_DEPLOY_ROLE_ARN --repo <owner>/<repo> --body "<DeployRoleArn>"
gh variable set AWS_DIFF_ROLE_ARN   --repo <owner>/<repo> --body "<DiffRoleArn>"
```

| Variable | Used by | Role |
|---|---|---|
| `AWS_DEPLOY_ROLE_ARN` | `.github/workflows/deploy.yml` (push to `main`, environment `dev`) | `{stage}-github-deploy` |
| `AWS_DIFF_ROLE_ARN` | `.github/workflows/pr.yml` (pull requests to `main`) | `{stage}-github-diff` |

Pipeline behavior is confirmed by the first real pull request and merge, not by unit tests. Fork pull requests get no OIDC token, so their diff step is skipped.

## Stage_Config

One file per stage: `config/stages/<stage>.yaml`. The file name must equal the `stage` field. Keys are snake_case. Models are pydantic v2 (`src/tokenization_infra/core/config.py`) and unknown keys are errors. Validation errors print one line per issue as `<stage>: <dotted.path>: <reason>`. An unknown stage prints `Unknown stage "<x>". Known stages: dev` and exits non-zero before any stack is built.

| Field | Meaning |
|---|---|
| `stage` | Stage name, `^[a-z][a-z0-9]{1,9}$`. Prefix of every stack id and resource name. |
| `project` | Project name, `^[a-z][a-z0-9-]{1,19}$`. First segment of the SSM parameter path. |
| `account` | 12-digit AWS account id. |
| `region` | AWS region. Default `us-east-1`. |
| `removal_policy` | `RETAIN` or `DESTROY`. Applies to data resources, deletion protection and stack termination protection. KMS keys are always retained. |
| `network.nat_gateways` | 1 or 2 (default 1). Zero is not allowed because Lambdas need egress to Stellar, the Anchor and KYC providers. |
| `network.cidr` | VPC CIDR, prefix `/16` to `/20`. Default `10.40.0.0/16`. |
| `network.availability_zones` | Exactly 2 AZs in `region`. Explicit so synthesis needs no AWS lookup (D8). |
| `lambda_runtime` | `dotnet8` (default) or `dotnet10`. |
| `lambda_architecture` | `arm64` (default) or `x86_64`. Must match the published zips. |
| `functions.<id>.artifact` | Artifact_Reference: `bucket`, `key`, optional `version`. |
| `functions.<id>.handler` | .NET handler string `Assembly::Namespace.Class::Method`. |
| `functions.<id>.memory_mb`, `timeout_seconds` | Sizing. A domain function timeout above 29 s only warns (O10). |
| `functions.<id>.reserved_concurrency` | Optional. |
| `functions.<id>.uses_database` | Grants RDS Proxy connect as `app_user`. The signer cannot use it. |
| `functions.<id>.data_access` | Any of `idempotency-table`, `saga-table`, `documents-bucket`. The signer cannot have any. |
| `database.engine_version`, `instance_class`, `allocated_storage_gb` (20 to 1000), `multi_az`, `backup_retention_days` (1 to 35) | RDS PostgreSQL sizing. |
| `database.database_name`, `database.app_user` | Optional. Defaults `tokenization` and `app_user`. |
| `kyc_providers` | Provider ids (kebab-case, at least one). Each gets two secrets. |
| `cognito.domain_prefix` | Hosted UI domain prefix. Must start with `<stage>-` and must not contain `aws`, `amazon` or `cognito`. |
| `cognito.groups` | Optional. Default `investor`, `seller`, `backoffice`. |
| `cognito.resource_server` | `identifier` and `scopes` (`name`, `description`). |
| `portal.origin` | HTTPS origin of the hand-made Amplify branch or its custom domain. No path, query or fragment. |
| `portal.callback_path`, `portal.logout_path` | Optional. Defaults `/callback` and `/`. |
| `portal.scopes` | Resource server scope names the Portal_Client may request. |
| `portal.local_dev_callback_urls` | Optional `http://localhost` or `http://127.0.0.1` callback URLs. |
| `api.throttling_rate_limit`, `throttling_burst_limit` | Stage throttling. |
| `api.route_scopes` | Per domain function, the full scopes (`<identifier>/<scope>`) its routes require. |
| `events.archive_retention_days` | EventBridge archive retention. |
| `queues[]` | `name`, `consumer`, `max_receive_count`, `visibility_timeout_seconds` (not below the consumer timeout), optional `batch_size`. |
| `rules[]` | `name`, `event_pattern`, `target` (`queue` or `state-machine`). |
| `saga.definition_file` | Path to the state machine definition, relative to the repo root. Allowed placeholders: `${CollateralIdentityFunctionArn}`, `${SagasPaymentsFunctionArn}`, `${ReportsFunctionArn}`, `${StellarKycFunctionArn}`, `${TradingOperationsFunctionArn}`. `${SignerFunctionArn}` is rejected. |
| `scheduler.targets[]` | `lambda` (a domain function), `state-machine` or `queue`. The signer is not a valid target. |
| `observability.log_retention_days`, `alarm_emails`, `access_log_expiration_days` | Log retention (valid CloudWatch values), Alert_Topic recipients, access log lifecycle. |
| `cicd.github_owner`, `github_repo` | Repository the OIDC roles trust. |
| `cicd.branch`, `cdk_qualifier`, `create_oidc_provider` | Optional. Defaults `main`, `hnb659fds`, `true`. |

### Add a stage

Adding a stage means adding a file; stack code does not change.

1. Copy `config/stages/dev.yaml` to `config/stages/<stage>.yaml`.
2. Set `stage: <stage>` (it must equal the file name), `account`, `region`, availability zones, `cognito.domain_prefix` (starting with `<stage>-`), `portal.origin`, artifacts, `alarm_emails` and the `cicd` section.
3. Run `uv run pytest` and `npx cdk synth -c stage=<stage>`.
4. Deploy the Bootstrap_Stack for the stage, create the GitHub environment named `<stage>`, and add a deploy workflow job for it. The shipped workflows target `dev` only.

## Deploy commands

Deploys normally run through the GitHub Actions deploy workflow on merge to `main`. Pull requests run lint, format check, mypy, pytest, `cdk synth` and a `cdk diff` comment.

For a manual run by an administrator:

```bash
npx cdk synth -c stage=dev                 # synthesize; cdk-nag runs here
npx cdk diff --all -c stage=dev
npx cdk deploy --all -c stage=dev          # every app stack, not the Bootstrap_Stack
```

Deleting a stage with `removal_policy: DESTROY`: KMS keys are always retained, and `auto_delete_objects` is not used, so empty the Documents_Bucket and the Access_Log_Bucket before `cdk destroy` or the stack deletion fails.

## Artifact_References

The application repository must publish every Lambda zip to S3 before the first deploy and before every deploy that changes code. Each entry in `functions` carries an `artifact` with `bucket`, `key` and an optional `version` (S3 object version id). The zip must target `lambda_runtime` and `lambda_architecture`.

| Key path | `dev.yaml` key | Fields |
|---|---|---|
| `functions.collateral-identity.artifact` | `lambdas/collateral-identity/1.0.0.zip` | `.bucket`, `.key`, `.version` |
| `functions.sagas-payments.artifact` | `lambdas/sagas-payments/1.0.0.zip` | `.bucket`, `.key`, `.version` |
| `functions.reports.artifact` | `lambdas/reports/1.0.0.zip` | `.bucket`, `.key`, `.version` |
| `functions.stellar-kyc.artifact` | `lambdas/stellar-kyc/1.0.0.zip` | `.bucket`, `.key`, `.version` |
| `functions.trading-operations.artifact` | `lambdas/trading-operations/1.0.0.zip` | `.bucket`, `.key`, `.version` |
| `functions.signer.artifact` | `lambdas/stellar-signer/1.0.0.zip` | `.bucket`, `.key`, `.version` |

In `dev.yaml` all six use the bucket `tokenization-artifacts-dev`. The full paths are, for example, `functions.collateral-identity.artifact.bucket`, `functions.collateral-identity.artifact.key` and `functions.collateral-identity.artifact.version`.

**New code needs a new key or a version.** If the key is unchanged and `version` is not set, CloudFormation sees no change and does not update the function. Either publish versioned keys (for example `lambdas/reports/1.0.1.zip`) and update `key`, or enable versioning on the bucket and set `version` to the S3 object version id.

### Pre-deploy checklist

Confirm each object exists. A missing object fails the Lambda resource and rolls the stack back.

```bash
aws s3api head-object --bucket tokenization-artifacts-dev --key lambdas/collateral-identity/1.0.0.zip
aws s3api head-object --bucket tokenization-artifacts-dev --key lambdas/sagas-payments/1.0.0.zip
aws s3api head-object --bucket tokenization-artifacts-dev --key lambdas/reports/1.0.0.zip
aws s3api head-object --bucket tokenization-artifacts-dev --key lambdas/stellar-kyc/1.0.0.zip
aws s3api head-object --bucket tokenization-artifacts-dev --key lambdas/trading-operations/1.0.0.zip
aws s3api head-object --bucket tokenization-artifacts-dev --key lambdas/stellar-signer/1.0.0.zip
# add --version-id <id> when functions.<id>.artifact.version is set
```

## Post-deployment manual steps

1. **Populate Kyc_Secrets.** For each provider in `kyc_providers`, two secrets exist with the placeholder value `{}`: `<stage>/kyc/<provider>/api-credentials` and `<stage>/kyc/<provider>/webhook-signing`. Set the real values, for example:
   ```bash
   aws secretsmanager put-secret-value --secret-id dev/kyc/provider-a/api-credentials --secret-string file://creds.json
   ```
   Only the `stellar-kyc` function can read them.
2. **Confirm Alert_Topic email subscriptions.** Each address in `observability.alarm_emails` receives an SNS confirmation email. The subscription stays pending until the recipient confirms it. DLQ alarms notify the topic on `ALARM` and `OK`.
3. **Create the `app_user` PostgreSQL role (D5).** Lambdas connect through RDS Proxy as `app_user` (name from `database.app_user`), not as the master user. The IaC generates the `app_user` secret (`<stage>/db/app_user`) and registers it with the proxy. The application repository's migrations must create the PostgreSQL role with that name and the secret's password, and grant it access to the `tokenization` database.
4. **Set the Portal_App environment variables** from the Extension_Parameters (next section).
5. **Check the first pipeline run** to confirm the OIDC subject format (O2).

## Portal_App manual setup

The Portal_App is an Amplify Hosting app created by hand (D4, O8). The IaC reads its origin from `portal.origin` and publishes what the front end needs. Order matters:

1. **Before the first deployment**, create the Amplify app and branch. Note its HTTPS origin (for example `https://main.d1abc2def3.amplifyapp.com`, or a custom domain attached by hand).
2. Record the origin in `portal.origin` in `config/stages/<stage>.yaml` and deploy. Cognito callback and logout URLs and API CORS origins are derived from it.
3. Add the SPA rewrite rule in Amplify: source for paths without a static file extension, target `/index.html`, type 200 (rewrite).
4. **After deployment**, set the Amplify environment variables from these Extension_Parameters and the region. Amplify reserves the `AWS_` prefix, so choose other variable names (for example `VITE_...`):
   - `identity/user-pool-id`
   - `identity/portal-client-id`
   - `identity/hosted-ui-domain`
   - `api/base-url`
   - the AWS region (`region` in the stage file)
5. **If the portal domain changes**, update `portal.origin` and redeploy. Until then, sign-in redirects and CORS fail.

Front-end notes:

- **Send Cognito access tokens, not ID tokens (D3, O1).** API Gateway checks scopes only on access tokens. Confirm this with the front-end team.
- **PKCE is enforced by the SPA library (O6).** Cognito cannot require PKCE. The Portal_Client allows only the authorization code grant and has no secret.
- **CORS on API Gateway 4XX and 5XX gateway responses allows only `portal.origin` (O7).** Local development origins get no CORS headers on those error responses.

## Extension_Parameters

Each stack publishes SSM parameters for the resources it owns, under `/{project}/{stage}/`. Later specs read these and never use CloudFormation exports. Names come from `core/parameters.py` (`extension_parameter_names`). For `dev` with project `tokenization`, `network/vpc-id` is `/tokenization/dev/network/vpc-id`.

| Suffix | Full name | Meaning |
|---|---|---|
| `network/vpc-id` | `/{project}/{stage}/network/vpc-id` | VPC id |
| `network/private-subnet-ids` | `/{project}/{stage}/network/private-subnet-ids` | Private subnet ids (egress through NAT) |
| `network/isolated-subnet-ids` | `/{project}/{stage}/network/isolated-subnet-ids` | Isolated subnet ids (database, no internet route) |
| `network/lambda-security-group-id` | `/{project}/{stage}/network/lambda-security-group-id` | Security group of every domain Lambda and the signer |
| `api/id` | `/{project}/{stage}/api/id` | API Gateway REST API id |
| `api/root-resource-id` | `/{project}/{stage}/api/root-resource-id` | Root resource id of the REST API |
| `api/base-url` | `/{project}/{stage}/api/base-url` | Base URL of the deployed API stage |
| `identity/user-pool-id` | `/{project}/{stage}/identity/user-pool-id` | Cognito user pool id |
| `identity/user-pool-arn` | `/{project}/{stage}/identity/user-pool-arn` | Cognito user pool ARN |
| `identity/resource-server-identifier` | `/{project}/{stage}/identity/resource-server-identifier` | Resource server identifier that prefixes the API scopes |
| `identity/portal-client-id` | `/{project}/{stage}/identity/portal-client-id` | Cognito app client id of the Portal_App |
| `identity/hosted-ui-domain` | `/{project}/{stage}/identity/hosted-ui-domain` | Cognito Hosted UI domain |
| `keys/data-key-arn` | `/{project}/{stage}/keys/data-key-arn` | Data_Key (symmetric KMS key for data at rest) ARN |
| `keys/signing-key-arn` | `/{project}/{stage}/keys/signing-key-arn` | Signing_Key (Ed25519 KMS key for Stellar) ARN |
| `events/bus-name` | `/{project}/{stage}/events/bus-name` | EventBridge bus name |
| `events/bus-arn` | `/{project}/{stage}/events/bus-arn` | EventBridge bus ARN |
| `observability/alert-topic-arn` | `/{project}/{stage}/observability/alert-topic-arn` | Alert_Topic (SNS) ARN |

## Deferred to Later Specs

Out of scope for this spec (spec 1). Recorded so nothing is lost.

Spec 2 (planned), `tokenization-agentcore-iac`:

- Bedrock AgentCore Runtime hosting the agent artifact.
- Bedrock AgentCore Memory.
- Bedrock AgentCore Gateway exposing Api operations as agent tools, with inbound authorization against the User_Pool.
- The AgentCore DynamoDB table.
- A Cognito machine-to-machine app client (client credentials grant) for the AgentCore Gateway.
- The Cognito settings for the Stellar agent UI and the `stellar-agent` User_Pool group. The Stellar agent UI Amplify app itself is created by hand.

Undecided or later:

- AWS WAF web ACLs for the Api and for Amplify apps (managed rule groups and a rate-based rule).
- Amazon GuardDuty detector.
- Analytics and audit pipeline: Kinesis data stream, Kinesis Data Firehose, analytics S3 bucket, Glue database and table, Athena workgroup, and the related Domain_Lambda and Reports grants.
- AWS CloudTrail trail and Documents_Bucket data events.
- CloudWatch dashboard and alarms beyond DLQ alarms (Lambda errors and throttles, Api 5XX, Saga_State_Machine failures, Database CPU).
- The Cognito and API settings for the backoffice front end. Its Amplify app is created by hand.
- Cognito advanced security features (threat protection).

## Development

```bash
uv run ruff check .                # lint
uv run ruff format --check .       # format check (uv run ruff format . to fix)
uv run mypy                        # strict type check
uv run pytest                      # all tests, single run
npx cdk synth -c stage=dev         # synth app stacks; cdk-nag runs here
npx cdk --app "uv run --frozen python bootstrap_app.py" synth -c stage=dev   # Bootstrap_Stack
```

**Definition of done:** lint, format check, mypy, pytest and `cdk synth` all pass. A cdk-nag finding without an acknowledgement (in `src/tokenization_infra/nag/acknowledgements.py`, with a reason) fails synthesis.

Conventions: `src/tokenization_infra/core` is pure Python and never imports `aws_cdk` or `constructs`. Use absolute imports only. There are no CloudFormation custom resources (the allowlist is empty and a test enforces it) and no `AWS::Amplify::*` resources.

## Open items to confirm

From "Risks and Open Items" in `design.md`:

| Id | Item | Status |
|---|---|---|
| O1 | The Portal_App must send access tokens, not ID tokens (D3). | Confirm with the front-end team. |
| O2 | GitHub OIDC subject format. | Confirm against a real token on the first run. Only `core/oidc.py` changes if it differs. |
| O3 | Scheduler use at runtime. Only the schedule group and execution role exist. If Domain_Lambdas create schedules at runtime, they also need `scheduler:CreateSchedule` and `iam:PassRole`. | Not designed. |
| O4 | cdk-nag v3 behavior. Fall back to cdk-nag v2 only if v3 does not fail synthesis on errors. | Decided. |
| O5 | Log groups use default CloudWatch encryption (D9). | Decided. |
| O6 | PKCE is enforced by the SPA library, not Cognito. | Decided. |
| O7 | CORS on gateway 4XX and 5XX responses allows only `portal.origin`. | Decided. |
| O8 | Amplify apps are created by hand, including later apps. | Decided. |
| O9 | `AWS::ApiGateway::Account` is account-wide. If another stack manages it, set `cloud_watch_role=False`. | Check per account. |
| O10 | A Domain_Lambda timeout above 29 s is allowed, but API calls to it still time out at 29 s. Config validation warns. | Known. |
| O11 | Removing or renaming an output that another stack imports fails the deployment. Change such references in two deploys. | Known. |
