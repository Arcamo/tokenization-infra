"""NetworkStack: VPC, NAT, endpoints, security groups and flow logs (Requirements 2.1 to 2.11)."""

from typing import Any

import aws_cdk as cdk
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_logs as logs
from constructs import Construct

from tokenization_infra.constructs.extension_parameters import publish_extension_parameters
from tokenization_infra.constructs.log_retention import retention_days
from tokenization_infra.core.config import StageConfig
from tokenization_infra.core.naming import NameKind, resource_name
from tokenization_infra.core.parameters import ExtensionKey

PUBLIC_GROUP = "public"
PRIVATE_GROUP = "private"
ISOLATED_GROUP = "isolated"

HTTPS_PORT = 443
POSTGRES_PORT = 5432

INTERFACE_ENDPOINTS: dict[str, ec2.InterfaceVpcEndpointAwsService] = {
    "SecretsManager": ec2.InterfaceVpcEndpointAwsService.SECRETS_MANAGER,
    "Kms": ec2.InterfaceVpcEndpointAwsService.KMS,
    "Sqs": ec2.InterfaceVpcEndpointAwsService.SQS,
    "StepFunctions": ec2.InterfaceVpcEndpointAwsService.STEP_FUNCTIONS,
    "EventBridge": ec2.InterfaceVpcEndpointAwsService.EVENTBRIDGE,
    "CloudWatchLogs": ec2.InterfaceVpcEndpointAwsService.CLOUDWATCH_LOGS,
    "XRay": ec2.InterfaceVpcEndpointAwsService.XRAY,
    "Sts": ec2.InterfaceVpcEndpointAwsService.STS,
}


class NetworkStack(cdk.Stack):
    """Owns the VPC and the security groups. It depends on no other stack."""

    vpc: ec2.Vpc
    private_subnets: ec2.SubnetSelection
    isolated_subnets: ec2.SubnetSelection
    endpoint_sg: ec2.SecurityGroup
    lambda_sg: ec2.SecurityGroup
    proxy_sg: ec2.SecurityGroup
    db_sg: ec2.SecurityGroup
    flow_log_group: logs.LogGroup

    def __init__(self, scope: Construct, construct_id: str, *, cfg: StageConfig, **kwargs: Any):
        # Set first: the Vpc reads ``availability_zones`` while it is constructed.
        self._configured_azs = list(cfg.network.availability_zones)
        super().__init__(scope, construct_id, **kwargs)
        stage = cfg.stage
        network = cfg.network

        self.private_subnets = ec2.SubnetSelection(subnet_group_name=PRIVATE_GROUP)
        self.isolated_subnets = ec2.SubnetSelection(subnet_group_name=ISOLATED_GROUP)

        self.vpc = ec2.Vpc(
            self,
            "Vpc",
            vpc_name=resource_name(stage, "vpc", NameKind.GENERIC),
            ip_addresses=ec2.IpAddresses.cidr(str(network.cidr)),
            # D8: explicit AZs keep synthesis free of context lookups.
            availability_zones=list(network.availability_zones),
            nat_gateways=network.nat_gateways,
            subnet_configuration=[
                ec2.SubnetConfiguration(
                    name=PUBLIC_GROUP, subnet_type=ec2.SubnetType.PUBLIC, cidr_mask=24
                ),
                ec2.SubnetConfiguration(
                    name=PRIVATE_GROUP,
                    subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS,
                    cidr_mask=22,
                ),
                ec2.SubnetConfiguration(
                    name=ISOLATED_GROUP,
                    subnet_type=ec2.SubnetType.PRIVATE_ISOLATED,
                    cidr_mask=24,
                ),
            ],
            # True would add a custom resource (Requirement 1.12).
            restrict_default_security_group=False,
        )

        self._create_gateway_endpoints()
        self._create_security_groups(str(network.cidr))
        self._create_interface_endpoints()
        self._create_flow_log(cfg)

        publish_extension_parameters(
            self,
            cfg,
            {
                ExtensionKey.VPC_ID: self.vpc.vpc_id,
                ExtensionKey.PRIVATE_SUBNET_IDS: self._subnet_ids(self.private_subnets),
                ExtensionKey.ISOLATED_SUBNET_IDS: self._subnet_ids(self.isolated_subnets),
                ExtensionKey.LAMBDA_SG_ID: self.lambda_sg.security_group_id,
            },
        )

        if cfg.removal_policy == "RETAIN":
            self.termination_protection = True

    @property
    def availability_zones(self) -> list[str]:
        """The Availability Zones from Stage_Config (D8).

        ``ec2.Vpc`` checks its ``availability_zones`` against the stack's. The default
        implementation runs an ``availability-zones`` context lookup, which needs AWS
        credentials at synthesis. Returning the configured list keeps synthesis offline.
        """
        return list(self._configured_azs)

    def _subnet_ids(self, selection: ec2.SubnetSelection) -> str:
        subnets = self.vpc.select_subnets(subnet_group_name=selection.subnet_group_name).subnets
        return cdk.Fn.join(",", [subnet.subnet_id for subnet in subnets])

    def _create_gateway_endpoints(self) -> None:
        # 2.5: S3 and DynamoDB, routed from the private and isolated route tables.
        subnets = [self.private_subnets, self.isolated_subnets]
        self.vpc.add_gateway_endpoint(
            "S3Endpoint", service=ec2.GatewayVpcEndpointAwsService.S3, subnets=subnets
        )
        self.vpc.add_gateway_endpoint(
            "DynamoDbEndpoint", service=ec2.GatewayVpcEndpointAwsService.DYNAMODB, subnets=subnets
        )

    def _create_security_groups(self, cidr: str) -> None:
        # The configured CIDR is a plain string, so cdk-nag can evaluate the rules (EC23).
        vpc_cidr = ec2.Peer.ipv4(cidr)

        # 2.10: endpoints accept HTTPS from inside the VPC only and never initiate traffic.
        self.endpoint_sg = self._security_group(
            "EndpointSg", "Interface VPC endpoints: HTTPS from the VPC CIDR"
        )
        self.endpoint_sg.add_ingress_rule(
            vpc_cidr, ec2.Port.tcp(HTTPS_PORT), "HTTPS from the VPC CIDR"
        )

        self.lambda_sg = self._security_group("LambdaSg", "Lambda functions in the VPC")
        self.proxy_sg = self._security_group("ProxySg", "RDS Proxy")
        self.db_sg = self._security_group("DbSg", "RDS PostgreSQL instance")

        # 2.7: Lambda egress is HTTPS to anywhere (Stellar, Anchor, KYC providers, NAT) and
        # PostgreSQL to the proxy. allow_to adds the matching proxy ingress rule (2.8).
        self.lambda_sg.add_egress_rule(
            ec2.Peer.any_ipv4(), ec2.Port.tcp(HTTPS_PORT), "HTTPS to any destination"
        )
        self.lambda_sg.connections.allow_to(
            self.proxy_sg, ec2.Port.tcp(POSTGRES_PORT), "PostgreSQL to the RDS Proxy"
        )

        # The proxy reaches the database (2.9 ingress comes from allow_to) and the Secrets
        # Manager endpoint inside the VPC.
        self.proxy_sg.connections.allow_to(
            self.db_sg, ec2.Port.tcp(POSTGRES_PORT), "PostgreSQL to the database"
        )
        self.proxy_sg.add_egress_rule(
            vpc_cidr, ec2.Port.tcp(HTTPS_PORT), "HTTPS to VPC endpoints (Secrets Manager)"
        )

    def _security_group(self, construct_id: str, description: str) -> ec2.SecurityGroup:
        return ec2.SecurityGroup(
            self,
            construct_id,
            vpc=self.vpc,
            description=description,
            allow_all_outbound=False,
        )

    def _create_interface_endpoints(self) -> None:
        # 2.6: private DNS (the default) makes the standard service names resolve to the
        # endpoints. open=False keeps the L2 from adding its own ingress rule; endpoint_sg
        # already has the only rule we want.
        for name, service in INTERFACE_ENDPOINTS.items():
            self.vpc.add_interface_endpoint(
                f"{name}Endpoint",
                service=service,
                subnets=self.private_subnets,
                security_groups=[self.endpoint_sg],
                private_dns_enabled=True,
                open=False,
            )

    def _create_flow_log(self, cfg: StageConfig) -> None:
        # 2.11: explicit log group so retention is set without the log retention custom resource.
        self.flow_log_group = logs.LogGroup(
            self,
            "FlowLogGroup",
            log_group_name=resource_name(cfg.stage, "vpc/flow-logs", NameKind.LOG_GROUP),
            retention=retention_days(cfg.observability.log_retention_days),
            removal_policy=cdk.RemovalPolicy[cfg.removal_policy],
        )
        self.vpc.add_flow_log(
            "FlowLog",
            destination=ec2.FlowLogDestination.to_cloud_watch_logs(self.flow_log_group),
            traffic_type=ec2.FlowLogTrafficType.ALL,
        )
