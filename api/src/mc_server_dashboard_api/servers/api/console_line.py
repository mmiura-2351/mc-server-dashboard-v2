"""Request-schema types for text that becomes a console line (issue #3363).

A console line is bounded in UTF-8 **bytes** (``MAX_CONSOLE_LINE_BYTES``: what
one RCON command packet carries). A JSON schema can only state a length in
characters, so each type declares the character bound — a necessary condition,
and the one generated clients see as ``maxLength`` — and then checks the encoded
size, which is the real limit for non-ASCII text. Both fail as the same 422
``string_too_long`` entry.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import AfterValidator, Field
from pydantic_core import PydanticCustomError

from mc_server_dashboard_api.servers.domain.control_plane import (
    MAX_CONSOLE_LINE_BYTES,
    console_line_bytes,
)
from mc_server_dashboard_api.servers.domain.schedule import MAX_WARNING_MESSAGE_BYTES


def _at_most_utf8_bytes(limit: int) -> AfterValidator:
    def check(value: str) -> str:
        if console_line_bytes(value) > limit:
            raise PydanticCustomError(
                "string_too_long",
                "String should have at most {max_bytes} bytes in UTF-8",
                {"max_bytes": limit},
            )
        return value

    return AfterValidator(check)


# A line sent to the console as is: the console route's ``line`` and a command
# schedule's ``command``.
ConsoleLine = Annotated[
    str,
    Field(max_length=MAX_CONSOLE_LINE_BYTES),
    _at_most_utf8_bytes(MAX_CONSOLE_LINE_BYTES),
]

# A schedule warning step's message, broadcast as ``say <message>``.
WarningMessage = Annotated[
    str,
    Field(max_length=MAX_WARNING_MESSAGE_BYTES),
    _at_most_utf8_bytes(MAX_WARNING_MESSAGE_BYTES),
]
