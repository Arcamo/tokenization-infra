"""Custom resource guard tests (Requirements 1.12, 12.5).

The design allows no CloudFormation custom resources, so the allowlist is empty. A failure
names the stack and the logical id.
"""

from synthesis import SynthVariant
from tokenization_infra.core.custom_resources import (
    ALLOWLIST,
    MIN_REASON_LENGTH,
    AllowlistEntry,
    find_custom_resources,
)


def _allowed(stack_id: str, logical_id: str) -> bool:
    return any(
        stack_id.endswith(entry.stack) and logical_id.startswith(entry.logical_id_prefix)
        for entry in ALLOWLIST
    )


def test_no_custom_resources_beyond_the_allowlist(variant: SynthVariant) -> None:
    assert variant.templates, "the variant synthesized no stacks"
    offenders = [
        f"{stack_id}: {logical_id}"
        for stack_id, template in variant.templates.items()
        for logical_id in find_custom_resources(template.to_json())
        if not _allowed(stack_id, logical_id)
    ]
    assert offenders == [], f"custom resources in variant {variant.name}: {offenders}"


def test_every_allowlist_entry_has_a_reason() -> None:
    for entry in ALLOWLIST:
        assert len(entry.reason.strip()) >= MIN_REASON_LENGTH, entry


def test_allowlist_is_empty() -> None:
    """The product decision is no custom resources; adding one needs a design change."""
    assert ALLOWLIST == ()


def test_scan_reports_custom_resources_with_their_logical_ids() -> None:
    template = {
        "Resources": {
            "Fine": {"Type": "AWS::S3::Bucket"},
            "Bad1": {"Type": "Custom::Thing"},
            "Bad2": {"Type": "AWS::CloudFormation::CustomResource"},
        }
    }
    assert sorted(find_custom_resources(template)) == ["Bad1", "Bad2"]


def test_allowlist_entry_rejects_a_short_reason() -> None:
    try:
        AllowlistEntry(stack="security", logical_id_prefix="X", reason="too short")
    except ValueError as err:
        assert "security/X" in str(err)
    else:
        raise AssertionError("a short reason must be rejected")
