"""IdentityStack: User_Pool, groups, Resource_Server, Hosted UI domain, Portal_Client (Req. 4)."""

from typing import Any

import aws_cdk as cdk
from aws_cdk import aws_cognito as cognito
from constructs import Construct

from tokenization_infra.constructs.extension_parameters import publish_extension_parameters
from tokenization_infra.core.config import StageConfig
from tokenization_infra.core.parameters import ExtensionKey
from tokenization_infra.core.urls import callback_urls, cors_origins, logout_urls


class IdentityStack(cdk.Stack):
    """Owns the Cognito resources. It has no dependencies on other stacks.

    The Portal_App is an Amplify Hosting app created by hand (D4, Requirement 4.11), so this
    stack creates no Amplify resource. It reads the Portal_Origin from Stage_Config and derives
    the callback, logout and CORS URLs from it.
    """

    user_pool: cognito.UserPool
    groups: dict[str, cognito.CfnUserPoolGroup]
    resource_server: cognito.UserPoolResourceServer
    hosted_ui_domain: cognito.UserPoolDomain
    hosted_ui_domain_name: str
    portal_client: cognito.UserPoolClient
    portal_origin: str
    cors_origins: list[str]

    def __init__(self, scope: Construct, construct_id: str, *, cfg: StageConfig, **kwargs: Any):
        super().__init__(scope, construct_id, **kwargs)
        self._cfg = cfg
        removal_policy = cdk.RemovalPolicy[cfg.removal_policy]

        self._create_user_pool(cfg, removal_policy)
        self._create_groups(cfg)
        self._create_resource_server(cfg)
        self._create_hosted_ui_domain(cfg)

        self.portal_origin = cfg.portal.origin
        self.cors_origins = cors_origins(cfg.portal.origin, cfg.portal.local_dev_callback_urls)
        self._create_portal_client(cfg)

        publish_extension_parameters(
            self,
            cfg,
            {
                ExtensionKey.USER_POOL_ID: self.user_pool.user_pool_id,
                ExtensionKey.USER_POOL_ARN: self.user_pool.user_pool_arn,
                ExtensionKey.RESOURCE_SERVER_ID: cfg.cognito.resource_server.identifier,
                ExtensionKey.PORTAL_CLIENT_ID: self.portal_client.user_pool_client_id,
                ExtensionKey.HOSTED_UI_DOMAIN: self.hosted_ui_domain_name,
            },
        )

        if cfg.removal_policy == "RETAIN":
            self.termination_protection = True

    def _create_user_pool(self, cfg: StageConfig, removal_policy: cdk.RemovalPolicy) -> None:
        self.user_pool = cognito.UserPool(
            self,
            "UserPool",
            user_pool_name=f"{cfg.stage}-users",
            sign_in_aliases=cognito.SignInAliases(email=True),
            self_sign_up_enabled=True,
            auto_verify=cognito.AutoVerifiedAttrs(email=True),
            password_policy=cognito.PasswordPolicy(
                min_length=12,
                require_lowercase=True,
                require_uppercase=True,
                require_digits=True,
                require_symbols=True,
            ),
            # 4.2: MFA is optional and TOTP is the only second factor.
            mfa=cognito.Mfa.OPTIONAL,
            mfa_second_factor=cognito.MfaSecondFactor(otp=True, sms=False),
            account_recovery=cognito.AccountRecovery.EMAIL_ONLY,
            deletion_protection=cfg.removal_policy == "RETAIN",
            removal_policy=removal_policy,
        )

    def _create_groups(self, cfg: StageConfig) -> None:
        self.groups = {
            group: cognito.CfnUserPoolGroup(
                self,
                f"Group-{group}",
                user_pool_id=self.user_pool.user_pool_id,
                group_name=group,
            )
            for group in cfg.cognito.groups
        }

    def _create_resource_server(self, cfg: StageConfig) -> None:
        server = cfg.cognito.resource_server
        self._scopes = {
            scope.name: cognito.ResourceServerScope(
                scope_name=scope.name, scope_description=scope.description
            )
            for scope in server.scopes
        }
        self.resource_server = self.user_pool.add_resource_server(
            "ResourceServer",
            identifier=server.identifier,
            scopes=list(self._scopes.values()),
        )

    def _create_hosted_ui_domain(self, cfg: StageConfig) -> None:
        # D11: the prefix already carries the stage, so it is passed unchanged.
        prefix = cfg.cognito.domain_prefix
        self.hosted_ui_domain = self.user_pool.add_domain(
            "HostedUiDomain",
            cognito_domain=cognito.CognitoDomainOptions(domain_prefix=prefix),
        )
        self.hosted_ui_domain_name = f"{prefix}.auth.{cfg.region}.amazoncognito.com"

    def _create_portal_client(self, cfg: StageConfig) -> None:
        portal = cfg.portal
        resource_scopes = [
            cognito.OAuthScope.resource_server(self.resource_server, self._scopes[name])
            for name in portal.scopes
        ]
        self.portal_client = self.user_pool.add_client(
            "PortalClient",
            user_pool_client_name=f"{cfg.stage}-portal",
            generate_secret=False,
            o_auth=cognito.OAuthSettings(
                flows=cognito.OAuthFlows(authorization_code_grant=True),
                scopes=[
                    cognito.OAuthScope.OPENID,
                    cognito.OAuthScope.EMAIL,
                    cognito.OAuthScope.PROFILE,
                    *resource_scopes,
                ],
                callback_urls=callback_urls(
                    self.portal_origin, portal.callback_path, portal.local_dev_callback_urls
                ),
                logout_urls=logout_urls(
                    self.portal_origin, portal.logout_path, portal.local_dev_callback_urls
                ),
            ),
            # 4.4: no password or SRP flows; the code grant and refresh tokens are all it needs.
            auth_flows=cognito.AuthFlow(
                user_password=False,
                user_srp=False,
                admin_user_password=False,
                custom=False,
                user=False,
            ),
            prevent_user_existence_errors=True,
        )
