"""Model-rendered DDL for the community tables.

The ``is_preset`` server default must be the SQL boolean literal
(``sqlalchemy.false()`` -> ``false``), not the function call ``func.false()``
which renders ``false()`` and PostgreSQL rejects (issue #62 / PR #63 lesson).
"""

from __future__ import annotations

from sqlalchemy import DefaultClause
from sqlalchemy.sql.elements import False_

from mc_server_dashboard_api.community.adapters.models import RoleModel


def test_is_preset_server_default_is_boolean_literal() -> None:
    server_default = RoleModel.__table__.c.is_preset.server_default
    assert isinstance(server_default, DefaultClause)
    # ``False_`` renders as the literal ``false``; ``func.false()`` would render
    # the invalid ``false()`` call instead.
    assert isinstance(server_default.arg, False_)
