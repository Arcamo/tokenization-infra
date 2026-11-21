---
inclusion: fileMatch
fileMatchPattern: ["tests/**", "pyproject.toml"]
---

# Testing

The full strategy is in the design's "Testing Strategy" section.

## Kinds of tests

- **Property tests** (`tests/core/test_*_properties.py`): hypothesis, one test per design property (1 to 11), only for `tokenization_infra.core`. Strategies live in `tests/core/strategies.py`. Each test has a docstring tag: `Feature: real-estate-tokenization-iac, Property <n>: <title>`. Edge cases come from the strategies, not separate tests.
- **Assertion tests** (`tests/stacks/`): `aws_cdk.assertions` (`Template`, `Match`, `Annotations`) against synthesized templates. Assert on the properties the requirements name, not on full template snapshots.
- **Guard tests** (required, never skip or weaken):
  - `test_custom_resources.py`: no custom resource in any stack of any variant beyond the empty allowlist.
  - `test_nag.py`: the dev app synthesizes with `AwsSolutionsChecks`; a fixture with an unacknowledged violation fails.
  - `test_config_failures.py`: unknown stage, missing field, `nat_gateways: 0`, and `app.py -c stage=nope` exits non-zero.
  - `test_project.py`: the `cdk.json` flag, and no `aws_cdk` import in `core`.
  - Exclusive-grant scan over every IAM policy in every stack.
  - No `AWS::Amplify::*` resource in any stack of any variant (Requirement 12.4).

## Fixtures

- `conftest.py` synthesizes the app once per session per variant: dev; dev with `removal_policy: RETAIN`; dev with `nat_gateways: 2` in another region (for example `eu-west-1`). Reuse these fixtures instead of synthesizing in each test; synthesis is slow.
- The hypothesis `ci` profile (`max_examples=100`, `deadline=None`) is loaded by default.
- Tests need no AWS credentials, network access or Docker.

## Rules

- Every requirement-driven assertion test is mandatory. Property tests and core example tests are optional (`*` in `tasks.md`) but follow the same conventions when written.
- When a test fails, fix the code or the test's wrong assumption. Do not loosen assertions, add allowlist entries, or add cdk-nag acknowledgements just to get green.
- Tests are type-checked: mypy strict covers `tests/`.
- Run with `uv run pytest` (single run). Narrow with `uv run pytest tests/stacks/test_network.py -q` while iterating, then run the full suite.
