"""Extension_Parameters: every name is published by the stack that owns the resource.

Requirements 11.1 to 11.7 and 12.1. The names come from
``core.parameters.extension_parameter_names``. Each one must exist as an ``AWS::SSM::Parameter``
in its owning stack, and its value must reference that stack's resource. The test runs for every
variant (dev, dev with ``RETAIN``, dev with two NAT gateways in ``eu-west-1``).
"""

from collections.abc import Callable, Mapping
from typing import Any

from aws_cdk.assertions import Template

from synthesis import SynthVariant
from tokenization_infra.core.parameters import ExtensionKey, extension_parameter_names

Json = Mapping[str, Any]
Matcher = Callable[[Json, Any], None]
"""Takes the template JSON and the parameter value, and asserts the value is right."""

OWNER: dict[ExtensionKey, str] = {
    ExtensionKey.VPC_ID: "network",
    ExtensionKey.PRIVATE_SUBNET_IDS: "network",
    ExtensionKey.ISOLATED_SUBNET_IDS: "network",
    ExtensionKey.LAMBDA_SG_ID: "network",
    ExtensionKey.API_ID: "api-compute",
    ExtensionKey.API_ROOT_RESOURCE_ID: "api-compute",
    ExtensionKey.API_BASE_URL: "api-compute",
    ExtensionKey.USER_POOL_ID: "identity",
    ExtensionKey.USER_POOL_ARN: "identity",
    ExtensionKey.RESOURCE_SERVER_ID: "identity",
    ExtensionKey.PORTAL_CLIENT_ID: "identity",
    ExtensionKey.HOSTED_UI_DOMAIN: "identity",
    ExtensionKey.DATA_KEY_ARN: "security",
    ExtensionKey.SIGNING_KEY_ARN: "security",
    ExtensionKey.EVENT_BUS_NAME: "messaging",
    ExtensionKey.EVENT_BUS_ARN: "messaging",
    ExtensionKey.ALERT_TOPIC_ARN: "observability",
}


def _resources(template_json: Json, resource_type: str) -> dict[str, Json]:
    resources: dict[str, Json] = template_json["Resources"]
    return {
        logical_id: body for logical_id, body in resources.items() if body["Type"] == resource_type
    }


def _only(template_json: Json, resource_type: str, **properties: Any) -> str:
    """Logical id of the single resource of the type whose properties include ``properties``."""
    found = [
        logical_id
        for logical_id, body in _resources(template_json, resource_type).items()
        if all(body["Properties"].get(k) == v for k, v in properties.items())
    ]
    assert len(found) == 1, f"{resource_type} {properties}: expected one, found {found}"
    return found[0]


def _ref(logical_id: str) -> Json:
    return {"Ref": logical_id}


def _get_att(logical_id: str, attribute: str) -> Json:
    return {"Fn::GetAtt": [logical_id, attribute]}


def _references(value: Any) -> set[str]:
    """Logical ids referenced by ``Ref`` and ``Fn::GetAtt`` anywhere in ``value``."""
    found: set[str] = set()
    if isinstance(value, dict):
        for key, inner in value.items():
            if key == "Ref":
                found.add(str(inner))
            elif key == "Fn::GetAtt":
                found.add(str(inner[0]))
            else:
                found |= _references(inner)
    elif isinstance(value, list):
        for item in value:
            found |= _references(item)
    return found


def _subnet_ids_value(group: str, template_json: Json) -> Matcher:
    """A comma-joined list of the Ref of every subnet in ``group`` (one per AZ)."""

    def check(_: Json, value: Any) -> None:
        expected = sorted(
            logical_id
            for logical_id, body in _resources(template_json, "AWS::EC2::Subnet").items()
            if {"Key": "aws-cdk:subnet-name", "Value": group} in body["Properties"]["Tags"]
        )
        assert expected, f"no subnets in group {group}"
        separator, items = value["Fn::Join"]
        assert separator == ","
        assert sorted(item["Ref"] for item in items) == expected

    return check


def _stack_expectations(variant: SynthVariant, owner: str) -> dict[ExtensionKey, Any]:
    """Expected value per key for the stack ``owner``. Callables are checkers, others literals."""
    cfg = variant.cfg
    template_json: Json = variant.template(owner).to_json()

    def one(resource_type: str, **properties: Any) -> str:
        return _only(template_json, resource_type, **properties)

    if owner == "network":
        vpc = one("AWS::EC2::VPC")
        lambda_sg = one("AWS::EC2::SecurityGroup", GroupDescription="Lambda functions in the VPC")
        return {
            ExtensionKey.VPC_ID: _ref(vpc),
            ExtensionKey.PRIVATE_SUBNET_IDS: _subnet_ids_value("private", template_json),
            ExtensionKey.ISOLATED_SUBNET_IDS: _subnet_ids_value("isolated", template_json),
            ExtensionKey.LAMBDA_SG_ID: _get_att(lambda_sg, "GroupId"),
        }
    if owner == "api-compute":
        api = one("AWS::ApiGateway::RestApi")
        stage = one("AWS::ApiGateway::Stage", StageName=cfg.stage)

        def base_url(_: Json, value: Any) -> None:
            # https://<api id>.execute-api.<region>.<URL suffix>/<stage>/
            assert _references(value) == {api, stage, "AWS::URLSuffix"}
            assert _ref(api) in value["Fn::Join"][1]
            assert _ref(stage) in value["Fn::Join"][1]

        return {
            ExtensionKey.API_ID: _ref(api),
            ExtensionKey.API_ROOT_RESOURCE_ID: _get_att(api, "RootResourceId"),
            ExtensionKey.API_BASE_URL: base_url,
        }
    if owner == "identity":
        pool = one("AWS::Cognito::UserPool")
        client = one("AWS::Cognito::UserPoolClient", ClientName=f"{cfg.stage}-portal")
        return {
            ExtensionKey.USER_POOL_ID: _ref(pool),
            ExtensionKey.USER_POOL_ARN: _get_att(pool, "Arn"),
            ExtensionKey.RESOURCE_SERVER_ID: cfg.cognito.resource_server.identifier,
            ExtensionKey.PORTAL_CLIENT_ID: _ref(client),
            ExtensionKey.HOSTED_UI_DOMAIN: (
                f"{cfg.cognito.domain_prefix}.auth.{cfg.region}.amazoncognito.com"
            ),
        }
    if owner == "security":
        data_key = one("AWS::KMS::Key", Description=f"{cfg.stage}: encrypts data at rest")
        signing_key = one("AWS::KMS::Key", KeySpec="ECC_NIST_EDWARDS25519")
        return {
            ExtensionKey.DATA_KEY_ARN: _get_att(data_key, "Arn"),
            ExtensionKey.SIGNING_KEY_ARN: _get_att(signing_key, "Arn"),
        }
    if owner == "messaging":
        bus = one("AWS::Events::EventBus")
        return {
            ExtensionKey.EVENT_BUS_NAME: _ref(bus),
            ExtensionKey.EVENT_BUS_ARN: _get_att(bus, "Arn"),
        }
    assert owner == "observability"
    topic = one("AWS::SNS::Topic")
    return {ExtensionKey.ALERT_TOPIC_ARN: _ref(topic)}


def _ssm_parameters(template: Template) -> dict[str, Json]:
    """Name to resource body, for every ``AWS::SSM::Parameter`` of the template."""
    parameters: dict[str, Json] = {}
    for body in template.find_resources("AWS::SSM::Parameter").values():
        name = body["Properties"]["Name"]
        assert name not in parameters, f"duplicate SSM parameter {name}"
        parameters[name] = body
    return parameters


def test_every_key_has_an_owner() -> None:
    """The ownership table covers all 17 keys (Requirements 11.1 to 11.7)."""
    assert set(OWNER) == set(ExtensionKey)
    assert len(ExtensionKey) == 17


def test_each_parameter_is_published_by_its_owning_stack_with_the_right_value(
    variant: SynthVariant,
) -> None:
    """Requirements 11.1 to 11.7, 12.1: name, owner stack, type and referenced resource."""
    names = extension_parameter_names(variant.cfg.project, variant.cfg.stage)
    assert len(names) == 17

    for owner in sorted(set(OWNER.values())):
        template = variant.template(owner)
        template_json: Json = template.to_json()
        published = _ssm_parameters(template)
        expectations = _stack_expectations(variant, owner)

        assert set(expectations) == {key for key, o in OWNER.items() if o == owner}
        for key, expected in expectations.items():
            name = names[key]
            assert name in published, f"{name} missing in stack {owner}"
            properties = published[name]["Properties"]
            assert properties["Type"] == "String"
            actual = properties["Value"]
            if callable(expected):
                expected(template_json, actual)
            else:
                assert actual == expected, f"{name}: {actual!r} != {expected!r}"


def test_stacks_publish_only_their_own_parameters(variant: SynthVariant) -> None:
    """Each stack holds exactly its keys, and the 17 names appear once across the stacks."""
    names = extension_parameter_names(variant.cfg.project, variant.cfg.stage)
    seen: list[str] = []
    for short_name in variant.stacks_by_name():
        published = set(_ssm_parameters(variant.template(short_name)))
        expected = {names[key] for key, owner in OWNER.items() if owner == short_name}
        assert published == expected, f"stack {short_name}"
        seen.extend(published)
    assert sorted(seen) == sorted(names.values())
