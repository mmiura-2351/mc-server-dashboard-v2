"""Model-rendered DDL for the identity tables (issue #62).

``Base.metadata.create_all`` renders DDL from the ORM models, not from the
Alembic migration. The ``is_platform_admin`` server default must be the SQL
boolean literal (``sqlalchemy.false()`` -> ``false``), not the function call
``func.false()`` which renders ``false()`` and PostgreSQL rejects.
"""

from __future__ import annotations

from sqlalchemy import DefaultClause
from sqlalchemy.sql.elements import False_

from mc_server_dashboard_api.identity.adapters.models import UserModel


def test_is_platform_admin_server_default_is_boolean_literal() -> None:
    server_default = UserModel.__table__.c.is_platform_admin.server_default
    assert isinstance(server_default, DefaultClause)
    # ``False_`` renders as the literal ``false``; ``func.false()`` would render
    # the invalid ``false()`` call instead (issue #62).
    assert isinstance(server_default.arg, False_)
