"""NetworkStack assertion tests (Requirements 2.1, 2.2, 2.4 to 2.11, 11.2, 12.1).

Assertions are on the properties the requirements name, against the templates synthesized once
per session for each variant (dev, dev with ``removal_policy: RETAIN``, two NAT gateways).
"""

from typing import Any

from aws_cdk.assertions import Template

from synthesis import SynthVariant
from tokenization_infra.core.parameters import ExtensionKey, extension_parameter_names

Resources = dict[str, dict[str, Any]]
Rule = tuple[str, int, int, str, str | None]
"""(protocol, from port, to port, peer, description). The peer is a CIDR or an SG role."""

SUBNET_GROUPS = {"public": 24, "private": 22, "isolated": 24}
AZ_COUNT = 2
GATEWAY_ENDPOINT_SERVICES = {"s3", "dynamodb"}
INTERFACE_ENDPOINT_SERVICES = {
    "secretsmanager",
    "kms",
    "sqs",
    "states",
    "events",
    "logs",
    "xray",
    "sts",
}
SG_DESCRIPTIONS = {
    "endpoint": "Interface VPC endpoints: HTTPS from the VPC CIDR",
    "lambda": "Lambda functions in the VPC",
    "proxy": "RDS Proxy",
    "db": "RDS PostgreSQL instance",
}
# CDK adds this rule to a security group created with allow_all_outbound=False and no egress
# rule. The protocol and ports are placeholders (ICMP 252 to 86) that match no real traffic.
PLACEHOLDER_EGRESS: Rule = ("icmp", 252, 86, "255.255.255.255/32", "Disallow all traffic")


def _resources(template: Template, resource_type: str) -> Resources:
    found = template.find_resources(resource_type)
    return {logical_id: dict(resource) for logical_id, resource in found.items()}


def _ref(value: Any) -> str:
    """Logical id behind ``{"Ref": id}`` or ``{"Fn::GetAtt": [id, attribute]}``."""
    assert isinstance(value, dict), value
    if "Ref" in value:
        return str(value["Ref"])
    return str(value["Fn::GetAtt"][0])


def _tag(resource: dict[str, Any], key: str) -> str:
    tags = {tag["Key"]: tag["Value"] for tag in resource["Properties"]["Tags"]}
    return str(tags[key])


def _service_suffix(service_name: Any, region: str) -> str:
    """``s3`` from ``com.amazonaws.<region>.s3``, whether the region is literal or a Ref."""
    if isinstance(service_name, dict):
        parts = service_name["Fn::Join"][1]
        service_name = "".join(
            f"{region}" if isinstance(part, dict) else str(part) for part in parts
        )
    prefix = f"com.amazonaws.{region}."
    assert isinstance(service_name, str) and service_name.startswith(prefix), service_name
    return service_name.removeprefix(prefix)


def _subnets_by_group(template: Template) -> dict[str, dict[str, dict[str, Any]]]:
    groups: dict[str, dict[str, dict[str, Any]]] = {name: {} for name in SUBNET_GROUPS}
    for logical_id, subnet in _resources(template, "AWS::EC2::Subnet").items():
        groups[_tag(subnet, "aws-cdk:subnet-name")][logical_id] = subnet
    return groups


def _route_table_of(template: Template, subnet_id: str) -> str:
    associations = _resources(template, "AWS::EC2::SubnetRouteTableAssociation")
    tables = [
        _ref(assoc["Properties"]["RouteTableId"])
        for assoc in associations.values()
        if _ref(assoc["Properties"]["SubnetId"]) == subnet_id
    ]
    assert len(tables) == 1, f"subnet {subnet_id} has route tables {tables}"
    return tables[0]


def _default_routes(template: Template, route_table_id: str) -> list[dict[str, Any]]:
    routes = _resources(template, "AWS::EC2::Route")
    return [
        route["Properties"]
        for route in routes.values()
        if _ref(route["Properties"]["RouteTableId"]) == route_table_id
    ]


def _security_group_roles(template: Template) -> dict[str, str]:
    """Logical id of each security group, keyed back to its role (endpoint, lambda, ...)."""
    by_description = {description: role for role, description in SG_DESCRIPTIONS.items()}
    groups = _resources(template, "AWS::EC2::SecurityGroup")
    roles = {
        logical_id: by_description[group["Properties"]["GroupDescription"]]
        for logical_id, group in groups.items()
    }
    assert sorted(roles.values()) == sorted(SG_DESCRIPTIONS), roles
    return roles


def _rule(properties: dict[str, Any], roles: dict[str, str], peer_key: str) -> Rule:
    if "CidrIp" in properties:
        peer = str(properties["CidrIp"])
    else:
        peer = roles[_ref(properties[peer_key])]
    return (
        str(properties["IpProtocol"]),
        int(properties["FromPort"]),
        int(properties["ToPort"]),
        peer,
        properties.get("Description"),
    )


def _security_group_rules(template: Template) -> dict[str, dict[str, set[Rule]]]:
    """Ingress and egress rules per security group role, inline and standalone together."""
    roles = _security_group_roles(template)
    rules: dict[str, dict[str, set[Rule]]] = {
        role: {"ingress": set(), "egress": set()} for role in roles.values()
    }
    for logical_id, group in _resources(template, "AWS::EC2::SecurityGroup").items():
        properties = group["Properties"]
        for inline in properties.get("SecurityGroupIngress", []):
            rules[roles[logical_id]]["ingress"].add(_rule(inline, roles, "SourceSecurityGroupId"))
        for inline in properties.get("SecurityGroupEgress", []):
            rules[roles[logical_id]]["egress"].add(
                _rule(inline, roles, "DestinationSecurityGroupId")
            )
    for ingress in _resources(template, "AWS::EC2::SecurityGroupIngress").values():
        properties = ingress["Properties"]
        role = roles[_ref(properties["GroupId"])]
        rules[role]["ingress"].add(_rule(properties, roles, "SourceSecurityGroupId"))
    for egress in _resources(template, "AWS::EC2::SecurityGroupEgress").values():
        properties = egress["Properties"]
        role = roles[_ref(properties["GroupId"])]
        rules[role]["egress"].add(_rule(properties, roles, "DestinationSecurityGroupId"))
    return rules


def test_six_subnets_two_per_group_across_the_configured_azs(variant: SynthVariant) -> None:
    """2.1: public, private and isolated subnets in each of the 2 configured AZs."""
    template = variant.template("network")
    template.resource_count_is("AWS::EC2::Subnet", len(SUBNET_GROUPS) * AZ_COUNT)
    expected_azs = sorted(variant.cfg.network.availability_zones)
    assert len(expected_azs) == AZ_COUNT
    for group, subnets in _subnets_by_group(template).items():
        assert len(subnets) == AZ_COUNT, f"{group}: {list(subnets)}"
        azs = sorted(subnet["Properties"]["AvailabilityZone"] for subnet in subnets.values())
        assert azs == expected_azs, f"{group} subnets are in {azs}"
        for subnet in subnets.values():
            mask = int(subnet["Properties"]["CidrBlock"].rsplit("/", 1)[1])
            assert mask == SUBNET_GROUPS[group], f"{group} subnet has /{mask}"
            assert subnet["Properties"].get("MapPublicIpOnLaunch", False) is (group == "public")


def test_vpc_uses_the_configured_cidr(variant: SynthVariant) -> None:
    variant.template("network").has_resource_properties(
        "AWS::EC2::VPC",
        {"CidrBlock": str(variant.cfg.network.cidr), "EnableDnsHostnames": True},
    )


def test_nat_gateway_count_follows_the_configuration(variant: SynthVariant) -> None:
    """2.2: one NAT gateway in dev, two in the multi-NAT variant."""
    template = variant.template("network")
    expected = variant.cfg.network.nat_gateways
    template.resource_count_is("AWS::EC2::NatGateway", expected)
    template.resource_count_is("AWS::EC2::EIP", expected)


def test_dev_has_one_nat_gateway(dev_variant: SynthVariant) -> None:
    dev_variant.template("network").resource_count_is("AWS::EC2::NatGateway", 1)


def test_two_nat_variant_has_one_nat_gateway_per_az(two_nat_variant: SynthVariant) -> None:
    template = two_nat_variant.template("network")
    template.resource_count_is("AWS::EC2::NatGateway", 2)
    public = _subnets_by_group(template)["public"]
    nat_subnets = {
        _ref(nat["Properties"]["SubnetId"])
        for nat in _resources(template, "AWS::EC2::NatGateway").values()
    }
    assert nat_subnets == set(public), "each public subnet hosts exactly one NAT gateway"


def test_private_default_routes_go_to_a_nat_gateway(variant: SynthVariant) -> None:
    """2.4: every private subnet has one 0.0.0.0/0 route and it targets a NAT gateway."""
    template = variant.template("network")
    nat_gateways = set(_resources(template, "AWS::EC2::NatGateway"))
    used: set[str] = set()
    for subnet_id in _subnets_by_group(template)["private"]:
        routes = _default_routes(template, _route_table_of(template, subnet_id))
        assert len(routes) == 1, f"{subnet_id}: {routes}"
        route = routes[0]
        assert route["DestinationCidrBlock"] == "0.0.0.0/0"
        assert "GatewayId" not in route
        nat_id = _ref(route["NatGatewayId"])
        assert nat_id in nat_gateways
        used.add(nat_id)
    # One NAT is shared by both private subnets, two NATs serve one private subnet each.
    assert len(used) == variant.cfg.network.nat_gateways


def test_public_subnets_route_to_the_internet_gateway_and_isolated_have_no_default_route(
    variant: SynthVariant,
) -> None:
    template = variant.template("network")
    subnets = _subnets_by_group(template)
    for subnet_id in subnets["public"]:
        routes = _default_routes(template, _route_table_of(template, subnet_id))
        assert [route["DestinationCidrBlock"] for route in routes] == ["0.0.0.0/0"]
        assert "GatewayId" in routes[0]
    for subnet_id in subnets["isolated"]:
        assert _default_routes(template, _route_table_of(template, subnet_id)) == []


def test_gateway_endpoints_for_s3_and_dynamodb(variant: SynthVariant) -> None:
    """2.5: S3 and DynamoDB gateway endpoints, routed from the private and isolated tables."""
    template = variant.template("network")
    endpoints = [
        endpoint["Properties"]
        for endpoint in _resources(template, "AWS::EC2::VPCEndpoint").values()
        if endpoint["Properties"]["VpcEndpointType"] == "Gateway"
    ]
    services = [_service_suffix(e["ServiceName"], variant.cfg.region) for e in endpoints]
    assert sorted(services) == sorted(GATEWAY_ENDPOINT_SERVICES)
    subnets = _subnets_by_group(template)
    expected_tables = {
        _route_table_of(template, subnet_id)
        for group in ("private", "isolated")
        for subnet_id in subnets[group]
    }
    for endpoint in endpoints:
        assert {_ref(table) for table in endpoint["RouteTableIds"]} == expected_tables


def test_interface_endpoints_for_the_eight_services_in_private_subnets(
    variant: SynthVariant,
) -> None:
    """2.6: eight interface endpoints with private DNS, in the private subnets, on endpoint_sg."""
    template = variant.template("network")
    endpoints = [
        endpoint["Properties"]
        for endpoint in _resources(template, "AWS::EC2::VPCEndpoint").values()
        if endpoint["Properties"]["VpcEndpointType"] == "Interface"
    ]
    services = [_service_suffix(e["ServiceName"], variant.cfg.region) for e in endpoints]
    assert sorted(services) == sorted(INTERFACE_ENDPOINT_SERVICES)
    private_subnets = set(_subnets_by_group(template)["private"])
    endpoint_sg = next(
        logical_id
        for logical_id, role in _security_group_roles(template).items()
        if role == "endpoint"
    )
    for endpoint in endpoints:
        assert endpoint["PrivateDnsEnabled"] is True
        assert {_ref(subnet) for subnet in endpoint["SubnetIds"]} == private_subnets
        assert [_ref(sg) for sg in endpoint["SecurityGroupIds"]] == [endpoint_sg]


def test_lambda_security_group_egress_is_https_anywhere_and_postgres_to_the_proxy(
    variant: SynthVariant,
) -> None:
    """2.7: no ingress, outbound TCP 443 to 0.0.0.0/0 and TCP 5432 to the proxy group only."""
    rules = _security_group_rules(variant.template("network"))["lambda"]
    assert rules["ingress"] == set()
    assert rules["egress"] == {
        ("tcp", 443, 443, "0.0.0.0/0", "HTTPS to any destination"),
        ("tcp", 5432, 5432, "proxy", "PostgreSQL to the RDS Proxy"),
    }


def test_proxy_security_group_accepts_postgres_only_from_the_lambda_group(
    variant: SynthVariant,
) -> None:
    """2.8: inbound TCP 5432 only from the Lambda security group."""
    rules = _security_group_rules(variant.template("network"))["proxy"]
    assert rules["ingress"] == {
        ("tcp", 5432, 5432, "lambda", "PostgreSQL to the RDS Proxy"),
    }
    assert rules["egress"] == {
        ("tcp", 5432, 5432, "db", "PostgreSQL to the database"),
        (
            "tcp",
            443,
            443,
            str(variant.cfg.network.cidr),
            "HTTPS to VPC endpoints (Secrets Manager)",
        ),
    }


def test_database_security_group_accepts_postgres_only_from_the_proxy_group(
    variant: SynthVariant,
) -> None:
    """2.9: inbound TCP 5432 only from the proxy group; the only egress is CDK's placeholder."""
    rules = _security_group_rules(variant.template("network"))["db"]
    assert rules["ingress"] == {
        ("tcp", 5432, 5432, "proxy", "PostgreSQL to the database"),
    }
    assert rules["egress"] == {PLACEHOLDER_EGRESS}


def test_endpoint_security_group_accepts_https_only_from_the_vpc_cidr(
    variant: SynthVariant,
) -> None:
    """2.10: inbound TCP 443 only from the VPC CIDR; the only egress is CDK's placeholder."""
    rules = _security_group_rules(variant.template("network"))["endpoint"]
    assert rules["ingress"] == {
        ("tcp", 443, 443, str(variant.cfg.network.cidr), "HTTPS from the VPC CIDR"),
    }
    assert rules["egress"] == {PLACEHOLDER_EGRESS}


def test_security_groups_never_open_a_port_to_the_internet_for_ingress(
    variant: SynthVariant,
) -> None:
    for role, rules in _security_group_rules(variant.template("network")).items():
        for _, _, _, peer, _ in rules["ingress"]:
            assert peer not in ("0.0.0.0/0", "::/0"), f"{role} ingress is open to {peer}"


def test_flow_log_captures_all_traffic_into_a_log_group_with_retention(
    variant: SynthVariant,
) -> None:
    """2.11: ALL traffic for the VPC to CloudWatch Logs, with the configured retention."""
    template = variant.template("network")
    flow_logs = _resources(template, "AWS::EC2::FlowLog")
    assert len(flow_logs) == 1
    flow_log = next(iter(flow_logs.values()))["Properties"]
    vpc_id = next(iter(_resources(template, "AWS::EC2::VPC")))
    assert flow_log["TrafficType"] == "ALL"
    assert flow_log["ResourceType"] == "VPC"
    assert _ref(flow_log["ResourceId"]) == vpc_id
    assert flow_log["LogDestinationType"] == "cloud-watch-logs"

    log_groups = _resources(template, "AWS::Logs::LogGroup")
    assert list(log_groups) == [_ref(flow_log["LogGroupName"])]
    properties = next(iter(log_groups.values()))["Properties"]
    assert properties["LogGroupName"] == f"/{variant.cfg.stage}/vpc/flow-logs"
    assert properties["RetentionInDays"] == variant.cfg.observability.log_retention_days


def test_flow_log_group_deletion_policy_follows_the_removal_policy(variant: SynthVariant) -> None:
    """The flow log group is deleted with the stack in dev and kept in the RETAIN variant."""
    log_group = next(iter(_resources(variant.template("network"), "AWS::Logs::LogGroup").values()))
    expected = "Retain" if variant.cfg.removal_policy == "RETAIN" else "Delete"
    assert log_group["DeletionPolicy"] == expected
    assert log_group["UpdateReplacePolicy"] == expected


def test_retain_variant_keeps_the_flow_log_group(retain_variant: SynthVariant) -> None:
    log_group = next(
        iter(_resources(retain_variant.template("network"), "AWS::Logs::LogGroup").values())
    )
    assert log_group["DeletionPolicy"] == "Retain"


def test_dev_deletes_the_flow_log_group_with_the_stack(dev_variant: SynthVariant) -> None:
    log_group = next(
        iter(_resources(dev_variant.template("network"), "AWS::Logs::LogGroup").values())
    )
    assert log_group["DeletionPolicy"] == "Delete"


def test_extension_parameters_publish_the_network_handles(variant: SynthVariant) -> None:
    """11.2: vpc-id, private and isolated subnet ids, and the Lambda security group id."""
    template = variant.template("network")
    names = extension_parameter_names(variant.cfg.project, variant.cfg.stage)
    prefix = f"/{variant.cfg.project}/{variant.cfg.stage}/network/"
    expected_names = {
        names[ExtensionKey.VPC_ID]: prefix + "vpc-id",
        names[ExtensionKey.PRIVATE_SUBNET_IDS]: prefix + "private-subnet-ids",
        names[ExtensionKey.ISOLATED_SUBNET_IDS]: prefix + "isolated-subnet-ids",
        names[ExtensionKey.LAMBDA_SG_ID]: prefix + "lambda-security-group-id",
    }
    assert all(actual == wanted for actual, wanted in expected_names.items())

    parameters = {
        str(p["Properties"]["Name"]): p["Properties"]
        for p in _resources(template, "AWS::SSM::Parameter").values()
    }
    assert set(parameters) == set(expected_names.values())
    assert all(p["Type"] == "String" for p in parameters.values())

    vpc_id = next(iter(_resources(template, "AWS::EC2::VPC")))
    assert parameters[prefix + "vpc-id"]["Value"] == {"Ref": vpc_id}

    subnets = _subnets_by_group(template)
    for group, suffix in (("private", "private-subnet-ids"), ("isolated", "isolated-subnet-ids")):
        value = parameters[prefix + suffix]["Value"]["Fn::Join"]
        assert value[0] == ","
        assert {_ref(item) for item in value[1]} == set(subnets[group])

    lambda_sg = next(
        logical_id
        for logical_id, role in _security_group_roles(template).items()
        if role == "lambda"
    )
    assert _ref(parameters[prefix + "lambda-security-group-id"]["Value"]) == lambda_sg
