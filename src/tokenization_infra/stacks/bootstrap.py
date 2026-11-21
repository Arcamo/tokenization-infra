"""BootstrapStack: GitHub OIDC provider, Deploy_Role and Diff_Role (Requirements 13.3 to 13.5).

Built only by ``bootstrap_app.py`` (design D7). ``app.py`` never contains it, so
``cdk deploy --all`` in the pipeline cannot deploy it. An admin deploys it once.
"""

from typing import Any

import aws_cdk as cdk
from aws_cdk import aws_iam as iam
from constructs import Construct

from tokenization_infra.core.config import StageConfig
from tokenization_infra.core.naming import NameKind, resource_name
from tokenization_infra.core.oidc import (
    BOOTSTRAP_DEPLOY_ROLE_KINDS,
    GITHUB_OIDC_AUDIENCE,
    GITHUB_OIDC_HOST,
    bootstrap_role_arn,
    github_oidc_provider_arn,
    oidc_subjects,
)

SESSION_DURATION = cdk.Duration.hours(1)


class BootstrapStack(cdk.Stack):
    """The OIDC trust between GitHub Actions and the account.

    Both roles hold only ``sts:AssumeRole`` on CDK bootstrap roles. CloudFormation runs as the
    CDK execution role, so the roles here hold no resource permissions.
    """

    oidc_provider: iam.IOidcProvider
    deploy_role: iam.Role
    diff_role: iam.Role

    def __init__(self, scope: Construct, construct_id: str, *, cfg: StageConfig, **kwargs: Any):
        super().__init__(scope, construct_id, **kwargs)
        cicd = cfg.cicd
        subjects = oidc_subjects(cicd.github_owner, cicd.github_repo, cfg.stage, cicd.branch)

        if cicd.create_oidc_provider:
            provider: iam.IOidcProvider = iam.OidcProviderNative(
                self,
                "GithubOidcProvider",
                url=f"https://{GITHUB_OIDC_HOST}",
                client_ids=[GITHUB_OIDC_AUDIENCE],
            )
        else:
            # An account allows one provider per URL, so reuse the existing one.
            provider = iam.OidcProviderNative.from_oidc_provider_arn(
                self,
                "GithubOidcProvider",
                github_oidc_provider_arn(cfg.account, self.partition),
            )
        self.oidc_provider = provider

        def role_arns(*kinds: str) -> list[str]:
            qualifier, account, region = cicd.cdk_qualifier, cfg.account, cfg.region
            return [
                bootstrap_role_arn(kind, qualifier, account, region, self.partition)
                for kind in kinds
            ]

        self.deploy_role = iam.Role(
            self,
            "DeployRole",
            role_name=resource_name(cfg.stage, "github-deploy", NameKind.ROLE),
            description="GitHub Actions deploy job: may assume only the CDK bootstrap roles.",
            assumed_by=iam.WebIdentityPrincipal(
                provider.oidc_provider_arn,
                {
                    "StringEquals": {
                        f"{GITHUB_OIDC_HOST}:aud": GITHUB_OIDC_AUDIENCE,
                        f"{GITHUB_OIDC_HOST}:sub": subjects.deploy,
                    }
                },
            ),
            max_session_duration=SESSION_DURATION,
        )
        self.deploy_role.add_to_policy(
            iam.PolicyStatement(
                actions=["sts:AssumeRole"],
                resources=role_arns(*BOOTSTRAP_DEPLOY_ROLE_KINDS),
            )
        )

        self.diff_role = iam.Role(
            self,
            "DiffRole",
            role_name=resource_name(cfg.stage, "github-diff", NameKind.ROLE),
            description="GitHub Actions pull request diff: may assume only the CDK lookup role.",
            assumed_by=iam.WebIdentityPrincipal(
                provider.oidc_provider_arn,
                {
                    "StringEquals": {f"{GITHUB_OIDC_HOST}:aud": GITHUB_OIDC_AUDIENCE},
                    "StringLike": {f"{GITHUB_OIDC_HOST}:sub": subjects.pull_request},
                },
            ),
            max_session_duration=SESSION_DURATION,
        )
        self.diff_role.add_to_policy(
            iam.PolicyStatement(actions=["sts:AssumeRole"], resources=role_arns("lookup"))
        )

        cdk.CfnOutput(self, "DeployRoleArn", value=self.deploy_role.role_arn)
        cdk.CfnOutput(self, "DiffRoleArn", value=self.diff_role.role_arn)

        if cfg.removal_policy == "RETAIN":
            self.termination_protection = True
