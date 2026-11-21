"""CDK app entry point for the bootstrap (GitHub OIDC) stack only (design D7).

An admin deploys it once:
``npx cdk --app "uv run --frozen python bootstrap_app.py" deploy -c stage=dev``.
"""

import sys

import aws_cdk as cdk

from tokenization_infra.app_builder import build_bootstrap_app
from tokenization_infra.core.config import CONFIG_DIR, load_stage
from tokenization_infra.core.errors import ConfigError

app = cdk.App()
stage = str(app.node.try_get_context("stage") or "")
try:
    cfg = load_stage(stage, CONFIG_DIR)
except ConfigError as err:  # UnknownStageError is a subclass
    sys.exit(str(err))  # non-zero exit, message on stderr
build_bootstrap_app(app, cfg)
app.synth()
