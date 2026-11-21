"""Builds the CDK app: stage config in, stacks out (Requirements 1.4, 1.5, 1.8, 1.10, 1.11)."""

from dataclasses import dataclass

import aws_cdk as cdk

from tokenization_infra.core.config import StageConfig
from tokenization_infra.nag.acknowledgements import (
    apply_acknowledgements,
    apply_bootstrap_acknowledgements,
    register_nag,
)
from tokenization_infra.stacks.api_compute import ApiComputeStack
from tokenization_infra.stacks.bootstrap import BootstrapStack
from tokenization_infra.stacks.data import DataStack
from tokenization_infra.stacks.identity import IdentityStack
from tokenization_infra.stacks.messaging import MessagingStack
from tokenization_infra.stacks.network import NetworkStack
from tokenization_infra.stacks.observability import ObservabilityStack
from tokenization_infra.stacks.security import SecurityStack


@dataclass(frozen=True)
class StackSet:
    """The stacks of one stage, one field per stack.

    Each stack task adds its field here, a line in ``by_name`` and a line in ``build_app``.
    """

    security: SecurityStack
    network: NetworkStack
    data: DataStack
    messaging: MessagingStack
    identity: IdentityStack
    api_compute: ApiComputeStack
    observability: ObservabilityStack

    def by_name(self) -> dict[str, cdk.Stack]:
        """Map the short stack name (``security``, ``network``, ...) to the stack.

        ``apply_acknowledgements`` and the tests take this mapping.
        """
        return {
            "security": self.security,
            "network": self.network,
            "data": self.data,
            "messaging": self.messaging,
            "identity": self.identity,
            "api-compute": self.api_compute,
            "observability": self.observability,
        }


def build_bootstrap_app(app: cdk.App, cfg: StageConfig) -> BootstrapStack:
    """Create only ``{stage}-cicd-bootstrap`` in ``app``, tag it and register cdk-nag (D7).

    ``build_app`` never creates this stack, so ``cdk deploy --all`` in the pipeline cannot
    deploy it.
    """
    env = cdk.Environment(account=cfg.account, region=cfg.region)
    stack = BootstrapStack(app, f"{cfg.stage}-cicd-bootstrap", cfg=cfg, env=env)

    for key, value in {"project": cfg.project, "stage": cfg.stage, "managed-by": "cdk"}.items():
        cdk.Tags.of(app).add(key, value)

    register_nag(app)
    apply_bootstrap_acknowledgements(stack, cfg)
    return stack


def build_app(app: cdk.App, cfg: StageConfig) -> StackSet:
    """Create every stack of ``cfg.stage`` in ``app``, tag them and register cdk-nag."""
    env = cdk.Environment(account=cfg.account, region=cfg.region)
    security = SecurityStack(app, f"{cfg.stage}-security", cfg=cfg, env=env)
    network = NetworkStack(app, f"{cfg.stage}-network", cfg=cfg, env=env)
    data = DataStack(app, f"{cfg.stage}-data", cfg=cfg, network=network, security=security, env=env)
    messaging = MessagingStack(app, f"{cfg.stage}-messaging", cfg=cfg, security=security, env=env)
    identity = IdentityStack(app, f"{cfg.stage}-identity", cfg=cfg, env=env)
    api_compute = ApiComputeStack(
        app,
        f"{cfg.stage}-api-compute",
        cfg=cfg,
        network=network,
        security=security,
        data=data,
        messaging=messaging,
        identity=identity,
        env=env,
    )
    observability = ObservabilityStack(
        app,
        f"{cfg.stage}-observability",
        cfg=cfg,
        security=security,
        messaging=messaging,
        env=env,
    )
    stacks = StackSet(
        security=security,
        network=network,
        data=data,
        messaging=messaging,
        identity=identity,
        api_compute=api_compute,
        observability=observability,
    )

    for key, value in {"project": cfg.project, "stage": cfg.stage, "managed-by": "cdk"}.items():
        cdk.Tags.of(app).add(key, value)

    register_nag(app)
    apply_acknowledgements(stacks.by_name(), cfg)
    return stacks
