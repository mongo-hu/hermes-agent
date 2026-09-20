"""Thin Hermes tools backed by Charmdeer MCP callbacks."""

from __future__ import annotations

from typing import Any, Dict

from tools.hotel_mcp_client import call_consumer_mcp_tool
from tools.registry import registry


def _object_schema(description: str, properties: Dict[str, Any], required: list[str] | None = None) -> Dict[str, Any]:
    return {
        "type": "object",
        "description": description,
        "properties": properties,
        "required": list(required or []),
        "additionalProperties": True,
    }


ORDER_SEARCH_SCHEMA = {
    "name": "order_search",
    "description": (
        "Search hotel/order records through the hotel system. Use this for "
        "order number lookup, order status, confirmation status, booking date, "
        "travel date, and stay date questions."
    ),
    "parameters": _object_schema(
        "Order search input.",
        {
            "orderNo": {"type": "string", "description": "Exact order number, for example O123456."},
            "filter": {
                "type": "object",
                "description": (
                    "Order filters such as type, status, search, bookedAt, date, "
                    "confirmation.status, stay.date.checkIn, stay.date.checkOut."
                ),
            },
            "paging": {"type": "object", "description": "Paging options such as {skip, limit}."},
            "sort": {"type": "string", "description": "Sort option such as byBookedAtDescending."},
        },
    ),
}


ORDER_BI_QUERY_SCHEMA = {
    "name": "order_bi_query",
    "description": (
        "Run read-only order BI aggregation through the hotel system. Use this "
        "for admin analytics such as order count, sales amount, city/channel "
        "breakdowns, hotel rankings, and date-period summaries."
    ),
    "parameters": _object_schema(
        "Order BI query input.",
        {
            "criteria": {"type": "object", "description": "Base filters such as channelRef, userRef, type, status."},
            "advancedCriteria": {
                "type": "object",
                "description": (
                    "Advanced filters such as cityRef, country, createdDate, "
                    "consumeDateRange, moduleTpHotelRef, tpHotelChain."
                ),
            },
            "dimensions": {
                "type": "array",
                "description": "Grouping dimensions, for example _summary, cityRef, channelRef, moduleTpHotelRef.",
                "items": {"type": "string"},
            },
            "measures": {
                "type": "array",
                "description": "Optional metric declarations.",
                "items": {"type": "string"},
            },
            "sort": {"type": "object", "description": "Sort object, for example {orderCount: -1}."},
            "limit": {"type": "number", "description": "Maximum rows to return."},
            "skip": {"type": "number", "description": "Pagination offset."},
            "options": {"type": "object", "description": "Query options such as {amountBase: true}."},
        },
    ),
}


HOTELUX_SUPPORT_PLAYBOOK_SCHEMA = {
    "name": "hotelux_support_playbook",
    "description": (
        "Retrieve HoteLux support playbook knowledge for customer-service policy "
        "questions, loyalty membership, points, eligible nights, SNP, and related workflows."
    ),
    "parameters": _object_schema(
        "Support playbook lookup input.",
        {
            "topic": {"type": "string", "description": "Playbook topic key."},
            "skillName": {"type": "string", "description": "Support skill name."},
        },
    ),
}


ORDER_POINTS_SCHEMA = {
    "name": "order_points",
    "description": (
        "Query points records linked to an order, including points earned from booking "
        "and points redeemed for payment."
    ),
    "parameters": _object_schema(
        "order_points input.",
        {"orderNo": {"type": "string", "description": "Order number, for example O0260404421."}},
        required=["orderNo"],
    ),
}

HOTEL_PACKAGE_SEARCH_SCHEMA = {
    "name": "hotel_package_search",
    "description": (
        "Search currently bookable hotel packages for a hotel selected from a prior hotel search. "
        "Use this for package products, not ordinary room rates, promotions, or hotel benefits."
    ),
    "parameters": _object_schema(
        "Hotel package search input.",
        {
            "hotelId": {"type": "string", "description": "Hotel ID from a prior hotel_search result."},
            "resultRef": {
                "type": "string",
                "description": "Opaque resultRef returned by the hotel_search call that produced hotelId.",
            },
            "limit": {"type": "integer", "minimum": 1, "maximum": 10},
        },
        required=["hotelId", "resultRef"],
    ),
}

KNOWLEDGE_SEARCH_SCHEMA = {
    "name": "knowledge_search",
    "description": (
        "Search the Insight knowledge base for hotel policies, membership, booking consultation, "
        "and service explanations."
    ),
    "parameters": _object_schema(
        "Knowledge search input.",
        {
            "query": {"type": "string", "description": "Natural-language question."},
            "knowledgeKey": {"type": "string", "description": "Optional exact knowledge topic."},
            "domainCode": {"type": "string", "description": "Optional knowledge domain."},
            "topicCodes": {"type": "array", "items": {"type": "string"}},
            "hotelName": {"type": "string", "description": "Explicit hotel name, when present."},
        },
        required=["query"],
    ),
}

USER_INFO_SCHEMA = {
    "name": "user_info",
    "description": "Query public account and membership information for the currently logged-in user.",
    "parameters": _object_schema("user_info input.", {}),
}

CAR_SERVICE_CITIES_SCHEMA = {
    "name": "car_service_cities",
    "description": "List cities where concierge car service is available for the current channel.",
    "parameters": _object_schema("car_service_cities input.", {}),
}

RESULT_QUERY_SCHEMA = {
    "type": "object",
    "description": (
        "Safe declarative filters generated from the current user question. "
        "Only the documented fields are accepted; scripts and regular expressions are not supported."
    ),
    "properties": {
        "hotel": {
            "type": "object",
            "description": "Filters applied to stored hotel search results.",
            "properties": {
                "ids": {"type": "array", "items": {"type": "string"}},
                "codes": {"type": "array", "items": {"type": "string"}},
                "keywords": {"type": "array", "items": {"type": "string"}, "description": "Match any term."},
                "allKeywords": {"type": "array", "items": {"type": "string"}, "description": "Match every term."},
                "cities": {"type": "array", "items": {"type": "string"}},
                "brands": {"type": "array", "items": {"type": "string"}},
                "chains": {"type": "array", "items": {"type": "string"}},
                "minRating": {"type": "number"},
                "maxPrice": {"type": "number"},
            },
            "additionalProperties": False,
        },
        "room": {
            "type": "object",
            "description": (
                "Room filters: keywords, allKeywords, bedTypes, views, minSize, maxSize, minCapacity. "
                "Use keywords for room categories such as suite, villa, club room, or family room."
            ),
            "properties": {
                "ids": {"type": "array", "items": {"type": "string"}},
                "codes": {"type": "array", "items": {"type": "string"}},
                "keywords": {"type": "array", "items": {"type": "string"}, "description": "Match any term."},
                "allKeywords": {"type": "array", "items": {"type": "string"}, "description": "Match every term."},
                "bedTypes": {"type": "array", "items": {"type": "string"}},
                "views": {"type": "array", "items": {"type": "string"}},
                "minSize": {"type": "number"},
                "maxSize": {"type": "number"},
                "minCapacity": {"type": "number"},
            },
            "additionalProperties": False,
        },
        "rate": {
            "type": "object",
            "description": (
                "Rate filters: keywords, minPrice, maxPrice, currency, breakfastIncluded, "
                "cancellable, paymentTypes. Prices refer to the returned stay total when available."
            ),
            "properties": {
                "codes": {"type": "array", "items": {"type": "string"}},
                "benefitCodes": {"type": "array", "items": {"type": "string"}},
                "keywords": {"type": "array", "items": {"type": "string"}, "description": "Match any term."},
                "allKeywords": {"type": "array", "items": {"type": "string"}, "description": "Match every term."},
                "minPrice": {"type": "number"},
                "maxPrice": {"type": "number"},
                "currency": {"type": "array", "items": {"type": "string"}},
                "breakfastIncluded": {"type": "boolean"},
                "cancellable": {"type": "boolean"},
                "paymentTypes": {"type": "array", "items": {"type": "string"}},
            },
            "additionalProperties": False,
        },
        "sort": {
            "type": "string",
            "enum": [
                "recommendation", "price_asc", "price_desc", "name_asc", "name_desc",
                "rating_desc", "size_desc", "capacity_desc",
            ],
        },
        "detail": {
            "type": "string",
            "enum": ["index", "standard"],
            "description": "index returns the complete lightweight index; standard also returns paged details.",
        },
        "page": {
            "type": "object",
            "description": "Paging: offset, limit, rateOffset, rateLimitPerRoom.",
            "properties": {
                "offset": {"type": "integer", "minimum": 0},
                "limit": {"type": "integer", "minimum": 1, "maximum": 10},
                "rateOffset": {"type": "integer", "minimum": 0},
                "rateLimitPerRoom": {"type": "integer", "minimum": 1, "maximum": 10},
            },
            "additionalProperties": False,
        },
    },
    "additionalProperties": False,
}

HOTEL_RATES_SCHEMA = {
    "name": "hotel_rates",
    "description": (
        "Query live room types and rate plans for a selected hotel when the user "
        "needs detailed availability and pricing."
    ),
    "parameters": _object_schema(
        "Hotel rates input.",
        {
            "mode": {
                "type": "string",
                "description": "Query mode: sync, async, or asyncPaging.",
            },
            "stay": {
                "type": "object",
                "description": "Stay details containing date.checkIn/date.checkOut and optional guest counts.",
            },
            "hotel": {
                "type": "object",
                "description": "Hotel identity from the selected hotel result.",
            },
            "preferred": {
                "type": "object",
                "description": "Optional currency and other preference settings.",
            },
            "resultQuery": RESULT_QUERY_SCHEMA,
        },
        required=["stay", "hotel"],
    ),
}

ENTERPRISE_RESULT_QUERY_SCHEMA = {
    "name": "enterprise_result_query",
    "description": (
        "Continue filtering a large structured result previously returned with resultRef. "
        "Generate safe hotel, room, rate, sort, and paging conditions from the current user question. "
        "Reuse this instead of calling the upstream hotel API again."
    ),
    "parameters": _object_schema(
        "Stored enterprise result query input.",
        {
            "resultRef": {
                "type": "string",
                "description": "Opaque resultRef returned by hotel_search, hotel_rates, or a prior query.",
            },
            "query": RESULT_QUERY_SCHEMA,
        },
        required=["resultRef"],
    ),
}

HOTEL_RATE_RULE_SCHEMA = {
    "name": "hotel_rate_rule",
    "description": (
        "Query the booking and cancellation rules for one selected hotel rate plan."
    ),
    "parameters": _object_schema(
        "Hotel rate-rule input.",
        {
            "stay": {
                "type": "object",
                "description": "Stay details containing dates and optional guest counts.",
            },
            "hotel": {
                "type": "object",
                "description": "The hotel associated with the selected rate.",
            },
            "rate": {
                "type": "object",
                "description": "The selected hotel rate object.",
            },
            "preferred": {
                "type": "object",
                "description": "Optional currency and other preference settings.",
            },
        },
        required=["stay", "hotel", "rate"],
    ),
}

HOTELUX_HOTEL_POLICY_SCHEMA = {
    "name": "hotelux_hotel_policy",
    "description": "Look up HoteLux hotel policy by hotel name and optional city.",
    "parameters": _object_schema(
        "hotelux_hotel_policy input.",
        {
            "hotelName": {"type": "string", "description": "Hotel name."},
            "city": {"type": "string", "description": "Optional city name for disambiguation."},
        },
        required=["hotelName"],
    ),
}

CHARMDEER_SUPPORT_PLAYBOOK_SCHEMA = {
    "name": "charmdeer_support_playbook",
    "description": "Retrieve Charmdeer customer-service playbook knowledge by topic or skillName.",
    "parameters": _object_schema(
        "charmdeer_support_playbook input.",
        {
            "topic": {
                "type": "string",
                "description": "Support knowledge topic, for example member, order_after_sales, points_coupon.",
            },
            "skillName": {"type": "string", "description": "Support skill name."},
        },
    ),
}

CHARMDEER_HOTEL_POLICY_SCHEMA = {
    "name": "charmdeer_hotel_policy",
    "description": "Look up Charmdeer hotel policy by hotel name and optional city.",
    "parameters": _object_schema(
        "charmdeer_hotel_policy input.",
        {
            "hotelName": {"type": "string", "description": "Hotel name."},
            "city": {"type": "string", "description": "Optional city name for disambiguation."},
        },
        required=["hotelName"],
    ),
}

COUPON_STATUS_SCHEMA = {
    "name": "coupon_status",
    "description": "Query the current logged-in user's valid coupon status, optionally filtered by coupon name or redemption code.",
    "parameters": _object_schema(
        "coupon_status input.",
        {"query": {"type": "string", "description": "Optional coupon name or redemption-code keyword."}},
    ),
}

PROMOTION_EXPLAIN_SCHEMA = {
    "name": "promotion_explain",
    "description": "Explain hotel promotions such as stay-N-pay-M in current hotel results.",
    "parameters": _object_schema(
        "promotion_explain input.",
        {
            "promotionName": {"type": "string", "description": "Optional promotion-name keyword."},
            "hotelName": {"type": "string", "description": "Optional hotel-name keyword."},
        },
    ),
}

MEMBER_ENTITLEMENT_SCHEMA = {
    "name": "member_entitlement",
    "description": (
        "Query the current logged-in user's membership level, points, retain/upgrade thresholds, "
        "stay-night progress, promo codes, and benefit definitions."
    ),
    "parameters": _object_schema("member_entitlement input.", {}),
}

AFTERNOON_TEA_STATUS_SCHEMA = {
    "name": "afternoon_tea_status",
    "description": "Query the current logged-in user's afternoon tea entitlement, QR/code status, expiry, and verification status.",
    "parameters": _object_schema(
        "afternoon_tea_status input.",
        {"code": {"type": "string", "description": "Optional redemption or verification code."}},
    ),
}

POINTS_BALANCE_SCHEMA = {
    "name": "points_balance",
    "description": "Query the current logged-in user's platform points balance and nearest expiry information.",
    "parameters": _object_schema("points_balance input.", {}),
}

CASE_PRECHECK_SCHEMA = {
    "name": "case_precheck",
    "description": "Precheck a support case from support skill names and user message, returning handoff need, priority, case type, and missing fields.",
    "parameters": _object_schema(
        "case_precheck input.",
        {
            "supportSkills": {"type": "array", "items": {"type": "string"}, "description": "Support skill names."},
            "message": {"type": "string", "description": "User support message."},
        },
    ),
}

CASE_HANDOFF_SCHEMA = {
    "name": "case_handoff",
    "description": "Generate a unified support-case handoff summary from precheck result and support answers.",
    "parameters": _object_schema(
        "case_handoff input.",
        {
            "supportSkills": {"type": "array", "items": {"type": "string"}, "description": "Support skill names."},
            "message": {"type": "string", "description": "User support message."},
            "items": {"type": "array", "items": {"type": "object"}, "description": "Support answer items."},
            "precheck": {"type": "object", "description": "Precheck result from case_precheck."},
        },
    ),
}

ACTIVITY_RESULT_SEARCH_SCHEMA = {
    "name": "activity_result_search",
    "description": "Search activity participation results by activity, item, round, status, number, user, or channel.",
    "parameters": _object_schema(
        "activity_result_search input.",
        {
            "activity": {"type": "string", "description": "Activity key."},
            "item": {"type": "string", "description": "Item key."},
            "round": {"type": "string", "description": "Round."},
            "status": {"type": "string", "description": "Status."},
            "no": {"type": "string", "description": "Result number."},
            "userRef": {"type": "string", "description": "User ObjectId."},
            "channelRef": {"type": "string", "description": "Channel ObjectId."},
            "limit": {"type": "number", "description": "Result limit, default 20, maximum 100."},
        },
    ),
}

ACTIVITY_RESULT_COUNT_SCHEMA = {
    "name": "activity_result_count",
    "description": "Count activity participation results and unique users by activity, item, round, status, number, user, or channel.",
    "parameters": _object_schema(
        "activity_result_count input.",
        {
            "activity": {"type": "string", "description": "Activity key."},
            "item": {"type": "string", "description": "Item key."},
            "round": {"type": "string", "description": "Round."},
            "status": {"type": "string", "description": "Status."},
            "no": {"type": "string", "description": "Result number."},
            "userRef": {"type": "string", "description": "User ObjectId."},
            "channelRef": {"type": "string", "description": "Channel ObjectId."},
        },
    ),
}


def _resolver_schema(name: str, field: str, description: str, field_description: str) -> Dict[str, Any]:
    return {
        "name": name,
        "description": description,
        "parameters": _object_schema(
            f"{name} input.",
            {field: {"type": "string", "description": field_description}},
            required=[field],
        ),
    }


RESOLVER_TIME_SCHEMA = _resolver_schema(
    "resolver_time",
    "period",
    "Resolve a relative time phrase to concrete date/month/year fields for hotel BI queries.",
    "Relative period, for example today, thisMonth, lastMonth, thisYear, recent7Days.",
)
RESOLVER_CITY_SCHEMA = _resolver_schema(
    "resolver_city",
    "cityName",
    "Resolve a city name to the hotel system cityRef.",
    "City name.",
)
RESOLVER_COUNTRY_SCHEMA = _resolver_schema(
    "resolver_country",
    "countryName",
    "Resolve a country name to the hotel system country code.",
    "Country name.",
)
RESOLVER_HOTEL_SCHEMA = _resolver_schema(
    "resolver_hotel",
    "hotelName",
    "Resolve a hotel name to moduleTpHotelRef for hotel/order BI queries.",
    "Hotel name.",
)
RESOLVER_CHANNEL_SCHEMA = _resolver_schema(
    "resolver_channel",
    "channelName",
    "Resolve a channel name to channelRef for hotel/order BI queries.",
    "Channel name.",
)
RESOLVER_PAYMENT_TYPE_SCHEMA = _resolver_schema(
    "resolver_payment_type",
    "paymentType",
    "Resolve a payment method phrase to reservationType for order queries.",
    "Payment method, for example prepaid, pay at hotel, pay to supplier.",
)
RESOLVER_SHOP_BRAND_SCHEMA = _resolver_schema(
    "resolver_shop_brand",
    "brandName",
    "Resolve a hotel brand name to shopBrandRef for BI queries.",
    "Hotel brand name.",
)
RESOLVER_SHOP_GROUP_SCHEMA = _resolver_schema(
    "resolver_shop_group",
    "groupName",
    "Resolve a hotel group name to shopGroupRef for BI queries.",
    "Hotel group name.",
)


async def _call(args: Dict[str, Any], *, task_id: str | None, tool_name: str,
                capability_ref: str, aliases: set[str] | None = None,
                user_tool_name: str | None = None) -> str:
    return await call_consumer_mcp_tool(
        task_id=task_id,
        tool_name=tool_name,
        capability_ref=capability_ref,
        aliases=aliases or set(),
        payload=args or {},
        user_tool_name=user_tool_name,
    )


def _registration(schema: Dict[str, Any], *, toolset: str, mcp_tool: str,
                  capability_ref: str | None = None, aliases: set[str] | None = None) -> Dict[str, Any]:
    tool_name = schema["name"]
    capability = capability_ref or tool_name
    return {
        "name": tool_name,
        "toolset": toolset,
        "schema": schema,
        "handler": lambda args, task_id=None, _mcp_tool=mcp_tool, _capability=capability,
                          _aliases=aliases, _tool_name=tool_name, **_: _call(
                              args,
                              task_id=task_id,
                              tool_name=_mcp_tool,
                              capability_ref=_capability,
                              aliases=_aliases,
                              user_tool_name=_tool_name,
                          ),
        "is_async": True,
        "description": schema["description"],
        "max_result_size_chars": 120_000,
    }


registry.register(**_registration(ORDER_SEARCH_SCHEMA, toolset="order_search", mcp_tool="order.search"))
registry.register(**_registration(
    ORDER_BI_QUERY_SCHEMA,
    toolset="order_bi_query",
    mcp_tool="order.biQuery",
    capability_ref="order_biQuery",
    aliases={"order_bi_query"},
))
registry.register(**_registration(
    HOTELUX_SUPPORT_PLAYBOOK_SCHEMA,
    toolset="hotelux_support_playbook",
    mcp_tool="hotelux.support_playbook",
))
registry.register(**_registration(ORDER_POINTS_SCHEMA, toolset="order_points", mcp_tool="order.points"))
registry.register(**_registration(HOTEL_PACKAGE_SEARCH_SCHEMA, toolset="hotel_package_search", mcp_tool="hotel.package.search"))
registry.register(**_registration(KNOWLEDGE_SEARCH_SCHEMA, toolset="knowledge_search", mcp_tool="knowledge.search"))
registry.register(**_registration(USER_INFO_SCHEMA, toolset="user_info", mcp_tool="user.info"))
registry.register(**_registration(
    CAR_SERVICE_CITIES_SCHEMA,
    toolset="car_service_cities",
    mcp_tool="car.serviceCities",
    capability_ref="car_serviceCities",
    aliases={"car_service_cities"},
))
registry.register(**_registration(HOTEL_RATES_SCHEMA, toolset="hotel_rates", mcp_tool="hotel.rates"))
registry.register(**_registration(
    ENTERPRISE_RESULT_QUERY_SCHEMA,
    toolset="enterprise_result_query",
    mcp_tool="enterprise.resultQuery",
    capability_ref="enterprise_resultQuery",
    aliases={"enterprise_result_query"},
))
registry.register(**_registration(
    HOTEL_RATE_RULE_SCHEMA,
    toolset="hotel_rate_rule",
    mcp_tool="hotel.rateRule",
    capability_ref="hotel_rateRule",
    aliases={"hotel_rate_rule"},
))
registry.register(**_registration(HOTELUX_HOTEL_POLICY_SCHEMA, toolset="hotelux_hotel_policy", mcp_tool="hotelux.hotel_policy"))
registry.register(**_registration(CHARMDEER_SUPPORT_PLAYBOOK_SCHEMA, toolset="charmdeer_support_playbook", mcp_tool="charmdeer.support_playbook"))
registry.register(**_registration(CHARMDEER_HOTEL_POLICY_SCHEMA, toolset="charmdeer_hotel_policy", mcp_tool="charmdeer.hotel_policy"))
registry.register(**_registration(COUPON_STATUS_SCHEMA, toolset="coupon_status", mcp_tool="coupon.status"))
registry.register(**_registration(PROMOTION_EXPLAIN_SCHEMA, toolset="promotion_explain", mcp_tool="promotion.explain"))
registry.register(**_registration(MEMBER_ENTITLEMENT_SCHEMA, toolset="member_entitlement", mcp_tool="member.entitlement"))
registry.register(**_registration(
    AFTERNOON_TEA_STATUS_SCHEMA,
    toolset="afternoon_tea_status",
    mcp_tool="afternoonTea.status",
    capability_ref="afternoonTea_status",
    aliases={"afternoon_tea_status"},
))
registry.register(**_registration(POINTS_BALANCE_SCHEMA, toolset="points_balance", mcp_tool="points.balance"))
registry.register(**_registration(CASE_PRECHECK_SCHEMA, toolset="case_precheck", mcp_tool="case.precheck"))
registry.register(**_registration(CASE_HANDOFF_SCHEMA, toolset="case_handoff", mcp_tool="case.handoff"))
registry.register(**_registration(
    ACTIVITY_RESULT_SEARCH_SCHEMA,
    toolset="activity_result_search",
    mcp_tool="activityResult.search",
    capability_ref="activityResult_search",
    aliases={"activity_result_search"},
))
registry.register(**_registration(
    ACTIVITY_RESULT_COUNT_SCHEMA,
    toolset="activity_result_count",
    mcp_tool="activityResult.count",
    capability_ref="activityResult_count",
    aliases={"activity_result_count"},
))
registry.register(**_registration(RESOLVER_TIME_SCHEMA, toolset="resolver_time", mcp_tool="resolver.time"))
registry.register(**_registration(RESOLVER_CITY_SCHEMA, toolset="resolver_city", mcp_tool="resolver.city"))
registry.register(**_registration(RESOLVER_COUNTRY_SCHEMA, toolset="resolver_country", mcp_tool="resolver.country"))
registry.register(**_registration(RESOLVER_HOTEL_SCHEMA, toolset="resolver_hotel", mcp_tool="resolver.hotel"))
registry.register(**_registration(RESOLVER_CHANNEL_SCHEMA, toolset="resolver_channel", mcp_tool="resolver.channel"))
registry.register(**_registration(
    RESOLVER_PAYMENT_TYPE_SCHEMA,
    toolset="resolver_payment_type",
    mcp_tool="resolver.paymentType",
    capability_ref="resolver_paymentType",
    aliases={"resolver_payment_type"},
))
registry.register(**_registration(
    RESOLVER_SHOP_BRAND_SCHEMA,
    toolset="resolver_shop_brand",
    mcp_tool="resolver.shopBrand",
    capability_ref="resolver_shopBrand",
    aliases={"resolver_shop_brand"},
))
registry.register(**_registration(
    RESOLVER_SHOP_GROUP_SCHEMA,
    toolset="resolver_shop_group",
    mcp_tool="resolver.shopGroup",
    capability_ref="resolver_shopGroup",
    aliases={"resolver_shop_group"},
))
