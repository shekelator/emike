from __future__ import annotations

import os
import re
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, cast

import requests

from emike.config import AppConfig

_DURATION_PATTERN = re.compile(r"(\d+)\s*(seconds?|minutes?|hours?)", re.IGNORECASE)


@dataclass(slots=True)
class ActiveTimer:
    timer_id: str
    duration_seconds: int
    label: str
    created_at: float
    due_at: float


class TimerManager:
    def __init__(self, on_elapsed: Callable[[str], None]) -> None:
        self._on_elapsed = on_elapsed
        self._timers: dict[str, tuple[ActiveTimer, threading.Timer]] = {}
        self._lock = threading.Lock()

    def create_timer(self, duration_seconds: int, label: str) -> ActiveTimer:
        if duration_seconds <= 0:
            raise ValueError("Timer duration must be positive")

        now = time.time()
        timer_id = uuid.uuid4().hex[:8]
        active = ActiveTimer(
            timer_id=timer_id,
            duration_seconds=duration_seconds,
            label=label,
            created_at=now,
            due_at=now + duration_seconds,
        )

        def notify() -> None:
            with self._lock:
                self._timers.pop(timer_id, None)
            label_text = f" for {label}" if label else ""
            self._on_elapsed(
                f"Timer {timer_id} finished{label_text} after {duration_seconds} seconds."
            )

        timer = threading.Timer(duration_seconds, notify)
        timer.daemon = True
        timer.start()

        with self._lock:
            self._timers[timer_id] = (active, timer)

        return active

    def list_timers(self) -> list[ActiveTimer]:
        with self._lock:
            entries = [entry[0] for entry in self._timers.values()]
        return sorted(entries, key=lambda item: item.due_at)

    def cancel_timer(self, timer_id: str) -> bool:
        with self._lock:
            entry = self._timers.pop(timer_id, None)
        if entry is None:
            return False
        _, timer = entry
        timer.cancel()
        return True


class WeatherService:
    def __init__(self, timeout_seconds: int) -> None:
        self._timeout_seconds = timeout_seconds
        self._location_cache: dict[str, tuple[float, float, str, str, str]] = {}
        self._cache_lock = threading.Lock()

    def current_weather(self, location: str) -> str:
        resolved = self._resolve_location(location)
        if resolved is None:
            return f"I could not find that location: {location}."

        latitude, longitude, place_name, country, _ = resolved

        weather_params: dict[str, str | int | float] = {
            "latitude": latitude,
            "longitude": longitude,
            "current": "temperature_2m,apparent_temperature,weather_code,wind_speed_10m",
            "timezone": "auto",
            "temperature_unit": "fahrenheit",
            "wind_speed_unit": "mph",
        }
        weather_resp = requests.get(
            "https://api.open-meteo.com/v1/forecast",
            params=weather_params,
            timeout=self._timeout_seconds,
        )
        weather_resp.raise_for_status()
        payload = weather_resp.json()
        current = payload.get("current", {})
        if not current:
            return f"I could not fetch current weather for {location}."

        temp = current.get("temperature_2m")
        feels = current.get("apparent_temperature")
        wind = current.get("wind_speed_10m")
        code = int(current.get("weather_code", -1))
        description = _weather_code_to_text(code)

        region = f", {country}" if country else ""
        return (
            f"Current weather in {place_name}{region}: {description}, "
            f"temperature {temp} F, feels like {feels} F, wind {wind} miles per hour."
        )

    def forecast(self, location: str, forecast_for: str) -> str:
        resolved = self._resolve_location(location)
        if resolved is None:
            return f"I could not find that location: {location}."

        latitude, longitude, place_name, country, timezone_name = resolved

        daily_params: dict[str, str | int | float] = {
            "latitude": latitude,
            "longitude": longitude,
            "daily": "temperature_2m_max,temperature_2m_min,weather_code,precipitation_probability_max",
            "timezone": timezone_name,
            "forecast_days": 14,
            "temperature_unit": "fahrenheit",
        }
        forecast_resp = requests.get(
            "https://api.open-meteo.com/v1/forecast",
            params=daily_params,
            timeout=self._timeout_seconds,
        )
        forecast_resp.raise_for_status()
        payload = forecast_resp.json()

        daily = payload.get("daily", {})
        times = daily.get("time", [])
        highs = daily.get("temperature_2m_max", [])
        lows = daily.get("temperature_2m_min", [])
        codes = daily.get("weather_code", [])
        precip_probs = daily.get("precipitation_probability_max", [])

        if not (
            isinstance(times, list)
            and isinstance(highs, list)
            and isinstance(lows, list)
            and isinstance(codes, list)
            and isinstance(precip_probs, list)
        ):
            return f"I could not fetch forecast data for {location}."

        target = _resolve_forecast_date(forecast_for)
        if target is None:
            return (
                "I could not understand the requested forecast day. "
                "Try today, tomorrow, a weekday name, or YYYY-MM-DD."
            )
        target_iso = target.isoformat()

        if target_iso not in times:
            return (
                f"I could not find forecast data for {forecast_for}. "
                "Forecast range is up to the next 14 days."
            )

        idx = times.index(target_iso)
        description = _weather_code_to_text(int(codes[idx]))
        high = highs[idx]
        low = lows[idx]
        precip = precip_probs[idx]

        label = _human_day_label(target)
        region = f", {country}" if country else ""
        return (
            f"Forecast for {label} in {place_name}{region}: {description}. "
            f"High {high} F and low {low} F. "
            f"Precipitation chance up to {precip} percent."
        )

    def _resolve_location(self, location: str) -> tuple[float, float, str, str, str] | None:
        cache_key = _location_cache_key(location)
        with self._cache_lock:
            cached = self._location_cache.get(cache_key)
        if cached is not None:
            return cached

        geo_params: dict[str, str | int] = {
            "name": location,
            "count": 1,
            "language": "en",
            "format": "json",
        }
        geo_resp = requests.get(
            "https://geocoding-api.open-meteo.com/v1/search",
            params=geo_params,
            timeout=self._timeout_seconds,
        )
        geo_resp.raise_for_status()
        geo = geo_resp.json()
        results = geo.get("results", [])
        if not isinstance(results, list) or not results:
            return None

        top = results[0]
        if not isinstance(top, dict):
            return None

        latitude = float(top.get("latitude", 0.0))
        longitude = float(top.get("longitude", 0.0))
        place_name = str(top.get("name", location))
        country = str(top.get("country", ""))
        timezone_name = str(top.get("timezone", "auto"))
        resolved = (latitude, longitude, place_name, country, timezone_name)
        with self._cache_lock:
            self._location_cache[cache_key] = resolved
        return resolved


class SearchService:
    def __init__(
        self,
        provider: str,
        api_key_env: str,
        firecrawl_api_key_env: str,
        firecrawl_search_url: str,
        timeout_seconds: int,
    ) -> None:
        self._provider = provider.lower()
        self._api_key_env = api_key_env
        self._firecrawl_api_key_env = firecrawl_api_key_env
        self._firecrawl_search_url = firecrawl_search_url
        self._timeout_seconds = timeout_seconds

    def web_search(self, query: str) -> str:
        firecrawl_api_key = os.getenv(self._firecrawl_api_key_env)
        brave_api_key = os.getenv(self._api_key_env)

        if self._provider in {"firecrawl", "auto"} and firecrawl_api_key:
            try:
                return self._firecrawl_search(query, firecrawl_api_key)
            except requests.RequestException:
                if self._provider == "firecrawl":
                    raise

        if self._provider in {"brave", "auto"} and brave_api_key:
            try:
                return self._brave_search(query, brave_api_key)
            except requests.RequestException:
                if self._provider == "brave":
                    raise

        if self._provider in {"duckduckgo", "auto", "firecrawl", "brave"}:
            return self._duckduckgo_search(query)

        return self._duckduckgo_search(query)

    def _firecrawl_search(self, query: str, api_key: str) -> str:
        payload: dict[str, str | int] = {
            "query": query,
            "limit": 3,
        }
        resp = requests.post(
            self._firecrawl_search_url,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=self._timeout_seconds,
        )
        resp.raise_for_status()
        parsed = resp.json()

        data = parsed.get("data", [])
        if not isinstance(data, list) or not data:
            return f"No search results found for {query}."

        lines = []
        for item in data[:3]:
            if not isinstance(item, dict):
                continue
            metadata = item.get("metadata", {})
            metadata_title = ""
            if isinstance(metadata, dict):
                metadata_title = str(metadata.get("title", ""))
            title = str(item.get("title", metadata_title or "Untitled result"))
            url = str(item.get("url", item.get("sourceURL", "")))
            description = str(
                item.get("description", item.get("markdown", item.get("content", "")))
            ).strip()
            if len(description) > 220:
                description = description[:217].rstrip() + "..."
            lines.append(f"- {title}: {description} ({url})")

        if not lines:
            return f"No search results found for {query}."
        return "Top results:\n" + "\n".join(lines)

    def _brave_search(self, query: str, api_key: str) -> str:
        search_params: dict[str, str | int] = {"q": query, "count": 3}
        resp = requests.get(
            "https://api.search.brave.com/res/v1/web/search",
            headers={"Accept": "application/json", "X-Subscription-Token": api_key},
            params=search_params,
            timeout=self._timeout_seconds,
        )
        resp.raise_for_status()
        payload = resp.json()
        web = payload.get("web", {}).get("results", [])
        if not web:
            return f"No search results found for {query}."

        lines = []
        for result in web[:3]:
            title = str(result.get("title", "Untitled result"))
            url = str(result.get("url", ""))
            description = str(result.get("description", "")).strip()
            lines.append(f"- {title}: {description} ({url})")

        return "Top results:\n" + "\n".join(lines)

    def _duckduckgo_search(self, query: str) -> str:
        ddg_params: dict[str, str | int] = {
            "q": query,
            "format": "json",
            "no_html": 1,
            "skip_disambig": 1,
        }
        resp = requests.get(
            "https://api.duckduckgo.com/",
            params=ddg_params,
            timeout=self._timeout_seconds,
        )
        resp.raise_for_status()
        payload = resp.json()
        abstract = str(payload.get("AbstractText", "")).strip()
        abstract_url = str(payload.get("AbstractURL", "")).strip()
        if abstract:
            if abstract_url:
                return f"Search summary: {abstract} Source: {abstract_url}"
            return f"Search summary: {abstract}"

        related = cast(list[object], payload.get("RelatedTopics", []))
        if isinstance(related, list) and related:
            first = related[0]
            if isinstance(first, dict):
                text = str(first.get("Text", "")).strip()
                url = str(first.get("FirstURL", "")).strip()
                if text:
                    if url:
                        return f"Search result: {text} Source: {url}"
                    return f"Search result: {text}"
        return f"No search results found for {query}."


class ToolRouter:
    def __init__(self, config: AppConfig, on_timer_elapsed: Callable[[str], None]) -> None:
        self._enabled = {name.lower() for name in config.enabled_tools}
        self._default_location = config.default_location
        self._timers = TimerManager(on_timer_elapsed)
        self._weather = WeatherService(timeout_seconds=config.weather_timeout_seconds)
        self._search = SearchService(
            provider=config.search_provider,
            api_key_env=config.search_api_key_env,
            firecrawl_api_key_env=config.firecrawl_api_key_env,
            firecrawl_search_url=config.firecrawl_search_url,
            timeout_seconds=config.search_timeout_seconds,
        )

    def get_tool_definitions(self) -> list[dict[str, Any]]:
        tools: list[dict[str, Any]] = []

        if "weather" in self._enabled:
            tools.append(
                {
                    "type": "function",
                    "function": {
                        "name": "get_weather",
                        "description": "Get current weather for a specific location.",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "location": {
                                    "type": "string",
                                    "description": (
                                        "Optional city or location name, for example Seattle. "
                                        "If omitted, uses default location from config."
                                    ),
                                }
                            },
                        },
                    },
                }
            )
            tools.append(
                {
                    "type": "function",
                    "function": {
                        "name": "get_weather_forecast",
                        "description": (
                            "Get weather forecast for a target day. "
                            "Use for questions about tomorrow or weekday highs and lows."
                        ),
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "location": {
                                    "type": "string",
                                    "description": (
                                        "Optional city or location name, for example Seattle. "
                                        "If omitted, uses default location from config."
                                    ),
                                },
                                "forecast_for": {
                                    "type": "string",
                                    "description": (
                                        "Requested day such as today, tomorrow, Thursday, "
                                        "or YYYY-MM-DD."
                                    ),
                                },
                            },
                            "required": ["forecast_for"],
                        },
                    },
                }
            )

        if "search" in self._enabled:
            tools.append(
                {
                    "type": "function",
                    "function": {
                        "name": "web_search",
                        "description": "Search the web for current information.",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "query": {
                                    "type": "string",
                                    "description": "Search query text.",
                                }
                            },
                            "required": ["query"],
                        },
                    },
                }
            )

        if "timers" in self._enabled or "timer" in self._enabled:
            tools.extend(
                [
                    {
                        "type": "function",
                        "function": {
                            "name": "set_timer",
                            "description": "Create a timer. Provide duration_seconds, or duration_text like '5 minutes'.",
                            "parameters": {
                                "type": "object",
                                "properties": {
                                    "duration_seconds": {
                                        "type": "integer",
                                        "description": "Timer length in whole seconds.",
                                    },
                                    "duration_text": {
                                        "type": "string",
                                        "description": "Timer duration text like '10 minutes'.",
                                    },
                                    "label": {
                                        "type": "string",
                                        "description": "Optional human label for the timer.",
                                    },
                                },
                            },
                        },
                    },
                    {
                        "type": "function",
                        "function": {
                            "name": "list_timers",
                            "description": "List active timers and remaining seconds.",
                            "parameters": {"type": "object", "properties": {}},
                        },
                    },
                    {
                        "type": "function",
                        "function": {
                            "name": "cancel_timer",
                            "description": "Cancel a timer by timer_id.",
                            "parameters": {
                                "type": "object",
                                "properties": {
                                    "timer_id": {
                                        "type": "string",
                                        "description": "Timer id returned by set_timer or list_timers.",
                                    }
                                },
                                "required": ["timer_id"],
                            },
                        },
                    },
                ]
            )

        return tools

    def execute_tool_call(self, tool_name: str, args: dict[str, Any]) -> str:
        if tool_name == "get_weather":
            location = str(args.get("location", self._default_location)).strip()
            if not location:
                location = self._default_location
            return self._weather.current_weather(location)

        if tool_name == "get_weather_forecast":
            location = str(args.get("location", self._default_location)).strip()
            forecast_for = str(args.get("forecast_for", "tomorrow")).strip()
            if not location:
                location = self._default_location
            if not forecast_for:
                forecast_for = "tomorrow"
            return self._weather.forecast(location, forecast_for)

        if tool_name == "web_search":
            query = str(args.get("query", "")).strip()
            if not query:
                return "Missing query for web search."
            return self._search.web_search(query)

        if tool_name == "set_timer":
            duration_seconds = _duration_from_args(args)
            if duration_seconds is None:
                return "Could not parse timer duration."
            label = str(args.get("label", "")).strip()
            timer = self._timers.create_timer(duration_seconds, label)
            label_suffix = f" ({label})" if label else ""
            return f"Started timer {timer.timer_id}{label_suffix} for {duration_seconds} seconds."

        if tool_name == "list_timers":
            timers = self._timers.list_timers()
            if not timers:
                return "No active timers."
            now = time.time()
            lines = []
            for item in timers:
                remaining = max(0, int(item.due_at - now))
                label = f" ({item.label})" if item.label else ""
                lines.append(f"{item.timer_id}{label}: {remaining} seconds remaining")
            return "Active timers: " + "; ".join(lines)

        if tool_name == "cancel_timer":
            timer_id = str(args.get("timer_id", "")).strip().lower()
            if not timer_id:
                return "Missing timer_id for cancellation."
            if self._timers.cancel_timer(timer_id):
                return f"Cancelled timer {timer_id}."
            return f"I could not find timer {timer_id}."

        return f"Unknown tool '{tool_name}'."


def _parse_duration_seconds(text: str) -> int | None:
    matches = list(_DURATION_PATTERN.finditer(text))
    if not matches:
        return None

    total = 0
    for match in matches:
        amount = int(match.group(1))
        unit = match.group(2).lower()
        if unit.startswith("hour"):
            total += amount * 3600
        elif unit.startswith("minute"):
            total += amount * 60
        else:
            total += amount
    return total if total > 0 else None


def _duration_from_args(args: dict[str, Any]) -> int | None:
    value = args.get("duration_seconds")
    if isinstance(value, int):
        return value if value > 0 else None

    text = args.get("duration_text")
    if isinstance(text, str):
        return _parse_duration_seconds(text)

    return None


def _resolve_forecast_date(value: str) -> date | None:
    text = value.strip().lower()
    if not text:
        return None

    today = datetime.now().date()
    if text == "today":
        return today
    if text == "tomorrow":
        return today + timedelta(days=1)

    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        pass

    weekdays = {
        "monday": 0,
        "tuesday": 1,
        "wednesday": 2,
        "thursday": 3,
        "friday": 4,
        "saturday": 5,
        "sunday": 6,
    }

    if text in weekdays:
        target_weekday = weekdays[text]
        delta = (target_weekday - today.weekday()) % 7
        if delta == 0:
            delta = 7
        return today + timedelta(days=delta)

    if text.startswith("next "):
        day_name = text[5:]
        if day_name in weekdays:
            target_weekday = weekdays[day_name]
            delta = (target_weekday - today.weekday()) % 7
            if delta == 0:
                delta = 7
            return today + timedelta(days=delta)

    return None


def _human_day_label(target: date) -> str:
    today = datetime.now().date()
    if target == today:
        return "today"
    if target == today + timedelta(days=1):
        return "tomorrow"
    return target.strftime("%A")


def _location_cache_key(location: str) -> str:
    return " ".join(location.strip().lower().split())


def _weather_code_to_text(code: int) -> str:
    mapping = {
        0: "clear sky",
        1: "mainly clear",
        2: "partly cloudy",
        3: "overcast",
        45: "fog",
        48: "depositing rime fog",
        51: "light drizzle",
        53: "moderate drizzle",
        55: "dense drizzle",
        61: "slight rain",
        63: "moderate rain",
        65: "heavy rain",
        71: "slight snow",
        73: "moderate snow",
        75: "heavy snow",
        80: "slight rain showers",
        81: "moderate rain showers",
        82: "violent rain showers",
        95: "thunderstorm",
        96: "thunderstorm with slight hail",
        99: "thunderstorm with heavy hail",
    }
    return mapping.get(code, "unknown conditions")
