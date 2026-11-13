"""Bedrock Runtime SDK access for Nova Sonic — the only module that builds a client.

Credentials come from the standard AWS environment variables via
``EnvironmentCredentialsResolver`` (see README: on the Pi they live in
``.env``, loaded into the environment by systemd).
"""

from __future__ import annotations

import logging
from typing import Any

from aws_sdk_bedrock_runtime.client import (
    AsyncBedrockRuntimeClient,
    InvokeModelWithBidirectionalStreamOperationInput,
)
from aws_sdk_bedrock_runtime.config import AsyncBedrockRuntimeConfig
from smithy_aws_core.identity import AWSCredentialsIdentity, IdentityChain
from smithy_http.aio.crt import AWSCRTHTTPClient

from emike.config import SonicConfig
from emike.sonic_events import build_session_start
from emike.sonic_session import send_raw_event

LOGGER = logging.getLogger(__name__)


def exc_message(exc: Exception) -> str:
    """Human message for a (smithy/Bedrock) exception — str() is often empty."""
    text = str(exc) or str(getattr(exc, "message", "") or "")
    return text or type(exc).__name__


class SonicClient:
    """Opens Nova Sonic bidirectional streams (one model per call)."""

    def __init__(self, cfg: SonicConfig) -> None:
        self._cfg = cfg

    async def open_stream(self, model_id: str | None = None) -> tuple[Any, str]:
        """Open a bidirectional stream. Returns ``(duplex_stream, model_id)``.

        The ``sessionStart`` event is sent inside this call: Bedrock resolves
        model access on the first input chunk, so a subscription-disabled model
        must fail here rather than mid-turn (the runtime's fallback retry).
        """
        model_id = model_id or self._cfg.model_id
        duplex = await self._open(model_id)
        await self._send_session_start(duplex)
        return duplex, model_id

    async def _send_session_start(self, duplex: Any) -> None:
        """Send sessionStart before handing the stream over.

        Bedrock validates model access on the first input chunk, not at open —
        a 403 AccessDeniedException only surfaces there. Sending it here puts
        that failure inside the model-fallback retry path, where a
        subscription-disabled primary model can still give way to the fallback.
        """
        await send_raw_event(
            duplex,
            build_session_start(
                self._cfg.max_tokens,
                self._cfg.top_p,
                self._cfg.temperature,
                self._cfg.endpointing_sensitivity,
            ),
        )

    async def _open(self, model_id: str) -> Any:
        # Standard identity chain: AWS_* env vars first (what .env sets on the
        # Pi), then the shared ~/.aws profile files, then IMDS (EC2).
        credentials = await IdentityChain.create(AWSCredentialsIdentity)
        config = await AsyncBedrockRuntimeConfig.resolve(
            region=self._cfg.aws_region,
            aws_credentials_identity_resolver=credentials,
            user_agent_extra="emike",
            # The default aiohttp transport cannot do duplex event streaming;
            # bidirectional Nova Sonic requires the awscrt transport.
            transport=AWSCRTHTTPClient(),
        )
        client = AsyncBedrockRuntimeClient(config=config)
        operation = InvokeModelWithBidirectionalStreamOperationInput(model_id=model_id)
        return await client.invoke_model_with_bidirectional_stream(operation)
