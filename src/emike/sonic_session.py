"""A single Nova Sonic turn session: one duplex stream per button press.

This module isolates the SDK's eventstream shape from the rest of the code.
Everything above it works with plain ``{"event": {...}}`` JSON dicts as defined
``emike.sonic_events``.

Note on the duplex stream lifecycle: the SDK resolves the output stream via
``await_output()``, which waits for the service's initial response. The
service may not send it until input events start flowing, so **do not await it
up front**. Sending input and consuming output must overlap — that is what
the sender/receiver task pair in ``AssistantRuntime`` does, and
``output_events()`` resolves the output stream lazily.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from typing import Any

from aws_sdk_bedrock_runtime.models import (
    BidirectionalInputPayloadPart,
    InvokeModelWithBidirectionalStreamInputChunk,
    InvokeModelWithBidirectionalStreamOutputChunk,
)

LOGGER = logging.getLogger(__name__)


class SonicSessionError(RuntimeError):
    """Raised when the stream yields a Bedrock service exception."""


async def send_raw_event(duplex: Any, event: dict[str, Any]) -> None:
    """Send one ``{"event": {...}}`` payload on a raw duplex stream."""
    wire = json.dumps(event, separators=(",", ":")).encode("utf-8")
    chunk = InvokeModelWithBidirectionalStreamInputChunk(
        value=BidirectionalInputPayloadPart(bytes_=wire)
    )
    await duplex.input_stream.send(chunk)


class SonicTurnSession:
    def __init__(self, duplex: Any) -> None:
        self._duplex = duplex
        self._output_stream: AsyncIterator[Any] | None = None

    async def send_event(self, event: dict[str, Any]) -> None:
        await send_raw_event(self._duplex, event)

    async def send_events(self, events: list[dict[str, Any]]) -> None:
        for event in events:
            await self.send_event(event)

    async def output_events(self) -> AsyncIterator[dict[str, Any]]:
        """Yield the model's decoded ``{"event": {...}}`` frames in order.

        Iteration ends when the stream ends; any Bedrock service exception
        member raises :class:`SonicSessionError`.
        """
        if self._output_stream is None:
            _, self._output_stream = await self._duplex.await_output()
        async for output in self._output_stream:
            if not isinstance(output, InvokeModelWithBidirectionalStreamOutputChunk):
                raise SonicSessionError(f"Bedrock stream error: {output!r}")
            raw = output.value.bytes_ or b""
            if not raw:
                continue
            yield json.loads(raw.decode("utf-8"))

    async def close(self) -> None:
        """Close the duplex stream, suppressing shutdown errors."""
        try:
            await self._duplex.close()
        except Exception:
            LOGGER.debug("Error closing Nova Sonic stream", exc_info=True)
