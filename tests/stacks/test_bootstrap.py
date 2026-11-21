"""Bootstrap_Stack assertion tests (Requirements 13.4, 13.5, 1.12, 12.6).

``BootstrapStack`` is built only by ``bootstrap_app.py`` (design D7), so these tests build it
with ``build_bootstrap_app`` from ``dev.yaml`` plus overrides, the way ``synthesis.py`` builds
the application variants.
"""

import copy
from dataclasses import dataclass
from typing import Any

import pytest
from aws_cdk.assertions import Template

from synthesis import Raw, build_variant, load_raw_dev, new_app
from tokenization_infra.app_builder import build_bootstrap_app
from tokenization_infra.core.config import StageConfig, parse_stage_config
from tokenization_infra.core.custom_resources import find_custom_resources
from tokenization_infra.core.oidc import (
    BOOTSTRAP_DEPLOY_ROLE_KINDS,
    GITHUB_OIDC_AUDIENCE,
    GITHUB_OIDC_HOST,
    bootstrap_role_arn,
    github_oidc_provider_arn,
    oidc_subjects,
)
from tokenization_infra.stacks.bootstrap import BootstrapStack

AUD_KEY = f"{GITHUB_OIDC_HOST}:aud"
SUB_KEY = f"{GITHUB_OIDC_HOST}:sub"


@dataclass(frozen=True)
class BootstrapVariant:
    cfg: StageConfig
    stack: BootstrapStack
    template: Template


def _build(*, create_oidc_provider: bool) -> BootstrapVariant:
    raw: Raw = copy.deepcopy(load_raw_dev())
    raw["cicd"]["create_oidc_provider"] = create_oidc_provider
    cfg = parse_stage_config(raw, stage="dev")
    stack = build_bootstrap_app(new_app(), cfg)
    return BootstrapVariant(cfg=cfg, stack=stack, template=Template.from_stack(stack))


@pytest.fixture(scope="module")
def created() -> BootstrapVariant:
    return _build(create_oidc_provider=True)


@pytest.fixture(scope="module")
def imported() -> BootstrapVariant:
    return _build(create_oidc_provider=False)


def _role(variant: BootstrapVariant, name: str) -> dict[str, Any]:
    roles = variant.template.find_resources("AWS::IAM::Role", {"Properties": {"RoleName": name}})
    assert len(roles) == 1, f"expected exactly one role named {name}, got {list(roles)}"
    role: dict[str, Any] = dict(next(iter(roles.values())))
    return role


def _trust_statement(role: dict[str, Any]) -> dict[str, Any]:
    statements = role["Properties"]["AssumeRolePolicyDocument"]["Statement"]
    assert len(statements) == 1
    statement: dict[str, Any] = statements[0]
    assert statement["Effect"] == "Allow"
    assert statement["Action"] == "sts:AssumeRoleWithWebIdentity"
    return statement


def _policy_statements(variant: BootstrapVariant, role_logical_id: str) -> list[dict[str, Any]]:
    """Statements of every inline policy attached to the role with this logical id."""
    policies = variant.template.find_resources("AWS::IAM::Policy")
    return [
        statement
        for policy in policies.values()
        if policy["Properties"]["Roles"] == [{"Ref": role_logical_id}]
        for statement in policy["Properties"]["PolicyDocument"]["Statement"]
    ]


def _role_logical_id(variant: BootstrapVariant, name: str) -> str:
    roles = variant.template.find_resources("AWS::IAM::Role", {"Properties": {"RoleName": name}})
    return next(iter(roles))


def _oidc_subjects(cfg: StageConfig) -> Any:
    cicd = cfg.cicd
    return oidc_subjects(cicd.github_owner, cicd.github_repo, cfg.stage, cicd.branch)


def _role_arns(cfg: StageConfig, *kinds: str) -> list[str]:
    return [
        bootstrap_role_arn(kind, cfg.cicd.cdk_qualifier, cfg.account, cfg.region, "aws")
        for kind in kinds
    ]


def test_stack_id_is_stage_prefixed(created: BootstrapVariant) -> None:
    assert created.stack.node.id == "dev-cicd-bootstrap"


def test_application_app_never_contains_the_bootstrap_stack() -> None:
    """Design D7: ``cdk deploy --all`` on ``app.py`` cannot deploy the bootstrap stack."""
    stack_ids = {stack.node.id for stack in build_variant("dev").stacks_by_name().values()}
    assert "dev-cicd-bootstrap" not in stack_ids


def test_oidc_provider_for_github_actions(created: BootstrapVariant) -> None:
    created.template.resource_count_is("AWS::IAM::OIDCProvider", 1)
    created.template.has_resource_properties(
        "AWS::IAM::OIDCProvider",
        {"Url": f"https://{GITHUB_OIDC_HOST}", "ClientIdList": [GITHUB_OIDC_AUDIENCE]},
    )


def test_deploy_role_trusts_only_the_deploy_subject(created: BootstrapVariant) -> None:
    """Requirement 13.5: ``aud`` and ``sub`` use ``StringEquals``; ``sub`` is the deploy subject."""
    role = _role(created, "dev-github-deploy")
    statement = _trust_statement(role)

    assert statement["Condition"] == {
        "StringEquals": {
            AUD_KEY: GITHUB_OIDC_AUDIENCE,
            SUB_KEY: _oidc_subjects(created.cfg).deploy,
        }
    }
    provider_id = next(iter(created.template.find_resources("AWS::IAM::OIDCProvider")))
    assert statement["Principal"] == {"Federated": {"Ref": provider_id}}
    assert role["Properties"]["MaxSessionDuration"] == 3600


def test_deploy_role_may_only_assume_the_cdk_bootstrap_roles(created: BootstrapVariant) -> None:
    """Requirement 13.4: only ``sts:AssumeRole`` on the four CDK bootstrap role ARNs."""
    statements = _policy_statements(created, _role_logical_id(created, "dev-github-deploy"))

    assert len(statements) == 1
    assert statements[0]["Effect"] == "Allow"
    assert statements[0]["Action"] == "sts:AssumeRole"
    assert statements[0]["Resource"] == _role_arns(created.cfg, *BOOTSTRAP_DEPLOY_ROLE_KINDS)
    assert [arn.split("/cdk-")[1].split("-role-")[0] for arn in statements[0]["Resource"]] == [
        f"{created.cfg.cicd.cdk_qualifier}-{kind}" for kind in BOOTSTRAP_DEPLOY_ROLE_KINDS
    ]


def test_diff_role_trusts_only_pull_request_subjects(created: BootstrapVariant) -> None:
    """Requirement 13.5: ``aud`` is ``StringEquals``, ``sub`` is ``StringLike`` on pull refs."""
    role = _role(created, "dev-github-diff")
    statement = _trust_statement(role)

    assert statement["Condition"] == {
        "StringEquals": {AUD_KEY: GITHUB_OIDC_AUDIENCE},
        "StringLike": {SUB_KEY: _oidc_subjects(created.cfg).pull_request},
    }
    assert _oidc_subjects(created.cfg).pull_request.endswith(":pull_request:ref:refs/pull/*")
    provider_id = next(iter(created.template.find_resources("AWS::IAM::OIDCProvider")))
    assert statement["Principal"] == {"Federated": {"Ref": provider_id}}
    assert role["Properties"]["MaxSessionDuration"] == 3600


def test_diff_role_may_only_assume_the_lookup_role(created: BootstrapVariant) -> None:
    """Requirement 13.4: the Diff_Role cannot assume the deploy or publishing roles."""
    statements = _policy_statements(created, _role_logical_id(created, "dev-github-diff"))

    assert len(statements) == 1
    assert statements[0]["Effect"] == "Allow"
    assert statements[0]["Action"] == "sts:AssumeRole"
    assert statements[0]["Resource"] == _role_arns(created.cfg, "lookup")[0]


def test_roles_hold_no_other_permissions(created: BootstrapVariant) -> None:
    """Neither role has managed policies, and the only policies are the two inline ones."""
    created.template.resource_count_is("AWS::IAM::Role", 2)
    created.template.resource_count_is("AWS::IAM::Policy", 2)
    for name in ("dev-github-deploy", "dev-github-diff"):
        assert "ManagedPolicyArns" not in _role(created, name)["Properties"]


def test_no_custom_resources(created: BootstrapVariant, imported: BootstrapVariant) -> None:
    """Requirement 1.12: ``iam.OidcProviderNative`` avoids the OpenIdConnectProvider handler."""
    for variant in (created, imported):
        assert find_custom_resources(variant.template.to_json()) == []


@pytest.mark.parametrize("create_oidc_provider", [True, False])
def test_bootstrap_app_passes_cdk_nag(create_oidc_provider: bool) -> None:
    """Requirement 12.6: AwsSolutionsChecks reports nothing unacknowledged on a fresh app."""
    raw: Raw = copy.deepcopy(load_raw_dev())
    raw["cicd"]["create_oidc_provider"] = create_oidc_provider
    app = new_app()
    build_bootstrap_app(app, parse_stage_config(raw, stage="dev"))

    app.synth(force=True)  # raises when cdk-nag reports an unacknowledged error


def test_importing_the_provider_creates_no_provider_resource(imported: BootstrapVariant) -> None:
    """``cicd.create_oidc_provider: false`` reuses the account's existing provider."""
    imported.template.resource_count_is("AWS::IAM::OIDCProvider", 0)


def test_imported_provider_arn_is_the_trust_principal(imported: BootstrapVariant) -> None:
    arn = github_oidc_provider_arn(imported.cfg.account, "aws")
    for name in ("dev-github-deploy", "dev-github-diff"):
        assert _trust_statement(_role(imported, name))["Principal"] == {"Federated": arn}


def test_imported_variant_keeps_the_same_trust_conditions(
    created: BootstrapVariant, imported: BootstrapVariant
) -> None:
    for name in ("dev-github-deploy", "dev-github-diff"):
        assert (
            _trust_statement(_role(imported, name))["Condition"]
            == _trust_statement(_role(created, name))["Condition"]
        )
