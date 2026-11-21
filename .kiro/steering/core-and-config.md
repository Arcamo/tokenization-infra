---
inclusion: fileMatch
fileMatchPattern: ["src/tokenization_infra/core/**", "config/**"]
---

# Core package and stage config

## `tokenization_infra.core`

- Pure Python. Never import `aws_cdk` or `constructs` here; a test enforces it.
- Functions must accept CDK token strings as well as plain strings (URL helpers, naming), because stacks call the same functions the property tests exercise.
- Keep it deterministic and side-effect free, except `load_stage`, which reads the YAML file.

## Stage_Config

- One file per stage: `config/stages/<stage>.yaml`. The file name must equal the `stage` field. YAML keys are snake_case.
- Parsed by pydantic v2 models in `core/config.py`. Every model inherits `StrictModel` (`extra="forbid"`, `frozen=True`), so unknown keys are errors.
- Cross-field checks live in `@model_validator(mode="after")` and raise with the path of the offending entry.
- Errors surface as one `ConfigError` with a line per issue: `<stage>: <dotted.path>: <reason>`, for example `dev: network.nat_gateways: Input should be greater than or equal to 1`. Use `errors.format_loc` for paths (`queues[2].name`).
- An unknown stage raises `UnknownStageError: Unknown stage "<x>". Known stages: dev`, and `app.py` exits non-zero before building stacks.
- New configurable behavior means a new field on the model with validation, a value in `dev.yaml`, and a test. Do not read raw dicts in stacks.

## dev.yaml placeholders

`config/stages/dev.yaml` uses account `000000000000`, `ops@example.com` and the placeholder origin `https://main.d000000000000.amplifyapp.com`. Synthesis and tests work with them; the deploy workflow's account check blocks a real deploy until an operator sets real values. Do not commit real account ids or personal emails without the user asking.

## Portal origin

`portal.origin` is required and holds the HTTPS origin of the hand-made Amplify branch (for example `https://main.d1abc2def3.amplifyapp.com`) or its custom domain. It must pass `core.urls.is_https_origin` (no path, query or fragment). Cognito callback and logout URLs and the Api CORS origins are derived from it with `core.urls`. There are no Amplify settings (branch, repository, custom domain, env prefix) in the config.

## Saga definition

`config/statemachines/tokenization-saga.asl.json` may only use these placeholders: `${CollateralIdentityFunctionArn}`, `${SagasPaymentsFunctionArn}`, `${ReportsFunctionArn}`, `${StellarKycFunctionArn}`, `${TradingOperationsFunctionArn}`. `${SignerFunctionArn}` is rejected so signing stays behind `stellar-kyc`. Any other `${...}` is a config error.

## Function ids

`collateral-identity`, `sagas-payments`, `reports`, `stellar-kyc`, `trading-operations` (the five Domain_Lambdas) and `signer`. The signer has no API route, no database, no data access, and cannot be a queue consumer or scheduler target.
