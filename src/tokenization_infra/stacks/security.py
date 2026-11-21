"""SecurityStack: Data_Key, Signing_Key and Kyc_Secrets (Requirements 3.1 to 3.7)."""

from typing import Any

import aws_cdk as cdk
from aws_cdk import aws_iam as iam
from aws_cdk import aws_kms as kms
from aws_cdk import aws_secretsmanager as secretsmanager
from constructs import Construct

from tokenization_infra.constructs.extension_parameters import publish_extension_parameters
from tokenization_infra.core.config import FunctionId, StageConfig
from tokenization_infra.core.naming import role_name
from tokenization_infra.core.parameters import ExtensionKey

KYC_SECRET_KINDS = ("api-credentials", "webhook-signing")


class SecurityStack(cdk.Stack):
    """Owns the KMS keys and the Kyc_Secrets. It depends on no other stack."""

    data_key: kms.Key
    signing_key: kms.Key
    data_key_arn: str
    signing_key_arn: str
    kyc_secrets: dict[str, dict[str, secretsmanager.Secret]]

    def __init__(self, scope: Construct, construct_id: str, *, cfg: StageConfig, **kwargs: Any):
        super().__init__(scope, construct_id, **kwargs)
        stage = cfg.stage

        # D1: the roles live in the api-compute stack, so their ARNs come from their fixed names.
        signer_role_arn = self._role_arn(role_name(stage, FunctionId.SIGNER))
        kyc_role_arn = self._role_arn(role_name(stage, FunctionId.STELLAR_KYC))

        self.data_key = self._create_data_key(stage)
        self.signing_key = self._create_signing_key(stage, signer_role_arn)
        self.data_key_arn = self.data_key.key_arn
        self.signing_key_arn = self.signing_key.key_arn

        self.kyc_secrets = {
            provider: {
                kind: self._create_kyc_secret(cfg, provider, kind, kyc_role_arn)
                for kind in KYC_SECRET_KINDS
            }
            for provider in cfg.kyc_providers
        }

        publish_extension_parameters(
            self,
            cfg,
            {
                ExtensionKey.DATA_KEY_ARN: self.data_key_arn,
                ExtensionKey.SIGNING_KEY_ARN: self.signing_key_arn,
            },
        )

        # Keys are always RETAIN (3.7); other resources follow the stage's removal policy.
        if cfg.removal_policy == "RETAIN":
            self.termination_protection = True

    def _role_arn(self, name: str) -> str:
        return self.format_arn(service="iam", region="", resource="role", resource_name=name)

    def _create_data_key(self, stage: str) -> kms.Key:
        key = kms.Key(
            self,
            "DataKey",
            alias=f"alias/{stage}-data",
            description=f"{stage}: encrypts data at rest",
            enable_key_rotation=True,
            removal_policy=cdk.RemovalPolicy.RETAIN,
        )
        # D2: service principals cannot use caller grants, so the key policy names them. The
        # SourceAccount condition keeps other accounts' resources from using the key.
        for sid, service in (
            ("AllowEventBridgeDelivery", "events.amazonaws.com"),
            ("AllowCloudWatchAlarmActions", "cloudwatch.amazonaws.com"),
        ):
            key.add_to_resource_policy(
                iam.PolicyStatement(
                    sid=sid,
                    effect=iam.Effect.ALLOW,
                    principals=[iam.ServicePrincipal(service)],
                    actions=["kms:Decrypt", "kms:GenerateDataKey*"],
                    resources=["*"],
                    conditions={"StringEquals": {"aws:SourceAccount": self.account}},
                )
            )
        return key

    def _create_signing_key(self, stage: str, signer_role_arn: str) -> kms.Key:
        key = kms.Key(
            self,
            "SigningKey",
            alias=f"alias/{stage}-stellar-signing",
            description=f"{stage}: signs Stellar transactions (Ed25519)",
            key_spec=kms.KeySpec.ECC_NIST_EDWARDS25519,
            key_usage=kms.KeyUsage.SIGN_VERIFY,
            removal_policy=cdk.RemovalPolicy.RETAIN,
        )
        # D6: backs up the identity-policy grant. Only the signer role may sign.
        key.add_to_resource_policy(
            iam.PolicyStatement(
                sid="DenySignExceptSigner",
                effect=iam.Effect.DENY,
                principals=[iam.AnyPrincipal()],
                actions=["kms:Sign"],
                resources=["*"],
                conditions={"StringNotEquals": {"aws:PrincipalArn": signer_role_arn}},
            )
        )
        return key

    def _create_kyc_secret(
        self, cfg: StageConfig, provider: str, kind: str, kyc_role_arn: str
    ) -> secretsmanager.Secret:
        secret = secretsmanager.Secret(
            self,
            f"KycSecret-{provider}-{kind}",
            secret_name=f"{cfg.stage}/kyc/{provider}/{kind}",
            description=f"{cfg.stage}: KYC provider {provider} {kind}; operators set the value",
            encryption_key=self.data_key,
            # Non-secret placeholder. Operators replace it after deployment (3.5).
            secret_string_value=cdk.SecretValue.unsafe_plain_text("{}"),
            removal_policy=cdk.RemovalPolicy[cfg.removal_policy],
        )
        # D6: backs up the identity-policy grant. Only the Stellar_KYC role may read the value.
        secret.add_to_resource_policy(
            iam.PolicyStatement(
                sid="DenyGetSecretValueExceptStellarKyc",
                effect=iam.Effect.DENY,
                principals=[iam.AnyPrincipal()],
                actions=["secretsmanager:GetSecretValue"],
                resources=["*"],
                conditions={"StringNotEquals": {"aws:PrincipalArn": kyc_role_arn}},
            )
        )
        return secret
