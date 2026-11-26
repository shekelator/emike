import json
from datetime import datetime, timedelta
from typing import Any

import pytest

from emike.config import AppConfig, HardwareConfig, MessagingConfig, SonicConfig
from emike.messaging import TransportError
from emike.tools import (
    ToolRouter,
    _current_time_result,
    _location_cache_key,
    _parse_duration_seconds,
    _resolve_forecast_date,
)


def test_parse_duration_seconds_multiple_units() -> None:
    assert _parse_duration_seconds("set a timer for 1 hour 2 minutes 3 seconds") == 3723


def test_resolve_forecast_date_tomorrow() -> None:
    today = datetime.now().date()
    assert _resolve_forecast_date("tomorrow") == today + timedelta(days=1)


def test_resolve_forecast_date_weekday() -> None:
    resolved = _resolve_forecast_date("thursday")
    assert resolved is not None
    assert resolved.weekday() == 3


def test_location_cache_key_normalizes_spacing_and_case() -> None:
    assert _location_cache_key("  Jamaica   Plain, MA   02130 ") == "jamaica plain, ma 02130"


def _config_with_tools(*tools: str) -> AppConfig:
    return AppConfig(
        system_prompt="test prompt",
        sonic=SonicConfig(),
        hardware=HardwareConfig(),
        enabled_tools=list(tools),
    )


def test_current_time_result_format() -> None:
    result = _current_time_result()
    assert result.startswith("Current local time is ")
    assert " on " in result
    assert "The time zone is " in result


def test_nova_tool_definitions_include_time_and_map_to_tool_spec() -> None:
    router = _router_with_tools("weather", "search", "time")
    specs = router.get_nova_tool_definitions()

    names = [spec["toolSpec"]["name"] for spec in specs]
    assert names == ["get_current_time", "get_weather", "get_weather_forecast", "web_search"]
    get_time = specs[0]["toolSpec"]
    assert get_time["description"]
    # Nova Sonic requires the JSON Schema stringified inside inputSchema.json.
    assert json.loads(get_time["inputSchema"]["json"]) == {"type": "object", "properties": {}}


def test_nova_tool_definitions_exclude_disabled_tools() -> None:
    router = _router_with_tools("time")
    names = [spec["toolSpec"]["name"] for spec in router.get_nova_tool_definitions()]
    assert names == ["get_current_time"]

def _router_with_tools(*tools: str):

    from emike.tools import ToolRouter

    return ToolRouter(_config_with_tools(*tools))


# --- send_message tool -------------------------------------------------------


class FakeTransport:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    def send(self, chat_id: str, text: str) -> None:
        self.sent.append((chat_id, text))

    def poll(self, offset: int) -> list[Any]:
        return []


def _messaging_router(
    *tools: str,
    recipients: dict[str, str] | None = None,
    transport: FakeTransport | None = None,
    rate_per_minute: int = 5,
) -> ToolRouter:
    return ToolRouter(
        AppConfig(
            system_prompt="test prompt",
            sonic=SonicConfig(),
            hardware=HardwareConfig(),
            enabled_tools=list(tools),
            messaging=MessagingConfig(
                enabled=True,
                recipients=recipients if recipients is not None else {"mort": "123456789"},
                send_rate_per_minute=rate_per_minute,
            ),
        ),
        transport=transport,
    )


def test_send_message_definition_shape() -> None:
    router = _messaging_router("messaging", transport=FakeTransport())
    specs = router.get_nova_tool_definitions()
    names = [spec["toolSpec"]["name"] for spec in specs]
    assert names == ["send_message"]
    schema = json.loads(specs[0]["toolSpec"]["inputSchema"]["json"])
    assert schema["required"] == ["recipient", "message"]
    assert set(schema["properties"]) == {"recipient", "message"}


def test_send_message_not_registered_without_transport() -> None:
    router = _messaging_router("messaging")
    names = [d["function"]["name"] for d in router.get_tool_definitions()]
    assert "send_message" not in names


def test_send_message_not_registered_when_not_enabled() -> None:
    router = _messaging_router("time", transport=FakeTransport())
    names = [d["function"]["name"] for d in router.get_tool_definitions()]
    assert "send_message" not in names
    result = router.execute_tool_call("send_message", {"recipient": "mort", "message": "hi"})
    assert "sent" not in result.lower()


def test_send_message_unknown_recipient_does_not_send() -> None:
    transport = FakeTransport()
    router = _messaging_router("messaging", transport=transport)
    result = router.execute_tool_call(
        "send_message", {"recipient": "bob", "message": "hello"}
    )
    assert result == "Unknown recipient. Known people: mort."
    assert transport.sent == []


def test_send_message_success_uses_recipient_and_capitalized_name() -> None:
    transport = FakeTransport()
    router = _messaging_router("messaging", transport=transport)
    result = router.execute_tool_call(
        "send_message", {"recipient": "mort", "message": "pizza at six"}
    )
    assert result == "Message sent to Mort's phone."
    assert transport.sent == [("123456789", "pizza at six")]


def test_send_message_rate_limit_result_is_conversational() -> None:
    transport = FakeTransport()
    router = _messaging_router("messaging", transport=transport, rate_per_minute=1)
    first = router.execute_tool_call("send_message", {"recipient": "mort", "message": "hi"})
    assert first.startswith("Message sent")
    second = router.execute_tool_call("send_message", {"recipient": "mort", "message": "again"})
    assert second == "Send limit reached. Try again in a few minutes."
    assert len(transport.sent) == 1


def test_send_message_transport_error_propagates_redacted() -> None:
    class FailingTransport:
        def send(self, chat_id: str, text: str) -> None:
            raise TransportError("[REDACTED] sendMessage failed")

        def poll(self, offset: int) -> list[Any]:
            return []

    router = _messaging_router("messaging", transport=FailingTransport())  # type: ignore[arg-type]
    with pytest.raises(TransportError):
        router.execute_tool_call("send_message", {"recipient": "mort", "message": "hi"})
