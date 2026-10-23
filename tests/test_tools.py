from datetime import datetime, timedelta

from emike.tools import (
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
