"""State channels for `/btw` ephemeral side questions."""

from __future__ import annotations

from typing import Annotated, NotRequired
from typing_extensions import TypedDict

from langchain.agents.middleware.types import PrivateStateAttr


class BtwChannels(TypedDict):
    """Private state channel flagging an isolated `/btw` side-question turn."""

    _btw_side_question: NotRequired[Annotated[bool | None, PrivateStateAttr]]


__all__ = ["BtwChannels"]
