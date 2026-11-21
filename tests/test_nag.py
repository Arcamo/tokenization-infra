"""cdk-nag guard tests (Requirements 12.6, 12.7).

cdk-nag v3 runs as a CDK validation plugin when the app synthesizes. A finding that is not
acknowledged makes ``app.synth()`` raise, so these tests synthesize the assembly explicitly
instead of relying on ``Template.from_stack``.
"""

import aws_cdk as cdk
import pytest
from aws_cdk import aws_s3 as s3

from synthesis import build_variant, new_app
from tokenization_infra.nag.acknowledgements import (
    MIN_REASON_LENGTH,
    acknowledge,
    register_nag,
)


def test_dev_app_synthesizes_with_no_unacknowledged_errors() -> None:
    """Requirement 12.6: AwsSolutionsChecks is registered and nothing is unacknowledged."""
    variant = build_variant("dev")
    variant.app.synth(force=True)  # raises when cdk-nag reports an unacknowledged error


def test_unencrypted_bucket_without_acknowledgement_fails_synthesis() -> None:
    """Requirement 12.7: a violation with no acknowledgement fails synthesis."""
    app = new_app()
    stack = cdk.Stack(app, "nag-fixture")
    s3.Bucket(stack, "Unencrypted", encryption=s3.BucketEncryption.UNENCRYPTED)
    register_nag(app)

    with pytest.raises(Exception, match=r"AwsSolutions-S"):
        app.synth(force=True)


def test_acknowledged_violation_passes_synthesis() -> None:
    """The failure above comes from the missing acknowledgement, not from the fixture itself."""
    app = new_app()
    stack = cdk.Stack(app, "nag-fixture")
    bucket = s3.Bucket(stack, "Unencrypted", encryption=s3.BucketEncryption.UNENCRYPTED)
    register_nag(app)
    for rule_id in ("AwsSolutions-S1", "AwsSolutions-S10"):
        acknowledge(bucket, rule_id, "Fixture bucket that only exists to exercise the nag test.")

    app.synth(force=True)


def test_acknowledge_rejects_a_short_reason() -> None:
    app = new_app()
    stack = cdk.Stack(app, "nag-fixture")
    short_reason = "x" * (MIN_REASON_LENGTH - 1)

    with pytest.raises(ValueError, match="AwsSolutions-S1"):
        acknowledge(stack, "AwsSolutions-S1", short_reason)
