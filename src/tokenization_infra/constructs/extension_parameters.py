"""Helper that publishes Extension_Parameters as SSM String parameters."""

from collections.abc import Mapping

from aws_cdk import aws_ssm as ssm
from constructs import Construct

from tokenization_infra.core.config import StageConfig
from tokenization_infra.core.parameters import ExtensionKey, extension_parameter_names


def publish_extension_parameters(
    scope: Construct,
    cfg: StageConfig,
    values: Mapping[ExtensionKey, str],
) -> dict[ExtensionKey, ssm.StringParameter]:
    """Create one ``ssm.StringParameter`` per entry in ``values``.

    Parameter names come only from ``core.parameters.extension_parameter_names``. Each stack
    publishes the keys for the resources it owns. Values may be CDK tokens.
    """
    names = extension_parameter_names(cfg.project, cfg.stage)
    return {
        key: ssm.StringParameter(
            scope,
            f"ExtensionParameter-{key.value}",
            parameter_name=names[key],
            string_value=value,
            description=f"Extension_Parameter {key.value} for {cfg.project}/{cfg.stage}",
        )
        for key, value in values.items()
    }
