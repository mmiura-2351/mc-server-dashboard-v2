"""Model registration for Alembic autogenerate.

Importing this module imports each adapters model module so its tables register
on the shared ``Base.metadata`` that Alembic diffs against. ``env.py`` imports
it for that side effect and exposes ``Base.metadata`` as ``target_metadata``.

Keeping the imports here (rather than inline in ``env.py``) lets a test import
this registration in isolation -- with no app or test conftest -- to verify the
set of tables ``env.py`` actually registers, instead of relying on the
process-global ``Base.metadata`` that ordinary test imports already populate.
"""

from __future__ import annotations

from mc_server_dashboard_api.audit.adapters import models as _audit_models
from mc_server_dashboard_api.community.adapters import models as _community_models
from mc_server_dashboard_api.core.adapters.database import Base
from mc_server_dashboard_api.identity.adapters import models as _identity_models
from mc_server_dashboard_api.servers.adapters import backup_models as _backup_models
from mc_server_dashboard_api.servers.adapters import group_models as _group_models
from mc_server_dashboard_api.servers.adapters import models as _servers_models

# Importing the models registers their tables on ``Base.metadata`` for autogenerate.
_ = (
    _identity_models,
    _community_models,
    _servers_models,
    _backup_models,
    _group_models,
    _audit_models,
)

target_metadata = Base.metadata
