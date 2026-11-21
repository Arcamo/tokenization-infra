"""ObservabilityStack assertion tests (Requirements 10.1 to 10.5, 11.7, 12.1).

Assertions are on the properties the requirements name, against the templates synthesized once
per session for each variant (dev, dev with ``removal_policy: RETAIN``, two NAT gateways). The
stage config of ``dev.yaml`` has one queue, one rule and one alarm email. A fourth, "extended"
variant adds a second queue, a second rule and a second email, so the checks that count DLQs and
subscriptions see more than one of each.

The observability stack imports the Data_Key and the DLQ names from other stacks, so those values
are ``Fn::ImportValue``. ``_resolve_import`` follows them back to the exporting stack's output.
"""

import copy
from typing import Any

import pytest
from aws_cdk.assertions import Template

from synthesis import VARIANT_OVERRIDES, SynthVariant, load_raw_dev, new_app
from tokenization_infra.app_builder import build_app
from tokenization_infra.core.config import parse_stage_config
from tokenization_infra.core.parameters import ExtensionKey, extension_parameter_names

EXTENDED = "extended"
EXTRA_EMAIL = "oncall@example.com"
_EXTENDED_CACHE: dict[str, SynthVariant] = {}


def _build_extended() -> SynthVariant:
    """dev plus a second queue (and its DLQ), a second rule (and its DLQ) and a second email."""
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
        raw["observability"]["alarm_emails"].append(EXTRA_EMAIL)
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
def observability_variant(
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


def _observability(variant: SynthVariant) -> Template:
    return variant.template("observability")


def _topic(template: Template) -> tuple[str, dict[str, Any]]:
    topics = template.find_resources("AWS::SNS::Topic")
    assert len(topics) == 1, f"expected one topic, found {list(topics)}"
    logical_id, topic = next(iter(topics.items()))
    return logical_id, dict(topic)


def _expected_dlq_logical_ids(variant: SynthVariant) -> set[str]:
    """Logical ids (in the messaging stack) of every DLQ: work-queue DLQs and rule-target DLQs."""
    template = variant.template("messaging")
    expected: set[str] = set()
    for queue in template.find_resources("AWS::SQS::Queue").values():
        redrive = queue["Properties"].get("RedrivePolicy")
        if redrive is not None:
            expected.add(redrive["deadLetterTargetArn"]["Fn::GetAtt"][0])
    for rule in template.find_resources("AWS::Events::Rule").values():
        for target in rule["Properties"]["Targets"]:
            expected.add(target["DeadLetterConfig"]["Arn"]["Fn::GetAtt"][0])
    return expected


# --- Alert_Topic (10.2, 10.3, 12.1) ---


def test_alert_topic_is_encrypted_with_the_data_key(observability_variant: SynthVariant) -> None:
    """10.2, 12.1: topic ``{stage}-alarms`` whose KmsMasterKeyId resolves to the Data_Key."""
    template = _observability(observability_variant)
    cfg = observability_variant.cfg
    template.resource_count_is("AWS::SNS::Topic", 1)
    _, topic = _topic(template)
    properties = topic["Properties"]
    assert properties["TopicName"] == f"{cfg.stage}-alarms"
    key = _resolve_import(observability_variant, properties["KmsMasterKeyId"])
    assert key == _data_key_arn(observability_variant)


def test_alert_topic_denies_publish_without_tls(observability_variant: SynthVariant) -> None:
    """12.1: a topic policy that denies ``sns:Publish`` when ``aws:SecureTransport`` is false."""
    template = _observability(observability_variant)
    topic_id, _ = _topic(template)
    policies = template.find_resources("AWS::SNS::TopicPolicy")
    assert len(policies) == 1, f"expected one topic policy, found {list(policies)}"
    properties = next(iter(policies.values()))["Properties"]
    assert properties["Topics"] == [{"Ref": topic_id}]
    deny_tls = [
        statement
        for statement in properties["PolicyDocument"]["Statement"]
        if statement["Effect"] == "Deny"
        and statement["Action"] == "sns:Publish"
        and statement["Principal"] == "*"
        and statement["Resource"] == {"Ref": topic_id}
        and statement["Condition"] == {"Bool": {"aws:SecureTransport": "false"}}
    ]
    assert len(deny_tls) == 1, properties["PolicyDocument"]


def test_one_email_subscription_per_configured_address(
    observability_variant: SynthVariant,
) -> None:
    """10.3: each ``observability.alarm_emails`` entry is an email subscription to the topic."""
    template = _observability(observability_variant)
    emails = observability_variant.cfg.observability.alarm_emails
    topic_id, _ = _topic(template)
    subscriptions = template.find_resources("AWS::SNS::Subscription")
    assert len(subscriptions) == len(emails)
    endpoints: list[str] = []
    for logical_id, subscription in subscriptions.items():
        properties = subscription["Properties"]
        assert properties["Protocol"] == "email", logical_id
        assert properties["TopicArn"] == {"Ref": topic_id}, logical_id
        endpoints.append(properties["Endpoint"])
    assert sorted(endpoints) == sorted(emails)


def test_extended_variant_has_more_than_one_subscription() -> None:
    """10.3: the extended variant really exercises several addresses."""
    emails = _build_extended().cfg.observability.alarm_emails
    assert len(emails) == 2 and EXTRA_EMAIL in emails


# --- DLQ alarms (10.4, 10.5) ---


def test_one_alarm_per_dlq_with_the_required_settings(
    observability_variant: SynthVariant,
) -> None:
    """10.4: one alarm per DLQ (work-queue and rule-target) on the visible-message count."""
    template = _observability(observability_variant)
    expected = _expected_dlq_logical_ids(observability_variant)
    cfg = observability_variant.cfg
    assert len(expected) == len(cfg.queues) + len(cfg.rules)

    alarms = template.find_resources("AWS::CloudWatch::Alarm")
    assert len(alarms) == len(expected)

    watched: list[str] = []
    for logical_id, alarm in alarms.items():
        properties = alarm["Properties"]
        assert properties["Namespace"] == "AWS/SQS", logical_id
        assert properties["MetricName"] == "ApproximateNumberOfMessagesVisible", logical_id
        assert properties["Statistic"] == "Maximum", logical_id
        assert properties["Period"] == 60, logical_id
        assert properties["EvaluationPeriods"] == 1, logical_id
        assert properties["Threshold"] == 0, logical_id
        assert properties["ComparisonOperator"] == "GreaterThanThreshold", logical_id
        assert properties["TreatMissingData"] == "notBreaching", logical_id
        [dimension] = properties["Dimensions"]
        assert dimension["Name"] == "QueueName", logical_id
        queue_name = _resolve_import(observability_variant, dimension["Value"])
        assert set(queue_name) == {"Fn::GetAtt"} and queue_name["Fn::GetAtt"][1] == "QueueName"
        watched.append(queue_name["Fn::GetAtt"][0])

    assert sorted(watched) == sorted(expected), "alarms must watch each DLQ exactly once"


def test_every_alarm_notifies_the_topic_on_alarm_and_on_ok(
    observability_variant: SynthVariant,
) -> None:
    """10.5: AlarmActions and OKActions are exactly the Alert_Topic."""
    template = _observability(observability_variant)
    topic_id, _ = _topic(template)
    alarms = template.find_resources("AWS::CloudWatch::Alarm")
    assert alarms
    for logical_id, alarm in alarms.items():
        properties = alarm["Properties"]
        assert properties["AlarmActions"] == [{"Ref": topic_id}], logical_id
        assert properties["OKActions"] == [{"Ref": topic_id}], logical_id
        assert "InsufficientDataActions" not in properties, logical_id


# --- Extension_Parameter (11.7) ---


def test_alert_topic_arn_extension_parameter(observability_variant: SynthVariant) -> None:
    """11.7: ``observability/alert-topic-arn`` holds the topic ARN, and it is the only parameter."""
    template = _observability(observability_variant)
    cfg = observability_variant.cfg
    topic_id, _ = _topic(template)
    name = extension_parameter_names(cfg.project, cfg.stage)[ExtensionKey.ALERT_TOPIC_ARN]
    assert name == f"/{cfg.project}/{cfg.stage}/observability/alert-topic-arn"
    template.resource_count_is("AWS::SSM::Parameter", 1)
    template.has_resource_properties(
        "AWS::SSM::Parameter",
        {"Name": name, "Type": "String", "Value": {"Ref": topic_id}},
    )


# --- Log group retention across the app (10.1) ---


def test_every_log_group_in_every_stack_has_the_configured_retention(
    observability_variant: SynthVariant,
) -> None:
    """10.1: no ``AWS::Logs::LogGroup`` in any stack keeps its logs forever."""
    retention = observability_variant.cfg.observability.log_retention_days
    seen: dict[str, list[str]] = {}
    for stack_id, template in observability_variant.templates.items():
        for logical_id, group in template.find_resources("AWS::Logs::LogGroup").items():
            assert group["Properties"].get("RetentionInDays") == retention, (
                f"{stack_id}/{logical_id}"
            )
            seen.setdefault(stack_id, []).append(str(group["Properties"].get("LogGroupName")))
    assert seen, "no log group found in any stack"


def test_log_groups_cover_the_stacks_that_write_logs(dev_variant: SynthVariant) -> None:
    """10.1: the check above sees the VPC, API (access and execution), Lambda and saga groups."""
    cfg = dev_variant.cfg
    names: set[str] = set()
    for template in dev_variant.templates.values():
        for group in template.find_resources("AWS::Logs::LogGroup").values():
            name = group["Properties"].get("LogGroupName")
            names.add(name if isinstance(name, str) else "<intrinsic>")
    assert f"/{cfg.stage}/vpc/flow-logs" in names
    assert f"/{cfg.stage}/apigateway/access" in names
    assert f"/{cfg.stage}/states/tokenization-saga" in names
    assert f"/aws/lambda/{cfg.stage}-{next(iter(cfg.functions))}" in names
    assert "<intrinsic>" in names, "the API Gateway execution log group (name joins the API id)"
