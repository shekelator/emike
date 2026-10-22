from __future__ import annotations

import os
import re
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import cast

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

    def current_weather(self, location: str) -> str:
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
        if not results:
            return f"I could not find weather for {location}."

        top = results[0]
        latitude = top["latitude"]
        longitude = top["longitude"]
        place_name = top.get("name", location)
        country = top.get("country", "")

        weather_params: dict[str, str | int | float] = {
            "latitude": latitude,
            "longitude": longitude,
            "current": "temperature_2m,apparent_temperature,weather_code,wind_speed_10m",
            "timezone": "auto",
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
            f"temperature {temp} C, feels like {feels} C, wind {wind} km per hour."
        )


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

    def maybe_handle(self, user_text: str) -> str | None:
        text = user_text.strip()
        low = text.lower()

        if "timers" in self._enabled or "timer" in self._enabled:
            timer_result = self._handle_timers(low, text)
            if timer_result is not None:
                return timer_result

        if "weather" in self._enabled and "weather" in low:
            location = _extract_location(text) or self._default_location
            return self._weather.current_weather(location)

        if "search" in self._enabled:
            query = _extract_search_query(text)
            if query is not None:
                return self._search.web_search(query)

        return None

    def _handle_timers(self, low: str, original: str) -> str | None:
        if low.startswith("list timers") or "what timers" in low:
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

        cancel_match = re.search(r"cancel timer\s+([a-f0-9]{8})", low)
        if cancel_match:
            timer_id = cancel_match.group(1)
            if self._timers.cancel_timer(timer_id):
                return f"Cancelled timer {timer_id}."
            return f"I could not find timer {timer_id}."

        if "set a timer" in low or "set timer" in low or "start a timer" in low:
            duration_seconds = _parse_duration_seconds(original)
            if duration_seconds is None:
                return "Please say the timer duration, for example set a timer for five minutes."
            label_match = re.search(r"called\s+(.+)$", original, re.IGNORECASE)
            label = label_match.group(1).strip() if label_match else ""
            timer = self._timers.create_timer(duration_seconds, label=label)
            return (
                f"Started timer {timer.timer_id} for {duration_seconds} seconds. "
                "Say list timers to check status."
            )

        return None


def _extract_location(text: str) -> str | None:
    match = re.search(r"\bweather\b(?:\s+in\s+|\s+for\s+)(.+)$", text, re.IGNORECASE)
    if not match:
        return None
    location = match.group(1).strip(" ?!.,")
    return location or None


def _extract_search_query(text: str) -> str | None:
    patterns = [
        r"^search for\s+(.+)$",
        r"^look up\s+(.+)$",
        r"^find on web\s+(.+)$",
        r"^web search\s+(.+)$",
    ]
    for pattern in patterns:
        match = re.match(pattern, text, re.IGNORECASE)
        if match:
            query = match.group(1).strip(" ?!.,")
            return query or None
    return None


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
