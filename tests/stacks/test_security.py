"""SecurityStack assertion tests (Requirements 3.1, 3.2, 3.5, 3.7, 11.5, 12.1).

Assertions are on the properties the requirements name, against the templates synthesized once
per session for each variant (dev, dev with ``removal_policy: RETAIN``, two NAT gateways).
"""

import json
from typing import Any

from aws_cdk.assertions import Match, Template

from synthesis import SynthVariant
from tokenization_infra.core.config import FunctionId
from tokenization_infra.core.naming import role_name
from tokenization_infra.core.parameters import ExtensionKey, extension_parameter_names

SIGNING_KEY_SPEC = "ECC_NIST_EDWARDS25519"
KYC_SECRET_COUNT_PER_PROVIDER = 2


def _signing_key(template: Template) -> tuple[str, dict[str, Any]]:
    found = template.find_resources("AWS::KMS::Key", {"Properties": {"KeySpec": SIGNING_KEY_SPEC}})
    assert len(found) == 1, f"expected one signing key, found {list(found)}"
    logical_id, resource = next(iter(found.items()))
    return logical_id, dict(resource)


def _data_key(template: Template) -> tuple[str, dict[str, Any]]:
    found = template.find_resources("AWS::KMS::Key", {"Properties": {"EnableKeyRotation": True}})
    assert len(found) == 1, f"expected one rotating key, found {list(found)}"
    logical_id, resource = next(iter(found.items()))
    return logical_id, dict(resource)


def _deny_statements(statements: list[dict[str, Any]], action: str) -> list[dict[str, Any]]:
    return [
        statement
        for statement in statements
        if statement.get("Effect") == "Deny" and statement.get("Action") == action
    ]


def test_data_key_rotates(variant: SynthVariant) -> None:
    template = variant.template("security")
    template.resource_count_is("AWS::KMS::Key", 2)
    template.has_resource_properties(
        "AWS::KMS::Key",
        {"EnableKeyRotation": True, "Enabled": Match.absent()},
    )
    _, data_key = _data_key(template)
    assert "KeySpec" not in data_key["Properties"]


def test_signing_key_is_ed25519_sign_verify(variant: SynthVariant) -> None:
    template = variant.template("security")
    template.has_resource_properties(
        "AWS::KMS::Key",
        {"KeySpec": SIGNING_KEY_SPEC, "KeyUsage": "SIGN_VERIFY"},
    )
    _, signing_key = _signing_key(template)
    # AWS KMS does not support automatic rotation for asymmetric keys.
    assert signing_key["Properties"].get("EnableKeyRotation") in (None, False)


def test_both_keys_are_retained(variant: SynthVariant) -> None:
    """3.7: both keys are RETAIN whatever the stage's removal policy (dev and RETAIN variants)."""
    template = variant.template("security")
    for resource in (_data_key(template)[1], _signing_key(template)[1]):
        assert resource["DeletionPolicy"] == "Retain"
        assert resource["UpdateReplacePolicy"] == "Retain"


def test_keys_have_stage_aliases(variant: SynthVariant) -> None:
    template = variant.template("security")
    stage = variant.cfg.stage
    data_id, _ = _data_key(template)
    signing_id, _ = _signing_key(template)
    template.has_resource_properties(
        "AWS::KMS::Alias",
        {"AliasName": f"alias/{stage}-data", "TargetKeyId": {"Fn::GetAtt": [data_id, "Arn"]}},
    )
    template.has_resource_properties(
        "AWS::KMS::Alias",
        {
            "AliasName": f"alias/{stage}-stellar-signing",
            "TargetKeyId": {"Fn::GetAtt": [signing_id, "Arn"]},
        },
    )


def test_signing_key_policy_denies_sign_except_signer_role(variant: SynthVariant) -> None:
    """3.3, 3.6 (D6): only the signer role may call kms:Sign."""
    template = variant.template("security")
    _, signing_key = _signing_key(template)
    statements = signing_key["Properties"]["KeyPolicy"]["Statement"]
    denies = _deny_statements(statements, "kms:Sign")
    assert len(denies) == 1, f"expected one kms:Sign Deny, found {denies}"
    deny = denies[0]
    assert deny["Principal"] == {"AWS": "*"}
    assert deny["Resource"] == "*"
    condition = deny["Condition"]["StringNotEquals"]["aws:PrincipalArn"]
    assert role_name(variant.cfg.stage, FunctionId.SIGNER) in json.dumps(condition)
    # No other statement may allow or deny kms:Sign.
    others = [s for s in statements if s is not deny and "kms:Sign" in json.dumps(s.get("Action"))]
    assert others == []


def test_kyc_secrets_are_encrypted_and_hold_no_generated_value(variant: SynthVariant) -> None:
    """3.5: two Kyc_Secrets per provider, Data_Key encrypted, no GenerateSecretString."""
    template = variant.template("security")
    data_id, _ = _data_key(template)
    providers = variant.cfg.kyc_providers
    secrets = template.find_resources("AWS::SecretsManager::Secret")
    assert len(secrets) == KYC_SECRET_COUNT_PER_PROVIDER * len(providers)
    names = set()
    for resource in secrets.values():
        properties = resource["Properties"]
        assert "GenerateSecretString" not in properties
        assert properties["SecretString"] == "{}"
        assert properties["KmsKeyId"] == {"Fn::GetAtt": [data_id, "Arn"]}
        names.add(properties["Name"])
    stage = variant.cfg.stage
    assert names == {
        f"{stage}/kyc/{provider}/{kind}"
        for provider in providers
        for kind in ("api-credentials", "webhook-signing")
    }


def test_kyc_secret_removal_follows_stage_config(variant: SynthVariant) -> None:
    template = variant.template("security")
    expected = "Retain" if variant.cfg.removal_policy == "RETAIN" else "Delete"
    secrets = template.find_resources("AWS::SecretsManager::Secret")
    assert {resource["DeletionPolicy"] for resource in secrets.values()} == {expected}


def test_kyc_secret_policies_deny_get_secret_value_except_stellar_kyc(
    variant: SynthVariant,
) -> None:
    """3.6 (D6): each Kyc_Secret has a Deny on GetSecretValue for everyone but Stellar_KYC."""
    template = variant.template("security")
    secret_ids = set(template.find_resources("AWS::SecretsManager::Secret"))
    policies = template.find_resources("AWS::SecretsManager::ResourcePolicy")
    assert {policy["Properties"]["SecretId"]["Ref"] for policy in policies.values()} == secret_ids
    kyc_role = role_name(variant.cfg.stage, FunctionId.STELLAR_KYC)
    for logical_id, policy in policies.items():
        statements = policy["Properties"]["ResourcePolicy"]["Statement"]
        denies = _deny_statements(statements, "secretsmanager:GetSecretValue")
        assert len(denies) == 1, f"{logical_id}: expected one Deny, found {denies}"
        deny = denies[0]
        assert deny["Principal"] == {"AWS": "*"}
        condition = deny["Condition"]["StringNotEquals"]["aws:PrincipalArn"]
        assert kyc_role in json.dumps(condition)


def test_extension_parameters_publish_the_key_arns(variant: SynthVariant) -> None:
    """11.5: keys/data-key-arn and keys/signing-key-arn, and no other parameters."""
    template = variant.template("security")
    names = extension_parameter_names(variant.cfg.project, variant.cfg.stage)
    data_id, _ = _data_key(template)
    signing_id, _ = _signing_key(template)
    template.resource_count_is("AWS::SSM::Parameter", 2)
    template.has_resource_properties(
        "AWS::SSM::Parameter",
        {
            "Name": names[ExtensionKey.DATA_KEY_ARN],
            "Type": "String",
            "Value": {"Fn::GetAtt": [data_id, "Arn"]},
        },
    )
    template.has_resource_properties(
        "AWS::SSM::Parameter",
        {
            "Name": names[ExtensionKey.SIGNING_KEY_ARN],
            "Type": "String",
            "Value": {"Fn::GetAtt": [signing_id, "Arn"]},
        },
    )
    assert names[ExtensionKey.DATA_KEY_ARN].endswith("/keys/data-key-arn")
    assert names[ExtensionKey.SIGNING_KEY_ARN].endswith("/keys/signing-key-arn")
