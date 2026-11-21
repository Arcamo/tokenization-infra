"""DataStack: RDS PostgreSQL, Db_Proxy, DynamoDB tables and S3 buckets (Requirements 7 and 8)."""

from typing import Any

import aws_cdk as cdk
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_kms as kms
from aws_cdk import aws_logs as logs
from aws_cdk import aws_rds as rds
from aws_cdk import aws_secretsmanager as secretsmanager
from constructs import Construct

from tokenization_infra.constructs.log_retention import retention_days
from tokenization_infra.constructs.secure_bucket import SecureBucket
from tokenization_infra.core.config import StageConfig
from tokenization_infra.core.naming import NameKind, resource_name
from tokenization_infra.stacks.network import NetworkStack
from tokenization_infra.stacks.security import SecurityStack

MASTER_USERNAME = "postgres_admin"
DOCUMENTS_LOG_PREFIX = "documents/"
TTL_ATTRIBUTE = "expiresAt"


class DataStack(cdk.Stack):
    """Owns the database, the proxy, the DynamoDB tables and the buckets.

    It consumes the Data_Key by ARN (D2), so grants land in the consumers' identity policies and
    this stack never edits the key policy in the security stack.
    """

    data_key: kms.IKey
    database: rds.DatabaseInstance
    master_secret: secretsmanager.ISecret
    app_user: str
    app_user_secret: rds.DatabaseSecret
    proxy: rds.DatabaseProxy
    database_log_group: logs.LogGroup
    idempotency_table: dynamodb.TableV2
    saga_table: dynamodb.TableV2
    access_log_bucket: SecureBucket
    documents_bucket: SecureBucket
    documents_bucket_name: str
    """Plain-string name of the documents bucket; consumers build its ARN without an export."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        cfg: StageConfig,
        network: NetworkStack,
        security: SecurityStack,
        **kwargs: Any,
    ):
        super().__init__(scope, construct_id, **kwargs)
        removal_policy = cdk.RemovalPolicy[cfg.removal_policy]

        # D2: import the key by ARN so grants are written into the consumer's identity policy.
        self.data_key = kms.Key.from_key_arn(self, "DataKey", security.data_key_arn)

        self._create_database(cfg, network, removal_policy)
        self._create_proxy(cfg, network, removal_policy)
        self._create_tables(cfg, removal_policy)
        self._create_buckets(cfg, removal_policy)

        if cfg.removal_policy == "RETAIN":
            self.termination_protection = True

    def _immutable_sg(self, construct_id: str, group: ec2.ISecurityGroup) -> ec2.ISecurityGroup:
        """Reference a network security group that the L2 constructs here cannot modify.

        The network stack already holds every rule (lambda to proxy, proxy to db, all on 5432).
        If the proxy and instance constructs added their own rules to the real groups, the rules
        would reference the instance's port attribute from the network stack and create a
        dependency cycle between the two stacks.
        """
        return ec2.SecurityGroup.from_security_group_id(
            self, construct_id, group.security_group_id, mutable=False
        )

    def _create_database(
        self, cfg: StageConfig, network: NetworkStack, removal_policy: cdk.RemovalPolicy
    ) -> None:
        stage = cfg.stage
        db_config = cfg.database
        identifier = resource_name(stage, "db", NameKind.GENERIC)

        version = db_config.engine_version
        engine = rds.DatabaseInstanceEngine.postgres(
            version=rds.PostgresEngineVersion.of(version, version.split(".")[0])
        )

        # 7.1: rds.force_ssl rejects unencrypted client connections.
        parameter_group = rds.ParameterGroup(
            self,
            "ParameterGroup",
            engine=engine,
            description=f"{stage}: PostgreSQL parameters (TLS required)",
            parameters={"rds.force_ssl": "1"},
        )

        # 10.1: pre-created so retention is set without the RDS log retention custom resource.
        self.database_log_group = logs.LogGroup(
            self,
            "DatabaseLogGroup",
            log_group_name=f"/aws/rds/instance/{identifier}/postgresql",
            retention=retention_days(cfg.observability.log_retention_days),
            removal_policy=removal_policy,
        )

        # The L2 adds the "db." prefix itself, so Stage_Config holds the bare class (t4g.micro).
        instance_class = db_config.instance_class.removeprefix("db.")

        self.database = rds.DatabaseInstance(
            self,
            "Database",
            instance_identifier=identifier,
            engine=engine,
            instance_type=ec2.InstanceType(instance_class),
            database_name=db_config.database_name,
            allocated_storage=db_config.allocated_storage_gb,
            multi_az=db_config.multi_az,
            backup_retention=cdk.Duration.days(db_config.backup_retention_days),
            vpc=network.vpc,
            vpc_subnets=network.isolated_subnets,
            security_groups=[self._immutable_sg("DbSg", network.db_sg)],
            publicly_accessible=False,
            storage_encrypted=True,
            storage_encryption_key=self.data_key,
            credentials=rds.Credentials.from_generated_secret(
                MASTER_USERNAME,
                encryption_key=self.data_key,
                secret_name=f"{stage}/db/master",
            ),
            iam_authentication=True,
            parameter_group=parameter_group,
            cloudwatch_logs_exports=["postgresql"],
            deletion_protection=cfg.removal_policy == "RETAIN",
            removal_policy=removal_policy,
        )
        # The log group must exist (with retention) before RDS starts exporting to it.
        self.database.node.add_dependency(self.database_log_group)

        secret = self.database.secret
        assert secret is not None  # generated credentials always create a secret
        self.master_secret = secret

    def _create_proxy(
        self, cfg: StageConfig, network: NetworkStack, removal_policy: cdk.RemovalPolicy
    ) -> None:
        stage = cfg.stage
        self.app_user = cfg.database.app_user

        # D5: Lambdas connect as app_user. The application repository's migrations create the
        # PostgreSQL role; this secret holds its generated password.
        self.app_user_secret = rds.DatabaseSecret(
            self,
            "AppUserSecret",
            username=self.app_user,
            dbname=cfg.database.database_name,
            encryption_key=self.data_key,
            secret_name=f"{stage}/db/{self.app_user}",
        )
        self.app_user_secret.apply_removal_policy(removal_policy)

        self.proxy = rds.DatabaseProxy(
            self,
            "Proxy",
            db_proxy_name=resource_name(stage, "db-proxy", NameKind.GENERIC),
            proxy_target=rds.ProxyTarget.from_instance(self.database),
            secrets=[self.master_secret, self.app_user_secret],
            vpc=network.vpc,
            vpc_subnets=network.private_subnets,
            security_groups=[self._immutable_sg("ProxySg", network.proxy_sg)],
            iam_auth=True,
            require_tls=True,
        )

    def _create_tables(self, cfg: StageConfig, removal_policy: cdk.RemovalPolicy) -> None:
        stage = cfg.stage
        encryption = dynamodb.TableEncryptionV2.customer_managed_key(self.data_key)
        common: dict[str, Any] = {
            "billing": dynamodb.Billing.on_demand(),
            "encryption": encryption,
            "point_in_time_recovery_specification": dynamodb.PointInTimeRecoverySpecification(
                point_in_time_recovery_enabled=True
            ),
            "deletion_protection": cfg.removal_policy == "RETAIN",
            "removal_policy": removal_policy,
        }

        self.idempotency_table = dynamodb.TableV2(
            self,
            "IdempotencyTable",
            table_name=resource_name(stage, "idempotency", NameKind.GENERIC),
            partition_key=dynamodb.Attribute(name="pk", type=dynamodb.AttributeType.STRING),
            time_to_live_attribute=TTL_ATTRIBUTE,
            **common,
        )
        self.saga_table = dynamodb.TableV2(
            self,
            "SagaTable",
            table_name=resource_name(stage, "saga", NameKind.GENERIC),
            partition_key=dynamodb.Attribute(name="pk", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="sk", type=dynamodb.AttributeType.STRING),
            dynamo_stream=dynamodb.StreamViewType.NEW_AND_OLD_IMAGES,
            **common,
        )

    def _create_buckets(self, cfg: StageConfig, removal_policy: cdk.RemovalPolicy) -> None:
        stage = cfg.stage

        def bucket_name(base: str) -> str:
            # Plain strings (not tokens), so cdk-nag can evaluate the bucket rules.
            return resource_name(
                stage,
                base,
                NameKind.BUCKET,
                project=cfg.project,
                account=cfg.account,
                region=cfg.region,
            )

        # 8.6: S3 server access log targets cannot use SSE-KMS, so this bucket uses SSE-S3.
        self.access_log_bucket = SecureBucket(
            self,
            "AccessLogBucket",
            bucket_name=bucket_name("access-logs"),
            lifecycle_expiration_days=cfg.observability.access_log_expiration_days,
            removal_policy=removal_policy,
        )
        self.documents_bucket_name = bucket_name("documents")
        # 8.4, 8.5: versioned, SSE-KMS with the Data_Key, access logs into the Access_Log_Bucket.
        self.documents_bucket = SecureBucket(
            self,
            "DocumentsBucket",
            bucket_name=self.documents_bucket_name,
            encryption_key=self.data_key,
            bucket_key_enabled=True,
            versioned=True,
            server_access_logs_bucket=self.access_log_bucket.bucket,
            server_access_logs_prefix=DOCUMENTS_LOG_PREFIX,
            removal_policy=removal_policy,
        )
