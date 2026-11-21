"""Api assertion tests on the api-compute stack (Requirements 5.1 to 5.10, 11.3, 12.1).

Assertions are on the properties the requirements name, against the templates synthesized once
per session for each variant (dev, dev with ``removal_policy: RETAIN``, two NAT gateways).
Methods are identified by the path of their Resource, rebuilt from the ``ParentId`` chain.
"""

import json
from collections.abc import Mapping
from typing import Any

from aws_cdk.assertions import Match, Template

from synthesis import SynthVariant
from tokenization_infra.core.config import FunctionId
from tokenization_infra.core.parameters import ExtensionKey, extension_parameter_names
from tokenization_infra.core.urls import cors_origins

DOMAIN_FUNCTIONS = [fn for fn in FunctionId if fn is not FunctionId.SIGNER]
KYC_WEBHOOK_PATH = "/webhooks/kyc/{provider}"


# --- helpers ---


def _api(variant: SynthVariant) -> Template:
    return variant.template("api-compute")


def _only(resources: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
    assert len(resources) == 1, f"expected one resource, found {list(resources)}"
    logical_id, resource = next(iter(resources.items()))
    return logical_id, dict(resource)


def _rest_api_id(template: Template) -> str:
    logical_id, _ = _only(template.find_resources("AWS::ApiGateway::RestApi"))
    return logical_id


def _resource_path(template: Template, logical_id: str) -> str:
    """Path of an ``AWS::ApiGateway::Resource`` (``/reports/{proxy+}``), or ``/`` for the root."""
    resources = template.find_resources("AWS::ApiGateway::Resource")
    parts: list[str] = []
    current: Any = {"Ref": logical_id}
    while isinstance(current, dict) and "Ref" in current:
        resource = resources[current["Ref"]]["Properties"]
        parts.append(resource["PathPart"])
        current = resource["ParentId"]
    # The chain ends at ``{"Fn::GetAtt": [<RestApi>, "RootResourceId"]}``.
    assert current == {"Fn::GetAtt": [_rest_api_id(template), "RootResourceId"]}, current
    return "/" + "/".join(reversed(parts))


def _methods(template: Template, *, preflight: bool) -> dict[tuple[str, str], dict[str, Any]]:
    """(HTTP method, path) to method properties. ``preflight`` selects the OPTIONS methods."""
    root_id = {"Fn::GetAtt": [_rest_api_id(template), "RootResourceId"]}
    found: dict[tuple[str, str], dict[str, Any]] = {}
    for method in template.find_resources("AWS::ApiGateway::Method").values():
        properties = method["Properties"]
        if (properties["HttpMethod"] == "OPTIONS") is not preflight:
            continue
        resource_id = properties["ResourceId"]
        path = "/" if resource_id == root_id else _resource_path(template, resource_id["Ref"])
        key = (properties["HttpMethod"], path)
        assert key not in found, f"duplicate method {key}"
        found[key] = properties
    return found


def _joined(value: Any) -> Any:
    """Collapse an ``Fn::Join`` with an empty separator into a list of its parts."""
    assert isinstance(value, dict) and "Fn::Join" in value, value
    separator, parts = value["Fn::Join"]
    assert separator == ""
    return parts


# --- 5.1: endpoint ---


def test_api_is_regional(variant: SynthVariant) -> None:
    _api(variant).has_resource_properties(
        "AWS::ApiGateway::RestApi",
        {"EndpointConfiguration": {"Types": ["REGIONAL"]}},
    )


# --- 5.2, 5.3, 5.4, 5.5: routes, authorizer, scopes ---


def test_domain_lambdas_have_two_base_paths_with_proxy_integrations(
    variant: SynthVariant,
) -> None:
    template = _api(variant)
    methods = _methods(template, preflight=False)
    for fn in DOMAIN_FUNCTIONS:
        for path in (f"/{fn.value}", f"/{fn.value}/{{proxy+}}"):
            properties = methods[("ANY", path)]
            assert properties["Integration"]["Type"] == "AWS_PROXY", path
            assert properties["Integration"]["IntegrationHttpMethod"] == "POST", path
            uri = json.dumps(properties["Integration"]["Uri"])
            assert "functions/" in uri and "/invocations" in uri, path


def test_only_the_domain_routes_and_the_kyc_webhook_exist(variant: SynthVariant) -> None:
    expected = {
        ("ANY", path)
        for fn in DOMAIN_FUNCTIONS
        for path in (f"/{fn.value}", f"/{fn.value}/{{proxy+}}")
    } | {("POST", KYC_WEBHOOK_PATH)}
    assert set(_methods(_api(variant), preflight=False)) == expected


def test_signer_has_no_route(variant: SynthVariant) -> None:
    paths = {path for _, path in _methods(_api(variant), preflight=False)}
    assert not any(path.startswith("/signer") for path in paths)


def test_every_non_preflight_method_except_the_kyc_webhook_uses_cognito_with_route_scopes(
    variant: SynthVariant,
) -> None:
    template = _api(variant)
    authorizer_id, _ = _only(template.find_resources("AWS::ApiGateway::Authorizer"))
    route_scopes = {str(key): scopes for key, scopes in variant.cfg.api.route_scopes.items()}
    protected = {
        key: properties
        for key, properties in _methods(template, preflight=False).items()
        if key != ("POST", KYC_WEBHOOK_PATH)
    }
    assert len(protected) == 2 * len(DOMAIN_FUNCTIONS)
    for (_, path), properties in protected.items():
        fn = path.split("/")[1]
        assert properties["AuthorizationType"] == "COGNITO_USER_POOLS", path
        assert properties["AuthorizerId"] == {"Ref": authorizer_id}, path
        assert properties["AuthorizationScopes"] == list(route_scopes[fn]), path


def test_authorizer_is_cognito_user_pools_on_the_authorization_header(
    variant: SynthVariant,
) -> None:
    _api(variant).has_resource_properties(
        "AWS::ApiGateway::Authorizer",
        {
            "Type": "COGNITO_USER_POOLS",
            "IdentitySource": "method.request.header.Authorization",
            "ProviderARNs": [{"Fn::ImportValue": Match.string_like_regexp(r"^dev-identity:")}],
        },
    )


def test_kyc_webhook_is_open_and_proxies_to_stellar_kyc(variant: SynthVariant) -> None:
    template = _api(variant)
    properties = _methods(template, preflight=False)[("POST", KYC_WEBHOOK_PATH)]
    assert properties["AuthorizationType"] == "NONE"
    assert "AuthorizerId" not in properties
    assert "AuthorizationScopes" not in properties
    assert properties["Integration"]["Type"] == "AWS_PROXY"
    kyc_function, _ = _only(
        template.find_resources(
            "AWS::Lambda::Function",
            {"Properties": {"FunctionName": f"{variant.cfg.stage}-stellar-kyc"}},
        )
    )
    assert {"Fn::GetAtt": [kyc_function, "Arn"]} in _joined(properties["Integration"]["Uri"])


def test_every_route_has_a_lambda_invoke_permission_for_its_function(
    variant: SynthVariant,
) -> None:
    template = _api(variant)
    for fn in DOMAIN_FUNCTIONS:
        function_id, _ = _only(
            template.find_resources(
                "AWS::Lambda::Function",
                {"Properties": {"FunctionName": f"{variant.cfg.stage}-{fn.value}"}},
            )
        )
        permissions = template.find_resources(
            "AWS::Lambda::Permission",
            {
                "Properties": {
                    "Action": "lambda:InvokeFunction",
                    "Principal": "apigateway.amazonaws.com",
                    "FunctionName": {"Fn::GetAtt": [function_id, "Arn"]},
                }
            },
        )
        assert permissions, f"no API Gateway invoke permission for {fn.value}"


# --- 5.6, 5.7: stage settings ---


def test_stage_has_access_log_tracing_and_throttling(variant: SynthVariant) -> None:
    cfg = variant.cfg
    template = _api(variant)
    access_log_group, _ = _only(
        template.find_resources(
            "AWS::Logs::LogGroup",
            {"Properties": {"LogGroupName": f"/{cfg.stage}/apigateway/access"}},
        )
    )
    template.has_resource_properties(
        "AWS::ApiGateway::Stage",
        {
            "StageName": cfg.stage,
            "TracingEnabled": True,
            "AccessLogSetting": {
                "DestinationArn": {"Fn::Join": ["", Match.array_with([{"Ref": access_log_group}])]},
                "Format": Match.string_like_regexp(r"\$context\.requestId"),
            },
            "MethodSettings": [
                Match.object_like(
                    {
                        "HttpMethod": "*",
                        "ResourcePath": "/*",
                        "MetricsEnabled": True,
                        "ThrottlingRateLimit": cfg.api.throttling_rate_limit,
                        "ThrottlingBurstLimit": cfg.api.throttling_burst_limit,
                    }
                )
            ],
        },
    )


def test_access_log_group_has_configured_retention(variant: SynthVariant) -> None:
    cfg = variant.cfg
    _api(variant).has_resource_properties(
        "AWS::Logs::LogGroup",
        {
            "LogGroupName": f"/{cfg.stage}/apigateway/access",
            "RetentionInDays": cfg.observability.log_retention_days,
        },
    )


# --- 5.8: execution log group ---


def test_execution_log_group_name_joins_rest_api_ref_and_stage_with_retention(
    variant: SynthVariant,
) -> None:
    cfg = variant.cfg
    template = _api(variant)
    api_id = _rest_api_id(template)
    template.has_resource_properties(
        "AWS::Logs::LogGroup",
        {
            "LogGroupName": {
                "Fn::Join": ["", ["API-Gateway-Execution-Logs_", {"Ref": api_id}, f"/{cfg.stage}"]]
            },
            "RetentionInDays": cfg.observability.log_retention_days,
        },
    )


def test_stage_depends_on_the_execution_log_group(variant: SynthVariant) -> None:
    template = _api(variant)
    api_id = _rest_api_id(template)
    group_id, _ = _only(
        template.find_resources(
            "AWS::Logs::LogGroup",
            {
                "Properties": {
                    "LogGroupName": {
                        "Fn::Join": Match.array_with(
                            [Match.array_with(["API-Gateway-Execution-Logs_", {"Ref": api_id}])]
                        )
                    }
                }
            },
        )
    )
    _, stage = _only(template.find_resources("AWS::ApiGateway::Stage"))
    assert group_id in stage["DependsOn"]


# --- 5.9: CORS preflight ---


def test_preflight_allows_the_configured_origins_and_never_a_wildcard(
    variant: SynthVariant,
) -> None:
    cfg = variant.cfg
    origins = cors_origins(cfg.portal.origin, cfg.portal.local_dev_callback_urls)
    assert origins[0] == cfg.portal.origin
    preflight = _methods(_api(variant), preflight=True)
    assert preflight, "no OPTIONS methods"
    for (_, path), properties in preflight.items():
        assert properties["AuthorizationType"] == "NONE", path
        response = properties["Integration"]["IntegrationResponses"][0]
        allow_origin = response["ResponseParameters"][
            "method.response.header.Access-Control-Allow-Origin"
        ]
        # The static header holds the first origin; the template echoes the others it knows.
        assert allow_origin == f"'{cfg.portal.origin}'", path
        template_text = response["ResponseTemplates"]["application/json"]
        for origin in origins[1:]:
            assert f'"{origin}"' in template_text, (path, origin)
        assert "'*'" not in json.dumps(response), path


def test_preflight_methods_cover_every_resource(variant: SynthVariant) -> None:
    template = _api(variant)
    resources = template.find_resources("AWS::ApiGateway::Resource")
    paths = {_resource_path(template, logical_id) for logical_id in resources} | {"/"}
    assert {path for _, path in _methods(template, preflight=True)} == paths


# --- 5.10: gateway responses ---


def test_default_4xx_and_5xx_gateway_responses_allow_only_the_portal_origin(
    variant: SynthVariant,
) -> None:
    template = _api(variant)
    api_id = _rest_api_id(template)
    for response_type in ("DEFAULT_4XX", "DEFAULT_5XX"):
        template.has_resource_properties(
            "AWS::ApiGateway::GatewayResponse",
            {
                "ResponseType": response_type,
                "RestApiId": {"Ref": api_id},
                "ResponseParameters": Match.object_like(
                    {
                        "gatewayresponse.header.Access-Control-Allow-Origin": (
                            f"'{variant.cfg.portal.origin}'"
                        ),
                    }
                ),
            },
        )
    assert len(template.find_resources("AWS::ApiGateway::GatewayResponse")) == 2


# --- 11.3: Extension_Parameters ---


def test_api_extension_parameters_are_published(variant: SynthVariant) -> None:
    cfg = variant.cfg
    template = _api(variant)
    api_id = _rest_api_id(template)
    names = extension_parameter_names(cfg.project, cfg.stage)
    template.has_resource_properties(
        "AWS::SSM::Parameter",
        {"Name": names[ExtensionKey.API_ID], "Type": "String", "Value": {"Ref": api_id}},
    )
    template.has_resource_properties(
        "AWS::SSM::Parameter",
        {
            "Name": names[ExtensionKey.API_ROOT_RESOURCE_ID],
            "Type": "String",
            "Value": {"Fn::GetAtt": [api_id, "RootResourceId"]},
        },
    )
    template.has_resource_properties(
        "AWS::SSM::Parameter",
        {
            "Name": names[ExtensionKey.API_BASE_URL],
            "Type": "String",
            "Value": {
                "Fn::Join": [
                    "",
                    Match.array_with(["https://", {"Ref": api_id}]),
                ]
            },
        },
    )
    assert names[ExtensionKey.API_ID] == f"/{cfg.project}/{cfg.stage}/api/id"
    assert names[ExtensionKey.API_BASE_URL] == f"/{cfg.project}/{cfg.stage}/api/base-url"
    assert names[ExtensionKey.API_ROOT_RESOURCE_ID] == (
        f"/{cfg.project}/{cfg.stage}/api/root-resource-id"
    )
