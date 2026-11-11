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

LOGGER = logging.getLogger(__name__)


class SonicClient:
    """Opens Nova Sonic bidirectional streams, with configured model fallback."""

    def __init__(self, cfg: SonicConfig) -> None:
        self._cfg = cfg

    async def open_stream(self) -> tuple[Any, str]:
        """Open a bidirectional stream. Returns ``(duplex_stream, model_id)``.

        Tries the configured primary model first; on any failure retries once
        with the configured fallback model.
        """
        try:
            return await self._open(self._cfg.model_id), self._cfg.model_id
        except Exception as primary_error:
            fallback = self._cfg.fallback_model_id
            if not fallback or fallback == self._cfg.model_id:
                raise
            LOGGER.warning(
                "Primary model %s failed (%s: %s); falling back to %s",
                self._cfg.model_id,
                type(primary_error).__name__,
                primary_error,
                fallback,
                extra={
                    "event": "sonic_model_fallback",
                    "primary_model": self._cfg.model_id,
                    "fallback_model": fallback,
                    "error": str(primary_error),
                },
            )
            return await self._open(fallback), fallback

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
