from pathlib import Path

from emike.config import load_config


def test_load_config_from_example() -> None:
    cfg = load_config(Path("config/assistant.example.yaml"))

    assert cfg.system_prompt
    assert cfg.llm.endpoint.startswith("https://")
    assert cfg.stt.model
    assert cfg.hardware.button_gpio == 17
    assert "weather" in cfg.enabled_tools
    assert "search" in cfg.enabled_tools
    assert "timers" in cfg.enabled_tools
    assert cfg.search_provider == "auto"
    assert cfg.firecrawl_api_key_env == "FIRECRAWL_API_KEY"
    assert cfg.debug_mode is False
    assert cfg.default_location == "Jamaica Plain, MA 02130"
