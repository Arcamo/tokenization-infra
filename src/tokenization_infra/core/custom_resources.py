"""Custom resource scan for synthesized templates. Pure Python (Requirement 1.12).

The allowlist is empty: the design allows no CloudFormation custom resources.
"""

from collections.abc import Mapping
from dataclasses import dataclass

MIN_REASON_LENGTH = 20


@dataclass(frozen=True)
class AllowlistEntry:
    stack: str
    logical_id_prefix: str
    reason: str

    def __post_init__(self) -> None:
        if len(self.reason.strip()) < MIN_REASON_LENGTH:
            raise ValueError(
                f"Allowlist reason for {self.stack}/{self.logical_id_prefix} "
                f"must have at least {MIN_REASON_LENGTH} characters"
            )


ALLOWLIST: tuple[AllowlistEntry, ...] = ()


_CUSTOM_TYPE = "AWS::CloudFormation::CustomResource"


def _is_custom_resource(resource_type: object) -> bool:
    return isinstance(resource_type, str) and (
        resource_type == _CUSTOM_TYPE or resource_type.startswith("Custom::")
    )


def find_custom_resources(template: Mapping[str, object]) -> list[str]:
    """Return logical ids of ``AWS::CloudFormation::CustomResource`` and ``Custom::*`` resources."""
    resources = template.get("Resources")
    if not isinstance(resources, Mapping):
        return []
    return [
        str(logical_id)
        for logical_id, body in resources.items()
        if isinstance(body, Mapping) and _is_custom_resource(body.get("Type"))
    ]
