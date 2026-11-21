"""Deterministic, stage-prefixed resource names. Pure Python (no aws_cdk, no constructs).

Every generated name starts with ``{stage}-`` (``/{stage}/`` for log groups), uses only the
characters its kind allows and stays within the service limit for that kind. A name that would be
too long is shortened by cutting the base and appending a short hash of all inputs, so equal
inputs always give the same name and different inputs stay distinct. Bucket names keep the
``{account}-{region}`` suffix; if the name still does not fit, the project is cut as well.

CDK token strings (``${Token[...]}``) are accepted. Tokens are never sanitized or cut. An account
or region token is measured as its longest resolved value. A base that contains a token is not
shortened, because its resolved length is unknown.

The Hosted UI domain prefix is not generated here; ``core.config`` validates it.
"""

import hashlib
import re
from dataclasses import dataclass
from enum import StrEnum

from tokenization_infra.core.config import FunctionId

_TOKEN = re.compile(r"(\$\{Token\[[^\]]*\]\})")
_STAGE = re.compile(r"[a-z][a-z0-9-]*")
_HASH_LENGTH = 8
# Longest resolved lengths, used to measure account and region tokens.
_ACCOUNT_TOKEN_LENGTH = 12
_REGION_TOKEN_LENGTH = 14


class NameKind(StrEnum):
    GENERIC = "generic"
    BUCKET = "bucket"
    FUNCTION = "function"
    ROLE = "role"
    QUEUE = "queue"
    LOG_GROUP = "log-group"

    @property
    def max_length(self) -> int:
        return _RULES[self].max_length

    @property
    def allowed(self) -> re.Pattern[str]:
        """Full-match pattern for a complete name of this kind."""
        return re.compile(rf"[{_RULES[self].charset}]+")


@dataclass(frozen=True)
class _Rule:
    charset: str  # the inside of a regex character class
    max_length: int
    lowercase: bool = False


_RULES: dict[NameKind, _Rule] = {
    NameKind.GENERIC: _Rule("a-z0-9-", 128, lowercase=True),
    NameKind.BUCKET: _Rule("a-z0-9-", 63, lowercase=True),
    NameKind.FUNCTION: _Rule("A-Za-z0-9_-", 64),
    NameKind.ROLE: _Rule("A-Za-z0-9+=,.@_-", 64),
    NameKind.QUEUE: _Rule("A-Za-z0-9_-", 80),
    NameKind.LOG_GROUP: _Rule("A-Za-z0-9_/.#-", 512),
}


def _sanitize(value: str, rule: _Rule) -> str:
    """Replace each run of disallowed characters with ``-``, leaving tokens untouched."""
    invalid = re.compile(rf"[^{rule.charset}]+")
    pieces: list[str] = []
    for piece in _TOKEN.split(value):
        if _TOKEN.fullmatch(piece):
            pieces.append(piece)
            continue
        text = piece.lower() if rule.lowercase else piece
        pieces.append(invalid.sub("-", text))
    return "".join(pieces).strip("-")


def _compose(
    kind: NameKind,
    stage: str,
    project: str,
    base: str,
    digest: str,
    account: str,
    region: str,
) -> str:
    if kind is NameKind.LOG_GROUP:
        return f"/{stage}/" + "-".join(part for part in (base, digest) if part)
    parts = (
        (stage, project, base, digest, account, region)
        if kind is NameKind.BUCKET
        else (stage, base, digest)
    )
    return "-".join(part for part in parts if part)


def resource_name(
    stage: str,
    base: str,
    kind: NameKind,
    *,
    project: str = "",
    account: str = "",
    region: str = "",
) -> str:
    """Build the name of a resource of ``kind``.

    ``project``, ``account`` and ``region`` are used only for ``BUCKET``, whose shape is
    ``{stage}-{project}-{base}-{account}-{region}`` (empty parts are left out). The other kinds
    are ``{stage}-{base}``, and ``LOG_GROUP`` is ``/{stage}/{base}``.

    Raises ``ValueError`` for an invalid stage or when the name cannot fit the kind's limit.
    """
    rule = _RULES[kind]
    if not _STAGE.fullmatch(stage):
        raise ValueError(f'invalid stage "{stage}": use lowercase letters, digits and "-"')

    if kind is NameKind.BUCKET:
        project_part = _sanitize(project, rule)
        account_part, region_part = account, region
    else:
        project_part = account_part = region_part = ""
    base_part = _sanitize(base, rule)
    if kind is NameKind.LOG_GROUP:
        base_part = base_part.strip("/")

    # Slack between a token's text and the length it will have once resolved.
    slack = 0
    if _TOKEN.search(account_part):
        slack += _ACCOUNT_TOKEN_LENGTH - len(account_part)
    if _TOKEN.search(region_part):
        slack += _REGION_TOKEN_LENGTH - len(region_part)

    def build(project_value: str, base_value: str, digest: str) -> str:
        return _compose(kind, stage, project_value, base_value, digest, account_part, region_part)

    def fits(name: str) -> bool:
        return len(name) + slack <= rule.max_length

    name = build(project_part, base_part, "")
    if fits(name) or _TOKEN.search(base_part):
        return name

    seed = "\0".join((kind.value, stage, project, base, account, region))
    digest = hashlib.sha256(seed.encode()).hexdigest()[:_HASH_LENGTH]
    # Cut the base first, then the project, one character at a time. Inputs are short.
    while not fits(build(project_part, base_part, digest)) and base_part:
        base_part = base_part[:-1].rstrip("-/")
    while not fits(build(project_part, base_part, digest)) and project_part:
        project_part = project_part[:-1].rstrip("-")
    name = build(project_part, base_part, digest)
    if not fits(name):
        raise ValueError(
            f'cannot fit a {kind.value} name for stage "{stage}" in {rule.max_length} characters'
        )
    return name


def function_name(stage: str, fn: FunctionId) -> str:
    """Lambda function name: ``{stage}-{fn}``."""
    return resource_name(stage, fn.value, NameKind.FUNCTION)


def role_name(stage: str, fn: FunctionId) -> str:
    """Execution role name for a Lambda function: ``{stage}-{fn}-role``."""
    return resource_name(stage, f"{fn.value}-role", NameKind.ROLE)
