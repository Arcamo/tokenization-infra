"""ObservabilityStack: Alert_Topic and one alarm per dead-letter queue (Req. 10.2 to 10.5, 11.7)."""

from typing import Any

import aws_cdk as cdk
from aws_cdk import aws_cloudwatch as cloudwatch
from aws_cdk import aws_cloudwatch_actions as cw_actions
from aws_cdk import aws_kms as kms
from aws_cdk import aws_sns as sns
from aws_cdk import aws_sns_subscriptions as subscriptions
from constructs import Construct

from tokenization_infra.constructs.extension_parameters import publish_extension_parameters
from tokenization_infra.core.config import StageConfig
from tokenization_infra.core.naming import NameKind, resource_name
from tokenization_infra.core.parameters import ExtensionKey
from tokenization_infra.stacks.messaging import MessagingStack
from tokenization_infra.stacks.security import SecurityStack


class ObservabilityStack(cdk.Stack):
    """Owns the Alert_Topic and the DLQ alarms.

    It depends on the security stack (the Data_Key ARN, imported per D2) and on the messaging
    stack (the DLQs the alarms watch). Nothing depends on it, so the graph stays one-way.
    """

    data_key: kms.IKey
    alert_topic: sns.Topic
    dlq_alarms: list[cloudwatch.Alarm]

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        cfg: StageConfig,
        security: SecurityStack,
        messaging: MessagingStack,
        **kwargs: Any,
    ):
        super().__init__(scope, construct_id, **kwargs)

        # D2: import the key by ARN. The Data_Key policy already lets CloudWatch publish to a
        # topic encrypted with it (cloudwatch.amazonaws.com with aws:SourceAccount).
        self.data_key = kms.Key.from_key_arn(self, "DataKey", security.data_key_arn)

        self.alert_topic = sns.Topic(
            self,
            "AlertTopic",
            topic_name=resource_name(cfg.stage, "alarms", NameKind.GENERIC),
            master_key=self.data_key,
            enforce_ssl=True,
        )
        for email in cfg.observability.alarm_emails:
            self.alert_topic.add_subscription(subscriptions.EmailSubscription(email))

        self.dlq_alarms = self._create_dlq_alarms(messaging)

        publish_extension_parameters(
            self,
            cfg,
            {ExtensionKey.ALERT_TOPIC_ARN: self.alert_topic.topic_arn},
        )

        if cfg.removal_policy == "RETAIN":
            self.termination_protection = True

    def _create_dlq_alarms(self, messaging: MessagingStack) -> list[cloudwatch.Alarm]:
        action = cw_actions.SnsAction(self.alert_topic)
        alarms: list[cloudwatch.Alarm] = []
        for dlq in messaging.all_dlqs:
            # The construct path inside the messaging stack is unique and stable per queue.
            queue_path = dlq.node.path.removeprefix(f"{messaging.node.path}/").replace("/", "-")
            alarm = cloudwatch.Alarm(
                self,
                f"DlqAlarm-{queue_path}",
                metric=dlq.metric_approximate_number_of_messages_visible(
                    period=cdk.Duration.minutes(1),
                    statistic="Maximum",
                ),
                threshold=0,
                evaluation_periods=1,
                comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
                treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
                alarm_description="Messages are waiting in a dead-letter queue.",
            )
            alarm.add_alarm_action(action)
            alarm.add_ok_action(action)
            alarms.append(alarm)
        return alarms
