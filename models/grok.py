import os
from collections.abc import Sequence
from typing import Any, TypeVar

from pydantic import RootModel
from xai_sdk import AsyncClient
from xai_sdk.chat import user

OutputT = TypeVar("OutputT")


async def call_grok(
    input: str,
    output_type: type[OutputT],
    *,
    model: str = "grok-4.6",
    tools: Sequence[Any] | None = None,
) -> OutputT:
    """Asynchronously send ``input`` to Grok and parse its response."""
    async with AsyncClient(api_key=os.environ["XAI_API_KEY"]) as client:
        chat = client.chat.create(model=model, tools=tools)
        chat.append(user(input))

        response_model = RootModel[output_type]
        _, parsed = await chat.parse(response_model)
        return parsed.root
