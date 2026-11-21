---
inclusion: fileMatch
fileMatchPattern: ["src/tokenization_infra/stacks/**", "src/tokenization_infra/constructs/**", "src/tokenization_infra/nag/**", "src/tokenization_infra/app_builder.py", "app.py", "bootstrap_app.py", "cdk.json"]
---

# CDK conventions

Details per stack are in `.kiro/specs/real-estate-tokenization-iac/design.md` ("Components and Interfaces"). Follow it; these rules are the ones that are easy to break.

## No custom resources

Use L2 or L1 constructs from `aws-cdk-lib` only. These options add custom resources and must stay off:

- Lambda `log_retention`: create an explicit `logs.LogGroup` with retention and pass `log_group=`.
- S3 `auto_delete_objects`: never set it.
- RDS `cloudwatch_logs_retention`: pre-create `/aws/rds/instance/{stage}-db/postgresql` instead.
- VPC `restrict_default_security_group`: `False`, and the `@aws-cdk/aws-ec2:restrictDefaultSecurityGroup` flag stays `false` in `cdk.json`.
- `iam.OpenIdConnectProvider`: use `iam.OidcProviderNative`.

`core.custom_resources.ALLOWLIST` stays empty. Adding an entry needs a user decision.

## No Amplify resources

Amplify Hosting apps are created by hand (design D4). Do not add `AWS::Amplify::*` resources, the `aws-amplify-alpha` package, or a portal branch stack; a test fails on any Amplify resource. Values the front end needs are published as SSM parameters by the stack that owns them.

## Naming and tags

- Every stack id and explicit resource name starts with `{stage}-` (log groups with `/{stage}/`). Use `core.naming` helpers, not f-strings scattered in stacks.
- `build_app` tags everything with `project`, `stage` and `managed-by=cdk`.
- All values come from the typed `StageConfig`. No hardcoded account ids, regions, AZs, sizes or emails in stack code.

## Security defaults

- Encrypt with the Data_Key (customer managed KMS) for RDS, DynamoDB, SQS, SNS, Secrets Manager and the documents bucket. Exceptions are documented: log groups use default CloudWatch encryption (D9), and the access log bucket uses SSE-S3.
- Buckets go through `SecureBucket` (block all public access, `enforce_ssl`, bucket-owner-enforced).
- Security groups use `allow_all_outbound=False` with explicit rules.
- Exclusive grants, each backed by a Deny statement (D6): only the signer role may `kms:Sign`; only `stellar-kyc` may invoke the signer, read Kyc_Secrets, and (with the signer) call `kms:GetPublicKey`. Tests scan every IAM policy for these.
- Secret values are never written into templates. Kyc_Secrets hold the placeholder `{}`.
- KMS keys are always `RETAIN`. Other removal policies and `deletion_protection` follow `removal_policy` in the stage config. Stacks set `termination_protection` when it is `RETAIN`.

## cdk-nag

- `AwsSolutionsChecks` runs on every synth through `cdk.Validations.of(app).add_plugins(...)`. A finding without an acknowledgement fails synthesis.
- Put every acknowledgement in `nag/acknowledgements.py`, scoped to the narrowest construct and finding id (with sub-id, for example `AwsSolutions-IAM5[Resource::*]`), with a reason of at least 20 characters.
- Fix the finding first. Acknowledge only when the design's expected acknowledgements table covers it or the user agrees.
- Each stack task adds its own acknowledgements, so `uv run pytest` stays green after every task.

## Cross-stack references

- Strong references become CloudFormation exports. Removing or renaming an exported output that another stack imports fails the deploy; change it in two deploys (O11).
- Later specs read SSM parameters under `/{project}/{stage}/`. Spec 1 never reads its own parameters.

## Verify APIs

The CDK Python API changes between releases. Check unfamiliar constructs and enums against the AWS documentation MCP server (pinned `aws-cdk-lib==2.277.0`) before using them.
