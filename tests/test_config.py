from pathlib import Path

import pytest

from emike.config import load_config


def test_load_config_from_example() -> None:
    cfg = load_config(Path("config/assistant.example.yaml"))

    assert cfg.system_prompt
    assert cfg.sonic.aws_region == "us-east-1"
    assert cfg.sonic.model_id == "amazon.nova-2-5-sonic"
    assert cfg.sonic.fallback_model_id == "amazon.nova-2-sonic-v1:0"
    assert cfg.sonic.voice_id == "tiffany"
    assert cfg.sonic.input_sample_rate_hz == 16000
    assert cfg.sonic.output_sample_rate_hz == 24000
    assert cfg.sonic.chunk_ms == 60
    assert cfg.sonic.endpointing_sensitivity == "MEDIUM"
    assert cfg.sonic.turn_timeout_seconds < 480.0  # under the 8-minute Bedrock cap
    assert cfg.hardware.button_gpio == 17
    assert "weather" in cfg.enabled_tools
    assert "search" in cfg.enabled_tools
    assert "time" in cfg.enabled_tools
    assert cfg.search_provider == "auto"
    assert cfg.firecrawl_api_key_env == "FIRECRAWL_API_KEY"
    assert cfg.firecrawl_search_url.endswith("/v2/search")
    assert cfg.debug_mode is False
    # Messaging parses to disabled-by-default in configs with no messaging section.
    assert cfg.messaging.enabled is False
    assert cfg.messaging.chat_allowlist == []
    assert cfg.messaging.recipients == {}
    assert cfg.messaging.poll_timeout_seconds == 25
    assert cfg.messaging.send_rate_per_minute == 5
    assert cfg.messaging.send_window_seconds == 600
    assert cfg.messaging.max_announcements == 10
    assert cfg.messaging.announce_chime is True


def test_missing_model_id_is_required(tmp_path: Path) -> None:
    config_path = tmp_path / "assistant.yaml"
    config_path.write_text(
        """
system_prompt: hello
sonic:
  aws_region: us-east-1
""",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="model_id"):
        load_config(config_path)


def test_invalid_endpointing_sensitivity_rejected(tmp_path: Path) -> None:
    config_path = tmp_path / "assistant.yaml"
    config_path.write_text(
        """
system_prompt: hello
sonic:
  model_id: amazon.nova-2-5-sonic
  endpointing_sensitivity: VERYHIGH
""",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="endpointing"):
        load_config(config_path)


def test_non_sonic_input_sample_rate_rejected(tmp_path: Path) -> None:
    config_path = tmp_path / "assistant.yaml"
    config_path.write_text(
        """
system_prompt: hello
sonic:
  model_id: amazon.nova-2-5-sonic
  input_sample_rate_hz: 44100
""",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="16000"):
        load_config(config_path)


def test_messaging_section_parses(tmp_path: Path) -> None:
    config_path = tmp_path / "assistant.yaml"
    config_path.write_text(
        """
system_prompt: hello
sonic:
  model_id: amazon.nova-2-5-sonic
messaging:
  enabled: true
  poll_timeout_seconds: 10
  chat_allowlist: [123456789, 987654321]
  recipients:
    mort: "123456789"
    partner: 987654321
  send_rate_per_minute: 3
  send_window_seconds: 300
  max_announcements: 4
  announce_chime: false
""",
        encoding="utf-8",
    )
    cfg = load_config(config_path)
    assert cfg.messaging.enabled is True
    assert cfg.messaging.chat_allowlist == [123456789, 987654321]
    assert cfg.messaging.recipients == {"mort": "123456789", "partner": "987654321"}
    assert cfg.messaging.poll_timeout_seconds == 10
    assert cfg.messaging.send_rate_per_minute == 3
    assert cfg.messaging.send_window_seconds == 300
    assert cfg.messaging.max_announcements == 4
    assert cfg.messaging.announce_chime is False


def test_messaging_absent_section_defaults_disabled(tmp_path: Path) -> None:
    config_path = tmp_path / "assistant.yaml"
    config_path.write_text(
        """
system_prompt: hello
sonic:
  model_id: amazon.nova-2-5-sonic
""",
        encoding="utf-8",
    )
    cfg = load_config(config_path)
    assert cfg.messaging.enabled is False
    assert cfg.messaging.chat_allowlist == []
    assert cfg.messaging.recipients == {}


def test_messaging_invalid_values_rejected(tmp_path: Path) -> None:
    config_path = tmp_path / "assistant.yaml"
    config_path.write_text(
        """
system_prompt: hello
sonic:
  model_id: amazon.nova-2-5-sonic
messaging:
  poll_timeout_seconds: 90
""",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="poll_timeout_seconds"):
        load_config(config_path)
