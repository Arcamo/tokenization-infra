"""Configuration failures stop the build before any stack exists.

Requirements 1.5, 1.6, 2.3 and 12.8: an unknown stage, a missing required field and
``nat_gateways: 0`` each fail with a message that names the stage or the field, and
``app.py -c stage=nope`` exits non-zero and names the stage on stderr.
"""

import copy
import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

from synthesis import REPO_ROOT, load_raw_dev
from tokenization_infra.core.config import CONFIG_DIR, load_stage, parse_stage_config
from tokenization_infra.core.errors import ConfigError, UnknownStageError

SUBPROCESS_TIMEOUT_SECONDS = 300


def _without(raw: dict[str, Any], path: str) -> dict[str, Any]:
    """A deep copy of ``raw`` with the dotted ``path`` removed."""
    copied = copy.deepcopy(raw)
    *parents, leaf = path.split(".")
    node = copied
    for part in parents:
        node = node[part]
    del node[leaf]
    return copied


def _write_stage(config_dir: Path, name: str, raw: dict[str, Any]) -> None:
    (config_dir / f"{name}.yaml").write_text(yaml.safe_dump(raw), encoding="utf-8")


def test_unknown_stage_names_the_stage_and_the_known_stages() -> None:
    with pytest.raises(UnknownStageError) as excinfo:
        load_stage("nope", CONFIG_DIR)
    message = str(excinfo.value)
    assert '"nope"' in message
    assert "dev" in message


def test_unknown_stage_in_an_empty_config_dir_still_names_the_stage(tmp_path: Path) -> None:
    with pytest.raises(UnknownStageError, match="nope"):
        load_stage("nope", tmp_path)


@pytest.mark.parametrize(
    "path",
    [
        "portal.origin",
        "network.availability_zones",
        "database.instance_class",
        "cognito.resource_server",
        "removal_policy",
    ],
)
def test_missing_required_field_names_the_stage_and_the_field(path: str) -> None:
    raw = _without(load_raw_dev(), path)
    with pytest.raises(ConfigError) as excinfo:
        parse_stage_config(raw, stage="dev")
    lines = str(excinfo.value).splitlines()
    assert any(line.startswith(f"dev: {path}: ") for line in lines), lines


def test_missing_required_field_in_a_stage_file_fails_the_load(tmp_path: Path) -> None:
    _write_stage(tmp_path, "dev", _without(load_raw_dev(), "portal.origin"))
    with pytest.raises(ConfigError, match=r"dev: portal\.origin: "):
        load_stage("dev", tmp_path)


@pytest.mark.parametrize("nat_gateways", [0, 3])
def test_nat_gateways_outside_one_to_two_names_the_field(nat_gateways: int) -> None:
    raw = copy.deepcopy(load_raw_dev())
    raw["network"]["nat_gateways"] = nat_gateways
    with pytest.raises(ConfigError) as excinfo:
        parse_stage_config(raw, stage="dev")
    lines = str(excinfo.value).splitlines()
    assert any(line.startswith("dev: network.nat_gateways: ") for line in lines), lines


def test_nat_gateways_zero_in_a_stage_file_fails_the_load(tmp_path: Path) -> None:
    raw = copy.deepcopy(load_raw_dev())
    raw["network"]["nat_gateways"] = 0
    _write_stage(tmp_path, "dev", raw)
    with pytest.raises(ConfigError, match=r"dev: network\.nat_gateways: "):
        load_stage("dev", tmp_path)


def test_app_py_with_an_unknown_stage_exits_non_zero_and_names_the_stage() -> None:
    # `python app.py` ignores `-c`; the CDK CLI passes context to the app in CDK_CONTEXT_JSON.
    env = {**os.environ, "CDK_CONTEXT_JSON": json.dumps({"stage": "nope"})}
    result = subprocess.run(
        ["uv", "run", "--frozen", "python", "app.py"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=SUBPROCESS_TIMEOUT_SECONDS,
        check=False,
    )
    assert result.returncode != 0
    assert "nope" in result.stderr
