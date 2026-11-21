"""IdentityStack assertion tests (Requirements 4.1 to 4.9, 4.11, 11.4, 12.1, 12.4).

Assertions are on the properties the requirements name, against the templates synthesized once
per session for each variant. The portal origin, local development URLs and domain prefix are
read from the variant's ``StageConfig`` so the tests follow ``dev.yaml``.
"""

from typing import Any

from aws_cdk.assertions import Match, Template

from synthesis import SynthVariant
from tokenization_infra.core.parameters import ExtensionKey, extension_parameter_names
from tokenization_infra.core.urls import callback_urls, logout_urls

EXPECTED_KEYS = {
    ExtensionKey.USER_POOL_ID,
    ExtensionKey.USER_POOL_ARN,
    ExtensionKey.RESOURCE_SERVER_ID,
    ExtensionKey.PORTAL_CLIENT_ID,
    ExtensionKey.HOSTED_UI_DOMAIN,
}


def _only(template: Template, resource_type: str) -> dict[str, Any]:
    found = template.find_resources(resource_type)
    assert len(found) == 1, f"expected one {resource_type}, found {list(found)}"
    resource: dict[str, Any] = dict(next(iter(found.values())))
    return resource


def test_user_pool_sign_in_sign_up_and_password_policy(variant: SynthVariant) -> None:
    """Requirements 4.1, 4.3."""
    template = variant.template("identity")
    template.has_resource_properties(
        "AWS::Cognito::UserPool",
        {
            "UserPoolName": f"{variant.cfg.stage}-users",
            "UsernameAttributes": ["email"],
            "AutoVerifiedAttributes": ["email"],
            "AdminCreateUserConfig": {"AllowAdminCreateUserOnly": False},
            "Policies": {"PasswordPolicy": Match.object_like({"MinimumLength": 12})},
        },
    )


def test_user_pool_mfa_is_optional_with_totp_only(variant: SynthVariant) -> None:
    """Requirement 4.2."""
    template = variant.template("identity")
    template.has_resource_properties(
        "AWS::Cognito::UserPool",
        {"MfaConfiguration": "OPTIONAL", "EnabledMfas": ["SOFTWARE_TOKEN_MFA"]},
    )
    properties = _only(template, "AWS::Cognito::UserPool")["Properties"]
    assert "SMS_MFA" not in properties["EnabledMfas"]


def test_user_pool_recovers_accounts_by_email_only(variant: SynthVariant) -> None:
    """Requirement 4.9."""
    variant.template("identity").has_resource_properties(
        "AWS::Cognito::UserPool",
        {
            "AccountRecoverySetting": {
                "RecoveryMechanisms": [{"Name": "verified_email", "Priority": 1}]
            }
        },
    )


def test_user_pool_protection_follows_removal_policy(variant: SynthVariant) -> None:
    """Requirements 4.1, 11.4: deletion protection and removal policy come from the config."""
    retain = variant.cfg.removal_policy == "RETAIN"
    resource = _only(variant.template("identity"), "AWS::Cognito::UserPool")
    assert resource["DeletionPolicy"] == ("Retain" if retain else "Delete")
    assert resource["Properties"].get("DeletionProtection") == ("ACTIVE" if retain else "INACTIVE")


def test_one_group_per_configured_group(variant: SynthVariant) -> None:
    """Requirement 4.5."""
    template = variant.template("identity")
    groups = variant.cfg.cognito.groups
    assert groups
    template.resource_count_is("AWS::Cognito::UserPoolGroup", len(groups))
    for group in groups:
        template.has_resource_properties(
            "AWS::Cognito::UserPoolGroup",
            {"GroupName": group, "UserPoolId": Match.any_value()},
        )


def test_resource_server_has_the_configured_identifier_and_scopes(variant: SynthVariant) -> None:
    """Requirement 4.6."""
    server = variant.cfg.cognito.resource_server
    variant.template("identity").has_resource_properties(
        "AWS::Cognito::UserPoolResourceServer",
        {
            "Identifier": server.identifier,
            "Scopes": [
                {"ScopeName": scope.name, "ScopeDescription": scope.description}
                for scope in server.scopes
            ],
        },
    )


def test_portal_client_has_no_secret_and_only_the_code_grant(variant: SynthVariant) -> None:
    """Requirements 4.4, 4.7."""
    template = variant.template("identity")
    template.resource_count_is("AWS::Cognito::UserPoolClient", 1)
    template.has_resource_properties(
        "AWS::Cognito::UserPoolClient",
        {
            "ClientName": f"{variant.cfg.stage}-portal",
            "GenerateSecret": False,
            "AllowedOAuthFlowsUserPoolClient": True,
            "AllowedOAuthFlows": ["code"],
            "SupportedIdentityProviders": ["COGNITO"],
        },
    )


def test_portal_client_explicit_auth_flows_are_refresh_only(variant: SynthVariant) -> None:
    """Requirement 4.4: no password, SRP, admin or custom flows."""
    properties = _only(variant.template("identity"), "AWS::Cognito::UserPoolClient")["Properties"]
    assert properties["ExplicitAuthFlows"] == ["ALLOW_REFRESH_TOKEN_AUTH"]


def test_portal_client_scopes_are_openid_email_profile_and_configured_resource_scopes(
    variant: SynthVariant,
) -> None:
    """Requirement 4.7."""
    portal = variant.cfg.portal
    identifier = variant.cfg.cognito.resource_server.identifier
    template = variant.template("identity")
    server_ids = set(template.find_resources("AWS::Cognito::UserPoolResourceServer"))
    assert len(server_ids) == 1
    properties = _only(template, "AWS::Cognito::UserPoolClient")["Properties"]

    def resolve(scope: Any) -> str:
        """A resource scope is ``Fn::Join`` of ``Ref`` to the resource server (its identifier)."""
        if isinstance(scope, str):
            return scope
        parts = scope["Fn::Join"][1]
        return "".join(
            identifier if part == {"Ref": next(iter(server_ids))} else part for part in parts
        )

    scopes = [resolve(scope) for scope in properties["AllowedOAuthScopes"]]
    assert len(scopes) == len(set(scopes))
    assert set(scopes) == {
        "openid",
        "email",
        "profile",
        *(f"{identifier}/{name}" for name in portal.scopes),
    }


def test_portal_client_callback_urls_come_from_the_portal_origin_and_local_dev_urls(
    variant: SynthVariant,
) -> None:
    """Requirement 4.8."""
    portal = variant.cfg.portal
    properties = _only(variant.template("identity"), "AWS::Cognito::UserPoolClient")["Properties"]
    assert portal.local_dev_callback_urls
    assert properties["CallbackURLs"] == [
        portal.origin + portal.callback_path,
        *portal.local_dev_callback_urls,
    ]
    assert properties["CallbackURLs"] == callback_urls(
        portal.origin, portal.callback_path, portal.local_dev_callback_urls
    )


def test_portal_client_logout_urls_include_local_dev_origins_with_the_logout_path(
    variant: SynthVariant,
) -> None:
    """Requirement 4.8."""
    portal = variant.cfg.portal
    properties = _only(variant.template("identity"), "AWS::Cognito::UserPoolClient")["Properties"]
    assert properties["LogoutURLs"] == logout_urls(
        portal.origin, portal.logout_path, portal.local_dev_callback_urls
    )
    assert portal.origin + portal.logout_path in properties["LogoutURLs"]
    for url in portal.local_dev_callback_urls:
        origin = url.split("://", 1)[0] + "://" + url.split("://", 1)[1].split("/", 1)[0]
        assert origin + portal.logout_path in properties["LogoutURLs"]


def test_hosted_ui_domain_prefix_is_the_configured_value_unchanged(variant: SynthVariant) -> None:
    """Requirement 4.1 (D11): no second stage prefix."""
    prefix = variant.cfg.cognito.domain_prefix
    template = variant.template("identity")
    template.resource_count_is("AWS::Cognito::UserPoolDomain", 1)
    template.has_resource_properties("AWS::Cognito::UserPoolDomain", {"Domain": prefix})
    assert prefix.startswith(f"{variant.cfg.stage}-")
    assert not prefix.startswith(f"{variant.cfg.stage}-{variant.cfg.stage}-")


def test_dev_hosted_ui_domain_prefix_is_dev_tokenization_portal(dev_variant: SynthVariant) -> None:
    dev_variant.template("identity").has_resource_properties(
        "AWS::Cognito::UserPoolDomain", {"Domain": "dev-tokenization-portal"}
    )


def test_identity_publishes_the_five_extension_parameters(variant: SynthVariant) -> None:
    """Requirement 11.4."""
    names = extension_parameter_names(variant.cfg.project, variant.cfg.stage)
    template = variant.template("identity")
    parameters = template.find_resources("AWS::SSM::Parameter")
    published = {resource["Properties"]["Name"] for resource in parameters.values()}
    assert published == {names[key] for key in EXPECTED_KEYS}
    for key in EXPECTED_KEYS:
        template.has_resource_properties(
            "AWS::SSM::Parameter",
            {"Name": names[key], "Type": "String", "Value": Match.any_value()},
        )


def test_extension_parameter_values_are_the_resource_references(variant: SynthVariant) -> None:
    """Requirement 11.4: the resource server and hosted UI values are literals from the config."""
    names = extension_parameter_names(variant.cfg.project, variant.cfg.stage)
    template = variant.template("identity")
    template.has_resource_properties(
        "AWS::SSM::Parameter",
        {
            "Name": names[ExtensionKey.RESOURCE_SERVER_ID],
            "Value": variant.cfg.cognito.resource_server.identifier,
        },
    )
    template.has_resource_properties(
        "AWS::SSM::Parameter",
        {
            "Name": names[ExtensionKey.HOSTED_UI_DOMAIN],
            "Value": (
                f"{variant.cfg.cognito.domain_prefix}.auth.{variant.cfg.region}.amazoncognito.com"
            ),
        },
    )
    template.has_resource_properties(
        "AWS::SSM::Parameter",
        {
            "Name": names[ExtensionKey.USER_POOL_ID],
            "Value": {"Ref": Match.string_like_regexp("^UserPool")},
        },
    )


def test_no_amplify_resource_in_any_stack(variant: SynthVariant) -> None:
    """Requirements 4.11, 12.4: Amplify apps are created by hand."""
    assert variant.templates
    offenders = [
        f"{stack_id}: {logical_id}"
        for stack_id, template in variant.templates.items()
        for logical_id, resource in template.to_json().get("Resources", {}).items()
        if str(resource.get("Type", "")).startswith("AWS::Amplify")
    ]
    assert offenders == [], f"Amplify resources in variant {variant.name}: {offenders}"


def test_identity_stack_has_termination_protection_only_when_retaining(
    variant: SynthVariant,
) -> None:
    """Requirement 12.1."""
    stack = variant.stacks_by_name()["identity"]
    assert stack.termination_protection == (variant.cfg.removal_policy == "RETAIN")
