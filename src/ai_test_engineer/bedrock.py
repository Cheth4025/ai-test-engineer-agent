"""Bedrock model setup for generation (Strands BedrockModel).

Standard AWS credential chain only -- no explicit keys, ever. AWS_DEFAULT_REGION
is set defensively alongside AWS_REGION because botocore's `aws login` token
refresh path (LoginCredentialFetcher) resolves its own internal client's region
only from AWS_DEFAULT_REGION, not AWS_REGION -- confirmed directly: a bare
AWS_REGION export alone still raised NoRegionError during credential refresh.
"""

from __future__ import annotations

import os

from botocore.config import Config as BotocoreConfig
from strands.models import BedrockModel

DEFAULT_MODEL_ID = "us.anthropic.claude-sonnet-4-6"
DEFAULT_REGION = "us-east-1"
DEFAULT_TIMEOUT_SECONDS = 120


def get_bedrock_model() -> BedrockModel:
    region = os.environ.get("AWS_REGION", DEFAULT_REGION)
    os.environ.setdefault("AWS_DEFAULT_REGION", region)

    model_id = os.environ.get("AITE_BEDROCK_MODEL_ID", DEFAULT_MODEL_ID)
    timeout_seconds = int(
        os.environ.get("AITE_GENERATION_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS)
    )

    return BedrockModel(
        region_name=region,
        model_id=model_id,
        boto_client_config=BotocoreConfig(
            read_timeout=timeout_seconds, connect_timeout=timeout_seconds
        ),
    )
