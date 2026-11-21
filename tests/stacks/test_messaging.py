"""MessagingStack assertion tests (Requirements 9.1 to 9.3, 9.5 to 9.7, 9.9, 11.6, 12.1).

Assertions are on the properties the requirements name, against the templates synthesized once
per session for each variant (dev, dev with ``removal_policy: RETAIN``, two NAT gateways). The
stage config of ``dev.yaml`` has one queue, one rule that targets it and only the state machine
as Scheduler target. A fourth, "extended" variant adds a second queue, a rule that targets the
saga and Scheduler targets for a Lambda, a queue and the saga, so every branch of the stack runs.

The messaging stack imports the Data_Key by ARN, so the key reference is ``Fn::ImportValue``.
``_resolve_import`` follows it back to the exporting stack's output.
"""

import copy
import json
from collections.abc import Iterator, Mapping
from typing import Any

import pytest
from aws_cdk.assertions import Template

from synthesis import VARIANT_OVERRIDES, SynthVariant, load_raw_dev, new_app
from tokenization_infra.app_builder import build_app
from tokenization_infra.core.config import (
    REPO_ROOT,
    LambdaTarget,
    QueueTarget,
    RuleConfig,
    StateMachineTarget,
    parse_stage_config,
)
from tokenization_infra.core.parameters import ExtensionKey, extension_parameter_names
from tokenization_infra.core.saga import referenced_functions

DLQ_RETENTION_SECONDS = 14 * 24 * 60 * 60
EXTENDED = "extended"
_EXTENDED_CACHE: dict[str, SynthVariant] = {}


def _build_extended() -> SynthVariant:
    """dev plus a second queue, a rule that targets the saga, and all three Scheduler targets."""
    if EXTENDED not in _EXTENDED_CACHE:
        raw = copy.deepcopy(load_raw_dev())
        raw["queues"].append(
            {
                "name": "notifications",
                "consumer": "reports",
                "max_receive_count": 3,
                "visibility_timeout_seconds": 60,
            }
        )
        raw["rules"].append(
            {
                "name": "saga-requested",
                "event_pattern": {
                    "source": ["tokenization.sagas"],
                    "detail-type": ["SagaRequested"],
                },
                "target": {"type": "state-machine"},
            }
        )
        raw["scheduler"] = {
            "targets": [
                {"type": "lambda", "function": "reports"},
                {"type": "queue", "queue": "notifications"},
                {"type": "state-machine"},
            ]
        }
        cfg = parse_stage_config(raw, stage="dev")
        app = new_app()
        stack_set = build_app(app, cfg)
        templates = {
            stack.node.id: Template.from_stack(stack) for stack in stack_set.by_name().values()
        }
        _EXTENDED_CACHE[EXTENDED] = SynthVariant(
            name=EXTENDED, cfg=cfg, app=app, stack_set=stack_set, templates=templates
        )
    return _EXTENDED_CACHE[EXTENDED]


@pytest.fixture(scope="session", params=[*VARIANT_OVERRIDES, EXTENDED])
def messaging_variant(
    request: pytest.FixtureRequest,
    dev_variant: SynthVariant,
    retain_variant: SynthVariant,
    two_nat_variant: SynthVariant,
) -> SynthVariant:
    """Each synthesized variant in turn (dev, dev-retain, two NAT), then the extended one."""
    named = {
        "dev": dev_variant,
        "dev-retain": retain_variant,
        "dev-two-nat-eu-west-1": two_nat_variant,
    }
    name = str(request.param)
    return _build_extended() if name == EXTENDED else named[name]


# --- helpers ---


def _resolve_import(variant: SynthVariant, reference: Any) -> Any:
    """Value of the exporting stack's output behind ``{"Fn::ImportValue": name}``."""
    assert isinstance(reference, dict) and set(reference) == {"Fn::ImportValue"}, reference
    name = reference["Fn::ImportValue"]
    for template in variant.templates.values():
        for output in template.to_json().get("Outputs", {}).values():
            if output.get("Export", {}).get("Name") == name:
                return output["Value"]
    raise AssertionError(f"no stack exports {name}")


def _data_key_arn(variant: SynthVariant) -> dict[str, Any]:
    """``Fn::GetAtt`` of the Data_Key (the only rotating key) in the security stack."""
    keys = variant.template("security").find_resources(
        "AWS::KMS::Key", {"Properties": {"EnableKeyRotation": True}}
    )
    assert len(keys) == 1, f"expected one rotating key, found {list(keys)}"
    return {"Fn::GetAtt": [next(iter(keys)), "Arn"]}


def _messaging(variant: SynthVariant) -> Template:
    return variant.template("messaging")


def _only(resources: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
    assert len(resources) == 1, f"expected one resource, found {list(resources)}"
    logical_id, resource = next(iter(resources.items()))
    return logical_id, dict(resource)


def _queue(template: Template, queue_name: str) -> tuple[str, dict[str, Any]]:
    return _only(
        template.find_resources("AWS::SQS::Queue", {"Properties": {"QueueName": queue_name}})
    )


def _queue_by_logical_id(template: Template, logical_id: str) -> dict[str, Any]:
    queues = template.find_resources("AWS::SQS::Queue")
    assert logical_id in queues, f"{logical_id} is not a queue of the messaging stack"
    return dict(queues[logical_id])


def _saga(template: Template) -> tuple[str, dict[str, Any]]:
    return _only(template.find_resources("AWS::StepFunctions::StateMachine"))


def _role_id(template: Template, role_name: str | None = None) -> str:
    if role_name is not None:
        roles = template.find_resources("AWS::IAM::Role", {"Properties": {"RoleName": role_name}})
    else:
        roles = {
            logical_id: role
            for logical_id, role in template.find_resources("AWS::IAM::Role").items()
            if role["Properties"]["AssumeRolePolicyDocument"]["Statement"][0]["Principal"][
                "Service"
            ]
            == "states.amazonaws.com"
        }
    logical_id, _ = _only(roles)
    return logical_id


def _statements_of_role(template: Template, role_logical_id: str) -> list[dict[str, Any]]:
    """Every IAM statement of the policies attached to the role."""
    statements: list[dict[str, Any]] = []
    for policy in template.find_resources("AWS::IAM::Policy").values():
        if {"Ref": role_logical_id} in policy["Properties"]["Roles"]:
            statements.extend(policy["Properties"]["PolicyDocument"]["Statement"])
    return statements


def _listed(value: Any) -> list[Any]:
    return value if isinstance(value, list) else [value]


def _allowed_pairs(variant: SynthVariant, statements: list[dict[str, Any]]) -> set[tuple[str, str]]:
    """(action, canonical resource) of every Allow statement; imports are resolved."""
    pairs: set[tuple[str, str]] = set()
    for statement in statements:
        assert statement["Effect"] == "Allow", statement
        for action in _listed(statement["Action"]):
            for resource in _listed(statement["Resource"]):
                if isinstance(resource, dict) and "Fn::ImportValue" in resource:
                    resource = _resolve_import(variant, resource)
                pairs.add((action, json.dumps(resource, sort_keys=True)))
    return pairs


def _lambda_arn(variant: SynthVariant, function: str) -> str:
    cfg = variant.cfg
    return f"arn:aws:lambda:{cfg.region}:{cfg.account}:function:{cfg.stage}-{function}"


def _referenced_functions(variant: SynthVariant) -> list[str]:
    text = (REPO_ROOT / variant.cfg.saga.definition_file).read_text(encoding="utf-8")
    return sorted(str(fn) for fn in referenced_functions(text))


def _rules(variant: SynthVariant) -> Iterator[tuple[RuleConfig, dict[str, Any]]]:
    template = _messaging(variant)
    for rule in variant.cfg.rules:
        name = f"{variant.cfg.stage}-{rule.name}"
        _, resource = _only(
            template.find_resources("AWS::Events::Rule", {"Properties": {"Name": name}})
        )
        yield rule, resource


# --- Saga_State_Machine (9.1, 9.2, 9.3) ---


def test_saga_is_a_standard_traced_machine_logging_all_events(
    messaging_variant: SynthVariant,
) -> None:
    """9.1: STANDARD, X-Ray tracing, Level ALL into a log group with the configured retention."""
    template = _messaging(messaging_variant)
    cfg = messaging_variant.cfg
    template.resource_count_is("AWS::StepFunctions::StateMachine", 1)
    _, saga = _saga(template)
    properties = saga["Properties"]
    assert properties["StateMachineType"] == "STANDARD"
    assert properties["StateMachineName"] == f"{cfg.stage}-tokenization-saga"
    assert properties["TracingConfiguration"] == {"Enabled": True}
    logging = properties["LoggingConfiguration"]
    assert logging["Level"] == "ALL"

    group_id, group = _only(
        template.find_resources(
            "AWS::Logs::LogGroup",
            {"Properties": {"LogGroupName": f"/{cfg.stage}/states/tokenization-saga"}},
        )
    )
    assert group["Properties"]["RetentionInDays"] == cfg.observability.log_retention_days
    [destination] = logging["Destinations"]
    assert destination["CloudWatchLogsLogGroup"]["LogGroupArn"] == {"Fn::GetAtt": [group_id, "Arn"]}


def test_saga_definition_has_the_deterministic_function_arns(
    messaging_variant: SynthVariant,
) -> None:
    """9.2: the definition file is loaded and each placeholder becomes a deterministic ARN."""
    template = _messaging(messaging_variant)
    _, saga = _saga(template)
    definition = saga["Properties"]["DefinitionString"]
    assert isinstance(definition, str), "the definition must resolve to a plain string"
    assert "${" not in definition, "a placeholder was not substituted"
    document = json.loads(definition)
    assert document["StartAt"] in document["States"]

    referenced = _referenced_functions(messaging_variant)
    assert referenced, "the saga definition references no Domain_Lambda"
    for function in referenced:
        assert f'"{_lambda_arn(messaging_variant, function)}"' in definition
    for function in messaging_variant.cfg.functions:
        if str(function) not in referenced:
            assert f":function:{messaging_variant.cfg.stage}-{function}" not in definition


def test_saga_role_invokes_only_the_referenced_functions(
    messaging_variant: SynthVariant,
) -> None:
    """9.3: lambda:InvokeFunction on the referenced functions (and versions), no other Lambda."""
    template = _messaging(messaging_variant)
    statements = _statements_of_role(template, _role_id(template))
    lambda_statements = [
        statement
        for statement in statements
        if any(action.startswith("lambda:") for action in _listed(statement["Action"]))
    ]
    assert lambda_statements, "the saga role has no Lambda statement"
    for statement in lambda_statements:
        assert statement["Effect"] == "Allow"
        assert _listed(statement["Action"]) == ["lambda:InvokeFunction"]

    expected = {
        arn
        for function in _referenced_functions(messaging_variant)
        for arn in (
            _lambda_arn(messaging_variant, function),
            f"{_lambda_arn(messaging_variant, function)}:*",
        )
    }
    granted = [resource for s in lambda_statements for resource in _listed(s["Resource"])]
    assert sorted(granted) == sorted(expected)


# --- Event_Bus and archive (9.5, 11.6) ---


def test_bus_archive_keeps_events_for_the_configured_retention(
    messaging_variant: SynthVariant,
) -> None:
    """9.5: an archive of the bus with ``events.archive_retention_days``."""
    template = _messaging(messaging_variant)
    cfg = messaging_variant.cfg
    template.resource_count_is("AWS::Events::EventBus", 1)
    bus_id, bus = _only(template.find_resources("AWS::Events::EventBus"))
    assert bus["Properties"]["Name"] == f"{cfg.stage}-tokenization"

    template.resource_count_is("AWS::Events::Archive", 1)
    _, archive = _only(template.find_resources("AWS::Events::Archive"))
    assert archive["Properties"]["RetentionDays"] == cfg.events.archive_retention_days
    assert archive["Properties"]["SourceArn"] == {"Fn::GetAtt": [bus_id, "Arn"]}
    # The L2 emits KmsKeyIdentifier "" by default; the stack removes it (AWS owned key).
    assert "KmsKeyIdentifier" not in archive["Properties"]


def test_event_bus_extension_parameters(messaging_variant: SynthVariant) -> None:
    """11.6: the bus name and ARN are published, and nothing else is, by this stack."""
    template = _messaging(messaging_variant)
    cfg = messaging_variant.cfg
    names = extension_parameter_names(cfg.project, cfg.stage)
    bus_id, _ = _only(template.find_resources("AWS::Events::EventBus"))

    template.resource_count_is("AWS::SSM::Parameter", 2)
    template.has_resource_properties(
        "AWS::SSM::Parameter",
        {"Name": names[ExtensionKey.EVENT_BUS_NAME], "Type": "String", "Value": {"Ref": bus_id}},
    )
    template.has_resource_properties(
        "AWS::SSM::Parameter",
        {
            "Name": names[ExtensionKey.EVENT_BUS_ARN],
            "Type": "String",
            "Value": {"Fn::GetAtt": [bus_id, "Arn"]},
        },
    )
    assert names[ExtensionKey.EVENT_BUS_NAME] == f"/{cfg.project}/{cfg.stage}/events/bus-name"
    assert names[ExtensionKey.EVENT_BUS_ARN] == f"/{cfg.project}/{cfg.stage}/events/bus-arn"


# --- Work_Queues and DLQs (9.6, 12.1) ---


def test_each_configured_queue_has_a_dlq_and_max_receive_count(
    messaging_variant: SynthVariant,
) -> None:
    """9.6: queue and DLQ pair, encrypted with the Data_Key, ``maxReceiveCount`` from config."""
    template = _messaging(messaging_variant)
    cfg = messaging_variant.cfg
    data_key = _data_key_arn(messaging_variant)
    assert cfg.queues

    for queue_cfg in cfg.queues:
        dlq_id, dlq = _queue(template, f"{cfg.stage}-{queue_cfg.name}-dlq")
        _, queue = _queue(template, f"{cfg.stage}-{queue_cfg.name}")
        properties = queue["Properties"]
        assert properties["RedrivePolicy"] == {
            "deadLetterTargetArn": {"Fn::GetAtt": [dlq_id, "Arn"]},
            "maxReceiveCount": queue_cfg.max_receive_count,
        }
        assert properties["VisibilityTimeout"] == queue_cfg.visibility_timeout_seconds
        assert dlq["Properties"]["MessageRetentionPeriod"] == DLQ_RETENTION_SECONDS
        assert "RedrivePolicy" not in dlq["Properties"]
        for resource in (queue, dlq):
            key = resource["Properties"]["KmsMasterKeyId"]
            assert _resolve_import(messaging_variant, key) == data_key


def test_every_queue_denies_requests_without_tls(messaging_variant: SynthVariant) -> None:
    """12.1: all queues (work, DLQ and rule-target DLQ) are encrypted and deny non-TLS access."""
    template = _messaging(messaging_variant)
    cfg = messaging_variant.cfg
    queues = template.find_resources("AWS::SQS::Queue")
    assert len(queues) == 2 * len(cfg.queues) + len(cfg.rules)

    denied: set[str] = set()
    for policy in template.find_resources("AWS::SQS::QueuePolicy").values():
        for statement in policy["Properties"]["PolicyDocument"]["Statement"]:
            if (
                statement["Effect"] == "Deny"
                and statement["Action"] == "sqs:*"
                and statement["Condition"] == {"Bool": {"aws:SecureTransport": "false"}}
            ):
                denied.update(queue["Ref"] for queue in policy["Properties"]["Queues"])
    for logical_id, queue in queues.items():
        assert "KmsMasterKeyId" in queue["Properties"], logical_id
        assert logical_id in denied, f"{logical_id} has no TLS-deny policy"


# --- EventBridge rules (9.7) ---


def test_rules_route_bus_events_to_their_configured_targets(
    messaging_variant: SynthVariant,
) -> None:
    """9.7: rules on the custom bus, with the configured pattern and target."""
    template = _messaging(messaging_variant)
    cfg = messaging_variant.cfg
    bus_id, _ = _only(template.find_resources("AWS::Events::EventBus"))
    saga_id, _ = _saga(template)
    template.resource_count_is("AWS::Events::Rule", len(cfg.rules))

    for rule, resource in _rules(messaging_variant):
        properties = resource["Properties"]
        assert properties["EventBusName"] == {"Ref": bus_id}
        assert properties["EventPattern"] == rule.event_pattern
        [target] = properties["Targets"]
        if isinstance(rule.target, QueueTarget):
            queue_id, _ = _queue(template, f"{cfg.stage}-{rule.target.queue}")
            assert target["Arn"] == {"Fn::GetAtt": [queue_id, "Arn"]}
        else:
            assert isinstance(rule.target, StateMachineTarget)
            assert target["Arn"] == {"Ref": saga_id}
            assert "RoleArn" in target


def test_every_rule_target_has_its_own_dlq(messaging_variant: SynthVariant) -> None:
    """9.7: one DLQ per rule target, encrypted with the Data_Key and kept for 14 days."""
    template = _messaging(messaging_variant)
    cfg = messaging_variant.cfg
    data_key = _data_key_arn(messaging_variant)
    dlq_arns: list[str] = []

    for rule, resource in _rules(messaging_variant):
        [target] = resource["Properties"]["Targets"]
        dead_letter = target["DeadLetterConfig"]["Arn"]
        assert set(dead_letter) == {"Fn::GetAtt"}
        dlq_id, attribute = dead_letter["Fn::GetAtt"]
        assert attribute == "Arn"
        dlq = _queue_by_logical_id(template, dlq_id)["Properties"]
        assert dlq["QueueName"] == f"{cfg.stage}-{rule.name}-target-dlq"
        assert dlq["MessageRetentionPeriod"] == DLQ_RETENTION_SECONDS
        assert _resolve_import(messaging_variant, dlq["KmsMasterKeyId"]) == data_key
        dlq_arns.append(dlq_id)

    assert len(dlq_arns) == len(cfg.rules)
    assert len(set(dlq_arns)) == len(dlq_arns), "rule targets share a DLQ"


# --- Scheduler (9.9) ---


def test_schedule_group_and_role(messaging_variant: SynthVariant) -> None:
    """9.9: a schedule group and a role only EventBridge Scheduler (this account) can assume."""
    template = _messaging(messaging_variant)
    cfg = messaging_variant.cfg
    template.resource_count_is("AWS::Scheduler::ScheduleGroup", 1)
    template.has_resource_properties(
        "AWS::Scheduler::ScheduleGroup", {"Name": f"{cfg.stage}-schedules"}
    )
    role_id = _role_id(template, f"{cfg.stage}-scheduler-role")
    assert role_id
    template.has_resource_properties(
        "AWS::IAM::Role",
        {
            "RoleName": f"{cfg.stage}-scheduler-role",
            "AssumeRolePolicyDocument": {
                "Statement": [
                    {
                        "Action": "sts:AssumeRole",
                        "Effect": "Allow",
                        "Principal": {"Service": "scheduler.amazonaws.com"},
                        "Condition": {"StringEquals": {"aws:SourceAccount": cfg.account}},
                    }
                ],
                "Version": "2012-10-17",
            },
        },
    )


def test_scheduler_role_policy_is_limited_to_the_listed_targets(
    messaging_variant: SynthVariant,
) -> None:
    """9.9: the role may use exactly the configured targets, nothing else."""
    template = _messaging(messaging_variant)
    cfg = messaging_variant.cfg
    saga_id, _ = _saga(template)
    role_id = _role_id(template, f"{cfg.stage}-scheduler-role")

    expected: set[tuple[str, str]] = set()
    has_queue_target = False
    for target in cfg.scheduler.targets:
        if isinstance(target, LambdaTarget):
            expected.add(
                (
                    "lambda:InvokeFunction",
                    json.dumps(_lambda_arn(messaging_variant, target.function)),
                )
            )
        elif isinstance(target, StateMachineTarget):
            expected.add(("states:StartExecution", json.dumps({"Ref": saga_id}, sort_keys=True)))
        else:
            queue_id, _ = _queue(template, f"{cfg.stage}-{target.queue}")
            expected.add(
                (
                    "sqs:SendMessage",
                    json.dumps({"Fn::GetAtt": [queue_id, "Arn"]}, sort_keys=True),
                )
            )
            has_queue_target = True
    if has_queue_target:
        key = json.dumps(_data_key_arn(messaging_variant), sort_keys=True)
        expected |= {("kms:GenerateDataKey", key), ("kms:Decrypt", key)}

    statements = _statements_of_role(template, role_id)
    assert _allowed_pairs(messaging_variant, statements) == expected
    for statement in statements:
        assert "*" not in json.dumps(statement["Action"]), "wildcard action"
        assert statement["Resource"] != "*"


def test_scheduler_role_has_no_statements_without_targets(dev_variant: SynthVariant) -> None:
    """9.9: dev lists only the state machine, so the role may only start the saga."""
    assert [type(t) for t in dev_variant.cfg.scheduler.targets] == [StateMachineTarget]
    template = _messaging(dev_variant)
    role_id = _role_id(template, f"{dev_variant.cfg.stage}-scheduler-role")
    saga_id, _ = _saga(template)
    assert _statements_of_role(template, role_id) == [
        {"Action": "states:StartExecution", "Effect": "Allow", "Resource": {"Ref": saga_id}}
    ]
