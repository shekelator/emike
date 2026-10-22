from emike.tools import _extract_location, _extract_search_query, _parse_duration_seconds


def test_parse_duration_seconds_multiple_units() -> None:
    assert _parse_duration_seconds("set a timer for 1 hour 2 minutes 3 seconds") == 3723


def test_extract_weather_location() -> None:
    assert _extract_location("what is the weather in Paris?") == "Paris"


def test_extract_search_query() -> None:
    assert _extract_search_query("search for raspberry pi audio setup") == "raspberry pi audio setup"
