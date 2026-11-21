"""DataStack assertion tests (Requirements 7.1 to 7.4, 7.6, 8.1 to 8.6, 12.1, 12.2).

Assertions are on the properties the requirements name, against the templates synthesized once
per session for each variant (dev, dev with ``removal_policy: RETAIN``, two NAT gateways).
The data stack imports the Data_Key and the subnets from other stacks, so references arrive as
``Fn::ImportValue``. ``_resolve_import`` follows them back to the exporting stack's output.
"""

from collections.abc import Mapping
from typing import Any

from aws_cdk.assertions import Template

from synthesis import SynthVariant

BLOCK_PUBLIC_ACCESS_FLAGS = (
    "BlockPublicAcls",
    "BlockPublicPolicy",
    "IgnorePublicAcls",
    "RestrictPublicBuckets",
)
DATA_KEY_ENABLED_ROTATION = {"EnableKeyRotation": True}


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
        "AWS::KMS::Key", {"Properties": DATA_KEY_ENABLED_ROTATION}
    )
    assert len(keys) == 1, f"expected one rotating key, found {list(keys)}"
    return {"Fn::GetAtt": [next(iter(keys)), "Arn"]}


def _subnet_type(variant: SynthVariant, reference: Any) -> str:
    """The ``aws-cdk:subnet-name`` tag of the network subnet behind an imported subnet id."""
    value = _resolve_import(variant, reference)
    assert set(value) == {"Ref"}, value
    subnet = variant.template("network").to_json()["Resources"][value["Ref"]]
    assert subnet["Type"] == "AWS::EC2::Subnet"
    tags = {tag["Key"]: tag["Value"] for tag in subnet["Properties"]["Tags"]}
    return str(tags["aws-cdk:subnet-name"])


def _only(resources: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
    assert len(resources) == 1, f"expected one resource, found {list(resources)}"
    logical_id, resource = next(iter(resources.items()))
    return logical_id, dict(resource)


def _bucket(template: Template, name_fragment: str) -> tuple[str, dict[str, Any]]:
    return _only(
        {
            logical_id: resource
            for logical_id, resource in template.find_resources("AWS::S3::Bucket").items()
            if name_fragment in resource["Properties"]["BucketName"]
        }
    )


def _table(template: Template, table_name: str) -> dict[str, Any]:
    _, resource = _only(
        template.find_resources(
            "AWS::DynamoDB::GlobalTable", {"Properties": {"TableName": table_name}}
        )
    )
    return resource


def _secret(template: Template, name: str) -> dict[str, Any]:
    _, resource = _only(
        template.find_resources("AWS::SecretsManager::Secret", {"Properties": {"Name": name}})
    )
    return resource


# --- Database (7.1 to 7.3, 7.6) ---


def test_database_is_postgres_in_isolated_subnets(variant: SynthVariant) -> None:
    """7.1: RDS for PostgreSQL, not public, subnet group made only of Isolated_Subnets."""
    template = variant.template("data")
    template.resource_count_is("AWS::RDS::DBInstance", 1)
    template.has_resource_properties(
        "AWS::RDS::DBInstance", {"Engine": "postgres", "PubliclyAccessible": False}
    )
    _, group = _only(template.find_resources("AWS::RDS::DBSubnetGroup"))
    subnet_ids = group["Properties"]["SubnetIds"]
    assert len(subnet_ids) == len(variant.cfg.network.availability_zones)
    assert {_subnet_type(variant, subnet_id) for subnet_id in subnet_ids} == {"isolated"}


def test_database_storage_is_encrypted_with_the_data_key(variant: SynthVariant) -> None:
    """7.1: StorageEncrypted with KmsKeyId equal to the Data_Key ARN."""
    template = variant.template("data")
    _, database = _only(template.find_resources("AWS::RDS::DBInstance"))
    properties = database["Properties"]
    assert properties["StorageEncrypted"] is True
    assert _resolve_import(variant, properties["KmsKeyId"]) == _data_key_arn(variant)


def test_database_settings_come_from_stage_config(variant: SynthVariant) -> None:
    """7.2: engine version, class, storage, Multi-AZ and backup retention."""
    db = variant.cfg.database
    variant.template("data").has_resource_properties(
        "AWS::RDS::DBInstance",
        {
            "EngineVersion": db.engine_version,
            "DBInstanceClass": f"db.{db.instance_class.removeprefix('db.')}",
            "AllocatedStorage": str(db.allocated_storage_gb),
            "MultiAZ": db.multi_az,
            "BackupRetentionPeriod": db.backup_retention_days,
            "DBName": db.database_name,
            "EnableIAMDatabaseAuthentication": True,
        },
    )


def test_database_requires_tls(variant: SynthVariant) -> None:
    """7.1 (design): the parameter group sets rds.force_ssl=1 and the instance uses it."""
    template = variant.template("data")
    template.has_resource_properties(
        "AWS::RDS::DBParameterGroup", {"Parameters": {"rds.force_ssl": "1"}}
    )
    group_id, _ = _only(template.find_resources("AWS::RDS::DBParameterGroup"))
    template.has_resource_properties(
        "AWS::RDS::DBInstance", {"DBParameterGroupName": {"Ref": group_id}}
    )


def test_master_credentials_are_generated_and_encrypted(variant: SynthVariant) -> None:
    """7.3: the master secret is generated at deploy time, encrypted with the Data_Key."""
    secret = _secret(variant.template("data"), f"{variant.cfg.stage}/db/master")
    properties = secret["Properties"]
    assert "GenerateSecretString" in properties
    assert "SecretString" not in properties
    assert _resolve_import(variant, properties["KmsKeyId"]) == _data_key_arn(variant)


def test_app_user_secret_is_generated_and_encrypted(variant: SynthVariant) -> None:
    """D5: the app_user secret is generated, never written into the template."""
    cfg = variant.cfg
    secret = _secret(variant.template("data"), f"{cfg.stage}/db/{cfg.database.app_user}")
    properties = secret["Properties"]
    assert "GenerateSecretString" in properties
    assert "SecretString" not in properties
    assert _resolve_import(variant, properties["KmsKeyId"]) == _data_key_arn(variant)


def test_database_has_no_static_password_in_the_template(variant: SynthVariant) -> None:
    """7.3: the master password is a dynamic reference to the generated secret."""
    _, database = _only(variant.template("data").find_resources("AWS::RDS::DBInstance"))
    password = str(database["Properties"]["MasterUserPassword"])
    assert "{{resolve:secretsmanager:" in password


def test_database_deletion_protection_follows_removal_policy(variant: SynthVariant) -> None:
    """7.6: DeletionProtection is true only when removal_policy is RETAIN."""
    _, database = _only(variant.template("data").find_resources("AWS::RDS::DBInstance"))
    expected = variant.cfg.removal_policy == "RETAIN"
    assert database["Properties"]["DeletionProtection"] is expected


def test_deletion_protection_is_off_in_dev_and_on_in_retain(
    dev_variant: SynthVariant, retain_variant: SynthVariant
) -> None:
    """7.6: the two named variants pin the expected values."""
    dev_db = _only(dev_variant.template("data").find_resources("AWS::RDS::DBInstance"))[1]
    retain_db = _only(retain_variant.template("data").find_resources("AWS::RDS::DBInstance"))[1]
    assert dev_db["Properties"]["DeletionProtection"] is False
    assert retain_db["Properties"]["DeletionProtection"] is True
    assert retain_db["DeletionPolicy"] == "Retain"
    assert dev_db["DeletionPolicy"] == "Delete"


# --- Db_Proxy (7.4) ---


def test_proxy_requires_iam_auth_and_tls(variant: SynthVariant) -> None:
    """7.4: every auth entry has IAMAuth REQUIRED, and RequireTLS is true."""
    template = variant.template("data")
    template.has_resource_properties(
        "AWS::RDS::DBProxy", {"EngineFamily": "POSTGRESQL", "RequireTLS": True}
    )
    _, proxy = _only(template.find_resources("AWS::RDS::DBProxy"))
    auth = proxy["Properties"]["Auth"]
    assert len(auth) == 2, "master secret and app_user secret"
    assert {entry["IAMAuth"] for entry in auth} == {"REQUIRED"}
    assert {entry["AuthScheme"] for entry in auth} == {"SECRETS"}


def test_proxy_is_in_private_subnets(variant: SynthVariant) -> None:
    """7.4: the proxy subnets are the Private_Subnets, one per AZ."""
    _, proxy = _only(variant.template("data").find_resources("AWS::RDS::DBProxy"))
    subnet_ids = proxy["Properties"]["VpcSubnetIds"]
    assert len(subnet_ids) == len(variant.cfg.network.availability_zones)
    assert {_subnet_type(variant, subnet_id) for subnet_id in subnet_ids} == {"private"}


# --- DynamoDB tables (8.1 to 8.3) ---


def _replica(table: dict[str, Any]) -> dict[str, Any]:
    replicas = table["Properties"]["Replicas"]
    assert len(replicas) == 1, "a single-region table"
    return dict(replicas[0])


def test_tables_are_on_demand_encrypted_and_recoverable(variant: SynthVariant) -> None:
    """8.1: on-demand billing, SSE with the Data_Key, point-in-time recovery."""
    template = variant.template("data")
    stage = variant.cfg.stage
    data_key = _data_key_arn(variant)
    for table_name in (f"{stage}-idempotency", f"{stage}-saga"):
        table = _table(template, table_name)
        properties = table["Properties"]
        assert properties["BillingMode"] == "PAY_PER_REQUEST"
        assert "ProvisionedThroughput" not in properties
        assert properties["SSESpecification"] == {"SSEEnabled": True, "SSEType": "KMS"}
        replica = _replica(table)
        assert replica["PointInTimeRecoverySpecification"] == {"PointInTimeRecoveryEnabled": True}
        assert _resolve_import(variant, replica["SSESpecification"]["KMSMasterKeyId"]) == data_key


def test_idempotency_table_expires_items_on_expires_at(variant: SynthVariant) -> None:
    """8.2: TTL on ``expiresAt`` for the Idempotency_Table, and not on the Saga_Table."""
    template = variant.template("data")
    stage = variant.cfg.stage
    idempotency = _table(template, f"{stage}-idempotency")
    assert idempotency["Properties"]["TimeToLiveSpecification"] == {
        "AttributeName": "expiresAt",
        "Enabled": True,
    }
    assert idempotency["Properties"]["KeySchema"] == [{"AttributeName": "pk", "KeyType": "HASH"}]
    assert "TimeToLiveSpecification" not in _table(template, f"{stage}-saga")["Properties"]


def test_saga_table_streams_new_and_old_images(variant: SynthVariant) -> None:
    """8.3: a stream with NEW_AND_OLD_IMAGES on the Saga_Table only."""
    template = variant.template("data")
    stage = variant.cfg.stage
    saga = _table(template, f"{stage}-saga")
    assert saga["Properties"]["StreamSpecification"] == {"StreamViewType": "NEW_AND_OLD_IMAGES"}
    assert saga["Properties"]["KeySchema"] == [
        {"AttributeName": "pk", "KeyType": "HASH"},
        {"AttributeName": "sk", "KeyType": "RANGE"},
    ]
    assert "StreamSpecification" not in _table(template, f"{stage}-idempotency")["Properties"]


def test_tables_deletion_protection_follows_removal_policy(variant: SynthVariant) -> None:
    template = variant.template("data")
    stage = variant.cfg.stage
    expected = variant.cfg.removal_policy == "RETAIN"
    for table_name in (f"{stage}-idempotency", f"{stage}-saga"):
        assert _replica(_table(template, table_name))["DeletionProtectionEnabled"] is expected


# --- S3 buckets (8.4 to 8.6, 12.2) ---


def test_every_bucket_in_every_stack_blocks_public_access(variant: SynthVariant) -> None:
    """12.2, 8.4, 8.6: all four Block Public Access flags on every AWS::S3::Bucket."""
    seen = 0
    for stack_id, template in variant.templates.items():
        for logical_id, bucket in template.find_resources("AWS::S3::Bucket").items():
            seen += 1
            configuration = bucket["Properties"].get("PublicAccessBlockConfiguration")
            assert configuration is not None, f"{stack_id}/{logical_id}: no block configuration"
            for flag in BLOCK_PUBLIC_ACCESS_FLAGS:
                assert configuration.get(flag) is True, f"{stack_id}/{logical_id}: {flag}"
    assert seen >= 2, "the data stack must contribute the Documents and Access_Log buckets"


def test_data_stack_has_exactly_the_documents_and_access_log_buckets(
    variant: SynthVariant,
) -> None:
    template = variant.template("data")
    template.resource_count_is("AWS::S3::Bucket", 2)
    _bucket(template, "-documents-")
    _bucket(template, "-access-logs-")


def test_bucket_policies_deny_requests_without_tls(variant: SynthVariant) -> None:
    """8.4, 8.6: each data stack bucket has a Deny on aws:SecureTransport=false for s3:*."""
    template = variant.template("data")
    for fragment in ("-documents-", "-access-logs-"):
        bucket_id, _ = _bucket(template, fragment)
        policies = [
            policy
            for policy in template.find_resources("AWS::S3::BucketPolicy").values()
            if policy["Properties"]["Bucket"] == {"Ref": bucket_id}
        ]
        assert len(policies) == 1, f"{fragment}: expected one bucket policy"
        statements = policies[0]["Properties"]["PolicyDocument"]["Statement"]
        denies = [
            statement
            for statement in statements
            if statement["Effect"] == "Deny"
            and statement.get("Condition") == {"Bool": {"aws:SecureTransport": "false"}}
        ]
        assert len(denies) == 1, f"{fragment}: expected one TLS Deny, found {denies}"
        deny = denies[0]
        assert deny["Action"] == "s3:*"
        assert deny["Principal"] == {"AWS": "*"}
        assert len(deny["Resource"]) == 2, "the bucket and its objects"


def test_documents_bucket_is_versioned_and_encrypted_with_the_data_key(
    variant: SynthVariant,
) -> None:
    """8.4: versioning enabled and SSE-KMS with the Data_Key."""
    _, bucket = _bucket(variant.template("data"), "-documents-")
    properties = bucket["Properties"]
    assert properties["VersioningConfiguration"] == {"Status": "Enabled"}
    rules = properties["BucketEncryption"]["ServerSideEncryptionConfiguration"]
    assert len(rules) == 1
    default = rules[0]["ServerSideEncryptionByDefault"]
    assert default["SSEAlgorithm"] == "aws:kms"
    assert _resolve_import(variant, default["KMSMasterKeyID"]) == _data_key_arn(variant)


def test_documents_bucket_logs_to_the_access_log_bucket(variant: SynthVariant) -> None:
    """8.5: server access logging to the Access_Log_Bucket under the ``documents/`` prefix."""
    template = variant.template("data")
    access_log_id, _ = _bucket(template, "-access-logs-")
    _, documents = _bucket(template, "-documents-")
    assert documents["Properties"]["LoggingConfiguration"] == {
        "DestinationBucketName": {"Ref": access_log_id},
        "LogFilePrefix": "documents/",
    }


def test_access_log_bucket_uses_s3_managed_encryption(variant: SynthVariant) -> None:
    """8.6: SSE-S3 (AES256), because S3 log delivery cannot write to SSE-KMS buckets."""
    _, bucket = _bucket(variant.template("data"), "-access-logs-")
    rules = bucket["Properties"]["BucketEncryption"]["ServerSideEncryptionConfiguration"]
    assert len(rules) == 1
    default = rules[0]["ServerSideEncryptionByDefault"]
    assert default == {"SSEAlgorithm": "AES256"}


def test_access_log_bucket_expires_objects_per_stage_config(variant: SynthVariant) -> None:
    """8.6: a lifecycle rule expires objects after ``access_log_expiration_days``."""
    _, bucket = _bucket(variant.template("data"), "-access-logs-")
    rules = bucket["Properties"]["LifecycleConfiguration"]["Rules"]
    assert len(rules) == 1
    assert rules[0]["Status"] == "Enabled"
    assert rules[0]["ExpirationInDays"] == variant.cfg.observability.access_log_expiration_days


def test_buckets_disable_acls(variant: SynthVariant) -> None:
    """SecureBucket: BucketOwnerEnforced ownership on both buckets."""
    template = variant.template("data")
    for fragment in ("-documents-", "-access-logs-"):
        _, bucket = _bucket(template, fragment)
        assert bucket["Properties"]["OwnershipControls"] == {
            "Rules": [{"ObjectOwnership": "BucketOwnerEnforced"}]
        }
