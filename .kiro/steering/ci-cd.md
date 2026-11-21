---
inclusion: fileMatch
fileMatchPattern: [".github/**", "bootstrap_app.py", "src/tokenization_infra/stacks/bootstrap*", "src/tokenization_infra/core/oidc.py"]
---

# CI/CD

## Workflows

Both workflows share setup: `actions/checkout`, `astral-sh/setup-uv` (reads `.python-version`), `actions/setup-node` with Node 24, `npm ci`, `uv sync --frozen`. Every `run` step uses `bash -eo pipefail`.

- `.github/workflows/pr.yml` (pull requests to `main`; permissions `id-token: write`, `contents: read`, `pull-requests: write`): ruff check and format check, mypy, pytest, `npx cdk synth -c stage=dev`, then assume `vars.AWS_DIFF_ROLE_ARN` and run `npx cdk diff --all --no-change-set -c stage=dev`. It posts or updates one PR comment marked `<!-- cdk-diff:dev -->`, cut to 60,000 characters. Fork PRs get no OIDC token, so the diff steps are skipped and the comment says so. `actionlint` checks the workflow files.
- `.github/workflows/deploy.yml` (push to `main`, `environment: dev`, `concurrency: deploy-dev` without cancellation): the same checks, then assume `vars.AWS_DEPLOY_ROLE_ARN`, confirm `aws sts get-caller-identity` returns the account in `config/stages/dev.yaml`, and run `npx cdk deploy --all -c stage=dev --require-approval never`.

## Bootstrap stack

- `{stage}-cicd-bootstrap` lives only in `bootstrap_app.py`, so `cdk deploy --all` in the pipeline can never deploy it. An admin deploys it once: `npx cdk --app "uv run --frozen python bootstrap_app.py" deploy`.
- It uses `iam.OidcProviderNative`. When the account already has a GitHub OIDC provider, set `cicd.create_oidc_provider: false` to import it.
- Deploy_Role `{stage}-github-deploy`: trusted only for `repo:{owner}/{repo}:environment:{stage}:ref:refs/heads/{branch}`; may only assume the CDK bootstrap roles (`cdk-{qualifier}-*-role-{account}-{region}`); 1-hour sessions.
- Diff_Role `{stage}-github-diff`: trusted only for `repo:{owner}/{repo}:pull_request:ref:refs/pull/*`; may only assume the CDK lookup role.
- These subjects need the repo's OIDC claim template set to `include_claim_keys: ["repo", "context", "ref"]`. Subject strings come from `core.oidc.oidc_subjects`; if the real token format differs (open item O2), change only that module.

## Rules

- Keep roles least privilege. Do not give the GitHub roles resource permissions directly; CloudFormation runs as the CDK execution role.
- Pin third-party actions to a version. Never put secrets in workflow files; use repository or environment variables and OIDC.
- Pipeline behavior is checked by the first real PR and merge, not unit tests. Say so instead of claiming it works.
