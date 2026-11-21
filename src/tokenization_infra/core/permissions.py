"""Permission profiles: which capabilities each Lambda function gets. Pure Python.

``permission_profile`` returns pure data. ``DomainFunction.apply_profile`` is the only place that
turns capabilities into IAM statements, so exclusivity rules can be tested without CDK.
"""

from dataclasses import dataclass
from enum import StrEnum

from tokenization_infra.core.config import FunctionId, StageConfig


class CapabilityKind(StrEnum):
    """The capability names used in the permission matrix of the design."""

    BUS_PUT = "bus.put"
    SAGA_START = "saga.start"
    SIGNER_INVOKE = "signer.invoke"
    SIGNING_KEY_GET_PUBLIC_KEY = "signing_key.get_public_key"
    SIGNING_KEY_SIGN = "signing_key.sign"
    KYC_SECRETS_READ = "kyc_secrets.read"
    DB_CONNECT = "db.connect"
    DATA = "data"
    QUEUE_CONSUME = "queue.consume"


# Kinds that carry a resource name (``data:<resource>`` and ``queue.consume:<queue>``).
PARAMETRISED_KINDS = frozenset({CapabilityKind.DATA, CapabilityKind.QUEUE_CONSUME})


@dataclass(frozen=True)
class Capability:
    """One permission a function holds.

    A frozen dataclass of ``(kind, resource)`` instead of a bare string, so consumers match on
    ``cap.kind`` and read ``cap.resource`` without parsing, while the value stays hashable for
    ``frozenset``. ``str(cap)`` gives the matrix notation: ``"bus.put"``,
    ``"data:documents-bucket"``, ``"queue.consume:<queue>"``.
    """

    kind: CapabilityKind
    resource: str | None = None

    def __post_init__(self) -> None:
        if (self.kind in PARAMETRISED_KINDS) != (self.resource is not None):
            expected = "requires" if self.kind in PARAMETRISED_KINDS else "does not take"
            raise ValueError(f'capability "{self.kind}" {expected} a resource')

    def __str__(self) -> str:
        return self.kind.value if self.resource is None else f"{self.kind.value}:{self.resource}"


BUS_PUT = Capability(CapabilityKind.BUS_PUT)
SAGA_START = Capability(CapabilityKind.SAGA_START)
SIGNER_INVOKE = Capability(CapabilityKind.SIGNER_INVOKE)
SIGNING_KEY_GET_PUBLIC_KEY = Capability(CapabilityKind.SIGNING_KEY_GET_PUBLIC_KEY)
SIGNING_KEY_SIGN = Capability(CapabilityKind.SIGNING_KEY_SIGN)
KYC_SECRETS_READ = Capability(CapabilityKind.KYC_SECRETS_READ)
DB_CONNECT = Capability(CapabilityKind.DB_CONNECT)


def data_access(resource: str) -> Capability:
    """Access to a data resource (``idempotency-table``, ``saga-table`` or ``documents-bucket``)."""
    return Capability(CapabilityKind.DATA, resource)


def queue_consume(queue: str) -> Capability:
    """Consume messages from the queue named ``queue`` (its Stage_Config name)."""
    return Capability(CapabilityKind.QUEUE_CONSUME, queue)


def permission_profile(cfg: StageConfig, fn: FunctionId) -> frozenset[Capability]:
    """Return the capabilities of function ``fn`` under the permission matrix of the design.

    The signer only signs and reads its public key. It never gets any other capability, whatever
    the configuration says.
    """
    if fn is FunctionId.SIGNER:
        return frozenset({SIGNING_KEY_GET_PUBLIC_KEY, SIGNING_KEY_SIGN})

    fn_cfg = cfg.functions[fn]
    capabilities: set[Capability] = {BUS_PUT}
    if fn is FunctionId.SAGAS_PAYMENTS:
        capabilities.add(SAGA_START)
    if fn is FunctionId.STELLAR_KYC:
        capabilities |= {SIGNER_INVOKE, SIGNING_KEY_GET_PUBLIC_KEY, KYC_SECRETS_READ}
    if fn_cfg.uses_database:
        capabilities.add(DB_CONNECT)
    capabilities |= {data_access(resource) for resource in fn_cfg.data_access}
    capabilities |= {queue_consume(q.name) for q in cfg.queues if q.consumer == fn.value}
    return frozenset(capabilities)
