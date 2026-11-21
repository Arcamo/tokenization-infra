"""Lambda function with its own role, log group and capability-based IAM permissions."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

import aws_cdk as cdk
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_iam as iam
from aws_cdk import aws_kms as kms
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from aws_cdk import aws_rds as rds
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_sqs as sqs
from constructs import Construct

from tokenization_infra.constructs.log_retention import retention_days
from tokenization_infra.core.config import FunctionId, StageConfig
from tokenization_infra.core.naming import function_name, role_name
from tokenization_infra.core.permissions import Capability, CapabilityKind

LAMBDA_PRINCIPAL = "lambda.amazonaws.com"
VPC_ACCESS_POLICY = "service-role/AWSLambdaVPCAccessExecutionRole"

_RUNTIMES: dict[str, lambda_.Runtime] = {
    "dotnet8": lambda_.Runtime.DOTNET_8,
    "dotnet10": lambda_.Runtime("dotnet10", lambda_.RuntimeFamily.DOTNET_CORE),
}
_ARCHITECTURES: dict[str, lambda_.Architecture] = {
    "arm64": lambda_.Architecture.ARM_64,
    "x86_64": lambda_.Architecture.X86_64,
}

# Names used by ``data:<resource>`` capabilities (Stage_Config ``data_access``).
TABLE_RESOURCES = frozenset({"idempotency-table", "saga-table"})
BUCKET_RESOURCES = frozenset({"documents-bucket"})

_TABLE_ACTIONS = [
    "dynamodb:GetItem",
    "dynamodb:BatchGetItem",
    "dynamodb:Query",
    "dynamodb:PutItem",
    "dynamodb:UpdateItem",
    "dynamodb:DeleteItem",
    "dynamodb:BatchWriteItem",
    "dynamodb:ConditionCheckItem",
]
_BUCKET_OBJECT_ACTIONS = ["s3:GetObject", "s3:GetObjectVersion", "s3:PutObject"]
_BUCKET_ACTIONS = ["s3:ListBucket"]
_QUEUE_CONSUME_ACTIONS = ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes"]

# Data_Key actions per service that reads or writes data encrypted with it. Reading from SQS and
# Secrets Manager only needs Decrypt. DynamoDB and S3 also write, so they get GenerateDataKey.
_READ = ("kms:Decrypt",)
_READ_WRITE = ("kms:Decrypt", "kms:GenerateDataKey")
_DATA_SERVICE: dict[str, tuple[str, ...]] = {
    "dynamodb": _READ_WRITE,
    "s3": _READ_WRITE,
    "sqs": _READ,
    "secretsmanager": _READ,
}


@dataclass(frozen=True)
class DomainFunctionResources:
    """The handles ``DomainFunction.apply_profile`` needs to turn capabilities into statements.

    Cross-stack values are strings or imported handles, so applying a profile never edits a
    resource policy in another stack (design D1, D2). ``saga_state_machine_arn`` and
    ``signer_function_arn`` are built from the deterministic names, because the state machine
    lives in the messaging stack and the signer in this stack's own build order.

    ``tables`` is keyed by the ``data_access`` names ``idempotency-table`` and ``saga-table``,
    ``buckets`` by ``documents-bucket``, and ``queues`` by the Stage_Config queue name.
    """

    event_bus_arn: str
    saga_state_machine_arn: str
    data_key: kms.IKey
    signing_key_arn: str
    signer_function_arn: str
    kyc_secret_arns: Sequence[str]
    tables: Mapping[str, dynamodb.ITable]
    buckets: Mapping[str, s3.IBucket]
    queues: Mapping[str, sqs.IQueue]
    proxy: rds.IDatabaseProxy
    app_user: str


class DomainFunction(Construct):
    """One Lambda function: execution role, log group with retention, and the function itself.

    The role is trusted by ``lambda.amazonaws.com`` and holds the AWSLambdaVPCAccessExecutionRole
    managed policy. Everything else comes from ``apply_profile``. The log group is created
    explicitly (no ``log_retention``, which adds a custom resource).
    """

    role: iam.Role
    log_group: logs.LogGroup
    function: lambda_.Function
    function_name: str

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        cfg: StageConfig,
        fn: FunctionId,
        vpc: ec2.IVpc,
        subnets: ec2.SubnetSelection,
        security_group: ec2.ISecurityGroup,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(scope, construct_id)
        fn_cfg = cfg.functions[fn]
        removal_policy = cdk.RemovalPolicy[cfg.removal_policy]
        self.function_name = function_name(cfg.stage, fn)

        self.role = iam.Role(
            self,
            "Role",
            role_name=role_name(cfg.stage, fn),
            assumed_by=iam.ServicePrincipal(LAMBDA_PRINCIPAL),
            description=f"{cfg.stage}: execution role of the {fn.value} function",
            managed_policies=[iam.ManagedPolicy.from_aws_managed_policy_name(VPC_ACCESS_POLICY)],
        )

        self.log_group = logs.LogGroup(
            self,
            "LogGroup",
            log_group_name=f"/aws/lambda/{self.function_name}",
            retention=retention_days(cfg.observability.log_retention_days),
            removal_policy=removal_policy,
        )

        artifact = fn_cfg.artifact
        code_bucket = s3.Bucket.from_bucket_name(self, "ArtifactBucket", artifact.bucket)
        self.function = lambda_.Function(
            self,
            "Function",
            function_name=self.function_name,
            runtime=_RUNTIMES[cfg.lambda_runtime],
            architecture=_ARCHITECTURES[cfg.lambda_architecture],
            handler=fn_cfg.handler,
            code=lambda_.Code.from_bucket_v2(
                code_bucket, artifact.key, object_version=artifact.version
            ),
            memory_size=fn_cfg.memory_mb,
            timeout=cdk.Duration.seconds(fn_cfg.timeout_seconds),
            reserved_concurrent_executions=fn_cfg.reserved_concurrency,
            role=self.role,
            vpc=vpc,
            vpc_subnets=subnets,
            security_groups=[security_group],
            tracing=lambda_.Tracing.ACTIVE,
            log_group=self.log_group,
            environment=dict(environment) if environment else None,
        )

    def apply_profile(
        self, capabilities: frozenset[Capability], resources: DomainFunctionResources
    ) -> None:
        """Turn ``capabilities`` into IAM statements on the execution role.

        This is the only place that writes permissions for a domain function. Statements are added
        to the role (``add_to_policy``) instead of using L2 ``grant_*`` methods, which can edit
        resource policies in other stacks and create dependency cycles. Capabilities are applied
        in sorted order so the template is stable.
        """
        # Services (dynamodb, s3, sqs, secretsmanager) that read data encrypted with the Data_Key.
        key_services: set[str] = set()
        for capability in sorted(capabilities, key=str):
            self._apply_capability(capability, resources, key_services)
        self._grant_data_key(resources.data_key, key_services)

    def _add(self, actions: Sequence[str], resources: Sequence[str]) -> None:
        self.role.add_to_policy(
            iam.PolicyStatement(actions=list(actions), resources=list(resources))
        )

    def _apply_capability(
        self, capability: Capability, res: DomainFunctionResources, key_services: set[str]
    ) -> None:
        kind = capability.kind
        resource = capability.resource
        if kind is CapabilityKind.BUS_PUT:
            self._add(["events:PutEvents"], [res.event_bus_arn])
        elif kind is CapabilityKind.SAGA_START:
            self._add(["states:StartExecution"], [res.saga_state_machine_arn])
        elif kind is CapabilityKind.SIGNER_INVOKE:
            self._add(["lambda:InvokeFunction"], [res.signer_function_arn])
        elif kind is CapabilityKind.SIGNING_KEY_GET_PUBLIC_KEY:
            self._add(["kms:GetPublicKey"], [res.signing_key_arn])
        elif kind is CapabilityKind.SIGNING_KEY_SIGN:
            self._add(["kms:Sign"], [res.signing_key_arn])
        elif kind is CapabilityKind.KYC_SECRETS_READ:
            self._add(["secretsmanager:GetSecretValue"], list(res.kyc_secret_arns))
            key_services.add("secretsmanager")
        elif kind is CapabilityKind.DB_CONNECT:
            res.proxy.grant_connect(self.role, res.app_user)
        elif kind is CapabilityKind.DATA:
            assert resource is not None
            self._apply_data(resource, res, key_services)
        elif kind is CapabilityKind.QUEUE_CONSUME:
            assert resource is not None
            queue = self._lookup(res.queues, resource, "queue")
            self._add(_QUEUE_CONSUME_ACTIONS, [queue.queue_arn])
            key_services.add("sqs")
        else:  # pragma: no cover - every CapabilityKind is handled above
            raise ValueError(f"unsupported capability: {capability}")

    def _apply_data(
        self, resource: str, res: DomainFunctionResources, key_services: set[str]
    ) -> None:
        if resource in TABLE_RESOURCES:
            table = self._lookup(res.tables, resource, "table")
            self._add(_TABLE_ACTIONS, [table.table_arn])
            key_services.add("dynamodb")
        elif resource in BUCKET_RESOURCES:
            bucket = self._lookup(res.buckets, resource, "bucket")
            self._add(_BUCKET_ACTIONS, [bucket.bucket_arn])
            self._add(_BUCKET_OBJECT_ACTIONS, [bucket.arn_for_objects("*")])
            key_services.add("s3")
        else:
            raise ValueError(f'unknown data resource "{resource}"')

    @staticmethod
    def _lookup[T](handles: Mapping[str, T], name: str, what: str) -> T:
        try:
            return handles[name]
        except KeyError:
            raise ValueError(f'no {what} handle named "{name}" in the function resources') from None

    def _grant_data_key(self, data_key: kms.IKey, services: set[str]) -> None:
        """Allow Data_Key use only when the request comes through one of ``services``."""
        by_actions: dict[tuple[str, ...], list[str]] = {}
        for service in sorted(services):
            by_actions.setdefault(_DATA_SERVICE[service], []).append(service)
        region = cdk.Stack.of(self).region
        for actions, names in sorted(by_actions.items()):
            self.role.add_to_policy(
                iam.PolicyStatement(
                    actions=list(actions),
                    resources=[data_key.key_arn],
                    conditions={
                        "StringEquals": {
                            "kms:ViaService": [f"{name}.{region}.amazonaws.com" for name in names]
                        }
                    },
                )
            )
