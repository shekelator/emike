from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass(slots=True)
class SonicConfig:
    aws_region: str = "us-east-1"
    model_id: str = "amazon.nova-2-5-sonic"
    fallback_model_id: str | None = "amazon.nova-2-sonic-v1:0"
    voice_id: str = "tiffany"
    input_sample_rate_hz: int = 16_000
    output_sample_rate_hz: int = 24_000
    chunk_ms: int = 60
    release_tail_ms: int = 600
    max_tokens: int = 1024
    temperature: float = 0.7
    top_p: float = 0.9
    endpointing_sensitivity: str = "MEDIUM"
    duck_mic_while_speaking: bool = True
    duck_resume_ms: int = 300
    prebuffer_ms: int = 150
    open_timeout_seconds: float = 10.0
    turn_timeout_seconds: float = 420.0

    def __post_init__(self) -> None:
        if self.endpointing_sensitivity not in {"LOW", "MEDIUM", "HIGH"}:
            raise ValueError(
                "sonic.endpointing_sensitivity must be one of LOW, MEDIUM, HIGH; "
                f"got {self.endpointing_sensitivity!r}"
            )
        if self.input_sample_rate_hz != 16_000:
            raise ValueError(
                "sonic.input_sample_rate_hz must be 16000 (Nova Sonic input contract); "
                f"got {self.input_sample_rate_hz}"
            )
        if self.output_sample_rate_hz not in {8_000, 16_000, 24_000}:
            raise ValueError(
                "sonic.output_sample_rate_hz must be one of 8000, 16000, 24000; "
                f"got {self.output_sample_rate_hz}"
            )


@dataclass(slots=True)
class HardwareConfig:
    button_gpio: int = 17
    led_gpio: int | None = 27
    input_device: str | int | None = None
    output_device: str | int | None = None


@dataclass(slots=True)
class AppConfig:
    system_prompt: str
    sonic: SonicConfig
    hardware: HardwareConfig
    enabled_tools: list[str]
    default_location: str = "Jamaica Plain, MA 02130"
    search_provider: str = "auto"
    search_api_key_env: str = "BRAVE_API_KEY"
    firecrawl_api_key_env: str = "FIRECRAWL_API_KEY"
    firecrawl_search_url: str = "https://api.firecrawl.dev/v2/search"
    search_timeout_seconds: int = 10
    weather_timeout_seconds: int = 10
    timer_alarm_seconds: int = 6
    debug_mode: bool = False


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

    sonic_raw = _require(raw, "sonic")
    if not isinstance(sonic_raw, dict):
        raise TypeError("sonic must be a mapping")

    hw_raw = raw.get("hardware", {})
    if not isinstance(hw_raw, dict):
        raise TypeError("hardware must be a mapping")

    enabled_tools_raw = raw.get("enabled_tools", ["weather", "search", "time"])
    if not isinstance(enabled_tools_raw, list):
        raise TypeError("enabled_tools must be a list")

    endpointing = str(sonic_raw.get("endpointing_sensitivity", "MEDIUM")).upper()
    input_rate = int(sonic_raw.get("input_sample_rate_hz", 16_000))
    output_rate = int(sonic_raw.get("output_sample_rate_hz", 24_000))
    timer_alarm_seconds = int(raw.get("timer_alarm_seconds", 6))
    if timer_alarm_seconds < 1:
        raise ValueError(f"timer_alarm_seconds must be at least 1; got {timer_alarm_seconds}")

    return AppConfig(
        system_prompt=str(_require(raw, "system_prompt")).strip(),
        sonic=SonicConfig(
            aws_region=str(sonic_raw.get("aws_region", "us-east-1")),
            model_id=str(_require(sonic_raw, "model_id")),
            fallback_model_id=(
                str(sonic_raw["fallback_model_id"]) if sonic_raw.get("fallback_model_id") else None
            ),
            voice_id=str(sonic_raw.get("voice_id", "tiffany")),
            input_sample_rate_hz=input_rate,
            output_sample_rate_hz=output_rate,
            chunk_ms=int(sonic_raw.get("chunk_ms", 60)),
            release_tail_ms=int(sonic_raw.get("release_tail_ms", 600)),
            max_tokens=int(sonic_raw.get("max_tokens", 1024)),
            temperature=float(sonic_raw.get("temperature", 0.7)),
            top_p=float(sonic_raw.get("top_p", 0.9)),
            endpointing_sensitivity=endpointing,
            duck_mic_while_speaking=bool(sonic_raw.get("duck_mic_while_speaking", True)),
            duck_resume_ms=int(sonic_raw.get("duck_resume_ms", 300)),
            prebuffer_ms=int(sonic_raw.get("prebuffer_ms", 150)),
            open_timeout_seconds=float(sonic_raw.get("open_timeout_seconds", 10.0)),
            turn_timeout_seconds=float(sonic_raw.get("turn_timeout_seconds", 420.0)),
        ),
        hardware=HardwareConfig(
            button_gpio=int(hw_raw.get("button_gpio", 17)),
            led_gpio=(None if hw_raw.get("led_gpio") is None else int(hw_raw["led_gpio"])),
            input_device=(None if hw_raw.get("input_device") is None else hw_raw["input_device"]),
            output_device=(None if hw_raw.get("output_device") is None else hw_raw["output_device"]),
        ),
        enabled_tools=[str(tool).strip() for tool in enabled_tools_raw],
        default_location=str(raw.get("default_location", "Jamaica Plain, MA 02130")),
        search_provider=str(raw.get("search_provider", "auto")),
        search_api_key_env=str(raw.get("search_api_key_env", "BRAVE_API_KEY")),
        firecrawl_api_key_env=str(raw.get("firecrawl_api_key_env", "FIRECRAWL_API_KEY")),
        firecrawl_search_url=str(
            raw.get("firecrawl_search_url", "https://api.firecrawl.dev/v2/search")
        ),
        search_timeout_seconds=int(raw.get("search_timeout_seconds", 10)),
        weather_timeout_seconds=int(raw.get("weather_timeout_seconds", 10)),
        timer_alarm_seconds=timer_alarm_seconds,
        debug_mode=bool(raw.get("debug_mode", False)),
    )
