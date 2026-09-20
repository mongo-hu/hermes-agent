import tools.hotel_mcp_tools  # noqa: F401
from tools.registry import registry
from toolsets import resolve_toolset


def test_hotel_mcp_tools_register_individual_toolsets():
    expected = {
        "order_search": "order_search",
        "order_bi_query": "order_bi_query",
        "hotelux_support_playbook": "hotelux_support_playbook",
        "order_points": "order_points",
        "hotel_package_search": "hotel_package_search",
        "knowledge_search": "knowledge_search",
        "user_info": "user_info",
        "car_service_cities": "car_service_cities",
        "hotel_rates": "hotel_rates",
        "hotel_rate_rule": "hotel_rate_rule",
        "enterprise_result_query": "enterprise_result_query",
        "hotelux_hotel_policy": "hotelux_hotel_policy",
        "charmdeer_support_playbook": "charmdeer_support_playbook",
        "charmdeer_hotel_policy": "charmdeer_hotel_policy",
        "coupon_status": "coupon_status",
        "promotion_explain": "promotion_explain",
        "member_entitlement": "member_entitlement",
        "afternoon_tea_status": "afternoon_tea_status",
        "points_balance": "points_balance",
        "case_precheck": "case_precheck",
        "case_handoff": "case_handoff",
        "activity_result_search": "activity_result_search",
        "activity_result_count": "activity_result_count",
        "resolver_time": "resolver_time",
        "resolver_city": "resolver_city",
        "resolver_country": "resolver_country",
        "resolver_hotel": "resolver_hotel",
        "resolver_channel": "resolver_channel",
        "resolver_payment_type": "resolver_payment_type",
        "resolver_shop_brand": "resolver_shop_brand",
        "resolver_shop_group": "resolver_shop_group",
    }

    for tool_name, toolset in expected.items():
        entry = registry.get_entry(tool_name)
        assert entry is not None
        assert entry.toolset == toolset
        assert resolve_toolset(toolset) == [tool_name]


def test_hotel_mcp_tool_schema_uses_public_snake_case_names():
    assert registry.get_entry("order_bi_query").schema["name"] == "order_bi_query"
    assert registry.get_entry("hotel_rates").schema["name"] == "hotel_rates"
    assert registry.get_entry("hotel_rate_rule").schema["name"] == "hotel_rate_rule"
    assert registry.get_entry("enterprise_result_query").schema["name"] == "enterprise_result_query"
    assert registry.get_entry("resolver_payment_type").schema["name"] == "resolver_payment_type"
    assert registry.get_entry("resolver_shop_brand").schema["name"] == "resolver_shop_brand"
    assert registry.get_entry("resolver_shop_group").schema["name"] == "resolver_shop_group"
    assert registry.get_entry("afternoon_tea_status").schema["name"] == "afternoon_tea_status"
    assert registry.get_entry("activity_result_search").schema["name"] == "activity_result_search"
    assert registry.get_entry("car_service_cities").schema["name"] == "car_service_cities"


def test_hotel_mcp_tools_do_not_register_unsupported_remote_tools():
    unsupported = {
        "order_confirmation",
        "points_reconcile",
        "order_benefits",
        "cancellation_eligibility",
        "payment_diagnosis",
        "change_precheck",
    }

    assert all(registry.get_entry(tool_name) is None for tool_name in unsupported)


def test_hotel_rates_exposes_safe_runtime_result_query_schema():
    rates_schema = registry.get_entry("hotel_rates").schema["parameters"]
    result_query = rates_schema["properties"]["resultQuery"]
    assert result_query["additionalProperties"] is False
    assert set(result_query["properties"]) == {"hotel", "room", "rate", "sort", "detail", "page"}
    assert result_query["properties"]["hotel"]["additionalProperties"] is False
    assert result_query["properties"]["room"]["additionalProperties"] is False
    assert result_query["properties"]["rate"]["additionalProperties"] is False
    assert result_query["properties"]["page"]["properties"]["limit"]["maximum"] == 10
    assert result_query["properties"]["page"]["properties"]["rateLimitPerRoom"]["maximum"] == 10

    query_schema = registry.get_entry("enterprise_result_query").schema["parameters"]
    assert query_schema["required"] == ["resultRef"]
    assert "query" in query_schema["properties"]


def test_hotel_package_search_requires_the_source_result_reference():
    package_schema = registry.get_entry("hotel_package_search").schema["parameters"]
    assert package_schema["required"] == ["hotelId", "resultRef"]
