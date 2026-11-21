"""Exclusive-grant scan over every IAM policy of every stack and variant.

Requirements 3.3, 3.4, 3.6, 6.7, 9.4, 9.10, 12.3. The scan reads the synthesized templates and
checks, across all stacks:

* ``kms:Sign``: only the Signer role policy. The only other mention is the Deny statement in
  the Signing_Key key policy.
* ``kms:GetPublicKey``: only the Signer and Stellar_KYC role policies.
* ``secretsmanager:GetSecretValue`` on a Kyc_Secret: only the Stellar_KYC role policy. Each
  Kyc_Secret resource policy carries a Deny for it.
* ``lambda:InvokeFunction`` on the Signer function: only the Stellar_KYC role policy, and no
  ``AWS::Lambda::Permission`` on the Signer.
* ``events:PutEvents``: all five Domain_Lambda role policies, never the Signer's.
* ``states:StartExecution``: among Lambda roles only sagas-payments. The Scheduler role
  holds it for the saga it starts, and no other role does.
* No Lambda role policy has ``Action: "*"``, ``NotAction`` or a wildcard that implies one of
  the exclusive actions, and a Lambda role has no managed policy besides the VPC access one.

The scan is a pure function over template dictionaries (``find_violations``), so the last tests
feed it templates with an injected violation and check that each one is reported.
"""

import copy
import fnmatch
import json
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

import pytest

from synthesis import SynthVariant
from tokenization_infra.core.config import FunctionId
from tokenization_infra.core.naming import NameKind, function_name, resource_name, role_name

KMS_SIGN = "kms:Sign"
KMS_GET_PUBLIC_KEY = "kms:GetPublicKey"
GET_SECRET_VALUE = "secretsmanager:GetSecretValue"
LAMBDA_INVOKE = "lambda:InvokeFunction"
PUT_EVENTS = "events:PutEvents"
START_EXECUTION = "states:StartExecution"

EXCLUSIVE_ACTIONS = (
    KMS_SIGN,
    KMS_GET_PUBLIC_KEY,
    GET_SECRET_VALUE,
    LAMBDA_INVOKE,
    PUT_EVENTS,
    START_EXECUTION,
)

SIGNING_KEY_SPEC = "ECC_NIST_EDWARDS25519"
VPC_ACCESS_POLICY = "AWSLambdaVPCAccessExecutionRole"
DOMAIN_FUNCTIONS = tuple(fn for fn in FunctionId if fn is not FunctionId.SIGNER)

IDENTITY = "identity"
RESOURCE = "resource"
ALLOW = "Allow"
DENY = "Deny"

# Resource types whose property holds a policy document with ``Statement``.
RESOURCE_POLICY_PROPERTIES: dict[str, str] = {
    "AWS::KMS::Key": "KeyPolicy",
    "AWS::SecretsManager::ResourcePolicy": "ResourcePolicy",
    "AWS::S3::BucketPolicy": "PolicyDocument",
    "AWS::SQS::QueuePolicy": "PolicyDocument",
    "AWS::SNS::TopicPolicy": "PolicyDocument",
    "AWS::ApiGateway::RestApi": "Policy",
}

Templates = Mapping[str, Mapping[str, Any]]


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return list(value)
    return [value]


def _dump(value: Any) -> str:
    return json.dumps(value, sort_keys=True)


def _is_broad(element: Any) -> bool:
    """True for a resource string that still has a wildcard once a trailing ``/*`` or ``:*`` goes.

    ``function:name:*`` and ``bucket/*`` are scoped to one resource. ``*`` and ``function:dev-*``
    are not.
    """
    if not isinstance(element, str):
        return False
    trimmed = element
    for suffix in ("/*", ":*"):
        if trimmed.endswith(suffix):
            trimmed = trimmed[: -len(suffix)]
            break
    return "*" in trimmed


def implies(granted: str, action: str) -> bool:
    """True when the IAM action pattern ``granted`` (may hold ``*`` or ``?``) covers ``action``."""
    return fnmatch.fnmatchcase(action.lower(), granted.lower())


@dataclass(frozen=True)
class Statement:
    """One IAM policy statement with the place it came from."""

    stack: str
    source: str
    owner: str | None
    kind: str
    effect: str
    actions: tuple[str, ...]
    not_action: bool
    resources_text: str
    broad: bool
    principal_text: str

    @property
    def where(self) -> str:
        owner = f" of {self.owner}" if self.owner else ""
        return f"{self.stack}/{self.source}{owner}"

    def covers(self, action: str) -> bool:
        """True when an Allow of this statement would grant ``action`` (wildcards included)."""
        listed = any(implies(pattern, action) for pattern in self.actions)
        return not listed if self.not_action else listed

    def names(self, action: str) -> bool:
        """True when the statement lists ``action`` literally (no wildcard, no NotAction)."""
        return not self.not_action and any(a.lower() == action.lower() for a in self.actions)

    def touches(self, markers: Iterable[str]) -> bool:
        return self.broad or any(marker in self.resources_text for marker in markers)

    def targets_only(self, markers: Iterable[str]) -> bool:
        return not self.broad and any(marker in self.resources_text for marker in markers)


@dataclass
class Collected:
    statements: list[Statement] = field(default_factory=list)
    roles: dict[str, dict[str, Any]] = field(default_factory=dict)
    """Role name (or ``stack/logical-id`` without ``RoleName``) to its properties."""


def _statement(
    stack: str,
    source: str,
    owner: str | None,
    kind: str,
    raw: Mapping[str, Any],
    resources: list[Any] | None = None,
) -> Statement:
    not_action = "NotAction" in raw
    actions = _as_list(raw.get("NotAction" if not_action else "Action"))
    elements = resources if resources is not None else _as_list(raw.get("Resource"))
    return Statement(
        stack=stack,
        source=source,
        owner=owner,
        kind=kind,
        effect=str(raw.get("Effect", ALLOW)),
        actions=tuple(str(action) for action in actions),
        not_action=not_action,
        resources_text=_dump(elements),
        broad="NotResource" in raw or any(_is_broad(element) for element in elements),
        principal_text=_dump(raw.get("Principal", "")),
    )


def _statements(document: Any) -> list[Mapping[str, Any]]:
    if not isinstance(document, dict):
        return []
    return [item for item in _as_list(document.get("Statement")) if isinstance(item, dict)]


def _owner_names(
    logical_id: str, props: Mapping[str, Any], role_names: dict[str, str]
) -> list[str]:
    owners: list[str] = []
    for ref in _as_list(props.get("Roles")):
        if isinstance(ref, dict) and "Ref" in ref:
            owners.append(role_names.get(str(ref["Ref"]), str(ref["Ref"])))
        elif isinstance(ref, str):
            owners.append(ref)
        else:
            owners.append(_dump(ref))
    for key in ("Users", "Groups"):
        owners.extend(f"{key.lower()}:{_dump(value)}" for value in _as_list(props.get(key)))
    return owners or [f"unattached:{logical_id}"]


def collect(templates: Templates) -> Collected:
    """Every statement of every identity and resource policy in ``templates``."""
    collected = Collected()
    for stack, template in templates.items():
        resources: Mapping[str, Any] = template.get("Resources", {})
        role_names: dict[str, str] = {}
        for logical_id, resource in resources.items():
            if resource["Type"] == "AWS::IAM::Role":
                props = resource["Properties"]
                name = str(props.get("RoleName") or f"{stack}/{logical_id}")
                role_names[logical_id] = name
                collected.roles[name] = props
        for logical_id, resource in resources.items():
            _collect_resource(collected, stack, logical_id, resource, role_names)
    return collected


def _collect_resource(
    collected: Collected,
    stack: str,
    logical_id: str,
    resource: Mapping[str, Any],
    role_names: dict[str, str],
) -> None:
    resource_type = resource["Type"]
    props: Mapping[str, Any] = resource.get("Properties", {})
    out = collected.statements
    if resource_type in ("AWS::IAM::Policy", "AWS::IAM::ManagedPolicy"):
        for raw in _statements(props.get("PolicyDocument")):
            for owner in _owner_names(logical_id, props, role_names):
                out.append(_statement(stack, logical_id, owner, IDENTITY, raw))
    elif resource_type == "AWS::IAM::Role":
        owner = role_names[logical_id]
        for inline in _as_list(props.get("Policies")):
            for raw in _statements(inline.get("PolicyDocument")):
                out.append(_statement(stack, logical_id, owner, IDENTITY, raw))
    elif resource_type == "AWS::Lambda::Permission":
        raw = {"Effect": ALLOW, "Action": props.get("Action"), "Principal": props.get("Principal")}
        out.append(
            _statement(stack, logical_id, None, RESOURCE, raw, _as_list(props.get("FunctionName")))
        )
    elif resource_type in RESOURCE_POLICY_PROPERTIES:
        # The statement applies to the resource that holds the policy. A secret policy points
        # at its secret through ``SecretId``.
        target = _as_list(props.get("SecretId")) if "SecretId" in props else None
        document = props.get(RESOURCE_POLICY_PROPERTIES[resource_type])
        for raw in _statements(document):
            out.append(_statement(stack, logical_id, None, RESOURCE, raw, target))


@dataclass(frozen=True)
class Markers:
    """Strings that identify the resources the exclusive grants are about."""

    signer: tuple[str, ...]
    kyc_secrets: tuple[str, ...]
    kyc_secret_ids: tuple[str, ...]
    signing_key: tuple[str, ...]
    bus: tuple[str, ...]
    saga: tuple[str, ...]


def _find_markers(templates: Templates, stage: str) -> Markers:
    signer_name = function_name(stage, FunctionId.SIGNER)
    signer = [signer_name]
    kyc_ids: list[str] = []
    kyc = [f"{stage}/kyc/"]
    signing_key: list[str] = []
    bus: list[str] = []
    saga: list[str] = []
    for template in templates.values():
        for logical_id, resource in template.get("Resources", {}).items():
            props = resource.get("Properties", {})
            kind = resource["Type"]
            if kind == "AWS::Lambda::Function" and props.get("FunctionName") == signer_name:
                signer.append(logical_id)
            elif kind == "AWS::SecretsManager::Secret" and "/kyc/" in str(props.get("Name")):
                kyc_ids.append(logical_id)
                kyc.extend([logical_id, str(props["Name"])])
            elif kind == "AWS::KMS::Key" and props.get("KeySpec") == SIGNING_KEY_SPEC:
                signing_key.append(logical_id)
            elif kind == "AWS::Events::EventBus":
                bus.append(logical_id)
            elif kind == "AWS::StepFunctions::StateMachine":
                saga.append(logical_id)
    return Markers(
        signer=tuple(signer),
        kyc_secrets=tuple(kyc),
        kyc_secret_ids=tuple(kyc_ids),
        signing_key=tuple(signing_key),
        bus=tuple(bus),
        saga=tuple(saga),
    )


@dataclass(frozen=True)
class ExclusiveGrant:
    """One exclusive grant: who may hold it, and what its statements must target."""

    label: str
    holds: Callable[[Statement], bool]
    lambda_holders: frozenset[str]
    """Exact set of Lambda roles that must hold the grant."""
    other_holders: frozenset[str] | None
    """Non-Lambda roles allowed to hold it. ``None`` leaves non-Lambda roles unchecked."""
    scope: tuple[str, ...]
    """Markers every holding statement must target, with no wildcard resource."""


def _exclusive_grants(markers: Markers, stage: str) -> list[ExclusiveGrant]:
    def role(fn: FunctionId) -> str:
        return role_name(stage, fn)

    scheduler = resource_name(stage, "scheduler-role", NameKind.ROLE)
    nobody: frozenset[str] = frozenset()
    return [
        ExclusiveGrant(
            "kms:Sign",
            lambda s: s.covers(KMS_SIGN),
            frozenset({role(FunctionId.SIGNER)}),
            nobody,
            markers.signing_key,
        ),
        ExclusiveGrant(
            "kms:GetPublicKey",
            lambda s: s.covers(KMS_GET_PUBLIC_KEY),
            frozenset({role(FunctionId.SIGNER), role(FunctionId.STELLAR_KYC)}),
            nobody,
            markers.signing_key,
        ),
        ExclusiveGrant(
            "secretsmanager:GetSecretValue on Kyc_Secrets",
            lambda s: s.covers(GET_SECRET_VALUE) and s.touches(markers.kyc_secrets),
            frozenset({role(FunctionId.STELLAR_KYC)}),
            nobody,
            markers.kyc_secrets,
        ),
        ExclusiveGrant(
            "lambda:InvokeFunction on the Signer",
            lambda s: s.covers(LAMBDA_INVOKE) and s.touches(markers.signer),
            frozenset({role(FunctionId.STELLAR_KYC)}),
            nobody,
            markers.signer,
        ),
        ExclusiveGrant(
            "events:PutEvents",
            lambda s: s.covers(PUT_EVENTS),
            frozenset(role(fn) for fn in DOMAIN_FUNCTIONS),
            None,
            markers.bus,
        ),
        ExclusiveGrant(
            "states:StartExecution",
            lambda s: s.covers(START_EXECUTION),
            frozenset({role(FunctionId.SAGAS_PAYMENTS)}),
            frozenset({scheduler}),
            markers.saga,
        ),
    ]


def find_violations(templates: Templates, stage: str) -> list[str]:
    """Every breach of the exclusive-grant rules in ``templates``. Empty means the scan passed."""
    collected = collect(templates)
    markers = _find_markers(templates, stage)
    lambda_roles = {role_name(stage, fn): fn for fn in FunctionId}
    violations: list[str] = []

    for label, found in (
        ("Signer function", markers.signer[1:]),
        ("Kyc_Secrets", markers.kyc_secret_ids),
        ("Signing_Key", markers.signing_key),
        ("event bus", markers.bus),
        ("state machine", markers.saga),
    ):
        if not found:
            violations.append(f"the scan found no {label} in any stack")

    identity_allows = [s for s in collected.statements if s.kind == IDENTITY and s.effect == ALLOW]
    resource_statements = [s for s in collected.statements if s.kind == RESOURCE]

    violations += _lambda_role_violations(collected, identity_allows, lambda_roles)
    violations += _holder_violations(identity_allows, lambda_roles, markers, stage)
    violations += _resource_policy_violations(resource_statements, markers)
    return violations


def _lambda_role_violations(
    collected: Collected, identity_allows: list[Statement], lambda_roles: dict[str, FunctionId]
) -> list[str]:
    violations: list[str] = []
    for name in lambda_roles:
        props = collected.roles.get(name)
        if props is None:
            violations.append(f"Lambda role {name} not found in any stack")
            continue
        managed = [_dump(arn) for arn in _as_list(props.get("ManagedPolicyArns"))]
        if not managed or any(VPC_ACCESS_POLICY not in arn for arn in managed):
            violations.append(f"{name}: managed policies must be only {VPC_ACCESS_POLICY}")
    for statement in identity_allows:
        if statement.not_action:
            violations.append(f"{statement.where}: Allow with NotAction")
        if statement.owner not in lambda_roles:
            continue
        for pattern in statement.actions:
            if ("*" in pattern or "?" in pattern) and any(
                implies(pattern, action) for action in EXCLUSIVE_ACTIONS
            ):
                violations.append(
                    f"{statement.where}: wildcard action {pattern!r} implies an exclusive action"
                )
    return violations


def _holder_violations(
    identity_allows: list[Statement],
    lambda_roles: dict[str, FunctionId],
    markers: Markers,
    stage: str,
) -> list[str]:
    violations: list[str] = []
    for grant in _exclusive_grants(markers, stage):
        holding = [s for s in identity_allows if grant.holds(s)]
        owners = {s.owner for s in holding if s.owner is not None}
        in_lambda = {owner for owner in owners if owner in lambda_roles}
        others = owners - in_lambda
        if in_lambda != grant.lambda_holders:
            violations.append(
                f"{grant.label}: Lambda roles holding it {sorted(in_lambda)}, "
                f"expected {sorted(grant.lambda_holders)}"
            )
        if grant.other_holders is not None and not others <= grant.other_holders:
            extra = sorted(others - grant.other_holders)
            violations.append(f"{grant.label}: other roles may not hold it: {extra}")
        violations.extend(
            f"{s.where}: {grant.label} must target only {list(grant.scope)}, "
            f"found {s.resources_text}"
            for s in holding
            if not s.targets_only(grant.scope)
        )
    return violations


def _is_account_root(statement: Statement) -> bool:
    return ":root" in statement.principal_text


def _resource_policy_violations(statements: list[Statement], markers: Markers) -> list[str]:
    violations: list[str] = []
    guarded = (
        (KMS_SIGN, ()),
        (KMS_GET_PUBLIC_KEY, ()),
        (GET_SECRET_VALUE, markers.kyc_secrets),
        (LAMBDA_INVOKE, markers.signer),
    )
    for statement in statements:
        if statement.effect != ALLOW or _is_account_root(statement):
            continue
        for action, scope in guarded:
            if statement.covers(action) and (not scope or statement.touches(scope)):
                violations.append(f"{statement.where}: resource policy Allows {action}")
    # A resource policy may only name these actions in a Deny, whatever the principal.
    for statement in statements:
        for action in (KMS_SIGN, KMS_GET_PUBLIC_KEY, GET_SECRET_VALUE):
            if statement.names(action) and statement.effect != DENY:
                violations.append(
                    f"{statement.where}: resource policy names {action} in a non-Deny"
                )

    sign_mentions = [s for s in statements if s.names(KMS_SIGN)]
    if len(sign_mentions) != 1:
        violations.append(
            f"kms:Sign: expected one resource policy mention (the Deny), found "
            f"{[s.where for s in sign_mentions]}"
        )
    for statement in sign_mentions:
        owns_key = statement.source in markers.signing_key
        any_principal = '"*"' in statement.principal_text
        if statement.effect != DENY or not owns_key or not any_principal:
            violations.append(
                f"{statement.where}: the kms:Sign mention must be a Deny for every principal in "
                "the Signing_Key key policy"
            )

    for secret_id in markers.kyc_secret_ids:
        denied = any(
            s.effect == DENY and s.names(GET_SECRET_VALUE) and secret_id in s.resources_text
            for s in statements
        )
        if not denied:
            violations.append(f"{secret_id}: no resource policy Deny for {GET_SECRET_VALUE}")
    return violations


# --- tests -------------------------------------------------------------------------------------


def _json_templates(variant: SynthVariant) -> dict[str, dict[str, Any]]:
    return {
        stack_id: copy.deepcopy(dict(template.to_json()))
        for stack_id, template in variant.templates.items()
    }


def test_exclusive_grants_hold_in_every_stack_and_variant(variant: SynthVariant) -> None:
    templates = _json_templates(variant)
    assert templates, "the variant synthesized no stacks"
    violations = find_violations(templates, variant.cfg.stage)
    assert violations == [], f"exclusive-grant violations in variant {variant.name}: {violations}"


def test_scan_sees_the_six_lambda_roles_with_policies(variant: SynthVariant) -> None:
    """The scan must not pass because it found nothing: every function role has statements."""
    collected = collect(_json_templates(variant))
    stage = variant.cfg.stage
    owners = {s.owner for s in collected.statements if s.kind == IDENTITY}
    for fn in FunctionId:
        assert role_name(stage, fn) in owners, f"no policy statement found for the {fn} role"
    assert any(s.kind == RESOURCE and s.names(KMS_SIGN) for s in collected.statements)


def test_implies_handles_iam_wildcards() -> None:
    assert implies("*", KMS_SIGN)
    assert implies("kms:*", KMS_SIGN)
    assert implies("KMS:sign", KMS_SIGN)
    assert implies("kms:Get*", KMS_GET_PUBLIC_KEY)
    assert implies("lambda:Invoke*", LAMBDA_INVOKE)
    assert not implies("kms:Decrypt", KMS_SIGN)
    assert not implies("dynamodb:*", KMS_SIGN)


# Mutations that inject one breach into a copy of the dev templates. Each must be reported.


def _role_policy_statements(templates: dict[str, dict[str, Any]], name: str) -> list[Any]:
    for template in templates.values():
        resources = template["Resources"]
        role_ids = {
            logical_id
            for logical_id, resource in resources.items()
            if resource["Type"] == "AWS::IAM::Role"
            and resource["Properties"].get("RoleName") == name
        }
        for resource in resources.values():
            if resource["Type"] != "AWS::IAM::Policy":
                continue
            refs = resource["Properties"]["Roles"]
            if any(ref.get("Ref") in role_ids for ref in refs):
                statements = resource["Properties"]["PolicyDocument"]["Statement"]
                assert isinstance(statements, list)
                return statements
    raise AssertionError(f"no policy attached to role {name}")


def _allow(action: str | list[str], resource: Any) -> dict[str, Any]:
    return {"Effect": "Allow", "Action": action, "Resource": resource}


def _add(fn: FunctionId, action: str | list[str], resource: Any) -> Callable[..., None]:
    def mutate(templates: dict[str, dict[str, Any]], stage: str) -> None:
        statements = _role_policy_statements(templates, role_name(stage, fn))
        statements.append(_allow(action, resource))

    return mutate


def _arn(service: str, resource: str) -> str:
    return f"arn:aws:{service}:us-east-1:000000000000:{resource}"


def _remove_action(fn: FunctionId, action: str) -> Callable[..., None]:
    def mutate(templates: dict[str, dict[str, Any]], stage: str) -> None:
        statements = _role_policy_statements(templates, role_name(stage, fn))
        statements[:] = [s for s in statements if s["Action"] != action]

    return mutate


def _add_managed_policy(templates: dict[str, dict[str, Any]], stage: str) -> None:
    for template in templates.values():
        for resource in template["Resources"].values():
            props = resource["Properties"]
            if resource["Type"] == "AWS::IAM::Role" and props.get("RoleName") == role_name(
                stage, FunctionId.REPORTS
            ):
                props["ManagedPolicyArns"].append("arn:aws:iam::aws:policy/AdministratorAccess")
                return
    raise AssertionError("reports role not found")


def _add_signer_permission(templates: dict[str, dict[str, Any]], stage: str) -> None:
    for template in templates.values():
        for logical_id, resource in list(template["Resources"].items()):
            if resource["Type"] == "AWS::Lambda::Function" and resource["Properties"].get(
                "FunctionName"
            ) == function_name(stage, FunctionId.SIGNER):
                template["Resources"]["ExtraSignerPermission"] = {
                    "Type": "AWS::Lambda::Permission",
                    "Properties": {
                        "Action": LAMBDA_INVOKE,
                        "FunctionName": {"Fn::GetAtt": [logical_id, "Arn"]},
                        "Principal": "apigateway.amazonaws.com",
                    },
                }
                return
    raise AssertionError("signer function not found")


def _drop_sign_deny(templates: dict[str, dict[str, Any]], stage: str) -> None:
    for template in templates.values():
        for resource in template["Resources"].values():
            if resource["Type"] == "AWS::KMS::Key" and resource["Properties"].get("KeySpec"):
                document = resource["Properties"]["KeyPolicy"]
                document["Statement"] = [
                    s for s in document["Statement"] if s.get("Action") != KMS_SIGN
                ]


def _allow_sign_in_key_policy(templates: dict[str, dict[str, Any]], stage: str) -> None:
    for template in templates.values():
        for resource in template["Resources"].values():
            if resource["Type"] == "AWS::KMS::Key" and resource["Properties"].get("KeySpec"):
                resource["Properties"]["KeyPolicy"]["Statement"].append(
                    {
                        "Effect": "Allow",
                        "Action": KMS_SIGN,
                        "Principal": {"AWS": _arn("iam", "role/someone")},
                        "Resource": "*",
                    }
                )


def _drop_kyc_secret_deny(templates: dict[str, dict[str, Any]], stage: str) -> None:
    for template in templates.values():
        resources = template["Resources"]
        for logical_id in [
            lid
            for lid, r in resources.items()
            if r["Type"] == "AWS::SecretsManager::ResourcePolicy"
        ]:
            del resources[logical_id]


SIGNING_KEY_ARN = {"Fn::ImportValue": "dev-security:ExportsOutputFnGetAttSigningKeyEEFC2790Arn"}

MUTATIONS: dict[str, tuple[Callable[..., None], str]] = {
    "sign on another role": (_add(FunctionId.REPORTS, KMS_SIGN, SIGNING_KEY_ARN), "kms:Sign"),
    "sign on the signing key by wildcard resource": (
        _add(FunctionId.SIGNER, KMS_SIGN, "*"),
        "must target only",
    ),
    "get public key on a third role": (
        _add(FunctionId.REPORTS, KMS_GET_PUBLIC_KEY, SIGNING_KEY_ARN),
        "kms:GetPublicKey",
    ),
    "kms wildcard on a Lambda role": (
        _add(FunctionId.TRADING_OPERATIONS, "kms:*", "*"),
        "wildcard action",
    ),
    "star action on a Lambda role": (_add(FunctionId.REPORTS, "*", "*"), "wildcard action"),
    "lambda wildcard on a Lambda role": (
        _add(FunctionId.COLLATERAL_IDENTITY, "lambda:Invoke*", "*"),
        "wildcard action",
    ),
    "kyc secret read on another role": (
        _add(
            FunctionId.REPORTS,
            GET_SECRET_VALUE,
            _arn("secretsmanager", "secret:dev/kyc/provider-a/api-credentials-AbCdEf"),
        ),
        "GetSecretValue on Kyc_Secrets",
    ),
    "signer invoke on another role": (
        _add(FunctionId.REPORTS, LAMBDA_INVOKE, _arn("lambda", "function:dev-signer")),
        "lambda:InvokeFunction on the Signer",
    ),
    "signer invoke by the saga-style star": (
        _add(FunctionId.STELLAR_KYC, LAMBDA_INVOKE, "*"),
        "must target only",
    ),
    "signer invoke permission": (_add_signer_permission, "resource policy Allows"),
    "put events missing from a domain function": (
        _remove_action(FunctionId.REPORTS, PUT_EVENTS),
        "events:PutEvents",
    ),
    "put events on the signer": (
        _add(FunctionId.SIGNER, PUT_EVENTS, _arn("events", "event-bus/default")),
        "events:PutEvents",
    ),
    "start execution on another role": (
        _add(FunctionId.REPORTS, START_EXECUTION, "*"),
        "states:StartExecution",
    ),
    "start execution missing from sagas-payments": (
        _remove_action(FunctionId.SAGAS_PAYMENTS, START_EXECUTION),
        "states:StartExecution",
    ),
    "managed policy on a Lambda role": (_add_managed_policy, "managed policies"),
    "signing key Deny removed": (_drop_sign_deny, "kms:Sign"),
    "signing key Allow for another principal": (_allow_sign_in_key_policy, "kms:Sign"),
    "kyc secret Deny removed": (_drop_kyc_secret_deny, "no resource policy Deny"),
}


@pytest.mark.parametrize("name", list(MUTATIONS))
def test_scan_reports_an_injected_violation(dev_variant: SynthVariant, name: str) -> None:
    templates = _json_templates(dev_variant)
    stage = dev_variant.cfg.stage
    assert find_violations(templates, stage) == []
    mutate, expected = MUTATIONS[name]
    mutate(templates, stage)
    violations = find_violations(templates, stage)
    assert any(expected in violation for violation in violations), (
        f"{name}: expected a violation containing {expected!r}, got {violations}"
    )
