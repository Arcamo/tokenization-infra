---
inclusion: always
---

# Project structure

The layout below is the target from the spec 1 design. Code is built task by task from `tasks.md`, so some paths may not exist yet. Create files where this layout puts them; do not invent new top-level folders.

```
app.py                      # entry: resolve stage, build stacks, tags, cdk-nag
bootstrap_app.py            # entry for the bootstrap (GitHub OIDC) stack only
cdk.json                    # app command, context stage=dev, feature flags
pyproject.toml, uv.lock, .python-version
package.json, package-lock.json   # aws-cdk CLI pin only
config/
  stages/<stage>.yaml       # one validated Stage_Config per stage (dev.yaml today)
  statemachines/tokenization-saga.asl.json
src/tokenization_infra/
  app_builder.py            # build_app(app, cfg) -> StackSet
  core/                     # pure Python, never imports aws_cdk or constructs
  constructs/               # reusable CDK constructs (DomainFunction, QueueWithDlq, SecureBucket)
  stacks/                   # one module per stack
  nag/acknowledgements.py   # every cdk-nag acknowledgement, with its reason
tests/
  core/                     # hypothesis property tests + strategies.py
  stacks/                   # aws_cdk.assertions tests
  test_*.py                 # app-level, nag, custom resource, config failure, README tests
  conftest.py               # session-scoped synthesized variants, hypothesis ci profile
.github/workflows/pr.yml, deploy.yml
README.md
```

## Stacks and dependency direction

Stack ids are `{stage}-<name>`. Arrows go producer to consumer:

- `security`, `network`, `identity`: no dependencies
- `data` <- security, network
- `messaging` <- security
- `api-compute` <- security, network, data, messaging, identity
- `observability` <- security, messaging
- `cicd-bootstrap`: only in `bootstrap_app.py`, never in `app.py`

Keep the graph one-way. `messaging` must not depend on `api-compute`. When a reference would create a cycle, build the ARN from the deterministic stage-prefixed name (design D1) instead of passing the construct. Stacks other than `security` import KMS keys with `kms.Key.from_key_arn` so grants land in the consumer's identity policy (D2).

## Where logic goes

- Anything testable without CDK (config parsing, naming, URLs, permission profiles, saga substitution, SSM parameter names, OIDC subjects, custom resource scan) lives in `tokenization_infra.core`. A test parses its imports and fails on `aws_cdk`.
- IAM statements for Lambdas come from `core.permissions.permission_profile` and are applied in one place, `DomainFunction.apply_profile`. Do not hand-write grants in stacks for domain functions.
- SSM extension parameter names come only from `core.parameters.extension_parameter_names`. Each stack publishes parameters for the resources it owns.
- Stack modules wire constructs. They receive typed handles from producer stacks (for example `network.vpc`) through constructor arguments.

## Imports

Use absolute imports only (`from tokenization_infra.constructs.secure_bucket import SecureBucket`). Ruff `TID252` bans relative imports so the local `tokenization_infra.constructs` package never shadows the `constructs` library.
