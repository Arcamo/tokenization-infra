---
inclusion: always
---

# Tech stack and commands

## Toolchain (pinned by the design)

| Tool | Version or rule |
|---|---|
| Python | 3.12 (`.python-version`, `requires-python = ">=3.12"`) |
| uv | manages deps and the venv. `pyproject.toml` declares, `uv.lock` pins. CI uses `uv sync --frozen`. |
| `aws-cdk-lib` | `==2.277.0` (exact pin) |
| `constructs` | `>=10,<11` |
| `cdk-nag` | `==3.0.2` |
| `pydantic` | `>=2,<3` |
| `PyYAML` | `yaml.safe_load` only |
| Dev | `pytest`, `hypothesis`, `ruff`, `mypy` (strict, `pydantic.mypy` plugin), `types-PyYAML` |
| CDK CLI | `aws-cdk` `2.1148.1`, pinned in a `package.json` that contains only the CLI |
| Node.js | 24 LTS, only for the CDK CLI |

Do not change a pin without asking. Add Python dependencies with `uv add`, never by hand-editing `uv.lock`, and never with `pip`.

## Environment

- devbox plus direnv provide the shell (`devbox.json`, `.envrc`). Run commands inside it.
- The machine is Linux aarch64. Some Python packages lack aarch64 wheels; check before adding a dependency.
- AWS credentials come from `AWS_PROFILE`, exported by `.envrc`. It is an IAM Identity Center (SSO) profile. On expired-credential errors, run `aws sso login --profile "$AWS_PROFILE"`.
- Synthesis and tests need no AWS credentials and no Docker: AZs are explicit in the stage config and Lambda code comes from S3, so there are no context lookups or assets. Keep it that way.

## Commands

```bash
uv sync                                  # install deps
uv run ruff check .                      # lint
uv run ruff format --check .             # format check (uv run ruff format . to fix)
uv run mypy                              # strict type check
uv run pytest                            # all tests, single run (no watch mode)
npx cdk synth -c stage=dev               # synth app stacks; cdk-nag runs here
npx cdk --app "uv run --frozen python bootstrap_app.py" synth -c stage=dev   # bootstrap stack
```

A change is done when lint, format check, mypy, pytest and `cdk synth` all pass.

Do not run `cdk deploy`, `cdk destroy`, or anything else that changes the AWS account unless the user asks. Deploys go through the GitHub Actions deploy workflow on merge to `main`. The bootstrap stack is deployed once by an admin.

## MCP servers (`.kiro/settings/mcp.json`)

- `awslabs.aws-documentation-mcp-server`: use for AWS, CDK and CloudFormation lookups. Check CDK Python APIs here before relying on memory.
- `bedrock-agentcore-mcp-server`: AgentCore docs (spec 2).
- `awslabs.aws-pricing-mcp-server`: reads `${AWS_PROFILE}`. Kiro only fills that in when the variable is approved in "Mcp Approved Env Vars" and set in Kiro's own process environment. If pricing calls fail, the variable is probably missing there.
- `awslabs.aws-iac-mcp-server`: disabled. Its `guardpycfn` dependency has no aarch64 wheel and fails to build from source.
