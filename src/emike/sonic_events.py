"""Builders and classifiers for the Amazon Nova Sonic bidirectional-stream event protocol.

All builders return the full wire format (``{"event": {...}}``). These functions are
pure — no SDK imports, no I/O — so the whole protocol shape is unit-testable.
Field references: AWS Nova 2 user guide, "Handling input events" and
"Handling output events".
"""

from __future__ import annotations

import base64
import json
from typing import Any

AUDIO_MEDIA_TYPE = "audio/lpcm"
TEXT_MEDIA_TYPE = "text/plain"
DEFAULT_INPUT_SAMPLE_RATE_HZ = 16_000
DEFAULT_OUTPUT_SAMPLE_RATE_HZ = 24_000
END_OF_TURN = "END_TURN"


def encode_pcm_chunk(pcm: bytes) -> str:
    """Encode one frame of raw 16-bit PCM as the base64 audioInput payload."""
    return base64.b64encode(pcm).decode("ascii")


def peak_pcm_amplitude(chunk: bytes) -> int:
    """Peak absolute sample of little-endian int16 PCM (0 means all silence).

    Used for mic diagnostics: a turn that stays at 0 means capture is
    delivering silence — wrong device or no microphone permission.
    """
    peak = 0
    for i in range(0, len(chunk) - 1, 2):
        peak = max(peak, abs(int.from_bytes(chunk[i : i + 2], "little", signed=True)))
    return peak


def build_session_start(
    max_tokens: int,
    top_p: float,
    temperature: float,
    endpointing_sensitivity: str,
) -> dict[str, Any]:
    return {
        "event": {
            "sessionStart": {
                "inferenceConfiguration": {
                    "maxTokens": max_tokens,
                    "topP": top_p,
                    "temperature": temperature,
                },
                "turnDetectionConfiguration": {
                    "endpointingSensitivity": endpointing_sensitivity,
                },
            }
        }
    }


def build_prompt_start(
    prompt_name: str,
    voice_id: str,
    output_sample_rate_hz: int,
    tool_specs: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "event": {
            "promptStart": {
                "promptName": prompt_name,
                "textOutputConfiguration": {"mediaType": TEXT_MEDIA_TYPE},
                "audioOutputConfiguration": {
                    "mediaType": AUDIO_MEDIA_TYPE,
                    "sampleRateHertz": output_sample_rate_hz,
                    "sampleSizeBits": 16,
                    "channelCount": 1,
                    "voiceId": voice_id,
                    "encoding": "base64",
                    "audioType": "SPEECH",
                },
                "toolUseOutputConfiguration": {"mediaType": "application/json"},
                "toolConfiguration": {
                    "tools": tool_specs,
                    "toolChoice": {"auto": {}},
                },
            }
        }
    }


def build_system_prompt_events(
    prompt_name: str,
    content_name: str,
    system_prompt: str,
) -> list[dict[str, Any]]:
    """The system prompt travels as a SYSTEM role content triplet (there is no
    systemPrompt field on promptStart) — same shape as the official samples."""
    return [
        {
            "event": {
                "contentStart": {
                    "promptName": prompt_name,
                    "contentName": content_name,
                    "role": "SYSTEM",
                    "type": "TEXT",
                    "interactive": True,
                    "textInputConfiguration": {"mediaType": TEXT_MEDIA_TYPE},
                }
            }
        },
        {
            "event": {
                "textInput": {
                    "promptName": prompt_name,
                    "contentName": content_name,
                    "content": system_prompt,
                }
            }
        },
        {
            "event": {
                "contentEnd": {
                    "promptName": prompt_name,
                    "contentName": content_name,
                }
            }
        },
    ]


def build_audio_content_start(
    prompt_name: str,
    content_name: str,
    sample_rate_hz: int = DEFAULT_INPUT_SAMPLE_RATE_HZ,
) -> dict[str, Any]:
    return {
        "event": {
            "contentStart": {
                "promptName": prompt_name,
                "contentName": content_name,
                "role": "USER",
                "type": "AUDIO",
                "interactive": True,
                "audioInputConfiguration": {
                    "mediaType": AUDIO_MEDIA_TYPE,
                    "sampleRateHertz": sample_rate_hz,
                    "sampleSizeBits": 16,
                    "channelCount": 1,
                    "encoding": "base64",
                    "audioType": "SPEECH",
                },
            }
        }
    }


def build_audio_input(
    prompt_name: str,
    content_name: str,
    pcm_b64: str,
) -> dict[str, Any]:
    return {
        "event": {
            "audioInput": {
                "promptName": prompt_name,
                "contentName": content_name,
                "content": pcm_b64,
            }
        }
    }


def build_audio_content_end(
    prompt_name: str,
    content_name: str,
) -> dict[str, Any]:
    """Sent on button release to end the user's audio turn.

    Note: the official samples send no stopReason here; a stopReason value on
    the *input* contentEnd is not accepted and the turn never closes.
    """
    return {
        "event": {
            "contentEnd": {
                "promptName": prompt_name,
                "contentName": content_name,
            }
        }
    }


def build_tool_result_events(
    prompt_name: str,
    content_name: str,
    tool_use_id: str,
    payload: dict[str, Any] | str,
) -> list[dict[str, Any]]:
    """Sent after every toolUse event, including when the tool failed.

    ``payload`` may be a str (already-serialized JSON) or a dict; the wire format
    requires serialized JSON in ``content``. Mirroring the official samples, the
    TOOL content ends with its own contentEnd event.
    """
    content = payload if isinstance(payload, str) else json.dumps(payload)
    return [
        {
            "event": {
                "contentStart": {
                    "promptName": prompt_name,
                    "contentName": content_name,
                    "role": "TOOL",
                    "type": "TOOL",
                    "interactive": False,
                    "toolResultInputConfiguration": {
                        "toolUseId": tool_use_id,
                        "type": "TEXT",
                        "textInputConfiguration": {"mediaType": TEXT_MEDIA_TYPE},
                    },
                }
            }
        },
        {
            "event": {
                "toolResult": {
                    "promptName": prompt_name,
                    "contentName": content_name,
                    "content": content,
                }
            }
        },
        {
            "event": {
                "contentEnd": {
                    "promptName": prompt_name,
                    "contentName": content_name,
                }
            }
        },
    ]


def build_prompt_end(prompt_name: str) -> dict[str, Any]:
    return {"event": {"promptEnd": {"promptName": prompt_name}}}


def build_session_end() -> dict[str, Any]:
    return {"event": {"sessionEnd": {}}}


def openai_tool_to_tool_spec(defn: dict[str, Any]) -> dict[str, Any]:
    """Convert an OpenAI function-calling tool dict to a Nova Sonic toolSpec.

    Note: Nova Sonic wants the JSON Schema *stringified* inside
    ``inputSchema.json`` (same shape the official samples send); passing the
    schema as an object makes the service reject the whole chunk.
    """
    function = defn.get("function", defn)
    name = function.get("name")
    if not name:
        raise ValueError(f"Tool definition is missing a name: {defn!r}")
    spec: dict[str, Any] = {
        "toolSpec": {
            "name": name,
            "description": function.get("description", ""),
            "inputSchema": {"json": json.dumps(function.get("parameters", {}))},
        }
    }
    return spec


def classify_output_event(event: dict[str, Any]) -> str:
    """Map one output frame's inner event kind to a stable dispatch key."""
    inner = event.get("event", {})
    if "completionStart" in inner:
        return "completion_start"
    if "contentStart" in inner:
        return "content_start"
    if "textOutput" in inner:
        return "text_output"
    if "audioOutput" in inner:
        return "audio_output"
    if "toolUse" in inner:
        return "tool_use"
    if "contentEnd" in inner:
        return "content_end"
    if "completionEnd" in inner:
        return "completion_end"
    return "other"


def extract_content_start(event: dict[str, Any]) -> dict[str, Any]:
    result = event["event"]["contentStart"]
    assert isinstance(result, dict)
    return result


def extract_text_output(event: dict[str, Any]) -> dict[str, Any]:
    result = event["event"]["textOutput"]
    assert isinstance(result, dict)
    return result


def extract_audio_output(event: dict[str, Any]) -> str:
    result = event["event"]["audioOutput"]["content"]
    assert isinstance(result, str)
    return result


def extract_tool_use(event: dict[str, Any]) -> dict[str, Any]:
    result = event["event"]["toolUse"]
    assert isinstance(result, dict)
    return result


def extract_content_end(event: dict[str, Any]) -> dict[str, Any]:
    result = event["event"]["contentEnd"]
    assert isinstance(result, dict)
    return result


def extract_interrupted(text_output_content: str) -> bool:
    """Detect the model's barge-in notification, tolerantly."""
    try:
        parsed = json.loads(text_output_content)
    except (TypeError, json.JSONDecodeError):
        return False
    return bool(isinstance(parsed, dict) and parsed.get("interrupted"))
