import json
from datetime import datetime, timedelta

from emike.config import AppConfig, HardwareConfig, SonicConfig
from emike.tools import (
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
