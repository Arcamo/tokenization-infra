# Implementation Plan: real-estate-tokenization-iac

## Overview

The plan builds the spec 1 AWS CDK v2 app in Python in this order: toolchain, the pure `tokenization_infra.core` package, then one stack at a time in the dependency order of the stack graph, then the app-level tests, the separate Bootstrap_Stack entry point, the GitHub Actions workflows and the README.

cdk-nag (`AwsSolutionsChecks`) and the custom-resource scan are wired in with the first stack. Every later stack task must therefore add its own acknowledgements to `src/tokenization_infra/nag/acknowledgements.py` and keep the empty custom-resource allowlist intact, so `uv run pytest` stays green after each task.

Assertion tests that Requirement 12 demands are core tasks. Hypothesis property tests (Properties 1 to 12) and the strategies module are optional (`*`).

All commands run inside the devbox shell: `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy`, `uv run pytest`, `npx cdk synth -c stage=dev`.

## Tasks

- [ ] 1. Scaffold the project toolchain
  - [ ] 1.1 Create the toolchain and project skeleton
    - `devbox.json`: replace `python@latest` with `python@3.12`, keep `uv@latest`, add `nodejs@24`; replace the placeholder `test` script with `test: uv run pytest`, and add `lint` (`uv run ruff check . && uv run ruff format --check . && uv run mypy`) and `synth` (`npx cdk synth -c stage=dev`) scripts
    - `.python-version` = `3.12`
    - `pyproject.toml`: `requires-python = ">=3.12"`, src layout package `tokenization_infra`; dependencies `aws-cdk-lib==2.277.0`, `constructs>=10,<11`, `cdk-nag==3.0.2`, `pydantic>=2,<3`, `PyYAML`; dev group `pytest`, `hypothesis`, `ruff`, `mypy`, `types-PyYAML`
    - Ruff config with `TID252` (ban relative imports) enabled; mypy `strict = true` with the `pydantic.mypy` plugin, `files = ["src", "tests", "app.py", "bootstrap_app.py"]`; pytest `testpaths = ["tests"]`
    - Run `uv lock` to create `uv.lock`
    - `package.json` and `package-lock.json` containing only `aws-cdk` `2.1148.1` as a dev dependency (`npm install --save-exact`)
    - `cdk.json`: `"app": "uv run --frozen python app.py"`, context `"stage": "dev"`, current recommended feature flags with `"@aws-cdk/aws-ec2:restrictDefaultSecurityGroup": false`
    - `.gitignore` for `cdk.out/`, `node_modules/`, `.venv/`, caches
    - Empty packages: `src/tokenization_infra/{__init__,app_builder}.py` placeholder, `core/`, `constructs/`, `stacks/`, `nag/` with `__init__.py`; `tests/core/`, `tests/stacks/`
    - `tests/conftest.py` registering and loading the hypothesis `ci` profile (`max_examples=100`, `deadline=None`)
    - _Requirements: 1.1, 1.13_

  - [ ] 1.2 Write the toolchain smoke tests
    - `tests/test_project.py`: `cdk.json` has `restrictDefaultSecurityGroup` set to `false`; no module under `src/tokenization_infra/core` imports `aws_cdk` or `constructs` (parse import statements with `ast`)
    - Run `uv sync`, `uv run ruff check .`, `uv run mypy`, `uv run pytest`
    - _Requirements: 1.1, 1.13_

- [ ] 2. Implement Stage_Config parsing and the saga definition helpers
  - [ ] 2.1 Implement `core/errors.py`
    - `ConfigError`, `UnknownStageError(ConfigError)` with message `Unknown stage "<x>". Known stages: <sorted list>`, and `format_loc` (`("queues", 2, "name")` gives `queues[2].name`)
    - _Requirements: 1.5, 1.6_

  - [ ] 2.2 Implement the pydantic models and loaders in `core/config.py`
    - `StrictModel` (`extra="forbid"`, `frozen=True`), `FunctionId`, `DomainFunctionId`, `Kebab`, `LogRetentionDays` and every model listed in the design's Data Models section (`ArtifactReference`, `FunctionConfig`, `NetworkConfig` with `nat_gateways` 1 to 2 and exactly 2 AZs, `DatabaseConfig`, `CognitoConfig`, `ResourceServerConfig`, `ScopeConfig`, `CustomDomain`, `PortalConfig`, `ApiConfig`, `QueueConfig`, the discriminated rule and scheduler targets, `EventsConfig`, `SagaConfig`, `SchedulerConfig`, `ObservabilityConfig`, `CicdConfig`, `StageConfig` with `region` default `us-east-1`)
    - Cross-field `model_validator(mode="after")` checks from the design, each raising at the offending path: stage equals file name, AZs start with the region, all six function keys, signer has no database or data access, unique queue and rule names, rule and scheduler queue targets exist, no signer targets, visibility timeout at least the consumer timeout, route and portal scopes exist in the Resource_Server, repository and token secret set together, localhost-only dev URLs, `cognito.domain_prefix` starting with `{stage}-` (D11) and free of forbidden words, CIDR prefix `/16` to `/20`
    - Warning (not error) when a Domain_Lambda timeout exceeds 29 seconds (O10)
    - `parse_stage_config(raw, *, stage)` converts `ValidationError.errors()` into one `ConfigError` with lines `"{stage}: {format_loc(loc)}: {msg}"`; `resolve_stage(name, registry)`; `load_stage(name, config_dir)` with `yaml.safe_load`; `CONFIG_DIR` pointing at `<repo>/config/stages`
    - _Requirements: 1.2, 1.3, 1.4, 1.5, 1.6, 1.7, 1.9, 2.1, 2.2, 2.3, 4.2, 4.5, 4.8, 5.3, 9.7, 9.8, 9.9_

  - [ ] 2.3 Implement `core/saga.py` and hook it into config validation
    - Allowed placeholders `${CollateralIdentityFunctionArn}`, `${SagasPaymentsFunctionArn}`, `${ReportsFunctionArn}`, `${StellarKycFunctionArn}`, `${TradingOperationsFunctionArn}`; `${SignerFunctionArn}` and any other `${...}` raise an error naming the token
    - `referenced_functions(asl_text)` and `substitute(asl_text, arns)`; result must be valid JSON with no `${`
    - Add the `saga.definition_file` validator to `StageConfig` (file exists, valid JSON, allowed placeholders only); import `core.saga` inside the validator to avoid an import cycle
    - _Requirements: 9.2, 9.3, 6.7_

  - [ ] 2.4 Create the dev stage files
    - `config/stages/dev.yaml` following the design's dev configuration, with all six functions filled in, `account: "000000000000"` and `alarm_emails: ["ops@example.com"]` as operator placeholders (valid values so CI synthesis works; the deploy workflow's account check blocks deployment until replaced), and `cicd.github_owner` placeholder
    - `config/statemachines/tokenization-saga.asl.json`: a minimal Standard saga using `${SagasPaymentsFunctionArn}`, `${StellarKycFunctionArn}` and `${TradingOperationsFunctionArn}` in `Task` states with a compensation path
    - _Requirements: 1.3, 2.2, 9.2_

  - [ ] 2.5 Write unit tests for Stage_Config in `tests/core/test_config.py`
    - `load_stage("dev", CONFIG_DIR)` succeeds; region defaults to `us-east-1` when omitted
    - Unknown stage raises `UnknownStageError` naming it; a missing required field names its dotted path; `network.nat_gateways` of 0 and 3 fail at `network.nat_gateways`
    - One example per cross-field rule, including `rules[0].target.queue` naming an undefined queue, a saga file with `${SignerFunctionArn}`, and a `cognito.domain_prefix` without the `{stage}-` prefix failing at `cognito.domain_prefix`
    - _Requirements: 1.4, 1.5, 1.6, 1.7, 1.9, 2.3, 4.8, 12.8_

  - [ ]* 2.6 Write hypothesis strategies in `tests/core/strategies.py`
    - `valid_raw_configs()`, `stage_names()`, `asl_documents(subset)`, `local_dev_urls()`, `url_paths()`, `github_repos()`; cover empty queue, rule and scheduler lists, maximum-length names, `reserved_concurrency=None`, custom domain with and without prefix, empty local URL list
    - _Requirements: 1.2, 12.8_

  - [ ]* 2.7 Write property test for config parsing in `tests/core/test_config_parse_properties.py`
    - **Property 1: Config parsing preserves fields, defaults the region and is idempotent**
    - **Validates: Requirements 1.2, 1.7**

  - [ ]* 2.8 Write property test for stage resolution in `tests/core/test_stage_resolution_properties.py`
    - **Property 2: Stage resolution returns the matching entry or names the unknown stage**
    - **Validates: Requirements 1.4, 1.5**

  - [ ]* 2.9 Write property test for field-path errors in `tests/core/test_config_errors_properties.py`
    - **Property 3: Missing or invalid fields are reported by path**
    - **Validates: Requirements 1.6, 2.3, 12.8**

  - [ ]* 2.10 Write property test for dangling references in `tests/core/test_references_properties.py`
    - **Property 7: Dangling references are rejected with the offending path**
    - **Validates: Requirements 4.5, 5.3, 9.7, 9.8, 9.9**

  - [ ]* 2.11 Write property test for saga substitution in `tests/core/test_saga_properties.py`
    - **Property 8: Saga substitution is complete and matches the referenced functions**
    - **Validates: Requirements 9.2, 9.3**

- [ ] 3. Implement the remaining core helpers
  - [ ] 3.1 Implement `core/naming.py`
    - `NameKind` with per-kind character set and maximum length (bucket 63 lowercase, function 64, role 64, queue 80, log group 512); `resource_name`, `function_name` (`{stage}-{fn}`), `role_name` (`{stage}-{fn}-role`); deterministic, always prefixed with `{stage}-` (`/{stage}/` for log groups). The Hosted UI domain prefix is not generated here; it is validated in config (task 2.2)
    - _Requirements: 1.9_

  - [ ] 3.2 Implement `core/urls.py` and `core/spa.py`
    - `portal_origin`, `origin_of`, `callback_urls`, `logout_urls`, `cors_origins` (deduplicated, order-stable, never `*`, work with CDK token strings through plain concatenation)
    - `SPA_REWRITE_SOURCE` (the Amplify regex from the design) and `is_spa_route(path)` applying the inner regex
    - _Requirements: 4.6, 4.7, 4.10, 5.9_

  - [ ] 3.3 Implement `core/permissions.py`
    - `Capability` values and `permission_profile(cfg, fn)` following the permission matrix: `bus.put` for every Domain_Lambda, `saga.start` for sagas-payments, `signer.invoke` and `kyc_secrets.read` for stellar-kyc, `signing_key.get_public_key` for stellar-kyc and signer, `signing_key.sign` for signer, `db.connect` when `uses_database`, `data:<r>` per `data_access`, `queue.consume:<q>` per queue consumer; signer gets nothing else
    - _Requirements: 3.3, 3.4, 3.6, 6.6, 6.7, 7.5, 9.4, 9.8, 9.10_

  - [ ] 3.4 Implement `core/parameters.py`, `core/oidc.py` and `core/custom_resources.py`
    - `ExtensionKey` (15 keys) and `extension_parameter_names(project, stage)` producing `/{project}/{stage}/<suffix>` per the design table
    - `oidc_subjects(owner, repo, stage, branch)` returning `.deploy` = `repo:{owner}/{repo}:environment:{stage}:ref:refs/heads/{branch}` and `.pull_request` = `repo:{owner}/{repo}:pull_request:ref:refs/pull/*`
    - `AllowlistEntry`, `ALLOWLIST: tuple[AllowlistEntry, ...] = ()`, `find_custom_resources(template)` returning logical ids of `AWS::CloudFormation::CustomResource` and `Custom::*` resources
    - _Requirements: 1.12, 11.1, 11.2, 11.3, 11.4, 11.5, 11.6, 11.7, 13.5_

  - [ ]* 3.5 Write unit tests for the core helpers in `tests/core/test_helpers.py`
    - Example values for naming, URLs (custom domain with and without prefix), SPA paths (`/a/b` true, `/app.js` false), dev permission profiles, parameter names, OIDC subjects, and `find_custom_resources` on a hand-written template
    - _Requirements: 1.9, 1.12, 4.6, 4.10, 6.6, 11.1, 13.5_

  - [ ]* 3.6 Write property test for resource names in `tests/core/test_naming_properties.py`
    - **Property 4: Generated names carry the stage prefix and meet service limits**
    - **Validates: Requirements 1.9**

  - [ ]* 3.7 Write property test for exclusive capabilities in `tests/core/test_permissions_exclusive_properties.py`
    - **Property 5: Exclusive capabilities belong to the designated functions only**
    - **Validates: Requirements 3.3, 3.4, 3.6, 6.7**

  - [ ]* 3.8 Write property test for shared and conditional capabilities in `tests/core/test_permissions_profile_properties.py`
    - **Property 6: Shared and conditional capabilities follow the configuration**
    - **Validates: Requirements 6.6, 7.5, 9.4, 9.8, 9.10**

  - [ ]* 3.9 Write property test for portal URLs and CORS origins in `tests/core/test_urls_properties.py`
    - **Property 9: Portal URLs and CORS origins are exactly the derived set**
    - **Validates: Requirements 4.6, 4.7, 5.9**

  - [ ]* 3.10 Write property test for the SPA rewrite in `tests/core/test_spa_properties.py`
    - **Property 10: The SPA rewrite matches exactly the non-static paths**
    - **Validates: Requirements 4.10**

  - [ ]* 3.11 Write property test for extension parameter names in `tests/core/test_parameters_properties.py`
    - **Property 11: Extension parameter names are complete, unique and correctly prefixed**
    - **Validates: Requirements 11.1, 11.2, 11.3, 11.4, 11.5, 11.6, 11.7**

  - [ ]* 3.12 Write property test for OIDC subjects in `tests/core/test_oidc_properties.py`
    - **Property 12: OIDC subjects are bound to repository, stage and branch**
    - **Validates: Requirements 13.5**

- [ ] 4. Checkpoint - Core package complete
  - Run `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy` and `uv run pytest`. Ensure all tests pass, ask the user if questions arise.

- [ ] 5. Build the CDK foundation and the security stack
  - [ ] 5.1 Implement `nag/acknowledgements.py`
    - `register_nag(app)` using cdk-nag v3: `cdk.Validations.of(app).add_plugins(AwsSolutionsChecks(app))`; `acknowledge(construct, rule_id, reason)` wrapping `Validations.of(construct).acknowledge(...)` and rejecting reasons shorter than 20 characters; `apply_acknowledgements(stacks, cfg)` as the single place that holds every acknowledgement (start empty)
    - Verify the v3 Python API against the installed package. If it cannot fail synthesis on unacknowledged errors or its `acknowledge` signature differs, switch to the O4 fallback: pin `cdk-nag>=2,<3` in `pyproject.toml`, re-lock, and use `Aspects.of(app).add(AwsSolutionsChecks())` with `NagSuppressions`, changing only `nag/` and `app_builder.py`
    - _Requirements: 12.6, 12.7_

  - [ ] 5.2 Implement `SecurityStack` in `stacks/security.py`
    - Data_Key with rotation, alias `alias/{stage}-data`, `RETAIN`, key policy statements for `events.amazonaws.com` and `cloudwatch.amazonaws.com` with `aws:SourceAccount` (D2)
    - Signing_Key `ECC_NIST_EDWARDS25519` / `SIGN_VERIFY`, alias `alias/{stage}-stellar-signing`, `RETAIN`, Deny `kms:Sign` unless `aws:PrincipalArn` is the deterministic signer role ARN (D6)
    - Two Kyc_Secrets per `kyc_providers[i]` with `encryption_key=data_key`, value `SecretValue.unsafe_plain_text("{}")`, removal policy from config, resource policy Deny `GetSecretValue` except the Stellar_KYC role ARN
    - Expose `data_key_arn`, `signing_key_arn`, `kyc_secrets`; termination protection when `RETAIN`
    - Add `constructs/extension_parameters.py` with a helper that creates `ssm.StringParameter` entries named by `extension_parameter_names`; publish `keys/data-key-arn` and `keys/signing-key-arn`
    - _Requirements: 3.1, 3.2, 3.3, 3.5, 3.6, 3.7, 1.9, 11.1, 11.5_

  - [ ] 5.3 Implement `app_builder.py` and `app.py`
    - `StackSet` dataclass and `build_app(app, cfg)` creating `{stage}-security` with `cdk.Environment(account, region)`, tags `project`, `stage`, `managed-by=cdk` on the app, `register_nag(app)` and `apply_acknowledgements(...)`
    - `app.py` per the design: read context `stage`, `load_stage`, on `ConfigError` call `sys.exit(str(err))`, then `build_app` and `app.synth()`
    - Add acknowledgements needed by the security stack (SMG4 on Kyc_Secrets)
    - _Requirements: 1.4, 1.5, 1.8, 1.10, 1.11, 12.6, 12.7_

  - [ ] 5.4 Write the synthesis fixtures, custom-resource test and nag test
    - `tests/conftest.py`: session-scoped fixtures that build the variants (dev; dev with `removal_policy: RETAIN`; dev with `nat_gateways: 2` and region `eu-west-1` with matching AZs; dev with a custom domain and a repository) from `dev.yaml` plus overrides through `parse_stage_config`, call `build_app`, and expose `Template.from_stack` per stack id
    - `tests/test_custom_resources.py`: for every stack in every variant, `find_custom_resources` minus `ALLOWLIST` is empty and failures name stack and logical id; every allowlist entry has a reason of at least 20 characters
    - `tests/test_nag.py`: the dev app synthesizes with no unacknowledged errors; a fixture app with an unencrypted bucket and no acknowledgement fails synthesis; `acknowledge` rejects a short reason
    - _Requirements: 1.12, 12.5, 12.6, 12.7_

  - [ ] 5.5 Write security stack assertion tests in `tests/stacks/test_security.py`
    - Rotation on the Data_Key, `KeySpec` and `KeyUsage` on the Signing_Key, `DeletionPolicy: Retain` on both keys in the dev and RETAIN variants, the two Deny statements, Kyc_Secrets with `KmsKeyId` and no `GenerateSecretString`, the two Extension_Parameters
    - _Requirements: 3.1, 3.2, 3.5, 3.7, 11.5, 12.1_

- [ ] 6. Implement the network stack
  - [ ] 6.1 Implement `NetworkStack` in `stacks/network.py` and wire it into `build_app`
    - `ec2.Vpc` with configured CIDR, explicit AZs (D8), NAT count, subnet groups `public` /24, `private` PRIVATE_WITH_EGRESS /22, `isolated` /24, `restrict_default_security_group=False`
    - Gateway endpoints S3 and DynamoDB; interface endpoints Secrets Manager, KMS, SQS, Step Functions, EventBridge, CloudWatch Logs, X-Ray, STS in private subnets with `endpoint_sg` (ingress 443 from VPC CIDR)
    - `lambda_sg`, `proxy_sg`, `db_sg` with `allow_all_outbound=False` and the exact rules from the design
    - Flow log `ALL` to `LogGroup` `/{stage}/vpc/flow-logs` with configured retention
    - Extension_Parameters `network/vpc-id`, `network/private-subnet-ids`, `network/isolated-subnet-ids`, `network/lambda-security-group-id`
    - Add any network acknowledgements (for example EC2 security group description findings) to `nag/acknowledgements.py`
    - _Requirements: 2.1, 2.2, 2.4, 2.5, 2.6, 2.7, 2.8, 2.9, 2.10, 2.11, 10.1, 11.2_

  - [ ] 6.2 Write network assertion tests in `tests/stacks/test_network.py`
    - Six subnets, NAT count 1 (dev) and 2 (multi-NAT variant), private default routes to a NAT, two gateway endpoints and the eight interface endpoint service names, exact security group ingress and egress rules, flow log `TrafficType: ALL` with a retained log group, the four Extension_Parameters
    - _Requirements: 2.1, 2.2, 2.4, 2.5, 2.6, 2.7, 2.8, 2.9, 2.10, 2.11, 11.2, 12.1_

- [ ] 7. Implement the data stack
  - [ ] 7.1 Implement the `SecureBucket` construct in `constructs/secure_bucket.py`
    - Always `block_public_access=BLOCK_ALL`, `enforce_ssl=True`, `object_ownership=BUCKET_OWNER_ENFORCED`; never `auto_delete_objects`; encryption, versioning, lifecycle and access-log target passed in
    - _Requirements: 8.4, 8.6, 1.12_

  - [ ] 7.2 Implement `DataStack` in `stacks/data.py` and wire it into `build_app`
    - RDS PostgreSQL in isolated subnets with `db_sg`, Data_Key storage encryption, generated master secret `{stage}/db/master`, IAM authentication, `rds.force_ssl=1` parameter group, PostgreSQL log export to a pre-created log group with retention, deletion protection when `RETAIN`, config-driven engine, class, storage, Multi-AZ and backup retention
    - `app_user` `DatabaseSecret` (D5) and `DatabaseProxy` in private subnets with `iam_auth=True`, `require_tls=True`, both secrets
    - `TableV2` idempotency (`pk`, TTL `expiresAt`) and saga (`pk`, `sk`, stream `NEW_AND_OLD_IMAGES`), on-demand, Data_Key, PITR
    - Access_Log_Bucket (SSE-S3, lifecycle expiry) and Documents_Bucket (versioned, SSE-KMS with bucket key, access logs prefix `documents/`)
    - Acknowledgements: RDS3 (only when not Multi-AZ), RDS10 (only when `DESTROY`), RDS11, SMG4 on database secrets, S1 on the Access_Log_Bucket
    - _Requirements: 7.1, 7.2, 7.3, 7.4, 7.6, 8.1, 8.2, 8.3, 8.4, 8.5, 8.6, 10.1_

  - [ ] 7.3 Write data assertion tests in `tests/stacks/test_data.py`
    - Database in isolated subnets with `KmsKeyId` and config values, generated secrets, proxy `IAMAuth: REQUIRED` and `RequireTLS`, `DeletionProtection` only in the RETAIN variant, table settings, all four Block Public Access flags on every `AWS::S3::Bucket`, TLS-deny bucket policies, access logging and lifecycle rule
    - _Requirements: 7.1, 7.2, 7.3, 7.4, 7.6, 8.1, 8.2, 8.3, 8.4, 8.5, 8.6, 12.1, 12.2_

- [ ] 8. Implement the messaging stack
  - [ ] 8.1 Implement the `QueueWithDlq` construct in `constructs/queue_with_dlq.py`
    - DLQ `{stage}-{name}-dlq` (14-day retention) and queue `{stage}-{name}` with `max_receive_count` and visibility timeout; both KMS-encrypted with the Data_Key and `enforce_ssl=True`
    - _Requirements: 9.6_

  - [ ] 8.2 Implement `MessagingStack` in `stacks/messaging.py` and wire it into `build_app`
    - Event_Bus `{stage}-tokenization` with archive and configured retention
    - Work_Queues from config
    - Saga_State_Machine (STANDARD, tracing, `LogLevel.ALL` to `/{stage}/states/tokenization-saga` with retention) using `substitute` with deterministic Domain_Lambda ARNs (D1); role limited to `lambda:InvokeFunction` on referenced functions and their versions
    - Rules with SQS or state machine targets, each with its own encrypted `{stage}-{name}-target-dlq`
    - Scheduler group `{stage}-schedules` and role `{stage}-scheduler-role` trusted by `scheduler.amazonaws.com` with `aws:SourceAccount`, permissions only for the configured targets
    - Extension_Parameters `events/bus-name`, `events/bus-arn`; expose `bus`, `queues`, `all_dlqs`, `saga`
    - Acknowledgements: SQS3 on DLQs, IAM5 for Step Functions log delivery and `function:<name>:*`
    - _Requirements: 9.1, 9.2, 9.3, 9.5, 9.6, 9.7, 9.9, 10.1, 11.6_

  - [ ] 8.3 Write messaging assertion tests in `tests/stacks/test_messaging.py`
    - STANDARD machine with tracing and `Level: ALL`, definition containing the deterministic ARNs, role invoking only the referenced functions, archive retention, queue and DLQ pairs with `maxReceiveCount`, one DLQ per rule target, scheduler group and role policy limited to the listed targets, the two Extension_Parameters
    - _Requirements: 9.1, 9.2, 9.3, 9.5, 9.6, 9.7, 9.9, 11.6, 12.1_

- [ ] 9. Checkpoint - Foundation stacks synthesize cleanly
  - Run lint, `uv run mypy`, `uv run pytest` and `npx cdk synth -c stage=dev`. Ensure all tests pass, ask the user if questions arise.

- [ ] 10. Implement the identity and front-end stack
  - [ ] 10.1 Implement the `PortalApp` construct in `constructs/portal_app.py`
    - `amplify.CfnApp` named `{stage}-portal`, platform `WEB`, custom rule `SPA_REWRITE_SOURCE` to `/index.html` status `200`, app-level environment variables with `portal.env_prefix`, optional `repository` and `access_token` from `SecretValue.secrets_manager(...).unsafe_unwrap()`
    - _Requirements: 4.9, 4.10, 4.11, 4.12_

  - [ ] 10.2 Implement `IdentityFrontendStack` in `stacks/identity_frontend.py` and wire it into `build_app`
    - User_Pool `{stage}-users` with email sign-in, self sign-up, password policy (min 12), MFA optional TOTP only, email recovery, deletion protection and removal policy from config
    - `CfnUserPoolGroup` per configured group; Resource_Server with configured identifier and scopes; hosted UI domain with `cfg.cognito.domain_prefix` passed unchanged (no extra stage prefix, D11)
    - `PortalApp` with `USER_POOL_ID`, `USER_POOL_CLIENT_ID`, `COGNITO_DOMAIN`, `REGION`; Portal_Origin from `portal_origin(...)`
    - Portal_Client: no secret, only the code grant, scopes `openid`, `email`, `profile` plus configured resource server scopes, callback and logout URLs from `core.urls`, only `ALLOW_REFRESH_TOKEN_AUTH` in `ExplicitAuthFlows` (property override if the L2 cannot produce it), `prevent_user_existence_errors=True`
    - Extension_Parameters `identity/user-pool-id`, `identity/user-pool-arn`, `identity/resource-server-identifier`; expose `user_pool`, `resource_server`, `portal_client`, `portal_app`, `portal_origin`, `cors_origins`
    - Acknowledgements: COG2, COG3
    - _Requirements: 4.1, 4.2, 4.3, 4.4, 4.5, 4.6, 4.7, 4.8, 4.9, 4.11, 4.12, 11.4_

  - [ ] 10.3 Write identity assertion tests in `tests/stacks/test_identity_frontend.py`
    - User pool properties, groups, resource server scopes, client flows and scopes, callback URLs including local dev URLs, logout URLs including the local dev origins combined with the logout path, `ExplicitAuthFlows`, domain prefix equal to the configured value (for example `dev-tokenization-portal`, not `dev-dev-...`), the Amplify app rewrite rule and environment variables, repository present only in the repository variant, the three Extension_Parameters
    - _Requirements: 4.1, 4.2, 4.3, 4.4, 4.5, 4.6, 4.7, 4.8, 4.10, 4.11, 4.12, 11.4, 12.1_

- [ ] 11. Implement the API and compute stack
  - [ ] 11.1 Implement the `DomainFunction` construct in `constructs/domain_function.py`
    - Role `role_name(stage, fn)` trusted by `lambda.amazonaws.com` with `AWSLambdaVPCAccessExecutionRole`; `LogGroup` `/aws/lambda/{function_name}` with retention (no `log_retention`); `lambda.Function` with runtime mapping (`dotnet8`, `dotnet10`), architecture, handler, `Code.from_bucket_v2` with optional object version, memory, timeout, reserved concurrency, private subnets, `lambda_sg`, `Tracing.ACTIVE`
    - `apply_profile(capabilities, resources)` turning each `Capability` into IAM statements in one place, including Data_Key `kms:Decrypt`/`kms:GenerateDataKey` with `kms:ViaService` where needed and `proxy.grant_connect(role, app_user)` for `db.connect`
    - _Requirements: 6.1, 6.2, 6.3, 6.4, 6.5, 6.6, 6.9, 3.3, 3.4, 3.6, 7.5, 9.4, 9.10, 10.1_

  - [ ] 11.2 Implement the functions part of `ApiComputeStack` in `stacks/api_compute.py` and wire it into `build_app`
    - Six `DomainFunction`s with `permission_profile(cfg, fn)`; Signer invocation grant to Stellar_KYC only
    - Environment variables per the design, limited to the resources each function uses: `STAGE`, `EVENT_BUS_NAME`, `ALLOWED_ORIGINS` for every Domain_Lambda; table and bucket names per `data_access`; `DB_*` only when `uses_database`; `SAGA_STATE_MACHINE_ARN` only for sagas-payments; `QUEUE_URLS` only for consumed queues; Stellar_KYC extras `KYC_SECRET_ARNS` and `SIGNER_FUNCTION_NAME`; Signer only `STAGE` and `SIGNING_KEY_ID`
    - `SqsEventSource` per queue on its consumer with `batch_size` and `report_batch_item_failures=True`
    - Import KMS keys with `kms.Key.from_key_arn` (D2)
    - Acknowledgements: IAM4 on Lambda roles, IAM5 for X-Ray, ENI actions and DynamoDB `index/*`, L1 only when the configured runtime is not the newest
    - _Requirements: 6.1, 6.2, 6.3, 6.5, 6.6, 6.7, 6.8, 7.5, 9.4, 9.8, 9.10_

  - [ ] 11.3 Implement the Api part of `ApiComputeStack` in `stacks/api_compute.py`
    - REGIONAL `RestApi` `{stage}-tokenization-api` with `cloud_watch_role=True` (`RETAIN`), access log group `/{stage}/apigateway/access` with retention and JSON format, tracing, ERROR logging, metrics, throttling from config, default CORS preflight from `identity.cors_origins`, a body and parameter `RequestValidator`
    - Execution log group `API-Gateway-Execution-Logs_{api.rest_api_id}/{stage}` as a plain `logs.LogGroup` with the configured retention, and `api.deployment_stage.node.add_dependency(exec_log_group)` (D10)
    - `DEFAULT_4XX` and `DEFAULT_5XX` gateway responses with `Access-Control-Allow-Origin` set to the Portal_Origin only and `Vary: Origin` (O7, decided)
    - `CognitoUserPoolsAuthorizer` `{stage}-cognito`; per Domain_Lambda `/{fn}` and `/{fn}/{proxy+}` `ANY` with `LambdaIntegration(proxy=True)`, `COGNITO` and `route_scopes[fn]`
    - `POST /webhooks/kyc/{provider}` to Stellar_KYC with `authorization_type=NONE`, required `provider` parameter and body validator
    - Extension_Parameters `api/id`, `api/root-resource-id`, `api/base-url`; expose `url`
    - Acknowledgements: IAM4 on the API Gateway CloudWatch role, APIG3 on the stage, APIG4 and COG4 on the webhook method only
    - _Requirements: 5.1, 5.2, 5.3, 5.4, 5.5, 5.6, 5.7, 5.8, 5.9, 5.10, 10.1, 11.3_

  - [ ] 11.4 Write compute assertion tests in `tests/stacks/test_compute.py`
    - Six functions with runtime, handler, S3 code, memory, timeout and reserved concurrency from config; `VpcConfig.SubnetIds` only private subnets plus the Lambda security group; `TracingConfig: Active`; six distinct roles; each function's environment keys match the resources it uses (for example no `DB_PROXY_ENDPOINT` on a function without `uses_database`); one retained log group per function; ESMs with `ReportBatchItemFailures`; `rds-db:connect` only for database users
    - _Requirements: 6.1, 6.2, 6.3, 6.4, 6.5, 6.6, 6.8, 6.9, 7.5, 9.8, 12.1, 12.2_

  - [ ] 11.5 Write Api assertion tests in `tests/stacks/test_api.py`
    - REGIONAL endpoint; every non-`OPTIONS` method uses `COGNITO_USER_POOLS` with configured scopes except `POST /webhooks/kyc/{provider}`; five base paths with `AWS_PROXY` integrations; access log, tracing and throttling on the stage; the execution log group with retention, whose name joins `API-Gateway-Execution-Logs_` with the RestApi ref and `/{stage}`, and `AWS::ApiGateway::Stage` `DependsOn` it; CORS origins; gateway responses whose `Access-Control-Allow-Origin` is the Portal_Origin only; the three Extension_Parameters
    - _Requirements: 5.1, 5.2, 5.3, 5.4, 5.5, 5.6, 5.7, 5.8, 5.9, 5.10, 11.3, 12.1_

  - [ ] 11.6 Write the exclusive-grant scan in `tests/stacks/test_exclusive_grants.py`
    - Scan every IAM policy in every stack and variant: `kms:Sign` only in the signer role policy, `kms:GetPublicKey` only in signer and Stellar_KYC policies, `GetSecretValue` on Kyc_Secrets only in the Stellar_KYC policy, `lambda:InvokeFunction` on the Signer only in the Stellar_KYC policy, `events:PutEvents` in all five Domain_Lambda policies, `states:StartExecution` only for sagas-payments
    - _Requirements: 3.3, 3.4, 3.6, 6.7, 9.4, 9.10, 12.3_

- [ ] 12. Implement the portal branch stack
  - [ ] 12.1 Implement `PortalBranchStack` in `stacks/portal_branch.py` and wire it into `build_app`
    - `CfnBranch` on `identity.portal_app.attr_app_id` with `portal.branch`, `portal.stage` and branch-level `{prefix}API_BASE_URL` = `api.url` (D4)
    - `CfnDomain` with sub-domain setting and `enable_auto_sub_domain=False` when `portal.custom_domain` is set, depending on the branch
    - _Requirements: 4.9, 4.11, 4.13, 1.11_

  - [ ] 12.2 Write portal branch assertion tests in `tests/stacks/test_portal_branch.py`
    - Exactly one `AWS::Amplify::App` across all stacks in every variant; the branch name and `API_BASE_URL` variable; `AWS::Amplify::Domain` only in the custom domain variant
    - _Requirements: 4.9, 4.11, 4.13, 12.4_

- [ ] 13. Implement the observability stack
  - [ ] 13.1 Implement `ObservabilityStack` in `stacks/observability.py` and wire it into `build_app`
    - Alert_Topic `{stage}-alarms` with the imported Data_Key and `enforce_ssl=True`; one `EmailSubscription` per `alarm_emails` entry
    - One alarm per entry in `messaging.all_dlqs` on `ApproximateNumberOfMessagesVisible` (Maximum, 1 minute, 1 period, `GreaterThanThreshold` 0, missing data `NOT_BREACHING`) with alarm and OK actions to the topic
    - Extension_Parameter `observability/alert-topic-arn`
    - _Requirements: 10.2, 10.3, 10.4, 10.5, 11.7_

  - [ ] 13.2 Write observability assertion tests in `tests/stacks/test_observability.py`
    - Encrypted topic and subscriptions; one alarm per DLQ with threshold 0, `GreaterThanThreshold` and both actions; retention set on every `AWS::Logs::LogGroup` in every stack; the Extension_Parameter
    - _Requirements: 10.1, 10.2, 10.3, 10.4, 10.5, 11.7, 12.1_

- [ ] 14. Checkpoint - All application stacks synthesize cleanly
  - Run lint, `uv run mypy`, `uv run pytest` and `npx cdk synth -c stage=dev`, and confirm cdk-nag reports no unacknowledged findings. Ensure all tests pass, ask the user if questions arise.

- [ ] 15. Write app-level tests
  - [ ] 15.1 Write app assertion tests in `tests/test_app.py`
    - The eight stack ids (`security`, `network`, `data`, `messaging`, `identity-frontend`, `api-compute`, `portal-branch`, `observability`, each prefixed with the stage) exist and the bootstrap stack does not; every stack's region equals the variant region; every resource with `Tags` carries `project`, `stage` and `managed-by=cdk`; every stack name and explicit name property contains the stage as a prefix (`{stage}-`) or path segment (`/{stage}/`, `{stage}/`), except the 1.9 exceptions: SSM names under `/{project}/{stage}/`, KMS aliases under `alias/`, log groups under `/aws/` or named `API-Gateway-Execution-Logs_{restApiId}/{stage}`, `CfnUserPoolGroup` group names, the Resource_Server identifier and the Amplify branch name
    - _Requirements: 1.3, 1.8, 1.9, 1.10, 1.11, 13.3_

  - [ ] 15.2 Write the Extension_Parameters test in `tests/test_extension_parameters.py`
    - Each name from `extension_parameter_names` exists as `AWS::SSM::Parameter` in the stack that owns the resource and its value references the right resource
    - _Requirements: 11.1, 11.2, 11.3, 11.4, 11.5, 11.6, 11.7, 12.1_

  - [ ] 15.3 Write configuration failure tests in `tests/test_config_failures.py`
    - Building the app fails for an unknown stage, a missing required field and `nat_gateways: 0`, each naming the stage or field; `app.py` run as a subprocess with `-c stage=nope` (through `uv run python app.py` with `CDK_CONTEXT_JSON`) exits non-zero and names the stage on stderr
    - _Requirements: 1.5, 1.6, 2.3, 12.8_

- [ ] 16. Implement the Bootstrap_Stack
  - [ ] 16.1 Implement `BootstrapStack` in `stacks/bootstrap.py` and `bootstrap_app.py`
    - `iam.OidcProviderNative` for `https://token.actions.githubusercontent.com` with client id `sts.amazonaws.com`, or import by ARN when `cicd.create_oidc_provider` is false
    - Deploy_Role `{stage}-github-deploy`: `StringEquals` on `aud` and `sub = oidc_subjects(...).deploy`, only `sts:AssumeRole` on `cdk-{qualifier}-{deploy,file-publishing,image-publishing,lookup}-role-{account}-{region}`, 1-hour sessions
    - Diff_Role `{stage}-github-diff`: `StringLike` on `sub = oidc_subjects(...).pull_request`, may assume only the CDK lookup role
    - `bootstrap_app.py` loads the stage like `app.py`, synthesizes only `{stage}-cicd-bootstrap`, applies tags and cdk-nag (D7)
    - _Requirements: 13.3, 13.4, 13.5_

  - [ ] 16.2 Write bootstrap assertion tests in `tests/stacks/test_bootstrap.py`
    - OIDC provider, Deploy_Role trust with `aud` and `sub` equal to `oidc_subjects().deploy`, permissions limited to the CDK bootstrap role ARNs, Diff_Role trust and lookup-only permission, no custom resources, cdk-nag passes
    - _Requirements: 13.4, 13.5, 1.12, 12.6_

- [ ] 17. Implement the GitHub Actions workflows
  - [ ] 17.1 Create `.github/workflows/pr.yml`
    - On `pull_request` to `main`; permissions `id-token: write`, `contents: read`, `pull-requests: write`; `defaults.run.shell: bash -eo pipefail {0}`
    - Setup: checkout, `astral-sh/setup-uv`, `actions/setup-node` Node 24, `npm ci`, `uv sync --frozen`
    - Steps: ruff check and format check, mypy, pytest, actionlint, `npx cdk synth -c stage=dev`, then (skipped for forks) assume `vars.AWS_DIFF_ROLE_ARN`, `npx cdk diff --all --no-change-set -c stage=dev 2>&1 | tee diff.txt`, and `actions/github-script` creating or updating the `<!-- cdk-diff:dev -->` comment cut to 60,000 characters with a run link; fork PRs get a comment saying the diff was skipped
    - _Requirements: 13.1, 13.2, 13.4, 13.6_

  - [ ] 17.2 Create `.github/workflows/deploy.yml`
    - On `push` to `main`; job with `environment: dev`, `concurrency: deploy-dev` without cancellation, `id-token: write`
    - Same setup and checks as the PR workflow, then assume `vars.AWS_DEPLOY_ROLE_ARN`, compare `aws sts get-caller-identity` with the account in `config/stages/dev.yaml` (read with `yq`), and run `npx cdk deploy --all -c stage=dev --require-approval never`
    - _Requirements: 13.3, 13.4, 13.6, 13.7_

- [ ] 18. Write the README
  - [ ] 18.1 Create `README.md`
    - Prerequisites (devbox with Python 3.12, uv and Node 24), CDK bootstrap, deploying the Bootstrap_Stack with `npx cdk --app "uv run --frozen python bootstrap_app.py" deploy`, the `gh api` step for the OIDC subject claim template, the GitHub environment and branch rule, repository variables `AWS_DEPLOY_ROLE_ARN` and `AWS_DIFF_ROLE_ARN`
    - Stage_Config fields and how to add a stage; deploy commands
    - Artifact_References table and the versioned-key or `version` guidance; pre-deploy `aws s3api head-object` checklist
    - Post-deploy steps: populate Kyc_Secrets, confirm Alert_Topic email subscriptions, create the `app_user` PostgreSQL role through application migrations, create the Amplify repository token secret
    - Extension_Parameters table with names and meanings; the "Deferred to Later Specs" list; open items O1 (access tokens), O6 (PKCE), O7, O8; emptying buckets before deleting a `DESTROY` stage
    - _Requirements: 14.1, 14.2, 14.3, 14.4, 14.5_

  - [ ] 18.2 Write the README test in `tests/test_readme.py`
    - The README contains every Extension_Parameter name suffix from `extension_parameter_names` and every Artifact_Reference key path
    - _Requirements: 14.2, 14.4_

- [ ] 19. Final checkpoint - Ensure all tests pass
  - Run `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy`, `uv run pytest`, `npx cdk synth -c stage=dev` and `npx cdk --app "uv run --frozen python bootstrap_app.py" synth -c stage=dev`. Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional and can be skipped for a faster MVP. They are the hypothesis strategies, the 12 property tests and the example unit tests for core helpers. The `aws_cdk.assertions`, custom-resource, cdk-nag and config-failure tests are required by Requirement 12 and are not optional.
- Each stack task wires its stack into `build_app` and adds its cdk-nag acknowledgements in `nag/acknowledgements.py`, so the nag test from task 5.4 and the custom-resource scan stay green after every task. The custom-resource allowlist stays empty.
- `config/stages/dev.yaml` uses the placeholder account `000000000000` and `ops@example.com`. Synthesis and tests work with them; the deploy workflow's account check blocks a real deployment until an operator replaces them.
- Pipeline behavior (13.1 to 13.4, 13.6, 13.7) is confirmed by the first real pull request and merge, not by unit tests.
- Each task references specific requirements for traceability. Property tests validate universal properties of `tokenization_infra.core`; assertion tests validate the synthesized templates.

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1"] },
    { "id": 1, "tasks": ["1.2", "2.1"] },
    { "id": 2, "tasks": ["2.2"] },
    { "id": 3, "tasks": ["2.3"] },
    { "id": 4, "tasks": ["2.4", "3.1", "3.2", "3.3", "3.4"] },
    { "id": 5, "tasks": ["2.5", "2.6", "3.5"] },
    { "id": 6, "tasks": ["2.7", "2.8", "2.9", "2.10", "2.11", "3.6", "3.7", "3.8", "3.9", "3.10", "3.11", "3.12"] },
    { "id": 7, "tasks": ["4"] },
    { "id": 8, "tasks": ["5.1", "5.2"] },
    { "id": 9, "tasks": ["5.3"] },
    { "id": 10, "tasks": ["5.4"] },
    { "id": 11, "tasks": ["5.5"] },
    { "id": 12, "tasks": ["6.1"] },
    { "id": 13, "tasks": ["6.2", "7.1"] },
    { "id": 14, "tasks": ["7.2"] },
    { "id": 15, "tasks": ["7.3", "8.1"] },
    { "id": 16, "tasks": ["8.2"] },
    { "id": 17, "tasks": ["8.3"] },
    { "id": 18, "tasks": ["9"] },
    { "id": 19, "tasks": ["10.1"] },
    { "id": 20, "tasks": ["10.2"] },
    { "id": 21, "tasks": ["10.3", "11.1"] },
    { "id": 22, "tasks": ["11.2"] },
    { "id": 23, "tasks": ["11.3"] },
    { "id": 24, "tasks": ["11.4", "11.5", "11.6"] },
    { "id": 25, "tasks": ["12.1"] },
    { "id": 26, "tasks": ["12.2", "13.1"] },
    { "id": 27, "tasks": ["13.2"] },
    { "id": 28, "tasks": ["14"] },
    { "id": 29, "tasks": ["15.1", "15.2", "15.3", "16.1", "17.1", "17.2", "18.1"] },
    { "id": 30, "tasks": ["16.2", "18.2"] },
    { "id": 31, "tasks": ["19"] }
  ]
}
```
