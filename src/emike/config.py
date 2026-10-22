from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass(slots=True)
class LLMConfig:
    endpoint: str
    model: str
    api_key_env: str = "LLM_API_KEY"
    provider: str = "openai_compatible"
    temperature: float = 0.6
    max_tokens: int = 220
    timeout_seconds: int = 90


@dataclass(slots=True)
class STTConfig:
    model: str = "base.en"
    language: str = "en"
    device: str = "cpu"
    compute_type: str = "int8"


@dataclass(slots=True)
class TTSConfig:
    enabled: bool = True
    voice_model_path: str = "voices/en_US-lessac-medium.onnx"


@dataclass(slots=True)
class HardwareConfig:
    button_gpio: int = 17
    led_gpio: int | None = 27


@dataclass(slots=True)
class AppConfig:
    system_prompt: str
    llm: LLMConfig
    stt: STTConfig
    tts: TTSConfig
    hardware: HardwareConfig
    enabled_tools: list[str]
    default_location: str = "Seattle"
    search_provider: str = "auto"
    search_api_key_env: str = "BRAVE_API_KEY"
    firecrawl_api_key_env: str = "FIRECRAWL_API_KEY"
    firecrawl_search_url: str = "https://api.firecrawl.dev/v1/search"
    search_timeout_seconds: int = 10
    weather_timeout_seconds: int = 10
    debug_mode: bool = False
    history_turns: int = 6
    conversation_timeout_seconds: int = 120


DEFAULT_CONFIG_PATH = Path("config/assistant.yaml")


def _require(mapping: dict[str, object], key: str) -> object:
    if key not in mapping:
        raise ValueError(f"Missing required config key: {key}")
    return mapping[key]


def load_config(path: Path = DEFAULT_CONFIG_PATH) -> AppConfig:
    with path.open("r", encoding="utf-8") as file:
        raw = yaml.safe_load(file)

    if not isinstance(raw, dict):
        raise TypeError("Config root must be a mapping")

    llm_raw = _require(raw, "llm")
    if not isinstance(llm_raw, dict):
        raise TypeError("llm must be a mapping")

    stt_raw = raw.get("stt", {})
    if not isinstance(stt_raw, dict):
        raise TypeError("stt must be a mapping")

    tts_raw = raw.get("tts", {})
    if not isinstance(tts_raw, dict):
        raise TypeError("tts must be a mapping")

    hw_raw = raw.get("hardware", {})
    if not isinstance(hw_raw, dict):
        raise TypeError("hardware must be a mapping")

    enabled_tools_raw = raw.get("enabled_tools", ["weather", "search", "timers"])
    if not isinstance(enabled_tools_raw, list):
        raise TypeError("enabled_tools must be a list")

    return AppConfig(
        system_prompt=str(_require(raw, "system_prompt")).strip(),
        llm=LLMConfig(
            endpoint=str(_require(llm_raw, "endpoint")),
            model=str(_require(llm_raw, "model")),
            api_key_env=str(llm_raw.get("api_key_env", "LLM_API_KEY")),
            provider=str(llm_raw.get("provider", "openai_compatible")),
            temperature=float(llm_raw.get("temperature", 0.6)),
            max_tokens=int(llm_raw.get("max_tokens", 220)),
            timeout_seconds=int(llm_raw.get("timeout_seconds", 90)),
        ),
        stt=STTConfig(
            model=str(stt_raw.get("model", "base.en")),
            language=str(stt_raw.get("language", "en")),
            device=str(stt_raw.get("device", "cpu")),
            compute_type=str(stt_raw.get("compute_type", "int8")),
        ),
        tts=TTSConfig(
            enabled=bool(tts_raw.get("enabled", True)),
            voice_model_path=str(tts_raw.get("voice_model_path", "voices/en_US-lessac-medium.onnx")),
        ),
        hardware=HardwareConfig(
            button_gpio=int(hw_raw.get("button_gpio", 17)),
            led_gpio=(None if hw_raw.get("led_gpio") is None else int(hw_raw["led_gpio"])),
        ),
        enabled_tools=[str(tool).strip() for tool in enabled_tools_raw],
        default_location=str(raw.get("default_location", "Seattle")),
        search_provider=str(raw.get("search_provider", "auto")),
        search_api_key_env=str(raw.get("search_api_key_env", "BRAVE_API_KEY")),
        firecrawl_api_key_env=str(raw.get("firecrawl_api_key_env", "FIRECRAWL_API_KEY")),
        firecrawl_search_url=str(
            raw.get("firecrawl_search_url", "https://api.firecrawl.dev/v1/search")
        ),
        search_timeout_seconds=int(raw.get("search_timeout_seconds", 10)),
        weather_timeout_seconds=int(raw.get("weather_timeout_seconds", 10)),
        debug_mode=bool(raw.get("debug_mode", False)),
        history_turns=int(raw.get("history_turns", 6)),
        conversation_timeout_seconds=int(raw.get("conversation_timeout_seconds", 120)),
    )
