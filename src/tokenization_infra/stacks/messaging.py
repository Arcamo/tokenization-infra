"""MessagingStack: Event_Bus, Work_Queues, Saga_State_Machine, rules and Scheduler (Req. 9)."""

from collections.abc import Mapping
from typing import Any

import aws_cdk as cdk
from aws_cdk import aws_events as events
from aws_cdk import aws_events_targets as targets
from aws_cdk import aws_iam as iam
from aws_cdk import aws_kms as kms
from aws_cdk import aws_logs as logs
from aws_cdk import aws_scheduler as scheduler
from aws_cdk import aws_sqs as sqs
from aws_cdk import aws_stepfunctions as sfn
from constructs import Construct

from tokenization_infra.constructs.extension_parameters import publish_extension_parameters
from tokenization_infra.constructs.log_retention import retention_days
from tokenization_infra.constructs.queue_with_dlq import QueueWithDlq
from tokenization_infra.core.config import (
    REPO_ROOT,
    DomainFunctionId,
    FunctionId,
    LambdaTarget,
    QueueTarget,
    RuleConfig,
    StageConfig,
    StateMachineTarget,
)
from tokenization_infra.core.naming import NameKind, function_name, resource_name
from tokenization_infra.core.parameters import ExtensionKey
from tokenization_infra.core.saga import referenced_functions, substitute
from tokenization_infra.stacks.security import SecurityStack

SAGA_NAME = "tokenization-saga"
SAGA_LOG_BASE = f"states/{SAGA_NAME}"
SCHEDULER_PRINCIPAL = "scheduler.amazonaws.com"

# Stage_Config writes event patterns the way EventBridge does ("detail-type"); the L2 takes
# the snake_case field names.
_PATTERN_FIELDS = frozenset(
    {"version", "id", "detail_type", "source", "account", "time", "region", "resources", "detail"}
)


def event_pattern_from_config(raw: Mapping[str, object]) -> events.EventPattern:
    """Turn a Stage_Config ``event_pattern`` mapping into an ``events.EventPattern``."""
    fields: dict[str, Any] = {}
    for key, value in raw.items():
        field = key.replace("-", "_")
        if field not in _PATTERN_FIELDS:
            raise ValueError(f'unsupported event pattern field "{key}"')
        fields[field] = value
    return events.EventPattern(**fields)


class MessagingStack(cdk.Stack):
    """Owns the bus, the queues, the saga, the rules and the Scheduler.

    It depends only on the security stack (D2: the Data_Key is imported by ARN). Domain_Lambda
    ARNs are built from their deterministic names (D1), so this stack never references the
    api-compute stack.
    """

    data_key: kms.IKey
    bus: events.EventBus
    archive: events.Archive
    queues: dict[str, QueueWithDlq]
    rule_dlqs: dict[str, sqs.Queue]
    rules: dict[str, events.Rule]
    saga_log_group: logs.LogGroup
    saga: sfn.StateMachine
    schedule_group: scheduler.ScheduleGroup
    scheduler_role: iam.Role

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        cfg: StageConfig,
        security: SecurityStack,
        **kwargs: Any,
    ):
        super().__init__(scope, construct_id, **kwargs)
        self._cfg = cfg
        self._removal_policy = cdk.RemovalPolicy[cfg.removal_policy]

        # D2: import the key by ARN so grants land in identity policies, not the key policy.
        self.data_key = kms.Key.from_key_arn(self, "DataKey", security.data_key_arn)

        self._create_bus(cfg)
        self._create_queues(cfg)
        self._create_saga(cfg)
        self._create_rules(cfg)
        self._create_scheduler(cfg)

        publish_extension_parameters(
            self,
            cfg,
            {
                ExtensionKey.EVENT_BUS_NAME: self.bus.event_bus_name,
                ExtensionKey.EVENT_BUS_ARN: self.bus.event_bus_arn,
            },
        )

        if cfg.removal_policy == "RETAIN":
            self.termination_protection = True

    @property
    def all_dlqs(self) -> list[sqs.Queue]:
        """Work-queue DLQs followed by rule-target DLQs (the observability alarms cover all)."""
        return [q.dlq for q in self.queues.values()] + list(self.rule_dlqs.values())

    def function_arn(self, fn: FunctionId | DomainFunctionId) -> str:
        """Deterministic Domain_Lambda ARN (D1); the function lives in the api-compute stack."""
        return self.format_arn(
            service="lambda",
            resource="function",
            resource_name=function_name(self._cfg.stage, FunctionId(fn)),
            arn_format=cdk.ArnFormat.COLON_RESOURCE_NAME,
        )

    def _create_bus(self, cfg: StageConfig) -> None:
        stage = cfg.stage
        bus_name = resource_name(stage, "tokenization", NameKind.GENERIC)
        self.bus = events.EventBus(self, "Bus", event_bus_name=bus_name)
        # 9.1: keep every event of this account for replay, for the configured retention.
        self.archive = self.bus.archive(
            "Archive",
            archive_name=f"{bus_name}-archive",
            description=f"{stage}: archive of the {bus_name} bus",
            event_pattern=events.EventPattern(account=[self.account]),
            retention=cdk.Duration.days(cfg.events.archive_retention_days),
        )
        # The L2 renders ``KmsKeyIdentifier: ""`` when no key is passed. CloudFormation documents
        # an omitted property as "use an AWS owned key" and says nothing about an empty string,
        # so remove the property instead of relying on how the service treats "".
        cfn_archive = self.archive.node.default_child
        assert isinstance(cfn_archive, events.CfnArchive)
        cfn_archive.add_property_deletion_override("KmsKeyIdentifier")

    def _create_queues(self, cfg: StageConfig) -> None:
        self.queues = {
            queue.name: QueueWithDlq(
                self,
                f"Queue-{queue.name}",
                stage=cfg.stage,
                name=queue.name,
                data_key=self.data_key,
                max_receive_count=queue.max_receive_count,
                visibility_timeout_seconds=queue.visibility_timeout_seconds,
            )
            for queue in cfg.queues
        }

    def _create_saga(self, cfg: StageConfig) -> None:
        stage = cfg.stage
        text = (REPO_ROOT / cfg.saga.definition_file).read_text(encoding="utf-8")
        referenced = sorted(referenced_functions(text))
        arns: dict[DomainFunctionId, str] = {fn: self.function_arn(fn) for fn in referenced}
        definition = substitute(text, arns)

        self.saga_log_group = logs.LogGroup(
            self,
            "SagaLogGroup",
            log_group_name=resource_name(stage, SAGA_LOG_BASE, NameKind.LOG_GROUP),
            retention=retention_days(cfg.observability.log_retention_days),
            removal_policy=self._removal_policy,
        )
        self.saga = sfn.StateMachine(
            self,
            "Saga",
            state_machine_name=resource_name(stage, SAGA_NAME, NameKind.GENERIC),
            state_machine_type=sfn.StateMachineType.STANDARD,
            definition_body=sfn.DefinitionBody.from_string(definition),
            tracing_enabled=True,
            logs=sfn.LogOptions(
                destination=self.saga_log_group,
                level=sfn.LogLevel.ALL,
                include_execution_data=True,
            ),
            removal_policy=self._removal_policy,
        )

        # 9.2: the role may invoke only the functions the definition names, and their versions.
        # A string definition adds no grants, so the statement is written here.
        if referenced:
            function_arns = [arn for fn in referenced for arn in (arns[fn], f"{arns[fn]}:*")]
            self.saga.add_to_role_policy(
                iam.PolicyStatement(
                    actions=["lambda:InvokeFunction"],
                    resources=function_arns,
                )
            )

    def _target_dlq(self, rule: RuleConfig) -> sqs.Queue:
        """Create ``{stage}-{rule}-target-dlq``, encrypted with the Data_Key (9.5)."""
        return sqs.Queue(
            self,
            f"TargetDlq-{rule.name}",
            queue_name=resource_name(self._cfg.stage, f"{rule.name}-target-dlq", NameKind.QUEUE),
            retention_period=cdk.Duration.days(14),
            encryption=sqs.QueueEncryption.KMS,
            encryption_master_key=self.data_key,
            enforce_ssl=True,
        )

    def _create_rules(self, cfg: StageConfig) -> None:
        self.rule_dlqs = {}
        self.rules = {}
        for rule_cfg in cfg.rules:
            dlq = self._target_dlq(rule_cfg)
            self.rule_dlqs[rule_cfg.name] = dlq

            target: events.IRuleTarget
            if isinstance(rule_cfg.target, QueueTarget):
                target = targets.SqsQueue(
                    self.queues[rule_cfg.target.queue].queue, dead_letter_queue=dlq
                )
            else:
                target = targets.SfnStateMachine(self.saga, dead_letter_queue=dlq)

            self.rules[rule_cfg.name] = events.Rule(
                self,
                f"Rule-{rule_cfg.name}",
                rule_name=resource_name(cfg.stage, rule_cfg.name, NameKind.GENERIC),
                event_bus=self.bus,
                event_pattern=event_pattern_from_config(rule_cfg.event_pattern),
                targets=[target],
            )

    def _create_scheduler(self, cfg: StageConfig) -> None:
        stage = cfg.stage
        self.schedule_group = scheduler.ScheduleGroup(
            self,
            "ScheduleGroup",
            schedule_group_name=resource_name(stage, "schedules", NameKind.GENERIC),
            removal_policy=self._removal_policy,
        )
        self.scheduler_role = iam.Role(
            self,
            "SchedulerRole",
            role_name=resource_name(stage, "scheduler-role", NameKind.ROLE),
            assumed_by=iam.ServicePrincipal(
                SCHEDULER_PRINCIPAL,
                conditions={"StringEquals": {"aws:SourceAccount": self.account}},
            ),
            description=f"{stage}: EventBridge Scheduler execution role",
        )

        # 9.9: only the configured targets, nothing else.
        has_queue_target = False
        for target in cfg.scheduler.targets:
            if isinstance(target, LambdaTarget):
                self.scheduler_role.add_to_policy(
                    iam.PolicyStatement(
                        actions=["lambda:InvokeFunction"],
                        resources=[self.function_arn(target.function)],
                    )
                )
            elif isinstance(target, StateMachineTarget):
                self.saga.grant_start_execution(self.scheduler_role)
            else:
                # The queue is encrypted with the Data_Key, so sending also needs key use. The
                # statements are written out (no wildcard actions) instead of using
                # grant_send_messages, which adds kms:GenerateDataKey* and kms:ReEncrypt*.
                self.scheduler_role.add_to_policy(
                    iam.PolicyStatement(
                        actions=["sqs:SendMessage"],
                        resources=[self.queues[target.queue].queue.queue_arn],
                    )
                )
                has_queue_target = True
        if has_queue_target:
            self.scheduler_role.add_to_policy(
                iam.PolicyStatement(
                    actions=["kms:GenerateDataKey", "kms:Decrypt"],
                    resources=[self.data_key.key_arn],
                )
            )
