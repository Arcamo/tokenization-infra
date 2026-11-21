"""cdk-nag registration and the single home of every acknowledgement.

cdk-nag v3 plugs into CDK's native validations: ``register_nag`` adds the
``AwsSolutionsChecks`` plugin to the app, and any finding that is not
acknowledged interrupts synthesis. Acknowledgements are recorded with
``Validations.of(construct).acknowledge(Acknowledgment(...))``.

Every acknowledgement in the project goes through ``acknowledge`` and is listed
in ``apply_acknowledgements``. Stack modules never acknowledge anything
themselves. v3 requires one acknowledgement per finding id, for example
``AwsSolutions-IAM5[Resource::*]`` and ``AwsSolutions-IAM5[Action::s3:*]`` are
separate; prefix matching on ``AwsSolutions-IAM5`` is not supported.
"""

from collections.abc import Mapping

import aws_cdk as cdk
from cdk_nag import AwsSolutionsChecks
from constructs import IConstruct

from tokenization_infra.core.config import REPO_ROOT, StageConfig
from tokenization_infra.core.saga import referenced_functions
from tokenization_infra.stacks.api_compute import ApiComputeStack
from tokenization_infra.stacks.bootstrap import BootstrapStack
from tokenization_infra.stacks.data import DataStack
from tokenization_infra.stacks.identity import IdentityStack
from tokenization_infra.stacks.messaging import MessagingStack
from tokenization_infra.stacks.network import NetworkStack
from tokenization_infra.stacks.security import SecurityStack

MIN_REASON_LENGTH = 20
"""Shortest accepted acknowledgement reason, in characters."""


def register_nag(app: cdk.App) -> None:
    """Run the AWS Solutions rule pack on every stack of ``app``.

    Findings that are not acknowledged make synthesis fail.
    """
    cdk.Validations.of(app).add_plugins(AwsSolutionsChecks(app, verbose=True))


def acknowledge(construct: IConstruct, rule_id: str, reason: str) -> None:
    """Acknowledge ``rule_id`` (a cdk-nag finding id) on ``construct`` and its children.

    ``reason`` must be at least ``MIN_REASON_LENGTH`` characters after trimming,
    so every acknowledgement explains itself.
    """
    if len(reason.strip()) < MIN_REASON_LENGTH:
        raise ValueError(
            f"Acknowledgement of {rule_id} on {construct.node.path} needs a reason of at "
            f"least {MIN_REASON_LENGTH} characters, got {len(reason.strip())}."
        )
    cdk.Validations.of(construct).acknowledge(cdk.Acknowledgment(id=rule_id, reason=reason))


def apply_acknowledgements(stacks: Mapping[str, cdk.Stack], cfg: StageConfig) -> None:
    """Apply every cdk-nag acknowledgement of the app. This is the only place that does.

    ``stacks`` maps a short stack name (``security``, ``network``, ``data``,
    ``messaging``, ``identity``, ``api-compute``, ``observability``) to the built
    stack. Stacks that are not built yet are simply absent, so callers pass
    whatever exists; the ``StackSet`` of ``app_builder`` can supply this mapping
    through a ``dict`` of its stack fields. ``cfg`` lets an acknowledgement
    depend on the stage configuration (for example Multi-AZ or the removal
    policy), so an acknowledgement exists only while its finding does.

    Each stack task adds its acknowledgements here, with a reason of at least
    ``MIN_REASON_LENGTH`` characters.
    """
    security = stacks.get("security")
    if isinstance(security, SecurityStack):
        _acknowledge_security(security)

    network = stacks.get("network")
    if isinstance(network, NetworkStack):
        _acknowledge_network(network)

    data = stacks.get("data")
    if isinstance(data, DataStack):
        _acknowledge_data(data, cfg)

    messaging = stacks.get("messaging")
    if isinstance(messaging, MessagingStack):
        _acknowledge_messaging(messaging, cfg)
    identity = stacks.get("identity")
    if isinstance(identity, IdentityStack):
        _acknowledge_identity(identity)

    api_compute = stacks.get("api-compute")
    if isinstance(api_compute, ApiComputeStack):
        _acknowledge_api_compute(api_compute, cfg)


def apply_bootstrap_acknowledgements(stack: BootstrapStack, cfg: StageConfig) -> None:
    """Apply the cdk-nag acknowledgements of the Bootstrap_Stack (built by ``bootstrap_app.py``).

    The stack is not part of ``apply_acknowledgements`` because ``app.py`` never builds it (D7).
    Both roles hold only ``sts:AssumeRole`` on fixed role ARNs, so no finding is expected and
    nothing is acknowledged. Add an acknowledgement here, with a reason, if a finding appears.
    """
    del stack, cfg


def _acknowledge_security(stack: SecurityStack) -> None:
    for secrets in stack.kyc_secrets.values():
        for secret in secrets.values():
            acknowledge(
                secret,
                "AwsSolutions-SMG4",
                "Third-party KYC provider credentials are rotated by operators, so the secret "
                "has no rotation schedule.",
            )


def _acknowledge_network(stack: NetworkStack) -> None:
    # Not a cdk-nag finding: CloudFormation Validate warns about literal AZ names on every subnet.
    acknowledge(
        stack.vpc,
        "CloudFormation-Validate::W3010",
        "Availability Zones are listed in Stage_Config on purpose (design D8), so synthesis "
        "needs no context lookup and no AWS credentials.",
    )


def _acknowledge_data(stack: DataStack, cfg: StageConfig) -> None:
    # The master secret is the attachment returned by the instance; the Secret is its child.
    master = stack.database.node.find_child("Secret")
    for secret in (master, stack.app_user_secret):
        acknowledge(
            secret,
            "AwsSolutions-SMG4",
            "The Db_Proxy reads this database secret and the password is managed by the "
            "operators; no rotation schedule is configured in this MVP.",
        )
    # Only while the finding exists: a Multi-AZ instance or deletion protection clears it.
    if not cfg.database.multi_az:
        acknowledge(
            stack.database,
            "AwsSolutions-RDS3",
            "Single-AZ keeps the MVP cost low; Stage_Config.database.multi_az turns Multi-AZ on "
            "for stages that need failover.",
        )
    if cfg.removal_policy == "DESTROY":
        acknowledge(
            stack.database,
            "AwsSolutions-RDS10",
            "Deletion protection is off only in stages whose removal policy is DESTROY, so the "
            "stack can be torn down; RETAIN stages enable it.",
        )
    acknowledge(
        stack.database,
        "AwsSolutions-RDS11",
        "The instance keeps the default PostgreSQL port 5432 (it is only reachable from the "
        "proxy security group in isolated subnets); the proxy and security groups assume it.",
    )
    acknowledge(
        stack.access_log_bucket,
        "AwsSolutions-S1",
        "This is the server access log bucket, so it does not log its own access.",
    )


def _acknowledge_messaging(stack: MessagingStack, cfg: StageConfig) -> None:
    saga_policy = stack.saga.node.find_child("Role").node.find_child("DefaultPolicy")
    acknowledge(
        saga_policy,
        "AwsSolutions-IAM5[Resource::*]",
        "Step Functions log delivery and X-Ray write actions do not support resource-level "
        "permissions, so the CDK-generated statements for them use a wildcard resource.",
    )
    # 9.2: the invoke grant covers each referenced function and its versions (arn and arn:*).
    text = (REPO_ROOT / cfg.saga.definition_file).read_text(encoding="utf-8")
    for fn in sorted(referenced_functions(text)):
        function_arn = stack.resolve(stack.function_arn(fn))
        acknowledge(
            saga_policy,
            f"AwsSolutions-IAM5[Resource::{function_arn}:*]",
            "The saga may invoke published versions and aliases of this one function; the "
            "wildcard covers only the version suffix of a function named in the definition.",
        )
    for rule_name, dlq in stack.rule_dlqs.items():
        acknowledge(
            dlq,
            "AwsSolutions-SQS3",
            f"This queue is the dead-letter queue of the {rule_name} rule target; a DLQ "
            "behind a rule target has no further redrive queue.",
        )


def _acknowledge_identity(stack: IdentityStack) -> None:
    user_pool = stack.user_pool.node.default_child
    assert user_pool is not None
    acknowledge(
        user_pool,
        "AwsSolutions-COG2",
        "MFA is optional with TOTP only (Requirement 4.2) so that investors can sign up "
        "without friction in the MVP; users and groups can opt in to MFA.",
    )
    acknowledge(
        user_pool,
        "AwsSolutions-COG8",
        "The Plus feature plan (threat protection) is deferred to a later spec and costs "
        "extra; the MVP stays on the default feature plan.",
    )


NEWEST_LAMBDA_RUNTIME = "dotnet10"
"""The newest .NET runtime Stage_Config accepts; AwsSolutions-L1 stays quiet for it."""


def _acknowledge_api_compute(stack: ApiComputeStack, cfg: StageConfig) -> None:
    for fn, domain_function in stack.functions.items():
        fn_cfg = cfg.functions[fn]
        policy = domain_function.role.node.find_child("DefaultPolicy")

        acknowledge(
            domain_function.role,
            "AwsSolutions-IAM4[Policy::arn:<AWS::Partition>:iam::aws:policy/service-role/"
            "AWSLambdaVPCAccessExecutionRole]",
            "AWSLambdaVPCAccessExecutionRole is the AWS managed policy for Lambda functions in a "
            "VPC: it holds the network interface actions, which have no resource-level "
            "permissions, and CloudWatch Logs writes.",
        )
        acknowledge(
            policy,
            "AwsSolutions-IAM5[Resource::*]",
            "The X-Ray write actions (PutTraceSegments, PutTelemetryRecords) do not support "
            "resource-level permissions; CDK adds them for active tracing.",
        )
        if "documents-bucket" in fn_cfg.data_access:
            # The partition resolves to a literal when the stack has an explicit region.
            objects_arn = stack.resolve(
                f"arn:{stack.partition}:s3:::{stack.documents_bucket_name}/*"
            )
            acknowledge(
                policy,
                f"AwsSolutions-IAM5[Resource::{objects_arn}]",
                "The function reads and writes objects in the documents bucket; object keys are "
                "created by the application, so the grant covers the objects of that one bucket.",
            )
        if cfg.lambda_runtime != NEWEST_LAMBDA_RUNTIME:
            acknowledge(
                domain_function.function,
                "AwsSolutions-L1",
                f"{cfg.lambda_runtime} is the supported managed .NET runtime pinned by "
                "Stage_Config.lambda_runtime; the application repository builds for it, so it "
                "moves to the newest runtime only through a config change.",
            )
        if fn_cfg.artifact.version is None:
            acknowledge(
                domain_function.node.find_child("ArtifactBucket"),
                "Annotation::@aws-cdk/aws-lambda:codeFromBucketObjectVersionNotSpecified",
                "The artifact reference has no object version, so the function updates when its "
                "stage config changes the key; set artifact.version to track in-place uploads.",
            )

    _acknowledge_api(stack)


def _acknowledge_api(stack: ApiComputeStack) -> None:
    acknowledge(
        stack.api.node.find_child("CloudWatchRole"),
        "AwsSolutions-IAM4[Policy::arn:<AWS::Partition>:iam::aws:policy/service-role/"
        "AmazonAPIGatewayPushToCloudWatchLogs]",
        "AmazonAPIGatewayPushToCloudWatchLogs is the AWS managed policy API Gateway needs to "
        "write logs; it backs the account-wide CloudWatch role (design O9).",
    )
    acknowledge(
        stack.api.deployment_stage,
        "AwsSolutions-APIG3",
        "AWS WAF on the Api is deferred to a later spec (spec 3); throttling and Cognito scopes "
        "protect the Api in the MVP.",
    )
    acknowledge(
        stack.kyc_webhook_method,
        "AwsSolutions-APIG4",
        "The KYC provider webhook cannot hold a Cognito token, so POST /webhooks/kyc/{provider} "
        "has no authorizer; the function verifies the provider webhook signature.",
    )
    acknowledge(
        stack.kyc_webhook_method,
        "AwsSolutions-COG4",
        "The KYC provider webhook is called by a third party without a Cognito user pool "
        "token, so this one method does not use the Cognito authorizer.",
    )
