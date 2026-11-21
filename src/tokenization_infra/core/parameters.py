"""SSM Extension_Parameter names. Pure Python.

``extension_parameter_names`` is the single source for the parameter names that each stack
publishes and that later specs read (Requirements 11.1 to 11.7).
"""

from collections.abc import Mapping
from enum import StrEnum
from types import MappingProxyType


class ExtensionKey(StrEnum):
    VPC_ID = "VPC_ID"
    PRIVATE_SUBNET_IDS = "PRIVATE_SUBNET_IDS"
    ISOLATED_SUBNET_IDS = "ISOLATED_SUBNET_IDS"
    LAMBDA_SG_ID = "LAMBDA_SG_ID"
    API_ID = "API_ID"
    API_ROOT_RESOURCE_ID = "API_ROOT_RESOURCE_ID"
    API_BASE_URL = "API_BASE_URL"
    USER_POOL_ID = "USER_POOL_ID"
    USER_POOL_ARN = "USER_POOL_ARN"
    RESOURCE_SERVER_ID = "RESOURCE_SERVER_ID"
    PORTAL_CLIENT_ID = "PORTAL_CLIENT_ID"
    HOSTED_UI_DOMAIN = "HOSTED_UI_DOMAIN"
    DATA_KEY_ARN = "DATA_KEY_ARN"
    SIGNING_KEY_ARN = "SIGNING_KEY_ARN"
    EVENT_BUS_NAME = "EVENT_BUS_NAME"
    EVENT_BUS_ARN = "EVENT_BUS_ARN"
    ALERT_TOPIC_ARN = "ALERT_TOPIC_ARN"


_SUFFIXES: Mapping[ExtensionKey, str] = MappingProxyType(
    {
        ExtensionKey.VPC_ID: "network/vpc-id",
        ExtensionKey.PRIVATE_SUBNET_IDS: "network/private-subnet-ids",
        ExtensionKey.ISOLATED_SUBNET_IDS: "network/isolated-subnet-ids",
        ExtensionKey.LAMBDA_SG_ID: "network/lambda-security-group-id",
        ExtensionKey.API_ID: "api/id",
        ExtensionKey.API_ROOT_RESOURCE_ID: "api/root-resource-id",
        ExtensionKey.API_BASE_URL: "api/base-url",
        ExtensionKey.USER_POOL_ID: "identity/user-pool-id",
        ExtensionKey.USER_POOL_ARN: "identity/user-pool-arn",
        ExtensionKey.RESOURCE_SERVER_ID: "identity/resource-server-identifier",
        ExtensionKey.PORTAL_CLIENT_ID: "identity/portal-client-id",
        ExtensionKey.HOSTED_UI_DOMAIN: "identity/hosted-ui-domain",
        ExtensionKey.DATA_KEY_ARN: "keys/data-key-arn",
        ExtensionKey.SIGNING_KEY_ARN: "keys/signing-key-arn",
        ExtensionKey.EVENT_BUS_NAME: "events/bus-name",
        ExtensionKey.EVENT_BUS_ARN: "events/bus-arn",
        ExtensionKey.ALERT_TOPIC_ARN: "observability/alert-topic-arn",
    }
)


def extension_parameter_names(project: str, stage: str) -> Mapping[ExtensionKey, str]:
    """Return ``/{project}/{stage}/<suffix>`` for every ExtensionKey."""
    prefix = f"/{project}/{stage}/"
    return {key: f"{prefix}{suffix}" for key, suffix in _SUFFIXES.items()}
