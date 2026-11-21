"""Synthesis helpers shared by the stack, custom-resource and nag tests.

``build_variant`` turns ``config/stages/dev.yaml`` plus overrides into a validated
``StageConfig`` (through ``parse_stage_config``), builds it in a fresh ``cdk.App`` and
synthesizes one ``Template`` per stack. The variants are the ones in the design's testing
section: dev, dev with ``removal_policy: RETAIN``, and dev with two NAT gateways in
``eu-west-1``.

Nothing here lists stacks. The templates come from ``StackSet.by_name()``, so a stack task
that adds a stack to ``StackSet`` is picked up by every test that uses these helpers.
"""

import copy
import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aws_cdk as cdk
import yaml
from aws_cdk.assertions import Template

from tokenization_infra.app_builder import StackSet, build_app
from tokenization_infra.core.config import CONFIG_DIR, StageConfig, parse_stage_config

REPO_ROOT = Path(__file__).resolve().parents[1]

Raw = dict[str, Any]
Override = Callable[[Raw], None]


def _retain(raw: Raw) -> None:
    raw["removal_policy"] = "RETAIN"


def _two_nat_gateways_in_eu_west_1(raw: Raw) -> None:
    raw["region"] = "eu-west-1"
    raw["network"]["nat_gateways"] = 2
    raw["network"]["availability_zones"] = ["eu-west-1a", "eu-west-1b"]


VARIANT_OVERRIDES: dict[str, Override] = {
    "dev": lambda raw: None,
    "dev-retain": _retain,
    "dev-two-nat-eu-west-1": _two_nat_gateways_in_eu_west_1,
}
"""Variant name to the mutation applied to a copy of the raw ``dev.yaml`` mapping."""


def load_raw_dev() -> Raw:
    raw = yaml.safe_load((CONFIG_DIR / "dev.yaml").read_text(encoding="utf-8"))
    assert isinstance(raw, dict)
    return raw


def cdk_json_context() -> dict[str, Any]:
    """The ``context`` of ``cdk.json``, so test apps run with the feature flags of the real app."""
    document = json.loads((REPO_ROOT / "cdk.json").read_text(encoding="utf-8"))
    context = document["context"]
    assert isinstance(context, dict)
    return context


def new_app() -> cdk.App:
    """A fresh app with the ``cdk.json`` context (every ``build_app`` registers nag on its app)."""
    return cdk.App(context=cdk_json_context())


@dataclass(frozen=True)
class SynthVariant:
    """One synthesized variant of the dev stage."""

    name: str
    cfg: StageConfig
    app: cdk.App
    stack_set: StackSet
    templates: dict[str, Template]
    """Template per stack id (``dev-security``, ...), for every stack in the ``StackSet``."""

    def stacks_by_name(self) -> dict[str, cdk.Stack]:
        return self.stack_set.by_name()

    def template(self, short_name: str) -> Template:
        """Template of the stack with the short name (``security``, ``network``, ...)."""
        return self.templates[self.stack_set.by_name()[short_name].node.id]


def build_variant(name: str) -> SynthVariant:
    """Build and synthesize the variant ``name`` in a fresh app."""
    raw = copy.deepcopy(load_raw_dev())
    VARIANT_OVERRIDES[name](raw)
    cfg = parse_stage_config(raw, stage="dev")
    app = new_app()
    stack_set = build_app(app, cfg)
    templates = {
        stack.node.id: Template.from_stack(stack) for stack in stack_set.by_name().values()
    }
    return SynthVariant(name=name, cfg=cfg, app=app, stack_set=stack_set, templates=templates)
