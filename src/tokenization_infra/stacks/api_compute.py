"""ApiComputeStack: the Lambda functions (Req. 6.x) and the REST Api in front of them (Req. 5.x)."""

from collections.abc import Mapping
from typing import Any

import aws_cdk as cdk
from aws_cdk import aws_apigateway as apigateway
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_kms as kms
from aws_cdk import aws_logs as logs
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_sqs as sqs
from constructs import Construct

from tokenization_infra.constructs.domain_function import DomainFunction, DomainFunctionResources
from tokenization_infra.constructs.extension_parameters import publish_extension_parameters
from tokenization_infra.constructs.log_retention import retention_days
from tokenization_infra.core.config import FunctionId, StageConfig
from tokenization_infra.core.naming import NameKind, function_name, resource_name
from tokenization_infra.core.parameters import ExtensionKey
from tokenization_infra.core.permissions import Capability, CapabilityKind, permission_profile
from tokenization_infra.stacks.data import DataStack
from tokenization_infra.stacks.identity import IdentityStack
from tokenization_infra.stacks.messaging import MessagingStack
from tokenization_infra.stacks.network import POSTGRES_PORT, NetworkStack
from tokenization_infra.stacks.security import SecurityStack

# Order matters: the Signer is built first because Stellar_KYC is granted invoke on it.
FUNCTION_ORDER: tuple[FunctionId, ...] = (
    FunctionId.SIGNER,
    FunctionId.COLLATERAL_IDENTITY,
    FunctionId.SAGAS_PAYMENTS,
    FunctionId.REPORTS,
    FunctionId.STELLAR_KYC,
    FunctionId.TRADING_OPERATIONS,
)

# ``data_access`` name to the environment variable that carries the resource name (6.8).
DATA_ENVIRONMENT: dict[str, str] = {
    "idempotency-table": "IDEMPOTENCY_TABLE",
    "saga-table": "SAGA_TABLE",
    "documents-bucket": "DOCUMENTS_BUCKET",
}


class ApiComputeStack(cdk.Stack):
    """Owns the Lambda functions and the REST Api in front of them.

    The stack consumes every other stack: it imports KMS keys by ARN (D2) so grants land in the
    function roles, and it only reads handles from producers, so the stack graph stays one-way.
    ``functions`` holds one ``DomainFunction`` per function id. The Api integrates the five
    Domain_Lambdas in the same stack, so ``LambdaIntegration`` adds its invoke permissions on
    the functions without a cross-stack reference. The Signer has no route.
    """

    data_key: kms.IKey
    signing_key: kms.IKey
    functions: dict[FunctionId, DomainFunction]
    documents_bucket_name: str
    api: apigateway.RestApi
    authorizer: apigateway.CognitoUserPoolsAuthorizer
    url: str
    kyc_webhook_method: apigateway.Method

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        cfg: StageConfig,
        network: NetworkStack,
        security: SecurityStack,
        data: DataStack,
        messaging: MessagingStack,
        identity: IdentityStack,
        **kwargs: Any,
    ):
        super().__init__(scope, construct_id, **kwargs)
        self._cfg = cfg
        self._data = data
        self._messaging = messaging
        self._security = security
        self._identity = identity

        # D2: import the keys by ARN so grants are written into the role's identity policy.
        self.data_key = kms.Key.from_key_arn(self, "DataKey", security.data_key_arn)
        self.signing_key = kms.Key.from_key_arn(self, "SigningKey", security.signing_key_arn)

        self.documents_bucket_name = data.documents_bucket_name
        self._documents_bucket = s3.Bucket.from_bucket_name(
            self, "DocumentsBucket", self.documents_bucket_name
        )

        self.functions = {}
        for fn in FUNCTION_ORDER:
            self.functions[fn] = self._create_function(fn, network)
        self._create_event_sources(cfg)
        self._create_api(cfg)

        if cfg.removal_policy == "RETAIN":
            self.termination_protection = True

    def _create_function(self, fn: FunctionId, network: NetworkStack) -> DomainFunction:
        cfg = self._cfg
        capabilities = permission_profile(cfg, fn)
        domain_function = DomainFunction(
            self,
            f"Function-{fn.value}",
            cfg=cfg,
            fn=fn,
            vpc=network.vpc,
            subnets=network.private_subnets,
            security_group=network.lambda_sg,
            environment=self._environment(fn, capabilities),
        )
        domain_function.apply_profile(capabilities, self._resources(domain_function, fn))
        return domain_function

    def _resources(
        self, domain_function: DomainFunction, fn: FunctionId
    ) -> DomainFunctionResources:
        """Handles for ``apply_profile``. The signer is this stack's own function (built first)."""
        data = self._data
        messaging = self._messaging
        signer = self.functions.get(FunctionId.SIGNER, domain_function)
        tables: Mapping[str, dynamodb.ITable] = {
            "idempotency-table": data.idempotency_table,
            "saga-table": data.saga_table,
        }
        # Imported by its plain-string name (D1), so the grant carries no cross-stack export.
        buckets: Mapping[str, s3.IBucket] = {
            "documents-bucket": self._documents_bucket,
        }
        queues: Mapping[str, sqs.IQueue] = {
            name: queue.queue for name, queue in messaging.queues.items()
        }
        return DomainFunctionResources(
            event_bus_arn=messaging.bus.event_bus_arn,
            saga_state_machine_arn=messaging.saga.state_machine_arn,
            data_key=self.data_key,
            signing_key_arn=self._security.signing_key_arn,
            signer_function_arn=signer.function.function_arn,
            kyc_secret_arns=self._kyc_secret_arns(),
            tables=tables,
            buckets=buckets,
            queues=queues,
            proxy=data.proxy,
            app_user=data.app_user,
        )

    def _kyc_secret_arns(self) -> list[str]:
        return [
            secret.secret_arn
            for secrets in self._security.kyc_secrets.values()
            for secret in secrets.values()
        ]

    def _environment(self, fn: FunctionId, capabilities: frozenset[Capability]) -> dict[str, str]:
        """Environment variables limited to the resources ``fn`` uses (6.8, 7.5).

        Everything is derived from the permission profile, so a variable exists exactly when the
        function holds the matching capability.
        """
        cfg = self._cfg
        data = self._data
        messaging = self._messaging
        if fn is FunctionId.SIGNER:
            return {
                "STAGE": cfg.stage,
                "SIGNING_KEY_ID": self.signing_key.key_id,
            }

        env: dict[str, str] = {
            "STAGE": cfg.stage,
            "EVENT_BUS_NAME": messaging.bus.event_bus_name,
            "ALLOWED_ORIGINS": ",".join(self._identity.cors_origins),
        }
        kinds = {capability.kind for capability in capabilities}
        resource_names: dict[str, str] = {
            "idempotency-table": data.idempotency_table.table_name,
            "saga-table": data.saga_table.table_name,
            "documents-bucket": data.documents_bucket_name,
        }
        consumed_queues: dict[str, str] = {}
        for capability in sorted(capabilities, key=str):
            if capability.kind is CapabilityKind.DATA:
                assert capability.resource is not None
                env[DATA_ENVIRONMENT[capability.resource]] = resource_names[capability.resource]
            elif capability.kind is CapabilityKind.QUEUE_CONSUME:
                assert capability.resource is not None
                consumed_queues[capability.resource] = messaging.queues[
                    capability.resource
                ].queue.queue_url

        if CapabilityKind.DB_CONNECT in kinds:
            env["DB_PROXY_ENDPOINT"] = data.proxy.endpoint
            env["DB_PORT"] = str(POSTGRES_PORT)
            env["DB_NAME"] = cfg.database.database_name
            env["DB_USER"] = data.app_user
        if CapabilityKind.SAGA_START in kinds:
            env["SAGA_STATE_MACHINE_ARN"] = messaging.saga.state_machine_arn
        if consumed_queues:
            env["QUEUE_URLS"] = self.to_json_string(consumed_queues)
        if CapabilityKind.KYC_SECRETS_READ in kinds:
            env["KYC_SECRET_ARNS"] = self.to_json_string(
                {
                    provider: {kind: secret.secret_arn for kind, secret in secrets.items()}
                    for provider, secrets in self._security.kyc_secrets.items()
                }
            )
        if CapabilityKind.SIGNER_INVOKE in kinds:
            env["SIGNER_FUNCTION_NAME"] = function_name(cfg.stage, FunctionId.SIGNER)
        return env

    def _create_event_sources(self, cfg: StageConfig) -> None:
        """One SQS event source mapping per Work_Queue on its consumer (9.8).

        The mapping is created with ``add_event_source_mapping`` instead of ``SqsEventSource``.
        The L2 event source adds its own ``grant_consume_messages``, including an unconditional
        ``kms:Decrypt`` on the Data_Key that would override the ``kms:ViaService`` condition
        written by ``DomainFunction.apply_profile``. The ``queue.consume`` capability already
        grants everything the mapping needs on the function role, so no resource policy outside
        this stack changes. ``report_batch_item_failures`` lets the handler return partial batch
        failures.
        """
        for queue in cfg.queues:
            consumer = self.functions[FunctionId(queue.consumer)]
            consumer.function.add_event_source_mapping(
                f"Queue-{queue.name}",
                event_source_arn=self._messaging.queues[queue.name].queue.queue_arn,
                batch_size=queue.batch_size,
                report_batch_item_failures=True,
            )

    def _create_api(self, cfg: StageConfig) -> None:
        """REST Api with Cognito scopes on every route and the KYC webhook left open (Req. 5)."""
        stage = cfg.stage
        retention = retention_days(cfg.observability.log_retention_days)
        removal_policy = cdk.RemovalPolicy[cfg.removal_policy]
        portal_origin = self._identity.portal_origin

        access_log_group = logs.LogGroup(
            self,
            "AccessLogGroup",
            log_group_name=resource_name(stage, "apigateway/access", NameKind.LOG_GROUP),
            retention=retention,
            removal_policy=removal_policy,
        )
        self.api = apigateway.RestApi(
            self,
            "Api",
            rest_api_name=f"{stage}-tokenization-api",
            endpoint_configuration=apigateway.EndpointConfiguration(
                types=[apigateway.EndpointType.REGIONAL]
            ),
            # O9: this creates the account-wide ``AWS::ApiGateway::Account`` setting (one
            # CloudWatch role per account and region). If another stack in the account already
            # manages it, set this to False and rely on the existing setting. RETAIN keeps the
            # role when this stack is deleted, because other APIs in the account may use it.
            cloud_watch_role=True,
            cloud_watch_role_removal_policy=cdk.RemovalPolicy.RETAIN,
            deploy_options=apigateway.StageOptions(
                stage_name=stage,
                access_log_destination=apigateway.LogGroupLogDestination(access_log_group),
                access_log_format=apigateway.AccessLogFormat.json_with_standard_fields(
                    caller=True,
                    http_method=True,
                    ip=True,
                    protocol=True,
                    request_time=True,
                    resource_path=True,
                    response_length=True,
                    status=True,
                    user=True,
                ),
                tracing_enabled=True,
                logging_level=apigateway.MethodLoggingLevel.ERROR,
                metrics_enabled=True,
                throttling_rate_limit=cfg.api.throttling_rate_limit,
                throttling_burst_limit=cfg.api.throttling_burst_limit,
            ),
            # 5.9: the origins come from the Portal_Origin plus local dev URLs, never ``*``.
            default_cors_preflight_options=apigateway.CorsOptions(
                allow_origins=list(self._identity.cors_origins),
                allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
                allow_headers=["Authorization", "Content-Type", "Idempotency-Key", "X-Amz-Date"],
            ),
        )

        # D10: create the execution log group first so it has retention before API Gateway
        # would create it without one. The name uses the RestApi id, so a recreated Api never
        # collides with a retained group.
        execution_log_group = logs.LogGroup(
            self,
            "ExecutionLogGroup",
            log_group_name=f"API-Gateway-Execution-Logs_{self.api.rest_api_id}/{stage}",
            retention=retention,
            removal_policy=removal_policy,
        )
        self.api.deployment_stage.node.add_dependency(execution_log_group)

        self._add_gateway_responses(portal_origin)

        validator = self.api.add_request_validator(
            "RequestValidator",
            request_validator_name=f"{stage}-body-and-parameters",
            validate_request_body=True,
            validate_request_parameters=True,
        )
        self.authorizer = apigateway.CognitoUserPoolsAuthorizer(
            self,
            "Authorizer",
            cognito_user_pools=[self._identity.user_pool],
            authorizer_name=f"{stage}-cognito",
        )
        self._add_domain_routes(cfg)
        self._add_kyc_webhook(validator)

        self.url = self.api.url
        publish_extension_parameters(
            self,
            cfg,
            {
                ExtensionKey.API_ID: self.api.rest_api_id,
                ExtensionKey.API_ROOT_RESOURCE_ID: self.api.rest_api_root_resource_id,
                ExtensionKey.API_BASE_URL: self.api.url,
            },
        )

    def _add_gateway_responses(self, portal_origin: str) -> None:
        """5.10, O7: CORS headers on API Gateway's own 4XX and 5XX responses.

        A gateway response header holds one static value, so only the Portal_Origin is allowed
        here. Local development origins get no CORS headers on these errors. Echoing the request
        origin would allow every origin. Header values are quoted literals, as API Gateway needs.
        """
        headers = {
            "Access-Control-Allow-Origin": f"'{portal_origin}'",
            "Vary": "'Origin'",
        }
        self.api.add_gateway_response(
            "Default4xx",
            type=apigateway.ResponseType.DEFAULT_4_XX,
            response_headers=headers,
        )
        self.api.add_gateway_response(
            "Default5xx",
            type=apigateway.ResponseType.DEFAULT_5_XX,
            response_headers=headers,
        )

    def _add_domain_routes(self, cfg: StageConfig) -> None:
        """``/{fn}`` and ``/{fn}/{proxy+}`` ``ANY`` per Domain_Lambda, with Cognito scopes (5.3)."""
        route_scopes: dict[str, list[str]] = {str(k): v for k, v in cfg.api.route_scopes.items()}
        for fn in FUNCTION_ORDER:
            if fn is FunctionId.SIGNER:
                continue
            scopes = route_scopes.get(fn.value)
            if not scopes:
                raise ValueError(f"api.route_scopes has no scopes for {fn.value}")
            integration = apigateway.LambdaIntegration(self.functions[fn].function, proxy=True)
            # Scopes are already full names (``{resource_server_identifier}/{scope}``);
            # StageConfig validation checks them against the Resource_Server.
            base = self.api.root.add_resource(fn.value)
            for resource in (base, base.add_resource("{proxy+}")):
                resource.add_method(
                    "ANY",
                    integration,
                    authorization_type=apigateway.AuthorizationType.COGNITO,
                    authorizer=self.authorizer,
                    authorization_scopes=list(scopes),
                )

    def _add_kyc_webhook(self, validator: apigateway.IRequestValidator) -> None:
        """``POST /webhooks/kyc/{provider}`` to Stellar_KYC, without Cognito (5.5).

        The provider calls this URL, so it cannot hold a Cognito token. The function verifies the
        provider's webhook signature (application code).
        """
        webhook = (
            self.api.root.add_resource("webhooks").add_resource("kyc").add_resource("{provider}")
        )
        self.kyc_webhook_method = webhook.add_method(
            "POST",
            apigateway.LambdaIntegration(
                self.functions[FunctionId.STELLAR_KYC].function, proxy=True
            ),
            authorization_type=apigateway.AuthorizationType.NONE,
            request_parameters={"method.request.path.provider": True},
            request_validator=validator,
        )
