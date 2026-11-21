"""CDK app entry point: resolves the stage from context and builds the stacks."""

import sys

import aws_cdk as cdk

from tokenization_infra.app_builder import build_app
from tokenization_infra.core.config import CONFIG_DIR, load_stage
from tokenization_infra.core.errors import ConfigError

app = cdk.App()
stage = str(app.node.try_get_context("stage") or "")
try:
    cfg = load_stage(stage, CONFIG_DIR)
except ConfigError as err:  # UnknownStageError is a subclass
    sys.exit(str(err))  # non-zero exit, message on stderr
build_app(app, cfg)
app.synth()
