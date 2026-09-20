"""Tests for hermes-api-server toolset and API server tool availability."""
from unittest.mock import patch, MagicMock


from toolsets import resolve_toolset, get_toolset, validate_toolset


class TestHermesApiServerToolset:
    """Tests for the hermes-api-server toolset definition."""

    def test_toolset_exists(self):
        ts = get_toolset("hermes-api-server")
        assert ts is not None

    def test_toolset_validates(self):
        assert validate_toolset("hermes-api-server")

    def test_toolset_includes_web_tools(self):
        tools = resolve_toolset("hermes-api-server")
        assert "web_search" in tools
        assert "web_extract" in tools

    def test_toolset_includes_core_tools(self):
        tools = resolve_toolset("hermes-api-server")
        expected = [
            "terminal", "process",
            "read_file", "write_file", "patch", "search_files",
            "vision_analyze", "image_generate",
            "execute_code", "delegate_task",
            "todo", "memory", "session_search", "cronjob",
        ]
        for tool in expected:
            assert tool in tools, f"Missing expected tool: {tool}"

    def test_toolset_includes_browser_tools(self):
        tools = resolve_toolset("hermes-api-server")
        for tool in ["browser_navigate", "browser_snapshot", "browser_click",
                      "browser_type", "browser_scroll", "browser_back",
                      "browser_press"]:
            assert tool in tools, f"Missing browser tool: {tool}"

    def test_toolset_includes_homeassistant_tools(self):
        tools = resolve_toolset("hermes-api-server")
        for tool in ["ha_list_entities", "ha_get_state", "ha_list_services", "ha_call_service"]:
            assert tool in tools, f"Missing HA tool: {tool}"

    def test_toolset_excludes_clarify(self):
        tools = resolve_toolset("hermes-api-server")
        assert "clarify" not in tools

    def test_toolset_excludes_send_message(self):
        tools = resolve_toolset("hermes-api-server")
        assert "send_message" not in tools

    def test_toolset_excludes_text_to_speech(self):
        tools = resolve_toolset("hermes-api-server")
        assert "text_to_speech" not in tools


class TestApiServerPlatformConfig:
    def test_platforms_dict_includes_api_server(self):
        from hermes_cli.tools_config import PLATFORMS
        assert "api_server" in PLATFORMS
        assert PLATFORMS["api_server"]["default_toolset"] == "hermes-api-server"

    def test_default_api_server_includes_terminal_toolset(self):
        """Regression #49622: desktop-only read_terminal is registered into the
        'terminal' toolset (ships in-repo), so resolve_toolset('terminal') grows
        to include it after discovery. read_terminal is NOT in the
        hermes-api-server composite, so the old all-tools subset test dropped
        'terminal' entirely. Its static membership (terminal, process) IS in the
        composite, so it must stay enabled."""
        from tools.registry import discover_builtin_tools
        from hermes_cli.tools_config import _get_platform_tools
        discover_builtin_tools()
        assert "terminal" in _get_platform_tools({}, "api_server")

    def test_registering_tool_into_toolset_does_not_drop_toolset_from_inference(self):
        """Class invariant (covers the delegate_cli overlay case): registering a
        NEW tool into an existing configurable toolset must never remove that
        toolset from a platform whose composite lists the toolset's static
        tools. Synthetic registration keeps the test hermetic in CI."""
        from tools.registry import registry
        from hermes_cli.tools_config import _get_platform_tools

        sentinel = "test_sentinel_delegation_tool"
        registry.register(
            name=sentinel,
            toolset="delegation",
            schema={"name": sentinel, "description": "test",
                    "parameters": {"type": "object", "properties": {}}},
            handler=lambda args, **kw: "{}",
        )
        try:
            # delegation's static membership (delegate_task) is in the composite,
            # so the toolset must survive inference despite the extra registry tool.
            assert "delegation" in _get_platform_tools({}, "api_server"), (
                "registering a tool into 'delegation' dropped it from api_server"
            )
        finally:
            registry.deregister(sentinel)

    def test_default_off_and_restricted_toolsets_stay_off_on_api_server(self):
        """Negative contract: the static-membership comparison must NOT newly
        enable default-off or platform-restricted toolsets."""
        import os
        from unittest.mock import patch
        from hermes_cli.tools_config import _get_platform_tools
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("HASS_TOKEN", None)
            os.environ.pop("XAI_API_KEY", None)
            enabled = _get_platform_tools({}, "api_server")
        assert "homeassistant" not in enabled
        assert "discord" not in enabled
        assert "discord_admin" not in enabled
        assert "x_search" not in enabled


class TestApiServerAdapterToolset:
    @patch("gateway.platforms.api_server.AIOHTTP_AVAILABLE", True)
    def test_create_agent_reads_config_toolsets(self):
        """API server resolves toolsets from config like all other platforms."""
        from gateway.platforms.api_server import APIServerAdapter
        from gateway.config import PlatformConfig

        adapter = APIServerAdapter(PlatformConfig())

        with patch("gateway.run._resolve_runtime_agent_kwargs") as mock_kwargs, \
             patch("gateway.run._resolve_gateway_model") as mock_model, \
             patch("gateway.run._load_gateway_config") as mock_config, \
             patch("run_agent.AIAgent") as mock_agent_cls:

            mock_kwargs.return_value = {"api_key": "test-key", "base_url": None,
                                        "provider": None, "api_mode": None,
                                        "command": None, "args": []}
            mock_model.return_value = "test/model"
            # No platform_toolsets override — should fall back to hermes-api-server default
            mock_config.return_value = {}
            mock_agent_cls.return_value = MagicMock()

            adapter._create_agent()

            mock_agent_cls.assert_called_once()
            call_kwargs = mock_agent_cls.call_args
            toolsets = call_kwargs.kwargs.get("enabled_toolsets")
            assert isinstance(toolsets, list)
            assert len(toolsets) > 0
            assert call_kwargs.kwargs.get("platform") == "api_server"

    @patch("gateway.platforms.api_server.AIOHTTP_AVAILABLE", True)
    def test_create_agent_respects_config_override(self):
        """User can override API server toolsets via platform_toolsets in config.yaml."""
        from gateway.platforms.api_server import APIServerAdapter
        from gateway.config import PlatformConfig

        adapter = APIServerAdapter(PlatformConfig())

        with patch("gateway.run._resolve_runtime_agent_kwargs") as mock_kwargs, \
             patch("gateway.run._resolve_gateway_model") as mock_model, \
             patch("gateway.run._load_gateway_config") as mock_config, \
             patch("run_agent.AIAgent") as mock_agent_cls:

            mock_kwargs.return_value = {"api_key": "test-key", "base_url": None,
                                        "provider": None, "api_mode": None,
                                        "command": None, "args": []}
            mock_model.return_value = "test/model"
            # User overrides with just web and terminal
            mock_config.return_value = {
                "platform_toolsets": {"api_server": ["web", "terminal"]}
            }
            mock_agent_cls.return_value = MagicMock()

            adapter._create_agent()

            mock_agent_cls.assert_called_once()
            call_kwargs = mock_agent_cls.call_args
            toolsets = call_kwargs.kwargs.get("enabled_toolsets")
            assert sorted(toolsets) == ["terminal", "web"]

    @patch("gateway.platforms.api_server.AIOHTTP_AVAILABLE", True)
    def test_create_agent_toolsets_override_is_enterprise_hard_cap(self):
        """Enterprise runtimePolicy toolsets do not inherit the full API-server surface."""
        from gateway.platforms.api_server import APIServerAdapter
        from gateway.config import PlatformConfig

        adapter = APIServerAdapter(PlatformConfig())

        with patch("gateway.run._resolve_runtime_agent_kwargs") as mock_kwargs, \
             patch("gateway.run._resolve_gateway_model") as mock_model, \
             patch("gateway.run._load_gateway_config") as mock_config, \
             patch("run_agent.AIAgent") as mock_agent_cls:

            mock_kwargs.return_value = {"api_key": "test-key", "base_url": None,
                                        "provider": None, "api_mode": None,
                                        "command": None, "args": []}
            mock_model.return_value = "test/model"
            mock_config.return_value = {
                "platform_toolsets": {"api_server": ["web", "terminal"]}
            }
            mock_agent_cls.return_value = MagicMock()

            adapter._create_agent(
                extra_toolsets=["kanban"],
                toolsets_override=["hotel"],
                skip_context_files=True,
                minimal_system_prompt=True,
                skip_memory=True,
                request_overrides={"tool_choice": {"type": "function", "function": {"name": "hotel_search"}}},
            )

            mock_agent_cls.assert_called_once()
            call_kwargs = mock_agent_cls.call_args.kwargs
            assert call_kwargs["enabled_toolsets"] == ["hotel"]
            assert call_kwargs["skip_context_files"] is True
            assert call_kwargs["minimal_system_prompt"] is True
            assert call_kwargs["skip_memory"] is True
            assert call_kwargs["request_overrides"]["tool_choice"]["function"]["name"] == "hotel_search"

    def test_enterprise_hotel_tool_choice_heuristic(self, monkeypatch):
        from gateway.platforms.api_server import APIServerAdapter

        monkeypatch.delenv("HERMES_ENTERPRISE_FORCE_HOTEL_TOOL_CHOICE", raising=False)

        assert APIServerAdapter._enterprise_should_force_hotel_tool_choice(
            "帮我查一下上海明天入住的酒店报价",
            ["hotel_search"],
            ["hotel"],
        )
        assert not APIServerAdapter._enterprise_should_force_hotel_tool_choice(
            "我之前问过哪些问题",
            ["hotel_search"],
            ["hotel"],
        )
        assert not APIServerAdapter._enterprise_should_force_hotel_tool_choice(
            "帮我查一下上海明天入住的酒店报价",
            ["hotel_search", "web"],
            ["hotel", "web"],
        )
        assert not APIServerAdapter._enterprise_should_force_hotel_tool_choice(
            "hotel rates for tomorrow",
            ["hotel_search", "kanban"],
            ["hotel", "kanban"],
        )

    def test_enterprise_direct_hotel_text_parser(self):
        from gateway.platforms.api_server import APIServerAdapter

        args = APIServerAdapter._enterprise_parse_hotel_args_from_text(
            "帮我查一下上海明天入住住2晚的酒店报价，2人"
        )

        assert args["destination"] == "上海"
        assert "hotelName" not in args
        assert args["stayNights"] == 2
        assert args["guestCount"] == 2
        assert args["dateRangeStart"] < args["dateRangeEnd"]

    def test_enterprise_tool_result_success_is_explicitly_opted_in(self):
        from gateway.platforms.api_server import APIServerAdapter

        result = {"ok": True, "data": {"balance": 1200}}

        assert APIServerAdapter._normalize_enterprise_tool_result(
            "points_balance",
            result,
        ) is None
        assert APIServerAdapter._normalize_enterprise_tool_result(
            "points_balance",
            result,
            include_success=True,
        ) == {
            "type": "tool_result",
            "tool": "points_balance",
            "ok": True,
            "result": result,
        }

    def test_enterprise_success_events_are_limited_to_credential_backed_tools(self):
        from gateway.platforms.api_server import APIServerAdapter

        inputs = {
            "credential_scope": ["points_balance", "afternoonTea_status", "hotel_search"],
            "credential_toolsets": ["points_balance", "afternoon_tea_status", "hotel"],
        }

        assert APIServerAdapter._enterprise_should_emit_success_tool_result("points_balance", inputs)
        assert APIServerAdapter._enterprise_should_emit_success_tool_result("afternoon_tea_status", inputs)
        assert APIServerAdapter._enterprise_should_emit_success_tool_result("hotel_search", inputs)
        assert not APIServerAdapter._enterprise_should_emit_success_tool_result("browser_navigate", inputs)

    def test_enterprise_direct_hotel_uses_structured_args(self, monkeypatch):
        from gateway.platforms.api_server import APIServerAdapter
        from gateway.config import PlatformConfig

        monkeypatch.delenv("HERMES_ENTERPRISE_DIRECT_HOTEL_SEARCH", raising=False)
        adapter = APIServerAdapter(PlatformConfig())
        inputs = {
            "enterprise_toolsets": ["hotel"],
            "allowed_capabilities": ["hotel_search"],
            "user_message": "随便查一下",
            "contract": {
                "toolArgs": {
                    "hotel_search": {
                        "destination": "Singapore",
                        "dateRangeStart": "2099-07-10",
                        "dateRangeEnd": "2099-07-12",
                    }
                }
            },
        }

        args = adapter._enterprise_direct_hotel_args(inputs)

        assert args["destination"] == "Singapore"
        assert args["dateRangeStart"] == "2099-07-10"

    def test_enterprise_direct_hotel_disabled_for_non_hotel_toolsets(self):
        from gateway.platforms.api_server import APIServerAdapter
        from gateway.config import PlatformConfig

        adapter = APIServerAdapter(PlatformConfig())
        inputs = {
            "enterprise_toolsets": ["hotel", "web"],
            "allowed_capabilities": ["hotel_search", "web"],
            "user_message": "帮我查上海明天入住住2晚的酒店",
            "contract": {},
        }

        assert adapter._enterprise_direct_hotel_args(inputs) is None

    def test_enterprise_turn_inputs_maps_mcp_capabilities_to_toolsets(self, monkeypatch, tmp_path):
        from gateway.platforms.api_server import APIServerAdapter
        from gateway.config import PlatformConfig
        import gateway.enterprise_workspace as enterprise_workspace

        monkeypatch.setattr(enterprise_workspace, "get_hermes_home", lambda: tmp_path)
        adapter = APIServerAdapter(PlatformConfig())

        inputs = adapter._enterprise_turn_inputs({
            "version": "enterprise-hermes-consumer-v1",
            "requestId": "req-mcp",
            "user": {"id": "staff-1", "type": "staff"},
            "session": {"id": "chat-1"},
            "message": {"role": "user", "content": "查询订单和城市"},
            "runtimePolicy": {
                "allowedCapabilityRefs": [
                    "order_search",
                    "order_biQuery",
                    "order_points",
                    "hotel_package_search",
                    "knowledge_search",
                    "user_info",
                    "car_serviceCities",
                    "hotel_rates",
                    "hotel_rateRule",
                    "enterprise_resultQuery",
                    "hotelux_hotel_policy",
                    "charmdeer_support_playbook",
                    "charmdeer_hotel_policy",
                    "coupon_status",
                    "promotion_explain",
                    "member_entitlement",
                    "afternoonTea_status",
                    "points_balance",
                    "case_precheck",
                    "case_handoff",
                    "activityResult_search",
                    "activityResult_count",
                    "resolver_city",
                    "resolver_paymentType",
                    "hotelux_support_playbook",
                ]
            },
            "credentialBroker": {
                "credentialRef": "cred-1",
                "scope": [
                    "order_search",
                    "order_biQuery",
                    "order_points",
                    "hotel_package_search",
                    "knowledge_search",
                    "user_info",
                    "car_serviceCities",
                    "hotel_rates",
                    "hotel_rateRule",
                    "enterprise_resultQuery",
                    "hotelux_hotel_policy",
                    "charmdeer_support_playbook",
                    "charmdeer_hotel_policy",
                    "coupon_status",
                    "promotion_explain",
                    "member_entitlement",
                    "afternoonTea_status",
                    "points_balance",
                    "case_precheck",
                    "case_handoff",
                    "activityResult_search",
                    "activityResult_count",
                    "resolver_city",
                    "resolver_paymentType",
                    "hotelux_support_playbook",
                ],
            },
        })

        assert inputs["enterprise_toolsets"] == [
            "activity_result_count",
            "activity_result_search",
            "afternoon_tea_status",
            "car_service_cities",
            "case_handoff",
            "case_precheck",
            "charmdeer_hotel_policy",
            "charmdeer_support_playbook",
            "coupon_status",
            "enterprise_result_query",
            "hotel_package_search",
            "hotel_rate_rule",
            "hotel_rates",
            "hotelux_hotel_policy",
            "hotelux_support_playbook",
            "knowledge_search",
            "member_entitlement",
            "order_bi_query",
            "order_points",
            "order_search",
            "points_balance",
            "promotion_explain",
            "resolver_city",
            "resolver_payment_type",
            "user_info",
        ]
        assert set(inputs["credential_toolsets"]) <= set(inputs["enterprise_toolsets"])
        assert "afternoon_tea_status" in inputs["credential_toolsets"]
        assert "hotel_rate_rule" in inputs["credential_toolsets"]

    def test_enterprise_direct_hotel_disabled_when_profile_context_needed(self):
        from gateway.platforms.api_server import APIServerAdapter
        from gateway.config import PlatformConfig

        adapter = APIServerAdapter(PlatformConfig())
        inputs = {
            "enterprise_toolsets": ["hotel"],
            "allowed_capabilities": ["hotel_search"],
            "user_message": "按我的偏好查上海明天入住住2晚的酒店",
            "contract": {},
        }

        assert adapter._enterprise_direct_hotel_args(inputs) is None

    def test_enterprise_direct_hotel_does_not_open_session_db(self, monkeypatch):
        from gateway.platforms.api_server import APIServerAdapter
        from gateway.config import PlatformConfig

        adapter = APIServerAdapter(PlatformConfig())
        opened = False

        def _open_db(_path):
            nonlocal opened
            opened = True
            return object()

        monkeypatch.setattr(adapter, "_ensure_enterprise_session_db", _open_db)
        inputs = {
            "enterprise_toolsets": ["hotel"],
            "allowed_capabilities": ["hotel_search"],
            "user_message": "帮我查上海明天入住住2晚的酒店",
            "contract": {},
        }

        args = adapter._enterprise_direct_hotel_args(inputs)

        assert args is not None
        assert opened is False

    def test_enterprise_turn_inputs_keep_direct_hotel_context_minimal(self, monkeypatch, tmp_path):
        from gateway.platforms.api_server import APIServerAdapter
        from gateway.config import PlatformConfig
        import gateway.enterprise_workspace as enterprise_workspace

        monkeypatch.delenv("HERMES_ENTERPRISE_DIRECT_HOTEL_SEARCH", raising=False)
        monkeypatch.setattr(enterprise_workspace, "get_hermes_home", lambda: tmp_path)
        adapter = APIServerAdapter(PlatformConfig())

        inputs = adapter._enterprise_turn_inputs({
            "version": "enterprise-hermes-consumer-v1",
            "requestId": "req-1",
            "user": {"id": "staff-1", "type": "user"},
            "session": {"id": "chat-1"},
            "message": {
                "role": "user",
                "content": "查上海明天入住住2晚的酒店",
            },
            "runtimePolicy": {"allowedCapabilityRefs": ["hotel_search"]},
            "credentialBroker": {
                "credentialRef": "cred-1",
                "ttlSeconds": 300,
                "scope": ["hotel_search"],
            },
        })

        assert inputs["direct_hotel_args"] is not None
        assert inputs["system_prompt"] == ""
        assert inputs["skip_memory"] is False
        assert "contract" not in inputs["enterprise_context"]
        assert not (tmp_path / "enterprise" / "users" / "staff-1").exists()

    def test_enterprise_hotel_turn_skips_history_for_standalone_forced_tool(self, monkeypatch, tmp_path):
        from gateway.platforms.api_server import APIServerAdapter
        from gateway.config import PlatformConfig
        import gateway.enterprise_workspace as enterprise_workspace

        monkeypatch.setenv("HERMES_ENTERPRISE_DIRECT_HOTEL_SEARCH", "false")
        monkeypatch.delenv("HERMES_ENTERPRISE_SKIP_HISTORY_FOR_FORCED_HOTEL_TOOL", raising=False)
        monkeypatch.setattr(enterprise_workspace, "get_hermes_home", lambda: tmp_path)
        adapter = APIServerAdapter(PlatformConfig())

        inputs = adapter._enterprise_turn_inputs({
            "version": "enterprise-hermes-consumer-v1",
            "requestId": "req-2",
            "user": {"id": "staff-1", "type": "user"},
            "session": {"id": "chat-1"},
            "message": {"role": "user", "content": "帮我查上海明天入住的酒店"},
            "runtimePolicy": {"allowedCapabilityRefs": ["hotel_search"]},
            "credentialBroker": {"credentialRef": "cred-1", "scope": ["hotel_search"]},
        })

        assert inputs["direct_hotel_args"] is None
        assert inputs["request_overrides"]["tool_choice"]["function"]["name"] == "hotel_search"
        assert inputs["skip_history"] is True
        assert inputs["skip_memory"] is False

    def test_enterprise_hotel_turn_keeps_history_when_message_refers_to_previous_context(self, monkeypatch, tmp_path):
        from gateway.platforms.api_server import APIServerAdapter
        from gateway.config import PlatformConfig
        import gateway.enterprise_workspace as enterprise_workspace

        monkeypatch.setenv("HERMES_ENTERPRISE_DIRECT_HOTEL_SEARCH", "false")
        monkeypatch.delenv("HERMES_ENTERPRISE_SKIP_HISTORY_FOR_FORCED_HOTEL_TOOL", raising=False)
        monkeypatch.setattr(enterprise_workspace, "get_hermes_home", lambda: tmp_path)
        adapter = APIServerAdapter(PlatformConfig())

        inputs = adapter._enterprise_turn_inputs({
            "version": "enterprise-hermes-consumer-v1",
            "requestId": "req-3",
            "user": {"id": "staff-1", "type": "user"},
            "session": {"id": "chat-1"},
            "message": {"role": "user", "content": "上次那个酒店明天还有房吗"},
            "runtimePolicy": {"allowedCapabilityRefs": ["hotel_search"]},
            "credentialBroker": {"credentialRef": "cred-1", "scope": ["hotel_search"]},
        })

        assert inputs["request_overrides"]["tool_choice"]["function"]["name"] == "hotel_search"
        assert inputs["skip_history"] is False
        assert inputs["skip_memory"] is False
