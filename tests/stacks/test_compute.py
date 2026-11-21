"""ApiComputeStack function tests (Requirements 6.1 to 6.6, 6.8, 6.9, 7.5, 9.8, 12.1, 12.2).

Assertions are on the properties the requirements name, against the templates synthesized once
per session for each variant (dev, dev with ``removal_policy: RETAIN``, two NAT gateways). The
dev config has one queue and every function but the signer uses the database, with no reserved
concurrency and no artifact version. A fourth, "extended" variant covers the other branches:
``dotnet10`` on ``x86_64``, reserved concurrency, an artifact version, a second queue consumed
by a function that has no database and no data access.

The stack imports values from the network, security, data and messaging stacks, so those
references are ``Fn::ImportValue``. ``_resolve_import`` follows them back to the exporting
stack's output.
"""

import copy
import json
from typing import Any

import pytest
from aws_cdk.assertions import Template

from synthesis import VARIANT_OVERRIDES, SynthVariant, load_raw_dev, new_app
from tokenization_infra.app_builder import build_app
from tokenization_infra.core.config import FunctionId, StageConfig, parse_stage_config
from tokenization_infra.core.naming import function_name, role_name

EXTENDED = "extended"
_EXTENDED_CACHE: dict[str, SynthVariant] = {}

DOMAIN_FUNCTIONS = [fn for fn in FunctionId if fn is not FunctionId.SIGNER]
RUNTIMES = {"dotnet8": "dotnet8", "dotnet10": "dotnet10"}
ARCHITECTURES = {"arm64": "arm64", "x86_64": "x86_64"}
DATA_ENVIRONMENT = {
    "idempotency-table": "IDEMPOTENCY_TABLE",
    "saga-table": "SAGA_TABLE",
    "documents-bucket": "DOCUMENTS_BUCKET",
}
VPC_ACCESS_POLICY = "AWSLambdaVPCAccessExecutionRole"
LAMBDA_SG_DESCRIPTION = "Lambda functions in the VPC"
EXTENDED_ARTIFACT_VERSION = "3Lg0b1sJ0g3tQ8mWc1d6Yx9T2uVhE5aB"
EXTENDED_RESERVED_CONCURRENCY = 5
EXTENDED_BATCH_SIZE = 5


def _build_extended() -> SynthVariant:
    """dev plus the branches the dev config leaves out (see the module docstring)."""
    if EXTENDED not in _EXTENDED_CACHE:
        raw = copy.deepcopy(load_raw_dev())
        raw["lambda_runtime"] = "dotnet10"
        raw["lambda_architecture"] = "x86_64"
        raw["functions"]["stellar-kyc"]["reserved_concurrency"] = EXTENDED_RESERVED_CONCURRENCY
        raw["functions"]["collateral-identity"]["artifact"]["version"] = EXTENDED_ARTIFACT_VERSION
        raw["functions"]["reports"]["uses_database"] = False
        raw["functions"]["reports"]["data_access"] = []
        raw["queues"].append(
            {
                "name": "notifications",
                "consumer": "reports",
                "max_receive_count": 3,
                "visibility_timeout_seconds": 60,
                "batch_size": EXTENDED_BATCH_SIZE,
            }
        )
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
def compute_variant(
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


def _compute(variant: SynthVariant) -> Template:
    return variant.template("api-compute")


def _resolve_import(variant: SynthVariant, reference: Any) -> Any:
    """Value of the exporting stack's output behind ``{"Fn::ImportValue": name}``."""
    assert isinstance(reference, dict) and set(reference) == {"Fn::ImportValue"}, reference
    name = reference["Fn::ImportValue"]
    for template in variant.templates.values():
        for output in template.to_json().get("Outputs", {}).values():
            if output.get("Export", {}).get("Name") == name:
                return output["Value"]
    raise AssertionError(f"no stack exports {name}")


def _function(variant: SynthVariant, fn: FunctionId) -> tuple[str, dict[str, Any]]:
    """Logical id and properties of the function ``{stage}-{fn}``."""
    name = function_name(variant.cfg.stage, fn)
    found = _compute(variant).find_resources(
        "AWS::Lambda::Function", {"Properties": {"FunctionName": name}}
    )
    assert len(found) == 1, f"expected one function named {name}, found {list(found)}"
    logical_id, resource = next(iter(found.items()))
    return logical_id, dict(resource["Properties"])


def _role_logical_id(properties: dict[str, Any]) -> str:
    role = properties["Role"]
    assert set(role) == {"Fn::GetAtt"} and role["Fn::GetAtt"][1] == "Arn", role
    return str(role["Fn::GetAtt"][0])


def _ref(value: Any) -> str:
    assert isinstance(value, dict), value
    if "Ref" in value:
        return str(value["Ref"])
    return str(value["Fn::GetAtt"][0])


def _statements_of_role(template: Template, role_logical_id: str) -> list[dict[str, Any]]:
    """Every IAM statement of the policies attached to the role."""
    statements: list[dict[str, Any]] = []
    for policy in template.find_resources("AWS::IAM::Policy").values():
        if {"Ref": role_logical_id} in policy["Properties"]["Roles"]:
            statements.extend(policy["Properties"]["PolicyDocument"]["Statement"])
    return statements


def _listed(value: Any) -> list[Any]:
    return value if isinstance(value, list) else [value]


def _statements_with_action(statements: list[dict[str, Any]], action: str) -> list[dict[str, Any]]:
    return [s for s in statements if action in _listed(s["Action"])]


def _network_private_subnets(variant: SynthVariant) -> set[str]:
    """Logical ids of the private subnets in the network stack."""
    subnets = variant.template("network").find_resources("AWS::EC2::Subnet")
    private: set[str] = set()
    for logical_id, subnet in subnets.items():
        tags = {tag["Key"]: tag["Value"] for tag in subnet["Properties"]["Tags"]}
        if tags["aws-cdk:subnet-name"] == "private":
            private.add(logical_id)
    return private


def _network_lambda_sg(variant: SynthVariant) -> str:
    groups = variant.template("network").find_resources(
        "AWS::EC2::SecurityGroup", {"Properties": {"GroupDescription": LAMBDA_SG_DESCRIPTION}}
    )
    assert len(groups) == 1, list(groups)
    return next(iter(groups))


def _expected_env_keys(cfg: StageConfig, fn: FunctionId) -> set[str]:
    """Environment keys a function must have, derived from its config (6.8, 7.5)."""
    if fn is FunctionId.SIGNER:
        return {"STAGE", "SIGNING_KEY_ID"}
    fn_cfg = cfg.functions[fn]
    keys = {"STAGE", "EVENT_BUS_NAME", "ALLOWED_ORIGINS"}
    if fn_cfg.uses_database:
        keys |= {"DB_PROXY_ENDPOINT", "DB_PORT", "DB_NAME", "DB_USER"}
    keys |= {DATA_ENVIRONMENT[resource] for resource in fn_cfg.data_access}
    if fn is FunctionId.SAGAS_PAYMENTS:
        keys.add("SAGA_STATE_MACHINE_ARN")
    if any(queue.consumer == fn.value for queue in cfg.queues):
        keys.add("QUEUE_URLS")
    if fn is FunctionId.STELLAR_KYC:
        keys |= {"KYC_SECRET_ARNS", "SIGNER_FUNCTION_NAME"}
    return keys


# --- Functions (6.1, 6.2, 6.3, 6.5) ---


def test_six_functions_take_their_settings_from_the_config(
    compute_variant: SynthVariant,
) -> None:
    """6.1, 6.2, 6.3: runtime, handler, S3 code, memory, timeout and reserved concurrency."""
    cfg = compute_variant.cfg
    _compute(compute_variant).resource_count_is("AWS::Lambda::Function", len(FunctionId))
    assert len(FunctionId) == 6

    for fn in FunctionId:
        _, properties = _function(compute_variant, fn)
        fn_cfg = cfg.functions[fn]
        assert properties["Runtime"] == RUNTIMES[cfg.lambda_runtime], fn
        assert properties["Architectures"] == [ARCHITECTURES[cfg.lambda_architecture]], fn
        assert properties["Handler"] == fn_cfg.handler, fn
        assert properties["MemorySize"] == fn_cfg.memory_mb, fn
        assert properties["Timeout"] == fn_cfg.timeout_seconds, fn

        expected_code: dict[str, Any] = {
            "S3Bucket": fn_cfg.artifact.bucket,
            "S3Key": fn_cfg.artifact.key,
        }
        if fn_cfg.artifact.version is not None:
            expected_code["S3ObjectVersion"] = fn_cfg.artifact.version
        assert properties["Code"] == expected_code, fn

        if fn_cfg.reserved_concurrency is None:
            assert "ReservedConcurrentExecutions" not in properties, fn
        else:
            assert properties["ReservedConcurrentExecutions"] == fn_cfg.reserved_concurrency, fn


def test_extended_variant_exercises_the_optional_settings(
    compute_variant: SynthVariant,
) -> None:
    """Guard: the extended variant really sets the values the other variants leave out."""
    cfg = compute_variant.cfg
    if compute_variant.name != EXTENDED:
        assert cfg.functions[FunctionId.STELLAR_KYC].reserved_concurrency is None
        assert cfg.functions[FunctionId.COLLATERAL_IDENTITY].artifact.version is None
        return
    _, kyc = _function(compute_variant, FunctionId.STELLAR_KYC)
    assert kyc["ReservedConcurrentExecutions"] == EXTENDED_RESERVED_CONCURRENCY
    _, identity = _function(compute_variant, FunctionId.COLLATERAL_IDENTITY)
    assert identity["Code"]["S3ObjectVersion"] == EXTENDED_ARTIFACT_VERSION
    assert identity["Runtime"] == "dotnet10"
    assert identity["Architectures"] == ["x86_64"]


def test_functions_run_only_in_private_subnets_with_the_lambda_security_group(
    compute_variant: SynthVariant,
) -> None:
    """6.4: VpcConfig has the private subnets of the network stack and the Lambda SG only."""
    private_subnets = _network_private_subnets(compute_variant)
    lambda_sg = _network_lambda_sg(compute_variant)
    assert len(private_subnets) == 2

    for fn in FunctionId:
        _, properties = _function(compute_variant, fn)
        vpc_config = properties["VpcConfig"]
        subnet_ids = [
            _ref(_resolve_import(compute_variant, subnet)) for subnet in vpc_config["SubnetIds"]
        ]
        assert sorted(subnet_ids) == sorted(private_subnets), fn
        group_ids = [
            _ref(_resolve_import(compute_variant, group))
            for group in vpc_config["SecurityGroupIds"]
        ]
        assert group_ids == [lambda_sg], fn


def test_active_tracing_on_every_function(compute_variant: SynthVariant) -> None:
    """6.5: X-Ray active tracing."""
    for fn in FunctionId:
        _, properties = _function(compute_variant, fn)
        assert properties["TracingConfig"] == {"Mode": "Active"}, fn


# --- Roles (6.6) ---


def test_each_function_has_its_own_lambda_role(compute_variant: SynthVariant) -> None:
    """6.6: six distinct roles, trusted by Lambda, each with the VPC access managed policy."""
    template = _compute(compute_variant)
    cfg = compute_variant.cfg
    role_ids: list[str] = []
    for fn in FunctionId:
        _, properties = _function(compute_variant, fn)
        role_id = _role_logical_id(properties)
        role_ids.append(role_id)
        role = template.find_resources("AWS::IAM::Role")[role_id]["Properties"]
        assert role["RoleName"] == role_name(cfg.stage, fn), fn
        assert role["AssumeRolePolicyDocument"]["Statement"] == [
            {
                "Action": "sts:AssumeRole",
                "Effect": "Allow",
                "Principal": {"Service": "lambda.amazonaws.com"},
            }
        ]
        assert VPC_ACCESS_POLICY in json.dumps(role["ManagedPolicyArns"]), fn
    assert len(set(role_ids)) == len(FunctionId) == 6


# --- Environment (6.8, 7.5) ---


def test_environment_keys_match_the_resources_each_function_uses(
    compute_variant: SynthVariant,
) -> None:
    """6.8, 7.5: a variable exists exactly when the function uses the matching resource."""
    cfg = compute_variant.cfg
    for fn in FunctionId:
        _, properties = _function(compute_variant, fn)
        variables = properties["Environment"]["Variables"]
        assert set(variables) == _expected_env_keys(cfg, fn), fn
        assert variables["STAGE"] == cfg.stage, fn


def test_database_variables_only_on_functions_that_use_the_database(
    compute_variant: SynthVariant,
) -> None:
    """7.5: no DB_* variable and no connect statement without ``uses_database``."""
    cfg = compute_variant.cfg
    template = _compute(compute_variant)
    for fn in FunctionId:
        _, properties = _function(compute_variant, fn)
        variables = properties["Environment"]["Variables"]
        uses_database = cfg.functions[fn].uses_database and fn is not FunctionId.SIGNER
        db_keys = {key for key in variables if key.startswith("DB_")}
        if uses_database:
            assert db_keys == {"DB_PROXY_ENDPOINT", "DB_PORT", "DB_NAME", "DB_USER"}, fn
            assert variables["DB_PORT"] == "5432", fn
            assert variables["DB_NAME"] == cfg.database.database_name, fn
            assert variables["DB_USER"] == cfg.database.app_user, fn
        else:
            assert db_keys == set(), fn
        statements = _statements_of_role(template, _role_logical_id(properties))
        assert bool(_statements_with_action(statements, "rds-db:connect")) is uses_database, fn


def test_signer_environment_has_only_stage_and_signing_key_id(
    compute_variant: SynthVariant,
) -> None:
    """6.8: the signer sees nothing but its stage and the key it signs with."""
    _, properties = _function(compute_variant, FunctionId.SIGNER)
    assert set(properties["Environment"]["Variables"]) == {"STAGE", "SIGNING_KEY_ID"}


def test_only_stellar_kyc_knows_the_signer_and_the_kyc_secrets(
    compute_variant: SynthVariant,
) -> None:
    """6.8: KYC_SECRET_ARNS and SIGNER_FUNCTION_NAME are on stellar-kyc only."""
    cfg = compute_variant.cfg
    for fn in FunctionId:
        _, properties = _function(compute_variant, fn)
        variables = properties["Environment"]["Variables"]
        on_kyc = fn is FunctionId.STELLAR_KYC
        assert ("KYC_SECRET_ARNS" in variables) is on_kyc, fn
        assert ("SIGNER_FUNCTION_NAME" in variables) is on_kyc, fn
        assert ("SAGA_STATE_MACHINE_ARN" in variables) is (fn is FunctionId.SAGAS_PAYMENTS), fn
        if on_kyc:
            assert variables["SIGNER_FUNCTION_NAME"] == function_name(cfg.stage, FunctionId.SIGNER)


# --- Log groups (6.9, 12.2) ---


def test_each_function_has_a_log_group_with_the_configured_retention(
    compute_variant: SynthVariant,
) -> None:
    """6.9: ``/aws/lambda/{name}`` per function, with retention, set as the function's log group."""
    template = _compute(compute_variant)
    cfg = compute_variant.cfg
    groups = template.find_resources("AWS::Logs::LogGroup")
    lambda_groups = {
        logical_id: group
        for logical_id, group in groups.items()
        if str(group["Properties"]["LogGroupName"]).startswith("/aws/lambda/")
    }
    assert len(lambda_groups) == len(FunctionId)
    expected_policy = "Delete" if cfg.removal_policy == "DESTROY" else "Retain"
    used: set[str] = set()

    for fn in FunctionId:
        _, properties = _function(compute_variant, fn)
        group_id = _ref(properties["LoggingConfig"]["LogGroup"])
        group = lambda_groups[group_id]
        assert group["Properties"]["LogGroupName"] == f"/aws/lambda/{cfg.stage}-{fn.value}"
        assert group["Properties"]["RetentionInDays"] == cfg.observability.log_retention_days
        assert group["DeletionPolicy"] == expected_policy
        used.add(group_id)
    assert len(used) == len(FunctionId), "functions share a log group"


def test_every_log_group_of_the_stack_has_retention(compute_variant: SynthVariant) -> None:
    """12.2: no log group in the compute stack keeps its logs forever."""
    groups = _compute(compute_variant).find_resources("AWS::Logs::LogGroup")
    retention = compute_variant.cfg.observability.log_retention_days
    assert groups
    for logical_id, group in groups.items():
        assert group["Properties"]["RetentionInDays"] == retention, logical_id


# --- Event source mappings (9.8) ---


def test_each_queue_feeds_its_consumer_with_partial_batch_responses(
    compute_variant: SynthVariant,
) -> None:
    """9.8: one mapping per queue on its consumer, with BatchSize and ReportBatchItemFailures."""
    template = _compute(compute_variant)
    cfg = compute_variant.cfg
    messaging = compute_variant.template("messaging")
    mappings = template.find_resources("AWS::Lambda::EventSourceMapping")
    assert len(mappings) == len(cfg.queues)
    assert cfg.queues

    for queue in cfg.queues:
        consumer_id, _ = _function(compute_variant, FunctionId(queue.consumer))
        queue_resources = messaging.find_resources(
            "AWS::SQS::Queue", {"Properties": {"QueueName": f"{cfg.stage}-{queue.name}"}}
        )
        assert len(queue_resources) == 1
        queue_id = next(iter(queue_resources))

        matching = [
            mapping["Properties"]
            for mapping in mappings.values()
            if _ref(mapping["Properties"]["FunctionName"]) == consumer_id
            and _ref(_resolve_import(compute_variant, mapping["Properties"]["EventSourceArn"]))
            == queue_id
        ]
        assert len(matching) == 1, queue.name
        assert matching[0]["BatchSize"] == queue.batch_size
        assert matching[0]["FunctionResponseTypes"] == ["ReportBatchItemFailures"]


def test_only_the_consumer_may_receive_from_its_queue(compute_variant: SynthVariant) -> None:
    """9.8: sqs:ReceiveMessage is on the consuming function's role and on no other."""
    template = _compute(compute_variant)
    cfg = compute_variant.cfg
    consumers = {FunctionId(queue.consumer) for queue in cfg.queues}
    for fn in FunctionId:
        _, properties = _function(compute_variant, fn)
        statements = _statements_of_role(template, _role_logical_id(properties))
        receives = _statements_with_action(statements, "sqs:ReceiveMessage")
        assert bool(receives) is (fn in consumers), fn
        assert len(receives) == sum(1 for q in cfg.queues if q.consumer == fn.value), fn


# --- rds-db:connect (7.5) ---


def test_rds_db_connect_is_scoped_to_the_app_user(compute_variant: SynthVariant) -> None:
    """7.5: database users get one rds-db:connect statement, limited to the app user."""
    template = _compute(compute_variant)
    cfg = compute_variant.cfg
    for fn in FunctionId:
        _, properties = _function(compute_variant, fn)
        statements = _statements_of_role(template, _role_logical_id(properties))
        connects = _statements_with_action(statements, "rds-db:connect")
        if fn is FunctionId.SIGNER or not cfg.functions[fn].uses_database:
            assert connects == [], fn
            continue
        assert len(connects) == 1, fn
        [connect] = connects
        assert connect["Effect"] == "Allow"
        assert connect["Action"] == "rds-db:connect"
        resource = connect["Resource"]
        assert isinstance(resource, dict) and set(resource) == {"Fn::Join"}, resource
        parts = resource["Fn::Join"][1]
        assert "dbuser:" in parts[0], parts
        assert parts[-1] == f"/{cfg.database.app_user}", parts
        assert "*" not in json.dumps(parts), parts
