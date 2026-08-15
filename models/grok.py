import os
from collections.abc import Sequence
from typing import Any, TypeVar

from pydantic import RootModel
from xai_sdk import AsyncClient
from xai_sdk.chat import user

OutputT = TypeVar("OutputT")


def _api_key() -> str:
    """XAI_API_KEY from the environment, falling back to the .env file.

    Reading os.environ alone means any entry point that does not export the
    variable by hand dies here — including the API server, which loads .env
    through pydantic Settings everywhere except this module.
    """
    key = os.environ.get("XAI_API_KEY", "").strip()
    if key:
        return key
    from preprocess import get_settings

    key = get_settings().xai_api_key.strip()
    if not key:
        raise RuntimeError(
            "XAI_API_KEY is not set. Export it or add it to .env — see .env.example."
        )
    return key


async def call_grok(
    input: str,
    output_type: type[OutputT],
    *,
    model: str = "grok-4.6",
    tools: Sequence[Any] | None = None,
) -> OutputT:
    """Asynchronously send ``input`` to Grok and parse its response."""
    async with AsyncClient(api_key=_api_key()) as client:
        chat = client.chat.create(model=model, tools=tools)
        chat.append(user(input))

        response_model = RootModel[output_type]
        _, parsed = await chat.parse(response_model)
        return parsed.root
