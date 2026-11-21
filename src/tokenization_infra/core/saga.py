"""Saga definition placeholders: validation and substitution. Pure Python (no aws_cdk)."""

import json
import re
from collections.abc import Mapping
from typing import Final

from tokenization_infra.core.config import DomainFunctionId
from tokenization_infra.core.errors import ConfigError

# Placeholder name -> Domain_Lambda. `${SignerFunctionArn}` is deliberately absent: signing stays
# behind stellar-kyc (6.7).
ALLOWED_PLACEHOLDERS: Final[Mapping[str, DomainFunctionId]] = {
    "CollateralIdentityFunctionArn": "collateral-identity",
    "SagasPaymentsFunctionArn": "sagas-payments",
    "ReportsFunctionArn": "reports",
    "StellarKycFunctionArn": "stellar-kyc",
    "TradingOperationsFunctionArn": "trading-operations",
}

# Matches every "${" in the text, up to the closing brace (or the end of the token if the brace
# is missing), so an unterminated "${Foo" cannot slip through.
_TOKEN = re.compile(r'\$\{[^}"\\\s]*\}?')


class SagaDefinitionError(ConfigError):
    """The saga definition holds a placeholder that is not allowed, or cannot be substituted."""


def _name_of(token: str) -> str | None:
    """Return the placeholder name of a well-formed ``${Name}`` token, else None."""
    return token[2:-1] if token.endswith("}") else None


def _check_tokens(asl_text: str) -> list[DomainFunctionId]:
    """Return the referenced functions in order of appearance; raise on any other token."""
    found: list[DomainFunctionId] = []
    for match in _TOKEN.finditer(asl_text):
        token = match.group(0)
        name = _name_of(token)
        if name is None or name not in ALLOWED_PLACEHOLDERS:
            allowed = ", ".join(f"${{{n}}}" for n in ALLOWED_PLACEHOLDERS)
            raise SagaDefinitionError(
                f'placeholder "{token}" is not allowed in the saga definition; '
                f"allowed placeholders: {allowed}"
            )
        found.append(ALLOWED_PLACEHOLDERS[name])
    return found


def referenced_functions(asl_text: str) -> frozenset[DomainFunctionId]:
    """Return the Domain_Lambdas that ``asl_text`` references through placeholders.

    Raises SagaDefinitionError naming the first placeholder that is not allowed.
    """
    return frozenset(_check_tokens(asl_text))


def substitute(asl_text: str, arns: Mapping[DomainFunctionId, str]) -> str:
    """Replace each allowed placeholder in ``asl_text`` with the ARN of its function.

    ``arns`` may be plain strings or CDK token strings. Raises SagaDefinitionError when a
    placeholder is not allowed, when ``arns`` lacks a referenced function, or when the result
    is not valid JSON. No placeholder survives: the template holds only allowed tokens (checked
    first) and each one is replaced. Substituted values are not scanned, because a CDK token
    string itself contains ``${``.
    """
    referenced = referenced_functions(asl_text)
    missing = sorted(referenced - arns.keys())
    if missing:
        raise SagaDefinitionError(f"no ARN given for referenced function(s): {', '.join(missing)}")

    def replace(match: re.Match[str]) -> str:
        return arns[ALLOWED_PLACEHOLDERS[match.group(0)[2:-1]]]

    result = _TOKEN.sub(replace, asl_text)
    try:
        json.loads(result)
    except ValueError as err:
        raise SagaDefinitionError(
            f"the substituted saga definition is not valid JSON: {err}"
        ) from err
    return result
