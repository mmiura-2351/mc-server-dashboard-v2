"""The characters no name may carry (issues #3957, #3988).

A display name, a server name, an uploaded file's name, and a community's or a
role's name are each shown on one line of a list, a log or a terminal, and
stored as UTF-8. Each rule refuses the characters :func:`is_refused_name_character`
names, and may refuse more of its own.

It is a neutral root-level module that imports only the standard library, so
every context's inward layers may import it. That is the ``rfc3339`` precedent
in the root-level import contract.
"""

from __future__ import annotations

import unicodedata


def is_refused_name_character(c: str) -> bool:
    """Whether no name may carry ``c``.

    A control character (Unicode ``Cc``: C0, DEL and C1) would break the line the
    name is shown on or drive the terminal showing it, and so would the line and
    paragraph separators (``Zl`` and ``Zp``: U+2028 and U+2029). A lone surrogate
    (``Cs``) has no UTF-8 encoding, so could not be stored.
    """

    return unicodedata.category(c) in ("Cc", "Cs", "Zl", "Zp")
