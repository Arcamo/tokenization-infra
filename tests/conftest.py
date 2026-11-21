"""Shared pytest configuration: the hypothesis profile and the synthesized variants."""

import os

# Must run before the first aws_cdk import: the jsii runtime rejects a group-writable
# package cache (~/.cache/aws), which makes the import fail on some machines.
os.environ.setdefault("JSII_RUNTIME_PACKAGE_CACHE", "disabled")

import pytest  # noqa: E402
from hypothesis import settings  # noqa: E402

from synthesis import VARIANT_OVERRIDES, SynthVariant, build_variant  # noqa: E402

settings.register_profile("ci", max_examples=100, deadline=None)
settings.load_profile("ci")


@pytest.fixture(scope="session", params=list(VARIANT_OVERRIDES))
def variant(request: pytest.FixtureRequest) -> SynthVariant:
    """Each synthesized variant in turn (dev, dev-retain, dev-two-nat-eu-west-1).

    Tests that take this fixture run once per variant. Every variant is built once per
    session in its own app.
    """
    return _variant(str(request.param))


@pytest.fixture(scope="session")
def dev_variant() -> SynthVariant:
    return _variant("dev")


@pytest.fixture(scope="session")
def retain_variant() -> SynthVariant:
    return _variant("dev-retain")


@pytest.fixture(scope="session")
def two_nat_variant() -> SynthVariant:
    return _variant("dev-two-nat-eu-west-1")


_CACHE: dict[str, SynthVariant] = {}


def _variant(name: str) -> SynthVariant:
    """Build a variant once per session, whichever fixture asks first."""
    if name not in _CACHE:
        _CACHE[name] = build_variant(name)
    return _CACHE[name]
