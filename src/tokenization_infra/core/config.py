"""Stage_Config models, validation and loaders. Pure Python (no aws_cdk, no constructs)."""

import json
import warnings
from collections.abc import Mapping
from enum import StrEnum
from ipaddress import IPv4Network
from pathlib import Path
from typing import Annotated, Literal, Self
from urllib.parse import urlsplit

import yaml
from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    ValidationInfo,
    model_validator,
)
from pydantic_core import InitErrorDetails, PydanticCustomError

from tokenization_infra.core.errors import ConfigError, UnknownStageError, format_loc
from tokenization_infra.core.urls import is_https_origin

# <repo>/src/tokenization_infra/core/config.py -> parents[3] is the repository root.
REPO_ROOT: Path = Path(__file__).resolve().parents[3]
CONFIG_DIR: Path = REPO_ROOT / "config" / "stages"

DEFAULT_REGION = "us-east-1"
API_INTEGRATION_TIMEOUT_SECONDS = 29
FORBIDDEN_DOMAIN_WORDS = ("aws", "amazon", "cognito")
LOCAL_DEV_HOSTS = ("localhost", "127.0.0.1")

Loc = tuple[int | str, ...]


class ConfigWarning(UserWarning):
    """A Stage_Config value that is allowed but worth a second look (for example O10)."""


class StrictModel(BaseModel):
    """Base for every Stage_Config model: unknown keys are errors and instances are immutable."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class FunctionId(StrEnum):
    COLLATERAL_IDENTITY = "collateral-identity"
    SAGAS_PAYMENTS = "sagas-payments"
    REPORTS = "reports"
    STELLAR_KYC = "stellar-kyc"
    TRADING_OPERATIONS = "trading-operations"
    SIGNER = "signer"


DomainFunctionId = Literal[
    "collateral-identity",
    "sagas-payments",
    "reports",
    "stellar-kyc",
    "trading-operations",
]
Kebab = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9-]{0,30}[a-z0-9]$")]
LogRetentionDays = Literal[
    1, 3, 5, 7, 14, 30, 60, 90, 120, 150, 180, 365, 400, 545, 731,
    1096, 1827, 2192, 2557, 2922, 3288, 3653,
]  # fmt: skip


def _fail(message: str) -> PydanticCustomError:
    """Build a validation error whose message is shown as is (no "Value error," prefix)."""
    return PydanticCustomError("config_error", "{message}", {"message": message})


def _check_cidr_prefix(network: IPv4Network) -> IPv4Network:
    if not 16 <= network.prefixlen <= 20:
        raise _fail(f"CIDR prefix length must be between /16 and /20, got /{network.prefixlen}")
    return network


def _check_https_origin(origin: str) -> str:
    if not is_https_origin(origin):
        raise _fail(
            f'"{origin}" is not an HTTPS origin; use https://host[:port] '
            "with no path, query or fragment"
        )
    return origin


def _check_local_dev_url(url: str) -> str:
    try:
        parts = urlsplit(url)
        _ = parts.port
    except ValueError as err:
        raise _fail(f'"{url}" is not a valid URL') from err
    if parts.scheme != "http" or parts.hostname not in LOCAL_DEV_HOSTS or parts.fragment:
        raise _fail(
            f'"{url}" must be an http://localhost or http://127.0.0.1 URL without a fragment'
        )
    return url


def _check_domain_prefix(prefix: str) -> str:
    for word in FORBIDDEN_DOMAIN_WORDS:
        if word in prefix:
            raise _fail(f'Hosted UI domain prefix "{prefix}" must not contain "{word}"')
    return prefix


class ArtifactReference(StrictModel):
    bucket: Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")]
    key: Annotated[str, StringConstraints(min_length=1)]
    version: str | None = None


class FunctionConfig(StrictModel):
    artifact: ArtifactReference
    handler: Annotated[str, StringConstraints(pattern=r"^[^:]+::[^:]+::[^:]+$")]
    memory_mb: Annotated[int, Field(ge=128, le=10240)]
    timeout_seconds: Annotated[int, Field(ge=1, le=900)]
    reserved_concurrency: Annotated[int, Field(ge=1)] | None = None
    uses_database: bool = False
    data_access: frozenset[Literal["idempotency-table", "saga-table", "documents-bucket"]] = (
        frozenset()
    )


class NetworkConfig(StrictModel):
    nat_gateways: Annotated[int, Field(ge=1, le=2)] = 1
    cidr: Annotated[IPv4Network, AfterValidator(_check_cidr_prefix)] = IPv4Network("10.40.0.0/16")
    availability_zones: Annotated[list[str], Field(min_length=2, max_length=2)]


class DatabaseConfig(StrictModel):
    engine_version: str
    instance_class: str
    allocated_storage_gb: Annotated[int, Field(ge=20, le=1000)]
    multi_az: bool
    backup_retention_days: Annotated[int, Field(ge=1, le=35)]
    database_name: str = "tokenization"
    app_user: str = "app_user"


class ScopeConfig(StrictModel):
    name: Kebab
    description: str


class ResourceServerConfig(StrictModel):
    identifier: Kebab
    scopes: Annotated[list[ScopeConfig], Field(min_length=1)]


class CognitoConfig(StrictModel):
    domain_prefix: Annotated[Kebab, AfterValidator(_check_domain_prefix)]
    groups: list[Kebab] = ["investor", "seller", "backoffice"]
    resource_server: ResourceServerConfig


class PortalConfig(StrictModel):
    origin: Annotated[str, AfterValidator(_check_https_origin)]
    callback_path: Annotated[str, StringConstraints(pattern=r"^/[^?#\s]*$")] = "/callback"
    logout_path: Annotated[str, StringConstraints(pattern=r"^/[^?#\s]*$")] = "/"
    scopes: list[str] = []
    local_dev_callback_urls: list[Annotated[str, AfterValidator(_check_local_dev_url)]] = []


class ApiConfig(StrictModel):
    throttling_rate_limit: Annotated[float, Field(gt=0)]
    throttling_burst_limit: Annotated[int, Field(ge=0)]
    route_scopes: dict[DomainFunctionId, list[str]]


class QueueConfig(StrictModel):
    name: Kebab
    consumer: DomainFunctionId
    max_receive_count: Annotated[int, Field(ge=1, le=1000)]
    visibility_timeout_seconds: Annotated[int, Field(ge=1, le=43200)]
    batch_size: Annotated[int, Field(ge=1, le=10)] = 10


class QueueTarget(StrictModel):
    type: Literal["queue"]
    queue: Kebab


class StateMachineTarget(StrictModel):
    type: Literal["state-machine"]


class LambdaTarget(StrictModel):
    type: Literal["lambda"]
    function: DomainFunctionId  # the Signer is not a valid target (6.7)


class RuleConfig(StrictModel):
    name: Kebab
    event_pattern: Annotated[dict[str, object], Field(min_length=1)]
    target: Annotated[QueueTarget | StateMachineTarget, Field(discriminator="type")]


class EventsConfig(StrictModel):
    archive_retention_days: Annotated[int, Field(ge=1)]


class SagaConfig(StrictModel):
    # Relative to the repository root (REPO_ROOT); checked in StageConfig._check_saga_definition.
    definition_file: Path


class SchedulerConfig(StrictModel):
    targets: list[
        Annotated[LambdaTarget | StateMachineTarget | QueueTarget, Field(discriminator="type")]
    ] = []


class ObservabilityConfig(StrictModel):
    log_retention_days: LogRetentionDays
    alarm_emails: Annotated[
        list[Annotated[str, StringConstraints(pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")]],
        Field(min_length=1),
    ]
    access_log_expiration_days: Annotated[int, Field(ge=1)]


class CicdConfig(StrictModel):
    github_owner: Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9-]{0,38}$")]
    github_repo: Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9._-]{1,100}$")]
    branch: Annotated[str, StringConstraints(min_length=1)] = "main"
    cdk_qualifier: Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9]{1,10}$")] = "hnb659fds"
    create_oidc_provider: bool = True


class StageConfig(StrictModel):
    stage: Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9]{1,9}$")]
    project: Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9-]{1,19}$")]
    account: Annotated[str, StringConstraints(pattern=r"^\d{12}$")]
    region: Annotated[str, StringConstraints(pattern=r"^[a-z]{2}(-[a-z]+)+-\d$")] = DEFAULT_REGION
    removal_policy: Literal["RETAIN", "DESTROY"]
    network: NetworkConfig
    lambda_runtime: Literal["dotnet8", "dotnet10"] = "dotnet8"
    lambda_architecture: Literal["arm64", "x86_64"] = "arm64"
    functions: dict[FunctionId, FunctionConfig]
    database: DatabaseConfig
    kyc_providers: Annotated[list[Kebab], Field(min_length=1)]
    cognito: CognitoConfig
    portal: PortalConfig
    api: ApiConfig
    events: EventsConfig
    queues: list[QueueConfig] = []
    rules: list[RuleConfig] = []
    saga: SagaConfig
    scheduler: SchedulerConfig = SchedulerConfig()
    observability: ObservabilityConfig
    cicd: CicdConfig

    @model_validator(mode="after")
    def _check_cross_field_rules(self, info: ValidationInfo) -> Self:
        """Run the checks that span several fields; report every issue at its own path."""
        issues: list[tuple[Loc, str]] = []

        def add(loc: Loc, message: str) -> None:
            issues.append((loc, message))

        expected_stage = info.context.get("stage") if isinstance(info.context, dict) else None
        if isinstance(expected_stage, str) and self.stage != expected_stage:
            add(("stage",), f'stage "{self.stage}" does not match the file name "{expected_stage}"')

        for i, zone in enumerate(self.network.availability_zones):
            if not zone.startswith(self.region):
                add(
                    ("network", "availability_zones", i),
                    f'Availability Zone "{zone}" is not in region "{self.region}"',
                )

        for fn in FunctionId:
            if fn not in self.functions:
                add(("functions", fn.value), f'function "{fn.value}" is required')
        signer = self.functions.get(FunctionId.SIGNER)
        if signer is not None:
            if signer.uses_database:
                add(("functions", "signer", "uses_database"), "the signer cannot use the database")
            if signer.data_access:
                add(("functions", "signer", "data_access"), "the signer cannot have data access")

        for fn, fn_cfg in self.functions.items():
            if (
                fn is not FunctionId.SIGNER
                and fn_cfg.timeout_seconds > API_INTEGRATION_TIMEOUT_SECONDS
            ):
                warnings.warn(
                    f"{self.stage}: functions.{fn.value}.timeout_seconds is "
                    f"{fn_cfg.timeout_seconds}, above the {API_INTEGRATION_TIMEOUT_SECONDS} s "
                    "API Gateway integration timeout; API calls to this function time out "
                    "after 29 s",
                    ConfigWarning,
                    stacklevel=2,
                )

        queue_names: set[str] = set()
        for i, queue in enumerate(self.queues):
            if queue.name in queue_names:
                add(("queues", i, "name"), f'queue name "{queue.name}" is not unique')
            queue_names.add(queue.name)
            consumer = self.functions.get(FunctionId(queue.consumer))
            if consumer is not None and queue.visibility_timeout_seconds < consumer.timeout_seconds:
                add(
                    ("queues", i, "visibility_timeout_seconds"),
                    f"visibility timeout {queue.visibility_timeout_seconds} s is below the "
                    f'timeout of consumer "{queue.consumer}" ({consumer.timeout_seconds} s)',
                )

        rule_names: set[str] = set()
        for i, rule in enumerate(self.rules):
            if rule.name in rule_names:
                add(("rules", i, "name"), f'rule name "{rule.name}" is not unique')
            rule_names.add(rule.name)
            if isinstance(rule.target, QueueTarget) and rule.target.queue not in queue_names:
                add(
                    ("rules", i, "target", "queue"),
                    f'queue "{rule.target.queue}" is not defined in queues',
                )

        for i, target in enumerate(self.scheduler.targets):
            if isinstance(target, QueueTarget) and target.queue not in queue_names:
                add(
                    ("scheduler", "targets", i, "queue"),
                    f'queue "{target.queue}" is not defined in queues',
                )
            # LambdaTarget.function excludes the signer, so raw YAML naming it already fails.

        server = self.cognito.resource_server
        scope_names = {scope.name for scope in server.scopes}
        full_scopes = {f"{server.identifier}/{name}" for name in scope_names}
        for fn_id, scopes in self.api.route_scopes.items():
            for j, scope in enumerate(scopes):
                if scope not in full_scopes:
                    add(
                        ("api", "route_scopes", fn_id, j),
                        f'scope "{scope}" is not defined in cognito.resource_server '
                        f'(expected "{server.identifier}/<scope>")',
                    )
        for i, name in enumerate(self.portal.scopes):
            if name not in scope_names:
                add(
                    ("portal", "scopes", i),
                    f'scope "{name}" is not defined in cognito.resource_server',
                )

        if not self.cognito.domain_prefix.startswith(f"{self.stage}-"):
            add(
                ("cognito", "domain_prefix"),
                f'Hosted UI domain prefix "{self.cognito.domain_prefix}" must start with '
                f'"{self.stage}-"',
            )

        saga_issue = self._check_saga_definition()
        if saga_issue is not None:
            add(("saga", "definition_file"), saga_issue)

        if issues:
            raise ValidationError.from_exception_data(
                type(self).__name__,
                [
                    InitErrorDetails(type=_fail(message), loc=loc, input=None)
                    for loc, message in issues
                ],
            )
        return self

    def _check_saga_definition(self) -> str | None:
        """Return the problem with ``saga.definition_file``, or None when it is acceptable.

        The file must exist (relative paths resolve against REPO_ROOT), hold valid JSON and use
        only the allowed placeholders.
        """
        # Imported here because core.saga imports DomainFunctionId from this module.
        from tokenization_infra.core.saga import SagaDefinitionError, referenced_functions

        path = REPO_ROOT / self.saga.definition_file
        if not path.is_file():
            return f'saga definition file "{self.saga.definition_file}" does not exist'
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as err:
            return f'saga definition file "{self.saga.definition_file}" cannot be read: {err}'
        try:
            json.loads(text)
        except ValueError as err:
            return f'saga definition file "{self.saga.definition_file}" is not valid JSON: {err}'
        try:
            referenced_functions(text)
        except SagaDefinitionError as err:
            return str(err)
        return None


def parse_stage_config(raw: Mapping[str, object], *, stage: str) -> StageConfig:
    """Validate ``raw`` as the Stage_Config for ``stage``.

    Raises ConfigError with one ``"{stage}: {path}: {reason}"`` line per issue.
    """
    try:
        return StageConfig.model_validate(raw, context={"stage": stage})
    except ValidationError as err:
        lines = [f"{stage}: {format_loc(e['loc'])}: {e['msg']}" for e in err.errors()]
        raise ConfigError("\n".join(lines)) from err


def resolve_stage(name: str, registry: Mapping[str, Mapping[str, object]]) -> StageConfig:
    """Return the validated Stage_Config named ``name`` from ``registry``."""
    if name not in registry:
        raise UnknownStageError(name, registry.keys())
    return parse_stage_config(registry[name], stage=name)


def load_stage(name: str, config_dir: Path) -> StageConfig:
    """Read ``<config_dir>/<name>.yaml`` and validate it."""
    known = sorted(path.stem for path in config_dir.glob("*.yaml")) if config_dir.is_dir() else []
    if name not in known:
        raise UnknownStageError(name, known)
    path = config_dir / f"{name}.yaml"
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as err:
        raise ConfigError(f"{name}: {path.name}: invalid YAML: {err}") from err
    if not isinstance(raw, dict):
        raise ConfigError(f"{name}: {path.name}: the file must contain a mapping at the top level")
    return resolve_stage(name, {name: raw})
