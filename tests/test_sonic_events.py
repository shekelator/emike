import base64
import json

import numpy as np

from emike.sonic_events import (
    build_audio_content_end,
    build_audio_content_start,
    build_audio_input,
    build_prompt_end,
    build_prompt_start,
    build_session_end,
    build_session_start,
    build_system_prompt_events,
    build_tool_result_events,
    classify_output_event,
    encode_pcm_chunk,
    extract_interrupted,
    openai_tool_to_tool_spec,
)

PROMPT = "prompt-1"
CONTENT = "content-1"


def test_encode_pcm_chunk_matches_base64() -> None:
    pcm = np.array([1, 0, -1]).astype("<i2").tobytes()
    assert encode_pcm_chunk(pcm) == base64.b64encode(pcm).decode("ascii")


def test_build_session_start_carries_inference_and_endpointing() -> None:
    event = build_session_start(1024, 0.9, 0.7, "HIGH")["event"]["sessionStart"]

    assert event["inferenceConfiguration"] == {
        "maxTokens": 1024,
        "topP": 0.9,
        "temperature": 0.7,
    }
    assert event["turnDetectionConfiguration"] == {"endpointingSensitivity": "HIGH"}


def test_build_prompt_start_declares_speech_output_and_tools() -> None:
    tool_specs = [
        {"toolSpec": {"name": "get_current_time", "description": "d", "inputSchema": {"json": {}}}}
    ]
    event = build_prompt_start(PROMPT, "tiffany", 24000, tool_specs)["event"]["promptStart"]

    assert event["promptName"] == PROMPT
    audio_config = event["audioOutputConfiguration"]
    assert audio_config == {
        "mediaType": "audio/lpcm",
        "sampleRateHertz": 24000,
        "sampleSizeBits": 16,
        "channelCount": 1,
        "voiceId": "tiffany",
        "encoding": "base64",
        "audioType": "SPEECH",
    }
    assert event["toolUseOutputConfiguration"] == {"mediaType": "application/json"}
    assert event["toolConfiguration"]["tools"] == tool_specs


def test_system_prompt_travels_as_a_system_triplet() -> None:
    events = build_system_prompt_events(PROMPT, CONTENT, "You are Mike.")

    assert [next(iter(e["event"])) for e in events] == ["contentStart", "textInput", "contentEnd"]
    start = events[0]["event"]["contentStart"]
    assert start["role"] == "SYSTEM"
    assert start["type"] == "TEXT"
    assert start["interactive"] is True
    assert events[1]["event"]["textInput"]["content"] == "You are Mike."
    assert events[2]["event"]["contentEnd"]["contentName"] == CONTENT


def test_user_audio_turn_uses_content_start_and_end() -> None:
    start = build_audio_content_start(PROMPT, CONTENT, 16000)["event"]["contentStart"]
    assert start["role"] == "USER"
    assert start["type"] == "AUDIO"
    assert start["audioInputConfiguration"] == {
        "mediaType": "audio/lpcm",
        "sampleRateHertz": 16000,
        "sampleSizeBits": 16,
        "channelCount": 1,
        "encoding": "base64",
        "audioType": "SPEECH",
    }

    chunk = build_audio_input(PROMPT, CONTENT, "AAEAAA")["event"]["audioInput"]
    assert chunk == {"promptName": PROMPT, "contentName": CONTENT, "content": "AAEAAA"}

    end = build_audio_content_end(PROMPT, CONTENT)["event"]["contentEnd"]
    # No stopReason: the service rejects stopReason on input contentEnd.
    assert end == {"promptName": PROMPT, "contentName": CONTENT}


def test_tool_result_events_wrap_payload_in_serialized_json() -> None:
    events = build_tool_result_events(PROMPT, CONTENT, "tool-use-1", {"result": "3:04 PM"})

    assert [next(iter(e["event"])) for e in events] == ["contentStart", "toolResult", "contentEnd"]
    start = events[0]["event"]["contentStart"]
    assert start["role"] == "TOOL"
    assert start["interactive"] is False
    assert start["type"] == "TOOL"
    assert start["toolResultInputConfiguration"] == {
        "toolUseId": "tool-use-1",
        "type": "TEXT",
        "textInputConfiguration": {"mediaType": "text/plain"},
    }
    result = events[1]["event"]["toolResult"]
    assert result["content"] == json.dumps({"result": "3:04 PM"})


def test_close_sequence_events() -> None:
    assert build_prompt_end(PROMPT)["event"]["promptEnd"] == {"promptName": PROMPT}
    assert build_session_end() == {"event": {"sessionEnd": {}}}


def test_openai_tool_to_tool_spec_conversion() -> None:
    openai_tool = {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Search the web.",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        },
    }
    spec = openai_tool_to_tool_spec(openai_tool)["toolSpec"]

    assert spec["name"] == "web_search"
    assert spec["description"] == "Search the web."
    # Nova Sonic requires the JSON Schema stringified inside inputSchema.json.
    assert json.loads(spec["inputSchema"]["json"]) == openai_tool["function"]["parameters"]


def test_openai_tool_to_tool_spec_requires_name() -> None:
    try:
        openai_tool_to_tool_spec({"function": {}})
    except ValueError as exc:
        assert "name" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def _content_start_event(type_: str, role: str = "ASSISTANT") -> dict[str, object]:
    return {"event": {"contentStart": {"contentId": "c", "type": type_, "role": role}}}


def test_classify_output_event_dispatch_keys() -> None:
    kinds = {
        classify_output_event({"event": {"completionStart": {}}}): "completion_start",
        classify_output_event(_content_start_event("AUDIO")): "content_start",
        classify_output_event({"event": {"textOutput": {"content": "hi"}}}): "text_output",
        classify_output_event({"event": {"audioOutput": {"content": "AA"}}}): "audio_output",
        classify_output_event({"event": {"toolUse": {}}}): "tool_use",
        classify_output_event({"event": {"contentEnd": {}}}): "content_end",
        classify_output_event({"event": {"completionEnd": {}}}): "completion_end",
        classify_output_event({"event": {"usageEvent": {}}}): "other",
        classify_output_event({}): "other",
    }
    assert all(key == value for key, value in kinds.items())


def test_extract_interrupted() -> None:
    assert extract_interrupted('{"interrupted": true}') is True
    assert extract_interrupted('{"interrupted": false}') is False
    assert extract_interrupted("Hello there") is False
    assert extract_interrupted("") is False
    assert extract_interrupted("{ not json") is False
