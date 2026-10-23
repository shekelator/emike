from emike.config import LLMConfig
from emike.llm import ChatMessage, LLMClient


def test_message_building_includes_system_and_history() -> None:
    cfg = LLMConfig(
        endpoint="https://example.com/v1/chat/completions",
        model="demo",
    )
    client = LLMClient(cfg, system_prompt="test prompt")

    msgs = client._build_messages([ChatMessage(role="user", content="hello")])

    assert msgs[0]["role"] == "system"
    assert "test prompt" in msgs[0]["content"]
    assert msgs[1] == {"role": "user", "content": "hello"}
