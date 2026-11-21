"""GitHub OIDC subject claims for the CI/CD roles. Pure Python (Requirement 13.5)."""

from dataclasses import dataclass


@dataclass(frozen=True)
class OidcSubjects:
    """``deploy`` is an exact subject. ``pull_request`` ends in a wildcard, for ``StringLike``."""

    deploy: str
    pull_request: str


GITHUB_OIDC_HOST = "token.actions.githubusercontent.com"
"""Host of the GitHub OIDC issuer; also the prefix of the ``aud`` and ``sub`` condition keys."""

GITHUB_OIDC_AUDIENCE = "sts.amazonaws.com"

BOOTSTRAP_DEPLOY_ROLE_KINDS = ("deploy", "file-publishing", "image-publishing", "lookup")
"""CDK bootstrap role kinds the Deploy_Role may assume (design, BootstrapStack)."""


def bootstrap_role_arn(kind: str, qualifier: str, account: str, region: str, partition: str) -> str:
    """ARN of a CDK bootstrap role: ``cdk-{qualifier}-{kind}-role-{account}-{region}``."""
    return f"arn:{partition}:iam::{account}:role/cdk-{qualifier}-{kind}-role-{account}-{region}"


def github_oidc_provider_arn(account: str, partition: str) -> str:
    """ARN of the account's GitHub OIDC provider (one provider per URL per account)."""
    return f"arn:{partition}:iam::{account}:oidc-provider/{GITHUB_OIDC_HOST}"


def oidc_subjects(owner: str, repo: str, stage: str, branch: str) -> OidcSubjects:
    """Build the ``sub`` claim values the Deploy_Role and Diff_Role trust.

    The deploy subject assumes the repository's OIDC subject claim template includes
    ``repo``, ``context`` and ``ref`` (design, "Subject format").
    """
    return OidcSubjects(
        deploy=f"repo:{owner}/{repo}:environment:{stage}:ref:refs/heads/{branch}",
        pull_request=f"repo:{owner}/{repo}:pull_request:ref:refs/pull/*",
    )
