"""S3 bucket with the security settings every bucket in this project must have."""

from aws_cdk import Duration, RemovalPolicy
from aws_cdk import aws_kms as kms
from aws_cdk import aws_s3 as s3
from constructs import Construct


class SecureBucket(Construct):
    """An ``s3.Bucket`` that always blocks public access, enforces TLS and disables ACLs.

    The fixed settings are ``block_public_access=BLOCK_ALL``, ``enforce_ssl=True`` and
    ``object_ownership=BUCKET_OWNER_ENFORCED``. ``auto_delete_objects`` is never set, because it
    creates a custom resource (Requirement 1.12). Encryption, versioning, lifecycle expiry, the
    server access log target and the removal policy are parameters.

    Encryption is SSE-KMS with ``encryption_key`` when given, otherwise SSE-S3. Use SSE-S3 for a
    bucket that is a server access log target, because S3 log delivery cannot write to SSE-KMS
    buckets.
    """

    bucket: s3.Bucket

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        bucket_name: str,
        encryption_key: kms.IKey | None = None,
        versioned: bool = False,
        lifecycle_expiration_days: int | None = None,
        server_access_logs_bucket: s3.IBucket | None = None,
        server_access_logs_prefix: str | None = None,
        bucket_key_enabled: bool = False,
        removal_policy: RemovalPolicy = RemovalPolicy.RETAIN,
    ) -> None:
        super().__init__(scope, construct_id)

        if server_access_logs_prefix is not None and server_access_logs_bucket is None:
            raise ValueError("server_access_logs_prefix requires server_access_logs_bucket")
        if bucket_key_enabled and encryption_key is None:
            raise ValueError("bucket_key_enabled requires encryption_key (SSE-KMS)")
        if lifecycle_expiration_days is not None and lifecycle_expiration_days < 1:
            raise ValueError("lifecycle_expiration_days must be at least 1")

        lifecycle_rules = (
            None
            if lifecycle_expiration_days is None
            else [
                s3.LifecycleRule(
                    id="expire-objects",
                    enabled=True,
                    expiration=Duration.days(lifecycle_expiration_days),
                )
            ]
        )

        self.bucket = s3.Bucket(
            self,
            "Bucket",
            bucket_name=bucket_name,
            encryption=(
                s3.BucketEncryption.S3_MANAGED
                if encryption_key is None
                else s3.BucketEncryption.KMS
            ),
            encryption_key=encryption_key,
            bucket_key_enabled=bucket_key_enabled,
            versioned=versioned,
            lifecycle_rules=lifecycle_rules,
            server_access_logs_bucket=server_access_logs_bucket,
            server_access_logs_prefix=server_access_logs_prefix,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            enforce_ssl=True,
            object_ownership=s3.ObjectOwnership.BUCKET_OWNER_ENFORCED,
            removal_policy=removal_policy,
        )
