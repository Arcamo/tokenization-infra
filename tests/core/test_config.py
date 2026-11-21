"""Unit tests for Stage_Config loading and validation (requirements 1.4 to 1.9, 2.3, 4.8, 12.8).

Variants are built from the raw ``config/stages/dev.yaml`` mapping: a deep copy is mutated and
passed to ``parse_stage_config``.
"""

import copy
import warnings
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml

from tokenization_infra.core.config import (
    CONFIG_DIR,
    ConfigWarning,
    StageConfig,
    load_stage,
    parse_stage_config,
    resolve_stage,
)
from tokenization_infra.core.errors import ConfigError, UnknownStageError

Raw = dict[str, Any]
Mutation = Callable[[Raw], None]


@pytest.fixture(scope="module")
def dev_raw() -> Raw:
    raw = yaml.safe_load((CONFIG_DIR / "dev.yaml").read_text(encoding="utf-8"))
    assert isinstance(raw, dict)
    return raw


def parse(dev_raw: Raw, mutate: Mutation | None = None, *, stage: str = "dev") -> StageConfig:
    raw = copy.deepcopy(dev_raw)
    if mutate is not None:
        mutate(raw)
    return parse_stage_config(raw, stage=stage)


def error_lines(dev_raw: Raw, mutate: Mutation, *, stage: str = "dev") -> list[str]:
    """Return the lines of the ConfigError raised for the mutated config."""
    with pytest.raises(ConfigError) as excinfo:
        parse(dev_raw, mutate, stage=stage)
    return str(excinfo.value).splitlines()


def assert_fails_at(dev_raw: Raw, mutate: Mutation, path: str, reason: str = "") -> None:
    """Assert that one line starts with ``dev: <path>: `` and contains ``reason``."""
    lines = error_lines(dev_raw, mutate)
    prefix = f"dev: {path}: "
    assert any(line.startswith(prefix) and reason in line for line in lines), lines


# --- loading, defaults, unknown stage, required fields -------------------------------------


def test_load_stage_dev_succeeds() -> None:
    cfg = load_stage("dev", CONFIG_DIR)
    assert cfg.stage == "dev"
    assert cfg.region == "us-east-1"


def test_region_defaults_to_us_east_1_when_omitted(dev_raw: Raw) -> None:
    cfg = parse(dev_raw, lambda raw: raw.pop("region"))
    assert cfg.region == "us-east-1"


def test_unknown_stage_names_the_stage_and_lists_known_ones() -> None:
    with pytest.raises(UnknownStageError) as excinfo:
        load_stage("staging", CONFIG_DIR)
    assert excinfo.value.stage == "staging"
    assert str(excinfo.value) == 'Unknown stage "staging". Known stages: dev'


def test_resolve_stage_unknown_stage_raises() -> None:
    with pytest.raises(UnknownStageError, match='"qa"'):
        resolve_stage("qa", {})


def test_load_stage_rejects_non_mapping_yaml(tmp_path: Path) -> None:
    (tmp_path / "dev.yaml").write_text("- a\n- b\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="dev: dev.yaml: the file must contain a mapping"):
        load_stage("dev", tmp_path)


@pytest.mark.parametrize(
    ("mutate", "path"),
    [
        (lambda raw: raw.pop("portal"), "portal"),
        (lambda raw: raw["portal"].pop("origin"), "portal.origin"),
        (lambda raw: raw["database"].pop("instance_class"), "database.instance_class"),
        (lambda raw: raw["queues"][0].pop("consumer"), "queues[0].consumer"),
        (lambda raw: raw["functions"]["reports"].pop("handler"), "functions.reports.handler"),
    ],
)
def test_missing_required_field_names_its_dotted_path(
    dev_raw: Raw, mutate: Mutation, path: str
) -> None:
    assert_fails_at(dev_raw, mutate, path, "Field required")


def test_unknown_key_is_an_error(dev_raw: Raw) -> None:
    assert_fails_at(dev_raw, lambda raw: raw["network"].update(extra=1), "network.extra")


@pytest.mark.parametrize("nat_gateways", [0, 3])
def test_nat_gateways_outside_one_to_two_fail(dev_raw: Raw, nat_gateways: int) -> None:
    assert_fails_at(
        dev_raw,
        lambda raw: raw["network"].update(nat_gateways=nat_gateways),
        "network.nat_gateways",
    )


@pytest.mark.parametrize("nat_gateways", [1, 2])
def test_nat_gateways_one_and_two_are_accepted(dev_raw: Raw, nat_gateways: int) -> None:
    cfg = parse(dev_raw, lambda raw: raw["network"].update(nat_gateways=nat_gateways))
    assert cfg.network.nat_gateways == nat_gateways


def test_every_issue_is_reported_on_its_own_line(dev_raw: Raw) -> None:
    def mutate(raw: Raw) -> None:
        raw["network"]["nat_gateways"] = 0
        raw["database"].pop("instance_class")

    lines = error_lines(dev_raw, mutate)
    assert len(lines) == 2
    assert all(line.startswith("dev: ") for line in lines)


# --- cross-field rules ---------------------------------------------------------------------


def test_stage_must_equal_the_file_name(dev_raw: Raw) -> None:
    def mutate(raw: Raw) -> None:
        raw["stage"] = "prod"
        raw["cognito"]["domain_prefix"] = "prod-tokenization-portal"

    assert_fails_at(dev_raw, mutate, "stage", 'does not match the file name "dev"')


def test_availability_zones_must_start_with_the_region(dev_raw: Raw) -> None:
    assert_fails_at(
        dev_raw,
        lambda raw: raw["network"].update(availability_zones=["us-east-1a", "eu-west-1b"]),
        "network.availability_zones[1]",
        "eu-west-1b",
    )


def test_availability_zones_follow_a_configured_region(dev_raw: Raw) -> None:
    def mutate(raw: Raw) -> None:
        raw["region"] = "eu-west-1"

    # The dev zones are in us-east-1, so changing only the region must fail at both entries.
    lines = error_lines(dev_raw, mutate)
    assert any(line.startswith("dev: network.availability_zones[0]: ") for line in lines)
    assert any(line.startswith("dev: network.availability_zones[1]: ") for line in lines)


@pytest.mark.parametrize(
    "function_id",
    ["collateral-identity", "sagas-payments", "reports", "stellar-kyc", "trading-operations"]
    + ["signer"],
)
def test_all_six_function_keys_are_required(dev_raw: Raw, function_id: str) -> None:
    assert_fails_at(
        dev_raw,
        lambda raw: raw["functions"].pop(function_id),
        f"functions.{function_id}",
        "is required",
    )


def test_signer_cannot_use_the_database(dev_raw: Raw) -> None:
    assert_fails_at(
        dev_raw,
        lambda raw: raw["functions"]["signer"].update(uses_database=True),
        "functions.signer.uses_database",
    )


def test_signer_cannot_have_data_access(dev_raw: Raw) -> None:
    assert_fails_at(
        dev_raw,
        lambda raw: raw["functions"]["signer"].update(data_access=["saga-table"]),
        "functions.signer.data_access",
    )


def test_queue_names_must_be_unique(dev_raw: Raw) -> None:
    def mutate(raw: Raw) -> None:
        raw["queues"].append(copy.deepcopy(raw["queues"][0]))

    assert_fails_at(dev_raw, mutate, "queues[1].name", "not unique")


def test_rule_names_must_be_unique(dev_raw: Raw) -> None:
    def mutate(raw: Raw) -> None:
        raw["rules"].append(copy.deepcopy(raw["rules"][0]))

    assert_fails_at(dev_raw, mutate, "rules[1].name", "not unique")


def test_rule_queue_target_must_exist(dev_raw: Raw) -> None:
    assert_fails_at(
        dev_raw,
        lambda raw: raw["rules"][0]["target"].update(queue="missing"),
        "rules[0].target.queue",
        '"missing"',
    )


def test_scheduler_queue_target_must_exist(dev_raw: Raw) -> None:
    def mutate(raw: Raw) -> None:
        raw["scheduler"]["targets"].append({"type": "queue", "queue": "missing"})

    assert_fails_at(dev_raw, mutate, "scheduler.targets[1].queue", '"missing"')


def test_scheduler_queue_target_may_name_a_defined_queue(dev_raw: Raw) -> None:
    def mutate(raw: Raw) -> None:
        raw["scheduler"]["targets"].append({"type": "queue", "queue": "payments"})

    assert len(parse(dev_raw, mutate).scheduler.targets) == 2


def test_scheduler_cannot_target_the_signer(dev_raw: Raw) -> None:
    def mutate(raw: Raw) -> None:
        raw["scheduler"]["targets"].append({"type": "lambda", "function": "signer"})

    lines = error_lines(dev_raw, mutate)
    assert any("scheduler.targets[1]" in line and "function" in line for line in lines), lines


def test_queue_consumer_cannot_be_the_signer(dev_raw: Raw) -> None:
    assert_fails_at(
        dev_raw,
        lambda raw: raw["queues"][0].update(consumer="signer"),
        "queues[0].consumer",
    )


def test_visibility_timeout_must_cover_the_consumer_timeout(dev_raw: Raw) -> None:
    assert_fails_at(
        dev_raw,
        lambda raw: raw["queues"][0].update(visibility_timeout_seconds=28),
        "queues[0].visibility_timeout_seconds",
        "sagas-payments",
    )


def test_visibility_timeout_equal_to_the_consumer_timeout_is_accepted(dev_raw: Raw) -> None:
    cfg = parse(dev_raw, lambda raw: raw["queues"][0].update(visibility_timeout_seconds=29))
    assert cfg.queues[0].visibility_timeout_seconds == 29


def test_route_scopes_must_exist_in_the_resource_server(dev_raw: Raw) -> None:
    assert_fails_at(
        dev_raw,
        lambda raw: raw["api"]["route_scopes"].update(reports=["tokenization-api/missing"]),
        "api.route_scopes.reports[0]",
        "tokenization-api/missing",
    )


def test_route_scopes_must_use_the_resource_server_identifier(dev_raw: Raw) -> None:
    # "portal" exists as a scope name but the route must carry the "<identifier>/<scope>" form.
    assert_fails_at(
        dev_raw,
        lambda raw: raw["api"]["route_scopes"].update(reports=["portal"]),
        "api.route_scopes.reports[0]",
    )


def test_portal_scopes_must_exist_in_the_resource_server(dev_raw: Raw) -> None:
    assert_fails_at(
        dev_raw,
        lambda raw: raw["portal"].update(scopes=["portal", "admin"]),
        "portal.scopes[1]",
        '"admin"',
    )


@pytest.mark.parametrize(
    "origin",
    [
        "https://example.com/app",
        "https://example.com/",
        "https://example.com?x=1",
        "http://example.com",
        "example.com",
    ],
)
def test_portal_origin_must_be_an_https_origin_without_path(dev_raw: Raw, origin: str) -> None:
    assert_fails_at(
        dev_raw,
        lambda raw: raw["portal"].update(origin=origin),
        "portal.origin",
        "not an HTTPS origin",
    )


@pytest.mark.parametrize("origin", ["https://portal.example.com", "https://localhost:8443"])
def test_portal_origin_accepts_https_origins_with_or_without_a_port(
    dev_raw: Raw, origin: str
) -> None:
    assert parse(dev_raw, lambda raw: raw["portal"].update(origin=origin)).portal.origin == origin


@pytest.mark.parametrize(
    "url",
    [
        "https://localhost:5173/callback",
        "http://example.com/callback",
        "http://localhost:5173/callback#frag",
        "localhost:5173",
    ],
)
def test_local_dev_urls_must_be_http_localhost(dev_raw: Raw, url: str) -> None:
    assert_fails_at(
        dev_raw,
        lambda raw: raw["portal"].update(local_dev_callback_urls=["http://127.0.0.1:3000/", url]),
        "portal.local_dev_callback_urls[1]",
    )


def test_local_dev_urls_accept_localhost_and_loopback(dev_raw: Raw) -> None:
    urls = ["http://localhost:5173/callback", "http://127.0.0.1:3000/callback"]
    cfg = parse(dev_raw, lambda raw: raw["portal"].update(local_dev_callback_urls=urls))
    assert cfg.portal.local_dev_callback_urls == urls


@pytest.mark.parametrize("cidr", ["10.40.0.0/15", "10.40.0.0/21", "10.40.0.0/28"])
def test_cidr_prefix_outside_16_to_20_fails(dev_raw: Raw, cidr: str) -> None:
    assert_fails_at(
        dev_raw,
        lambda raw: raw["network"].update(cidr=cidr),
        "network.cidr",
        "between /16 and /20",
    )


@pytest.mark.parametrize("cidr", ["10.40.0.0/16", "10.40.0.0/20"])
def test_cidr_prefix_16_and_20_are_accepted(dev_raw: Raw, cidr: str) -> None:
    cfg = parse(dev_raw, lambda raw: raw["network"].update(cidr=cidr))
    assert str(cfg.network.cidr) == cidr


def test_cidr_with_host_bits_set_fails(dev_raw: Raw) -> None:
    assert_fails_at(dev_raw, lambda raw: raw["network"].update(cidr="10.40.1.1/16"), "network.cidr")


def test_domain_prefix_must_start_with_the_stage(dev_raw: Raw) -> None:
    assert_fails_at(
        dev_raw,
        lambda raw: raw["cognito"].update(domain_prefix="tokenization-portal"),
        "cognito.domain_prefix",
        '"dev-"',
    )


@pytest.mark.parametrize("prefix", ["dev-aws-portal", "dev-amazon-portal", "dev-cognito-portal"])
def test_domain_prefix_cannot_contain_reserved_words(dev_raw: Raw, prefix: str) -> None:
    assert_fails_at(
        dev_raw,
        lambda raw: raw["cognito"].update(domain_prefix=prefix),
        "cognito.domain_prefix",
        "must not contain",
    )


# --- saga definition -----------------------------------------------------------------------


def test_saga_definition_with_signer_placeholder_is_rejected(dev_raw: Raw, tmp_path: Path) -> None:
    saga = tmp_path / "saga.asl.json"
    saga.write_text('{"StartAt": "A", "Resource": "${SignerFunctionArn}"}', encoding="utf-8")
    assert_fails_at(
        dev_raw,
        lambda raw: raw["saga"].update(definition_file=str(saga)),
        "saga.definition_file",
        "${SignerFunctionArn}",
    )


def test_saga_definition_with_an_allowed_placeholder_is_accepted(
    dev_raw: Raw, tmp_path: Path
) -> None:
    saga = tmp_path / "saga.asl.json"
    saga.write_text('{"Resource": "${ReportsFunctionArn}"}', encoding="utf-8")
    cfg = parse(dev_raw, lambda raw: raw["saga"].update(definition_file=str(saga)))
    assert cfg.saga.definition_file == saga


def test_saga_definition_file_must_exist(dev_raw: Raw) -> None:
    assert_fails_at(
        dev_raw,
        lambda raw: raw["saga"].update(definition_file="config/statemachines/nope.json"),
        "saga.definition_file",
        "does not exist",
    )


def test_saga_definition_must_be_valid_json(dev_raw: Raw, tmp_path: Path) -> None:
    saga = tmp_path / "saga.asl.json"
    saga.write_text("{not json", encoding="utf-8")
    assert_fails_at(
        dev_raw,
        lambda raw: raw["saga"].update(definition_file=str(saga)),
        "saga.definition_file",
        "not valid JSON",
    )


# --- O10 timeout warning -------------------------------------------------------------------


def test_timeout_above_29_seconds_on_a_domain_function_warns(dev_raw: Raw) -> None:
    def mutate(raw: Raw) -> None:
        raw["functions"]["reports"]["timeout_seconds"] = 60

    with pytest.warns(ConfigWarning, match=r"functions\.reports\.timeout_seconds is 60"):
        cfg = parse(dev_raw, mutate)
    assert cfg.functions["reports"].timeout_seconds == 60  # type: ignore[index]


def test_dev_config_emits_no_warning(dev_raw: Raw) -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        parse(dev_raw)


def test_signer_timeout_above_29_seconds_does_not_warn(dev_raw: Raw) -> None:
    def mutate(raw: Raw) -> None:
        raw["functions"]["signer"]["timeout_seconds"] = 60

    with warnings.catch_warnings():
        warnings.simplefilter("error")
        parse(dev_raw, mutate)
