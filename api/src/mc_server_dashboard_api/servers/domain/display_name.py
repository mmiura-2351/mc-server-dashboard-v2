"""The rule for a plugin's or a resource pack's display name (issues #3922, #3957).

The resource-pack upload and the plugin upload ask it of their ``display_name``
form field. The parser decodes the field with whatever ``charset`` the request
declares, so a client can put in it any character, a lone surrogate included.

A display name the user did not type, such as a catalog project's title, is made
to fit the rule instead of refused (issue #3988).
"""

from __future__ import annotations

from pathlib import PurePosixPath

from mc_server_dashboard_api.name_characters import is_refused_name_character

# The longest display name an upload may carry, in characters, once stripped.
MAX_DISPLAY_NAME_LENGTH = 200


def checked_display_name(display_name: str) -> str | None:
    """``display_name`` stripped, as an upload stores it, or ``None``.

    ``None`` when the stripped name cannot name the upload. The name is shown in
    a list, and a resource pack's is typed to confirm a delete. Stripped, it must
    not be empty, must fit :data:`MAX_DISPLAY_NAME_LENGTH`, and must carry no
    control character (Unicode ``Cc``) and no line or paragraph separator
    (``Zl``, ``Zp``), which would break the line it is shown on or drive a
    terminal, and no lone surrogate, which has no UTF-8 encoding and so could not
    be stored. Any other character is a legitimate name.
    """

    name = display_name.strip()
    if (
        name == ""
        or len(name) > MAX_DISPLAY_NAME_LENGTH
        or any(is_refused_name_character(c) for c in name)
    ):
        return None
    return name


def fitted_display_name(display_name: str, *, filename: str) -> str:
    """``display_name`` made to pass :func:`checked_display_name` (issue #3988).

    For a display name the user did not type: a catalog project's title, an
    imported plugin's, or a restored jar's. The name is stripped, each character
    the rule refuses is replaced with ``_``, and the name is cut to
    :data:`MAX_DISPLAY_NAME_LENGTH`. A blank name gives way to ``filename`` less
    its extension, made to fit the same way, and that to ``filename`` itself,
    which must hold more than spaces.
    """

    for candidate in (display_name, PurePosixPath(filename).stem, filename):
        replaced = "".join(
            "_" if is_refused_name_character(c) else c for c in candidate.strip()
        )
        fitted = replaced[:MAX_DISPLAY_NAME_LENGTH].rstrip()
        if fitted:
            return fitted
    raise ValueError(f"no display name in a blank filename: {filename!r}")
