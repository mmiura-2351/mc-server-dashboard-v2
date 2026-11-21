"""The rule for the display name a user gives an upload (issues #3922, #3957).

The resource-pack upload and the plugin upload ask it of their ``display_name``
form field. The parser decodes the field with whatever ``charset`` the request
declares, so a client can put in it any character, a lone surrogate included.
"""

from __future__ import annotations

from mc_server_dashboard_api.name_characters import is_refused_name_character

# The longest display name an upload may carry, in characters, once stripped.
MAX_DISPLAY_NAME_LENGTH = 200


def checked_display_name(display_name: str) -> str | None:
    """``display_name`` stripped, as an upload stores it, or ``None``.

    ``None`` when the stripped name cannot name the upload. The name is shown in
    a list, and a resource pack's is typed to confirm a delete. Stripped, it must
    not be empty, must fit :data:`MAX_DISPLAY_NAME_LENGTH`, and must carry no
    control character (Unicode ``Cc``), which would break the line it is shown on
    or drive a terminal, and no lone surrogate, which has no UTF-8 encoding and so
    could not be stored. Any other character is a legitimate name.
    """

    name = display_name.strip()
    if (
        name == ""
        or len(name) > MAX_DISPLAY_NAME_LENGTH
        or any(is_refused_name_character(c) for c in name)
    ):
        return None
    return name
