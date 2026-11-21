"""SQS work queue paired with its dead-letter queue."""

from aws_cdk import Duration
from aws_cdk import aws_kms as kms
from aws_cdk import aws_sqs as sqs
from constructs import Construct

from tokenization_infra.core.naming import NameKind, resource_name

DLQ_RETENTION_DAYS = 14
_MAX_VISIBILITY_TIMEOUT_SECONDS = 43200  # SQS limit: 12 hours


class QueueWithDlq(Construct):
    """A queue ``{stage}-{name}`` and its dead-letter queue ``{stage}-{name}-dlq``.

    Both queues are encrypted with ``data_key`` (SSE-KMS) and deny requests that do not use TLS.
    The DLQ keeps messages for 14 days. The work queue moves a message to the DLQ after
    ``max_receive_count`` receives and hides received messages for ``visibility_timeout_seconds``.

    ``data_key`` is an ``IKey``. Stacks other than ``security`` pass a key imported with
    ``kms.Key.from_key_arn`` (design D2), so key grants land in the consumer's identity policy.
    """

    queue: sqs.Queue
    dlq: sqs.Queue

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        stage: str,
        name: str,
        data_key: kms.IKey,
        max_receive_count: int,
        visibility_timeout_seconds: int,
    ) -> None:
        super().__init__(scope, construct_id)

        if max_receive_count < 1:
            raise ValueError("max_receive_count must be at least 1")
        if not 0 <= visibility_timeout_seconds <= _MAX_VISIBILITY_TIMEOUT_SECONDS:
            raise ValueError(
                "visibility_timeout_seconds must be between 0 and "
                f"{_MAX_VISIBILITY_TIMEOUT_SECONDS}"
            )

        self.dlq = sqs.Queue(
            self,
            "Dlq",
            queue_name=resource_name(stage, f"{name}-dlq", NameKind.QUEUE),
            retention_period=Duration.days(DLQ_RETENTION_DAYS),
            encryption=sqs.QueueEncryption.KMS,
            encryption_master_key=data_key,
            enforce_ssl=True,
        )
        self.queue = sqs.Queue(
            self,
            "Queue",
            queue_name=resource_name(stage, name, NameKind.QUEUE),
            visibility_timeout=Duration.seconds(visibility_timeout_seconds),
            dead_letter_queue=sqs.DeadLetterQueue(
                queue=self.dlq,
                max_receive_count=max_receive_count,
            ),
            encryption=sqs.QueueEncryption.KMS,
            encryption_master_key=data_key,
            enforce_ssl=True,
        )
