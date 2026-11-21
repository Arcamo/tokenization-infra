"""App-level assertion tests (Requirements 1.3, 1.8, 1.9, 1.10, 1.11, 13.3).

They run against every synthesized variant: stack ids, regions, tags and the stage prefix on
stack names and explicit resource names.
"""

from typing import Any

import aws_cdk as cdk

from synthesis import SynthVariant
from tokenization_infra.core.config import CONFIG_DIR

STACK_NAMES = (
    "security",
    "network",
    "data",
    "messaging",
    "identity",
    "api-compute",
    "observability",
)

# Property names that hold the explicit name of a resource, by suffix or exact key.
NAME_KEY_SUFFIXES = ("Name", "Names")
NAME_KEYS_BY_TYPE = {"AWS::Cognito::UserPoolDomain": frozenset({"Domain"})}

# Properties that end in "Name" but are not the name of a named resource of this stack.
# ``PolicyName`` is the logical id CDK gives an inline policy; the others are service
# identifiers, a database name inside the instance, a fixed target group name or a metric.
NOT_RESOURCE_NAMES = frozenset(
    {"ServiceName", "DBName", "TargetGroupName", "MetricName", "PolicyName"}
)

# Resource types whose name is exempt from the stage prefix (Requirement 1.9).
USER_POOL_GROUP = "AWS::Cognito::UserPoolGroup"
RESOURCE_SERVER = "AWS::Cognito::UserPoolResourceServer"

# Rendered form of a name with a CloudFormation intrinsic, for example the API Gateway
# execution log group ``API-Gateway-Execution-Logs_${ApiF70053CD}/dev``.
REF_PLACEHOLDER = "${ref}"


def _render(value: object) -> str | None:
    """A literal name as a string, with intrinsics as ``${ref}``; ``None`` for a bare reference."""
    if isinstance(value, str):
        return value
    if isinstance(value, dict) and set(value) == {"Fn::Join"}:
        delimiter, parts = value["Fn::Join"]
        rendered = [p if isinstance(p, str) else REF_PLACEHOLDER for p in parts]
        return str(delimiter).join(rendered)
    return None


def _name_properties(resource: dict[str, Any]) -> dict[str, str]:
    """Explicit name properties of one template resource, rendered as strings."""
    names: dict[str, str] = {}
    extra_keys = NAME_KEYS_BY_TYPE.get(resource["Type"], frozenset())
    for key, value in (resource.get("Properties") or {}).items():
        is_name = key in extra_keys or key.endswith(NAME_KEY_SUFFIXES)
        if not is_name or key in NOT_RESOURCE_NAMES:
            continue
        rendered = _render(value)
        if rendered is not None:  # a Ref or GetAtt points at a name that is checked elsewhere
            names[key] = rendered
    return names


def _carries_stage(name: str, stage: str) -> bool:
    return (
        name == stage
        or name.startswith((f"{stage}-", f"{stage}/", f"/{stage}/"))
        or f"/{stage}/" in name
    )


def _is_exempt(resource_type: str, name: str, project: str, stage: str) -> bool:
    """The Requirement 1.9 exceptions."""
    if resource_type in (USER_POOL_GROUP, RESOURCE_SERVER):
        return True
    if resource_type == "AWS::SSM::Parameter":
        return name.startswith(f"/{project}/{stage}/")
    if resource_type == "AWS::KMS::Alias":
        return name.startswith("alias/")
    if resource_type == "AWS::Logs::LogGroup":
        return name.startswith("/aws/") or name == (
            f"API-Gateway-Execution-Logs_{REF_PLACEHOLDER}/{stage}"
        )
    return False


def _tag_maps(resource: dict[str, Any]) -> list[dict[str, str]]:
    """Every tag set on the resource as ``{key: value}``, wherever the resource type keeps it."""
    properties = resource.get("Properties") or {}
    candidates: list[object] = [properties.get("Tags"), properties.get("UserPoolTags")]
    candidates.extend(replica.get("Tags") for replica in properties.get("Replicas") or [])
    maps: list[dict[str, str]] = []
    for tags in candidates:
        if isinstance(tags, list):
            maps.append({tag["Key"]: tag["Value"] for tag in tags})
        elif isinstance(tags, dict):
            maps.append({str(k): str(v) for k, v in tags.items()})
    return maps


def test_dev_stage_config_exists(dev_variant: SynthVariant) -> None:
    """Requirement 1.3."""
    assert (CONFIG_DIR / "dev.yaml").is_file()
    assert dev_variant.cfg.stage == "dev"


def test_the_seven_stacks_exist_and_the_bootstrap_stack_does_not(variant: SynthVariant) -> None:
    """Requirements 1.11, 13.3."""
    stage = variant.cfg.stage
    stacks = [child for child in variant.app.node.children if isinstance(child, cdk.Stack)]
    assert sorted(stack.node.id for stack in stacks) == sorted(f"{stage}-{n}" for n in STACK_NAMES)
    assert sorted(variant.stacks_by_name()) == sorted(STACK_NAMES)
    assert not any("bootstrap" in stack.node.id for stack in stacks)
    assert set(variant.templates) == {stack.node.id for stack in stacks}


def test_every_stack_is_in_the_configured_region(variant: SynthVariant) -> None:
    """Requirement 1.8."""
    for short_name, stack in variant.stacks_by_name().items():
        assert stack.region == variant.cfg.region, short_name
        assert stack.account == variant.cfg.account, short_name


def test_every_tagged_resource_carries_the_three_tags(variant: SynthVariant) -> None:
    """Requirement 1.10."""
    expected = {
        "project": variant.cfg.project,
        "stage": variant.cfg.stage,
        "managed-by": "cdk",
    }
    for stack_id, template in variant.templates.items():
        tagged = 0
        for logical_id, resource in template.to_json()["Resources"].items():
            for tags in _tag_maps(resource):
                tagged += 1
                actual = {key: tags.get(key) for key in expected}
                assert actual == expected, f"{stack_id}: {logical_id} ({resource['Type']})"
        assert tagged > 0, f"{stack_id} has no tagged resource"


def test_stack_names_carry_the_stage_prefix(variant: SynthVariant) -> None:
    """Requirement 1.9, stack names."""
    for short_name, stack in variant.stacks_by_name().items():
        assert stack.stack_name == f"{variant.cfg.stage}-{short_name}"


def test_explicit_resource_names_carry_the_stage(variant: SynthVariant) -> None:
    """Requirement 1.9, resource names, with its exceptions."""
    stage, project = variant.cfg.stage, variant.cfg.project
    checked = 0
    offenders: list[str] = []
    for stack_id, template in variant.templates.items():
        for logical_id, resource in template.to_json()["Resources"].items():
            for key, name in _name_properties(resource).items():
                if _is_exempt(resource["Type"], name, project, stage):
                    continue
                checked += 1
                if not _carries_stage(name, stage):
                    offenders.append(f"{stack_id}: {resource['Type']} {logical_id} {key}={name!r}")
    assert checked > 0, "the scan found no explicit resource names"
    assert offenders == [], f"names without the stage in variant {variant.name}: {offenders}"


def test_name_scan_treats_the_documented_exceptions_as_exempt() -> None:
    assert _is_exempt("AWS::SSM::Parameter", "/tokenization/dev/api/id", "tokenization", "dev")
    assert not _is_exempt("AWS::SSM::Parameter", "/tokenization/api/id", "tokenization", "dev")
    assert _is_exempt("AWS::KMS::Alias", "alias/dev-data", "tokenization", "dev")
    assert _is_exempt("AWS::Logs::LogGroup", "/aws/lambda/x", "tokenization", "dev")
    assert not _is_exempt("AWS::Logs::LogGroup", "/other/x", "tokenization", "dev")
    assert _is_exempt(
        "AWS::Logs::LogGroup", f"API-Gateway-Execution-Logs_{REF_PLACEHOLDER}/dev", "t", "dev"
    )
    assert _is_exempt(USER_POOL_GROUP, "investor", "tokenization", "dev")
    assert _is_exempt(RESOURCE_SERVER, "tokenization-api", "tokenization", "dev")
    assert not _is_exempt("AWS::SQS::Queue", "payments", "tokenization", "dev")
    assert not _carries_stage("payments", "dev")
    assert _carries_stage("dev-payments", "dev")
    assert _carries_stage("/dev/vpc/flow-logs", "dev")
    assert _carries_stage("dev/db/master", "dev")
